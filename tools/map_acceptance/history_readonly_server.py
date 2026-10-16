"""Independent observer bootstrap. Never serves or opens the sealed source DB.

Factory: python -B -m uvicorn history_readonly_server:factory --factory
         --app-dir <this directory> --host 127.0.0.1 --port <observer port>
Set GISMIND_HISTORY_CLONE_ROOT to a prepared clone beneath this tooling root.
CLI: python -B history_readonly_server.py --clone-root <clone> --port <port>
Clone preparation is explicit via prepare_clone(original_shard, clone_root).
The shard contract is runtime.db plus cas/artifacts/sha256/...; stored
artifact storage_uri remains the logical artifacts/sha256/... URI.
Temporary fixtures and observer reads are not additional live LLM evidence.
Observer-only GET /api/sessions accepts X-History-Observer-Session to expose
an actual older clone row plus the unchanged recent 200. This projection is
marked by response headers; stock application behavior is not changed.
"""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import sys
import threading
from typing import Any

TOOLING_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = next(parent / 'backend' for parent in TOOLING_ROOT.parents
    if (parent / 'backend/app').is_dir() and (parent / 'frontend/src').is_dir())
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

RECEIPT_NAME = "observer_clone_receipt.json"
POLICY_CODE = "INDEPENDENT_HISTORY_READONLY"
OBSERVER_SERVICE_VERSION = "history-readonly-observer-v2"
SESSION_SELECTOR_HEADER = "X-History-Observer-Session"
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}")
_GET_PATH = re.compile(r"^/api/(?:health|sessions(?:/[^/]+(?:/(?:messages|runs))?)?|runs/[^/]+(?:/events)?|artifacts/[^/]+(?:/content)?)$")
_EGRESS_LOCK = threading.RLock()


class ObserverPolicyError(RuntimeError):
    """A deliberate independent-observer boundary refusal; contains no inputs."""


class ForbiddenPlanner:
    """A real fail-closed boundary, never a substitute model response."""

    def __init__(self):
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        raise ObserverPolicyError("independent observer planner invocation is forbidden")

    async def plan(self, *args, **kwargs):
        return self(*args, **kwargs)


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _reject_links(path: Path) -> None:
    for node in (path, *path.parents):
        if node.exists() and (node.is_symlink() or (hasattr(node, "is_junction") and node.is_junction())):
            raise ValueError("observer paths must not traverse links or junctions")


def _clone_root(value: str | Path) -> Path:
    raw = Path(value).absolute()
    _reject_links(raw)
    root = raw.resolve()
    if root == TOOLING_ROOT or not _inside(root, TOOLING_ROOT):
        raise ValueError("clone target must remain beneath the audit tooling directory")
    return root


def _file_hash(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
            size += len(block)
    return {"sha256": digest.hexdigest(), "size_bytes": size}


def _source_files(root: Path) -> dict[str, dict[str, Any]]:
    files = {}
    for name in ("runtime.db", "runtime.db-wal", "runtime.db-shm"):
        path = root / name
        _reject_links(path)
        if path.exists():
            if not path.is_file():
                raise ValueError("source database paths must be regular files")
            files[name] = _file_hash(path)
    cas = root / "cas" / "artifacts"
    _reject_links(cas)
    if not (root / "runtime.db").is_file() or not cas.is_dir():
        raise ValueError("sealed shard requires runtime.db and cas/artifacts directory")
    for path in sorted(cas.rglob("*")):
        _reject_links(path)
        if path.is_file():
            files[path.relative_to(root).as_posix()] = _file_hash(path)
        elif not path.is_dir():
            raise ValueError("CAS contains a non-regular filesystem entry")
    return files


def logical_identity(database: str | Path) -> dict[str, Any]:
    """Hash schema and all table rows on a working clone, including WAL state.

    This intentionally accepts only a tooling-scoped DB. mode=ro may adjust
    clone-side SHM bookkeeping; it must never be used on the original shard.
    It does not use immutable=1, which would ignore a copied WAL.
    """
    path = Path(database).absolute()
    _reject_links(path)
    path = path.resolve()
    if not _inside(path, TOOLING_ROOT) or not path.is_file():
        raise ValueError("logical identity may only read an existing audit working database")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        schema = conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        tables = {}
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            quoted = '"' + name.replace('"', '""') + '"'
            rows = conn.execute("SELECT * FROM " + quoted).fetchall()
            encoded = sorted(json.dumps([{"blob_hex": v.hex()} if isinstance(v, bytes) else v for v in row], ensure_ascii=False, separators=(",", ":")) for row in rows)
            tables[name] = {"rows": len(rows), "sha256": hashlib.sha256("\n".join(encoded).encode("utf-8")).hexdigest()}
        conn.rollback()
    payload = {"schema": schema, "tables": tables}
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"sha256": digest, "tables": tables}


def prepare_clone(original_shard: str | Path, clone_root: str | Path) -> dict[str, Any]:
    """Copy source DB/sidecars and cas/artifacts bytes into a fresh observer root.

    Source state is authenticated solely by filesystem reads. Logical state is
    derived from the copied DB+WAL after immediate byte-identity verification.
    Subsequent WAL/SHM changes are confined to the working clone.
    """
    source_raw = Path(original_shard).absolute()
    _reject_links(source_raw)
    source = source_raw.resolve()
    clone = _clone_root(clone_root)
    if _inside(clone, source) or _inside(source, clone):
        raise ValueError("source and observer clone must not overlap")
    if clone.exists():
        raise ValueError("clone target must not already exist")
    before = _source_files(source)
    clone.mkdir(parents=True)
    for relative in before:
        destination = clone / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, destination)
    # Empty CAS is still a valid directory; its file list is independently sealed.
    (clone / "cas" / "artifacts").mkdir(parents=True, exist_ok=True)
    copied = _source_files(clone)
    if copied != before:
        raise ObserverPolicyError("observer copy byte verification failed")
    identity = logical_identity(clone / "runtime.db")
    after = _source_files(source)
    if after != before:
        raise ObserverPolicyError("sealed source changed during observer preparation")
    receipt = {
        "kind": "independent_history_observer_clone_v1",
        "original_shard": str(source), "clone_root": str(clone),
        "source_files": before, "copied_files": copied,
        "copied_bytes_identical": True, "original_bytes_unchanged": True,
        "logical_identity": identity,
        "logical_identity_basis": "verified identical source DB/WAL bytes copied without opening original SQLite",
        "working_clone_sidecars_may_change": True,
        "new_llm_cases": 0,
    }
    (clone / RECEIPT_NAME).write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    return receipt


@contextmanager
def _deny_server_egress():
    """Block all server-originated HTTP and outbound connects during lifespan.

    Browser SDK/tile traffic lives in the browser process. No URLs, headers,
    request bodies, keys, or environment contents are included in errors.
    """
    import httpx
    import urllib.request
    from unittest.mock import patch

    def deny(*args, **kwargs):
        raise ObserverPolicyError("independent observer outbound network is forbidden")

    async def deny_async(*args, **kwargs):
        return deny()

    with _EGRESS_LOCK, patch.object(socket.socket, "connect", deny), patch.object(socket.socket, "connect_ex", deny), patch.object(httpx.HTTPTransport, "handle_request", deny), patch.object(httpx.AsyncHTTPTransport, "handle_async_request", deny_async), patch.object(urllib.request, "urlopen", deny):
        yield


def create_history_app(clone_root: str | Path):
    """Serve an authenticated observer clone through unchanged Runtime routes."""
    clone = _clone_root(clone_root)
    receipt_path = clone / RECEIPT_NAME
    if not receipt_path.is_file():
        raise ValueError("observer clone receipt is required")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("clone_root") != str(clone) or receipt.get("kind") != "independent_history_observer_clone_v1":
        raise ValueError("observer clone receipt identity mismatch")
    if logical_identity(clone / "runtime.db") != receipt.get("logical_identity"):
        raise ObserverPolicyError("observer database logical identity changed")
    for relative, expected in receipt["copied_files"].items():
        if not relative.startswith("cas/artifacts/"):
            continue
        path = clone / relative
        _reject_links(path)
        if not _inside(path.resolve(), clone) or _file_hash(path) != expected:
            raise ObserverPolicyError("observer CAS identity changed")

    from app.main import create_app
    from app.runtime import ArtifactStore, RuntimeDB, RuntimeStore, RuntimeSupervisor
    from starlette.responses import JSONResponse, Response

    class ReadOnlySupervisor(RuntimeSupervisor):
        def _forbidden_worker(self, *args, **kwargs):
            raise ObserverPolicyError("independent observer worker execution is forbidden")

        start = _forbidden_worker
        worker_loop = _forbidden_worker
        run_once = _forbidden_worker
        enqueue_run = _forbidden_worker
        recover_expired_leases = _forbidden_worker
        recover_cancelling_runs = _forbidden_worker

    runtime = RuntimeStore(RuntimeDB(clone / "runtime.db"))  # no initialize()
    artifacts = ArtifactStore(clone / "cas", runtime)
    supervisor = ReadOnlySupervisor(runtime, artifacts, planner=ForbiddenPlanner(), autostart=False)
    app = create_app(runtime_store=runtime, artifact_store=artifacts, runtime_supervisor=supervisor)
    app.state.history_clone_root = clone
    app.state.history_clone_receipt = receipt
    app.state.history_observer_policy = {
        "version": OBSERVER_SERVICE_VERSION,
        "independent_observer_only": True,
        "stock_app_modified": False,
        "selector_header": SESSION_SELECTOR_HEADER,
        "selector_route": "GET /api/sessions",
        "projection": "actual_selected_clone_row_plus_stock_recent200_deduplicated",
        "title_basis": "stock_constant_no_persisted_title",
        "no_selector_behavior": "unchanged_stock_recent200",
        "runtime_fact_writes": False,
    }
    base_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def observer_lifespan(instance):
        with _deny_server_egress():
            async with base_lifespan(instance):
                yield
        if logical_identity(clone / "runtime.db") != receipt["logical_identity"]:
            raise ObserverPolicyError("observer database facts changed during serving")

    app.router.lifespan_context = observer_lifespan

    @app.middleware("http")
    async def readonly_boundary(request, call_next):
        allowed_path = _GET_PATH.fullmatch(request.url.path) is not None
        if request.method not in {"GET", "HEAD", "OPTIONS"} or not allowed_path:
            return JSONResponse({"error": {"code": POLICY_CODE, "message": "Independent historical audit permits allowlisted reads only"}}, status_code=405 if request.method not in {"GET", "HEAD", "OPTIONS"} else 403, headers={"Allow": "GET, HEAD, OPTIONS"})
        if request.method == "OPTIONS":
            return Response(status_code=204, headers={"Allow": "GET, HEAD, OPTIONS", "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS", "Access-Control-Allow-Headers": "X-User-Id, X-Request-ID, Last-Event-ID, Range, X-History-Observer-Session"})
        selected_id = request.headers.get(SESSION_SELECTOR_HEADER)
        if request.method == "GET" and request.url.path == "/api/sessions" and selected_id is not None:
            projection_headers = {"X-History-Observer-Policy": OBSERVER_SERVICE_VERSION}
            if _SESSION_ID.fullmatch(selected_id) is None:
                return JSONResponse({"error": {"code": "HISTORY_OBSERVER_INVALID_SESSION", "message": "Independent observer session selector must be a valid session identifier"}}, status_code=400, headers=projection_headers)

            def read_sessions(conn):
                selected = conn.execute("SELECT * FROM sessions WHERE session_id=? AND deleted_at IS NULL", (selected_id,)).fetchone()
                if selected is None:
                    return None
                recent = conn.execute("SELECT * FROM sessions WHERE deleted_at IS NULL ORDER BY updated_at DESC LIMIT 200").fetchall()
                return [dict(selected)] + [dict(row) for row in recent if row["session_id"] != selected_id]

            rows = runtime._read(read_sessions)
            if rows is None:
                return JSONResponse({"error": {"code": "HISTORY_OBSERVER_SESSION_NOT_FOUND", "message": "Selected historical session is unknown or deleted in this independent observer clone"}}, status_code=404, headers=projection_headers)
            projection_headers[SESSION_SELECTOR_HEADER] = selected_id
            # Current Runtime has no persisted title column. Preserve its exact
            # public title constant; do not infer or fabricate a historical title.
            items = [{"id": row["session_id"], "title": "新会话", "created_at": row["created_at"], "updated_at": row["updated_at"]} for row in rows]
            return JSONResponse({"items": items}, headers=projection_headers)
        if request.method == "HEAD":
            request.scope["method"] = "GET"
            response = await call_next(request)
            return Response(status_code=response.status_code, headers=dict(response.headers), media_type=response.media_type)
        return await call_next(request)

    return app


def factory():
    value = os.environ.get("GISMIND_HISTORY_CLONE_ROOT")
    if not value:
        raise ValueError("GISMIND_HISTORY_CLONE_ROOT must explicitly name an observer clone")
    return create_history_app(value)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Serve one independent history observer clone; never starts a cohort")
    parser.add_argument("--clone-root", required=True)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args(argv)
    import uvicorn
    uvicorn.run(create_history_app(args.clone_root), host="127.0.0.1", port=args.port, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
