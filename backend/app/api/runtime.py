"""Public Runtime API.

The Runtime database is the source of truth for runs and events.  This module
only writes commands/metadata and projects persisted events through SSE; GIS
execution remains owned by :mod:`app.runtime.queue`.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Literal

from fastapi import APIRouter, File, Form, Header, Request, UploadFile, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.runtime import ArtifactStore, RuntimeDB, RuntimeStore, RuntimeSupervisor
from app.runtime.contracts import validate_crs_value
from app.runtime.db import utc_now

router = APIRouter(prefix="/api", tags=["runtime"])
TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}


@router.api_route("/chat", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@router.api_route("/chat/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@router.api_route("/upload", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def reject_legacy_runtime(request: Request, path: str = ""):
    """Stable destructive-migration response for the removed API surface."""
    return _error(request, 410, "LEGACY_RUNTIME_REMOVED", "legacy /api/chat and /api/upload are removed; use /api/runs and /api/artifacts/uploads")


@router.post("/sessions", status_code=201)
def create_runtime_session(request: Request):
    runtime, _artifacts, _supervisor = _resources(request)
    session_id = f"session_{uuid.uuid4().hex}"
    session = runtime.create_session(session_id)
    return {"id": session_id, "title": "新会话", "created_at": session["created_at"], "updated_at": session["updated_at"]}


@router.get("/sessions")
def list_runtime_sessions(request: Request):
    runtime, _artifacts, _supervisor = _resources(request)
    items = runtime._read(lambda c: [dict(row) for row in c.execute("SELECT * FROM sessions WHERE deleted_at IS NULL ORDER BY updated_at DESC LIMIT 200").fetchall()])
    return {"items": [{"id": row["session_id"], "title": "新会话", "created_at": row["created_at"], "updated_at": row["updated_at"]} for row in items]}


@router.get("/sessions/{session_id}")
def get_runtime_session(request: Request, session_id: str):
    runtime, _artifacts, _supervisor = _resources(request)
    row = runtime.get_session(session_id)
    if not row or row.get("deleted_at"):
        return _error(request, 404, "SESSION_NOT_FOUND", "session does not exist")
    return {"id": session_id, "title": "新会话", "created_at": row["created_at"], "updated_at": row["updated_at"]}


@router.get("/sessions/{session_id}/messages")
def get_runtime_messages(request: Request, session_id: str):
    runtime, _artifacts, _supervisor = _resources(request)
    if not runtime.get_session(session_id):
        return _error(request, 404, "SESSION_NOT_FOUND", "session does not exist")
    rows = runtime._read(lambda c: [dict(row) for row in c.execute("SELECT * FROM messages WHERE session_id=? ORDER BY message_index", (session_id,)).fetchall()])
    return {"messages": [{"id": row["message_id"], "role": row["role"], "content": row["content"], "created_at": row["created_at"]} for row in rows]}


@router.get("/sessions/{session_id}/runs")
def list_session_runs(request: Request, session_id: str):
    runtime, _artifacts, _supervisor = _resources(request)
    def read(conn):
        session = conn.execute("SELECT deleted_at FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if session is None or session[0] is not None:
            return None
        return [dict(row) for row in conn.execute(
            "SELECT run_id,session_id,status,run_version,created_at,updated_at FROM runs "
            "WHERE session_id=? ORDER BY created_at DESC,run_id DESC LIMIT 200", (session_id,),
        ).fetchall()]
    items = runtime._read(read)
    if items is None:
        return _error(request, 404, "SESSION_NOT_FOUND", "session does not exist")
    return {"items": items}


@router.patch("/sessions/{session_id}", status_code=204)
def rename_runtime_session(request: Request, session_id: str, body: dict[str, Any]):
    runtime, _artifacts, _supervisor = _resources(request)
    if not runtime.get_session(session_id):
        return _error(request, 404, "SESSION_NOT_FOUND", "session does not exist")
    runtime.update_session(session_id)
    return Response(status_code=204)


@router.delete("/sessions/{session_id}", status_code=204)
def delete_runtime_session(request: Request, session_id: str):
    runtime, _artifacts, _supervisor = _resources(request)
    if runtime.get_session(session_id):
        runtime.delete_session(session_id)
    return Response(status_code=204)


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    message: str = Field(min_length=1)
    input_artifact_ids: list[str] = Field(default_factory=list)
    client_request_id: str = Field(min_length=1)


class RunCommandRequest(BaseModel):
    # Internal cancellation controls (for example ``finalize`` or
    # ``defer_active``) are supervisor-owned and must never be client input.
    # Reject unknown fields so the public command cannot silently broaden its
    # behavior as the implementation evolves.
    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(min_length=1)
    expected_run_version: int | None = None


class ResumeRequest(RunCommandRequest):
    patch: dict[str, Any] = Field(default_factory=dict)
    action: str = "resume"


class ConfirmRequest(RunCommandRequest):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    action_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")
    confirmation: Literal[True]


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex}"


def _error(request: Request, status: int, code: str, message: str, details: dict[str, Any] | None = None) -> JSONResponse:
    trace_id = _request_id(request)
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": details or {}, "trace_id": trace_id}},
        headers={"X-Request-ID": trace_id},
    )


def _resources(request: Request) -> tuple[RuntimeStore, ArtifactStore, RuntimeSupervisor]:
    app = request.app
    runtime = getattr(app.state, "runtime_store", None)
    if runtime is None:
        runtime = RuntimeStore(RuntimeDB())
        runtime.initialize()
        app.state.runtime_store = runtime
    artifacts = getattr(app.state, "artifact_store", None)
    if artifacts is None:
        artifacts = ArtifactStore(runtime=runtime)
        app.state.artifact_store = artifacts
    supervisor = getattr(app.state, "runtime_supervisor", None)
    if supervisor is None:
        supervisor = RuntimeSupervisor(runtime, artifacts)
        app.state.runtime_supervisor = supervisor
    return runtime, artifacts, supervisor


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _command_hash(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _decode_json(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _decode_upload_json(value: str | None, field_name: str) -> Any:
    """Decode one multipart JSON declaration without silently dropping errors."""
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be valid canonical JSON") from exc


def _validate_declared_bbox(value: Any) -> list[float] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("declared_bbox must be an ordered four-number array")
    if any(isinstance(item, bool) for item in value):
        raise ValueError("declared_bbox coordinates must be finite numbers")
    try:
        bbox = [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise ValueError("declared_bbox coordinates must be finite numbers") from exc
    if any(not math.isfinite(item) for item in bbox) or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
        raise ValueError("declared_bbox must be finite and ordered")
    return bbox


def _event_envelope(row: dict[str, Any]) -> dict[str, Any]:
    payload = _decode_json(row.get("payload_json"), {})
    payload = payload if isinstance(payload, dict) else {"value": payload}
    envelope = {
        "event_id": int(row["event_id"]),
        "run_id": row["run_id"],
        "seq": int(row["seq"]),
        "type": row["event_type"],
        "occurred_at": row.get("created_at"),
        "schema_version": "1",
        "payload": payload,
    }
    # Keep common references at the envelope level for consumers that render
    # a timeline without parsing event-specific payloads.
    for key in ("task_id", "artifact_id"):
        if key in payload:
            envelope[key] = payload[key]
    return envelope


def _events_after(runtime: RuntimeStore, run_id: str, event_id: int) -> list[dict[str, Any]]:
    return runtime._read(
        lambda c: [
            _event_envelope(dict(row))
            for row in c.execute("SELECT * FROM events WHERE run_id=? AND event_id>? ORDER BY event_id", (run_id, event_id)).fetchall()
        ]
    )


def _snapshot(runtime: RuntimeStore, run_id: str) -> dict[str, Any] | None:
    projection = runtime.get_run_snapshot(run_id)
    if projection is None:
        return None
    run = projection["run"]
    plan = projection["plan"]
    tasks = projection["tasks"]
    input_revision = projection["input_revision"]
    outputs = [_public_artifact(artifact) for artifact in projection["output_artifacts"]]
    last_event_id = projection["last_event_id"]
    awaiting_payload = projection["awaiting_input"]
    pending_confirmations = projection["pending_confirmations"]
    return {
        "run_id": run["run_id"],
        "session_id": run["session_id"],
        "status": run["status"],
        "run_version": run["run_version"],
        "active_input_revision": ({"input_revision_id": input_revision[0], "revision": input_revision[1]} if input_revision else {"revision": run.get("input_revision", 1)}),
        "active_plan": ({"plan_id": plan["plan_id"], "revision": plan["plan_version"], "plan_hash": plan["plan_hash"]} if plan else None),
        "tasks": tasks,
        "planning_attempts": projection["planning_attempts"],
        "output_artifacts": outputs,
        "final_artifact_ids": projection["final_artifact_ids"],
        "last_event_id": last_event_id,
        "last_seq": int(run.get("last_event_seq", 0)),
        "awaiting_input": awaiting_payload,
        "pending_confirmations": pending_confirmations,
        "diagnostic": projection["diagnostic"],
        "patch_schema": awaiting_payload.get("patch_schema") if isinstance(awaiting_payload, dict) else None,
    }


def _public_artifact(artifact: dict[str, Any] | None) -> dict[str, Any]:
    if not artifact:
        return {}
    result = dict(artifact)
    result.pop("storage_uri", None)
    result["byte_size"] = result.pop("size_bytes", result.get("byte_size", 0))
    result["content_url"] = f"/api/artifacts/{artifact['artifact_id']}/content"
    return result


def _artifact_visible(runtime: RuntimeStore, artifact_id: str) -> dict[str, Any] | None:
    artifact = runtime.get_artifact(artifact_id)
    if not artifact or artifact.get("validation_status") != "valid":
        return None
    # This deployment has one authenticated principal.  A published upload is
    # therefore owned by that principal immediately; run/session references
    # still make lineage explicit for multi-user deployments later.
    if artifact.get("origin") == "upload":
        return artifact
    visible = runtime._read(
        lambda c: c.execute(
            """SELECT 1 FROM task_outputs o JOIN tasks t ON t.task_id=o.task_id JOIN plans p ON p.plan_id=t.plan_id WHERE o.artifact_id=?
               UNION SELECT 1 FROM task_inputs WHERE artifact_id=?
               UNION SELECT 1 FROM message_artifacts WHERE artifact_id=?
               UNION SELECT 1 FROM run_input_revisions WHERE input_json LIKE ? LIMIT 1""",
            (artifact_id, artifact_id, artifact_id, f"%{artifact_id}%"),
        ).fetchone()
    )
    return artifact if visible else None


@router.post("/runs", status_code=202)
async def create_run(request: Request, body: RunCreateRequest):
    runtime, _artifacts, supervisor = _resources(request)
    request_payload = body.model_dump(mode="json")
    digest = _command_hash(request_payload)
    existing = runtime._read(
        lambda c: c.execute(
            """SELECT r.run_id,rc.payload_json FROM run_commands rc JOIN runs r ON r.run_id=rc.run_id
               WHERE r.session_id=? AND rc.command_type='create' AND rc.idempotency_key=?""",
            (body.session_id, body.client_request_id),
        ).fetchone()
    )
    if existing:
        saved = _decode_json(existing[1], {})
        if saved.get("request_hash") != digest:
            return _error(request, 409, "CLIENT_REQUEST_CONFLICT", "client request id was already used with a different request")
        return JSONResponse(status_code=202, content=saved["response"], headers={"X-Request-ID": _request_id(request)})
    if runtime.get_session(body.session_id) is None:
        return _error(request, 404, "SESSION_NOT_FOUND", "session does not exist")
    for artifact_id in body.input_artifact_ids:
        artifact = runtime.get_artifact(artifact_id)
        if artifact is None or artifact.get("validation_status") != "valid":
            return _error(request, 404, "ARTIFACT_NOT_FOUND", "input artifact does not exist or is not owned")
    run_id = f"run_{uuid.uuid4().hex}"
    response = {"run_id": run_id, "status": "queued", "run_version": 1, "events_url": f"/api/runs/{run_id}/events"}
    try:
        created = runtime.create_run_with_input(
            run_id, body.session_id, message=body.message, input_artifact_ids=body.input_artifact_ids,
            command_id=f"cmd_{uuid.uuid4().hex}", idempotency_key=body.client_request_id,
            command_payload={"request_hash": digest, "response": response},
        )
    except RuntimeError as exc:
        if str(exc) == "CLIENT_REQUEST_CONFLICT":
            return _error(request, 409, "CLIENT_REQUEST_CONFLICT", "client request id was already used with a different request")
        raise
    if created.get("idempotent"):
        saved_response = created.get("response")
        return JSONResponse(status_code=202, content=saved_response or response, headers={"X-Request-ID": _request_id(request)})
    # The API only records a command.  A fenced worker calls the real LLM and
    # atomically activates a compiled plan later; clients cannot inject a
    # fake PlannerDraft through this public contract.
    supervisor.enqueue_run(run_id)
    return JSONResponse(status_code=202, content=response, headers={"X-Request-ID": _request_id(request)})


@router.get("/runs/{run_id}")
def get_run(request: Request, run_id: str):
    runtime, _artifacts, _supervisor = _resources(request)
    snapshot = _snapshot(runtime, run_id)
    return snapshot if snapshot is not None else _error(request, 404, "RUN_NOT_FOUND", "run does not exist")


async def _event_stream(runtime: RuntimeStore, run_id: str, cursor: int) -> AsyncIterator[str]:
    sent_terminal = False
    last_id = cursor
    idle = 0
    while True:
        events = _events_after(runtime, run_id, last_id)
        if events:
            for event in events:
                last_id = event["event_id"]
                if event["type"] in {"run.completed", "run.failed", "run.cancelled"}:
                    sent_terminal = True
                yield f"id: {last_id}\nevent: {event['type']}\ndata: {_canonical(event)}\n\n"
            if sent_terminal:
                return
            idle = 0
        else:
            snapshot = _snapshot(runtime, run_id)
            if snapshot and snapshot["status"] in TERMINAL_RUN_STATUSES:
                # A terminal commit can land after the empty event poll. Drain
                # that committed tail before closing the display channel.
                if snapshot["last_event_id"] > last_id:
                    continue
                return
            idle += 1
            if idle % 60 == 0:
                yield ": heartbeat\n\n"
        import asyncio
        await asyncio.sleep(0.25)


@router.get("/runs/{run_id}/events")
async def run_events(request: Request, run_id: str, after_event_id: int | None = None, last_event_id: int | None = None, last_event_header: str | None = Header(default=None, alias="Last-Event-ID")):
    runtime, _artifacts, _supervisor = _resources(request)
    if runtime.get_run(run_id) is None:
        return _error(request, 404, "RUN_NOT_FOUND", "run does not exist")
    header_cursor = None
    if last_event_header not in (None, ""):
        try:
            header_cursor = int(last_event_header)
        except ValueError:
            return _error(request, 409, "INVALID_EVENT_CURSOR", "Last-Event-ID must be an integer")
    query_cursor = after_event_id if after_event_id is not None else last_event_id
    if header_cursor is not None and query_cursor is not None and header_cursor != query_cursor:
        return _error(request, 409, "INVALID_EVENT_CURSOR", "Last-Event-ID and after_event_id must match")
    cursor = header_cursor if header_cursor is not None else (query_cursor or 0)
    if cursor < 0 or cursor > 2**63 - 1:
        return _error(request, 409, "INVALID_EVENT_CURSOR", "event cursor must be a non-negative SQLite integer")
    if cursor:
        check = runtime._read(lambda c: c.execute("SELECT run_id FROM events WHERE event_id=?", (cursor,)).fetchone())
        max_event = runtime._read(lambda c: c.execute("SELECT MAX(event_id) FROM events WHERE run_id=?", (run_id,)).fetchone()[0] or 0)
        if check is None or check[0] != run_id or cursor > max_event:
            return _error(request, 409, "INVALID_EVENT_CURSOR", "event cursor does not belong to this run")
    return StreamingResponse(_event_stream(runtime, run_id, cursor), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Request-ID": _request_id(request)})


def _iter_file_range(path: Path, start: int, end: int, *, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    """Stream a bounded Artifact range without materializing the whole file."""
    remaining = end - start + 1
    with path.open("rb") as stream:
        stream.seek(start)
        while remaining:
            chunk = stream.read(min(chunk_size, remaining))
            if not chunk:
                return
            remaining -= len(chunk)
            yield chunk


@router.post("/runs/{run_id}/cancel", status_code=202)
def cancel_run(request: Request, run_id: str, body: RunCommandRequest):
    runtime, _artifacts, supervisor = _resources(request)
    run = runtime.get_run(run_id)
    if run is None:
        return _error(request, 404, "RUN_NOT_FOUND", "run does not exist")
    payload = body.model_dump(mode="json")
    digest = _command_hash(payload)
    existing = runtime.get_run_command(body.command_id)
    if existing:
        if existing.get("run_id") != run_id:
            return _error(request, 409, "COMMAND_ID_CONFLICT", "command id belongs to another run")
        saved = _decode_json(existing.get("payload_json"), {})
        if saved.get("request_hash") != digest:
            return _error(request, 409, "COMMAND_ID_CONFLICT", "command id was already used with a different request")
        stored = saved.get("response")
        if isinstance(stored, dict):
            # A client retry may arrive after the reservation committed but
            # before the original request reached child termination.  Resume
            # that hand-off rather than replaying a non-terminal response and
            # leaving the Run in ``cancelling`` forever.
            if stored.get("status") == "cancelling":
                unresolved = supervisor.terminate_run_processes(run_id) or []
                if unresolved:
                    stored = runtime.mark_cancel_indeterminate(
                        run_id, command_id=body.command_id,
                    )
                else:
                    stored = runtime.finalize_cancel_command(run_id, command_id=body.command_id)
            # The command payload is an idempotency record, not the current
            # Run projection.  Recovery/finalization may have advanced the
            # terminal version since it was written, so replay the durable
            # Run facts instead of returning a stale stored response.
            current = runtime.get_run(run_id)
            return {"run_id": run_id, "status": current["status"], "run_version": current["run_version"]}
        return {"run_id": run_id, "status": run["status"], "run_version": run["run_version"]}
    if body.expected_run_version is not None and body.expected_run_version != run["run_version"]:
        return _error(request, 409, "RUN_VERSION_CONFLICT", "run state changed; refresh before retrying", {"expected": body.expected_run_version, "actual": run["run_version"]})
    try:
        result = runtime.apply_cancel_command(
            run_id, command_id=body.command_id, request_hash=digest,
            expected_run_version=body.expected_run_version, finalize=False,
            defer_active=True,
        )
    except RuntimeError as exc:
        code = str(exc)
        if code == "RUN_VERSION_CONFLICT":
            current = runtime.get_run(run_id)
            return _error(request, 409, code, "run state changed; refresh before retrying", {
                "expected": body.expected_run_version, "actual": current["run_version"] if current else None,
            })
        if code == "COMMAND_ID_CONFLICT":
            return _error(request, 409, code, "command id was already used with a different request")
        raise
    # Reserve first while active task/attempt rows remain addressable. Only
    # then terminate children; an unresolved kill must pause for user input
    # instead of claiming a terminal cancellation.
    unresolved = supervisor.terminate_run_processes(run_id) or []
    if unresolved:
        result = runtime.mark_cancel_indeterminate(
            run_id, command_id=body.command_id,
        )
    else:
        result = runtime.finalize_cancel_command(run_id, command_id=body.command_id)
    return {"run_id": result["run_id"], "status": result["status"], "run_version": result["run_version"]}


@router.post("/runs/{run_id}/resume", status_code=202)
def resume_run(request: Request, run_id: str, body: ResumeRequest):
    runtime, _artifacts, supervisor = _resources(request)
    run = runtime.get_run(run_id)
    if run is None:
        return _error(request, 404, "RUN_NOT_FOUND", "run does not exist")
    payload = body.model_dump(mode="json")
    digest = _command_hash(payload)
    existing = runtime.get_run_command(body.command_id)
    if existing:
        if existing.get("run_id") != run_id:
            return _error(request, 409, "COMMAND_ID_CONFLICT", "command id belongs to another run")
        saved = _decode_json(existing.get("payload_json"), {})
        if saved.get("request_hash") != digest:
            return _error(request, 409, "COMMAND_ID_CONFLICT", "command id was already used with a different request")
        stored = saved.get("response")
        if isinstance(stored, dict):
            return {key: stored[key] for key in ("run_id", "status", "run_version", "requires_user_decision", "pending_task_ids") if key in stored}
        return {"run_id": run_id, "status": run["status"], "run_version": run["run_version"]}
    if run["status"] != "awaiting_input":
        return _error(request, 409, "RUN_NOT_AWAITING_INPUT", "run is not awaiting input")
    if body.action != "resume":
        return _error(request, 422, "INVALID_RESUME_ACTION", "resume action must be 'resume'")
    if body.expected_run_version is not None and body.expected_run_version != run["run_version"]:
        return _error(request, 409, "RUN_VERSION_CONFLICT", "run state changed; refresh before retrying", {"expected": body.expected_run_version, "actual": run["run_version"]})
    diagnostic_row = runtime._read(lambda c: c.execute("SELECT payload_json FROM events WHERE run_id=? AND event_type='run.awaiting_input' ORDER BY event_id DESC LIMIT 1", (run_id,)).fetchone())
    diagnostic = _decode_json(diagnostic_row[0], {}) if diagnostic_row else {}
    schema = diagnostic.get("patch_schema") if isinstance(diagnostic, dict) else None
    def validate_patch(value: Any, spec: Any, path: str = "patch") -> str | None:
        if not isinstance(spec, dict):
            return None
        kind = spec.get("type")
        if kind == "object" and not isinstance(value, dict):
            return f"{path} must be an object"
        if kind == "string" and not isinstance(value, str):
            return f"{path} must be a string"
        if kind == "string" and isinstance(value, str):
            if "minLength" in spec and len(value) < spec["minLength"]:
                return f"{path} must contain at least {spec['minLength']} characters"
            if "maxLength" in spec and len(value) > spec["maxLength"]:
                return f"{path} must contain at most {spec['maxLength']} characters"
        if kind == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
            return f"{path} must be a number"
        if kind == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
            return f"{path} must be an integer"
        if isinstance(value, dict):
            if spec.get("additionalProperties") is False:
                unexpected = set(value) - set(spec.get("properties", {}))
                if unexpected:
                    return f"{path} contains unsupported fields: {', '.join(sorted(unexpected))}"
            for required in spec.get("required", []):
                if required not in value:
                    return f"{path}.{required} is required"
            if spec.get("properties") and not value:
                return f"{path} must include a patch value"
            for key, child in spec.get("properties", {}).items():
                if key in value:
                    problem = validate_patch(value[key], child, f"{path}.{key}")
                    if problem:
                        return problem
        return None
    invalid_patch = validate_patch(body.patch, schema)
    if invalid_patch:
        return _error(request, 422, "PATCH_SCHEMA_INVALID", invalid_patch, {"patch_schema": schema or {}})
    try:
        result = runtime.apply_resume_command(
            run_id, command_id=body.command_id, request_hash=digest,
            patch=body.patch, expected_run_version=body.expected_run_version,
        )
    except RuntimeError as exc:
        code = str(exc)
        if code == "RUN_VERSION_CONFLICT":
            current = runtime.get_run(run_id)
            return _error(request, 409, code, "run state changed; refresh before retrying", {
                "expected": body.expected_run_version, "actual": current["run_version"] if current else None,
            })
        if code == "COMMAND_ID_CONFLICT":
            return _error(request, 409, code, "command id was already used with a different request")
        if code == "RUN_NOT_AWAITING_INPUT":
            return _error(request, 409, code, "run is not awaiting input")
        raise
    # Patches are immutable input revisions.  The planner worker receives the
    # original natural-language goal plus every accepted revision; the public
    # API never reconstructs or executes a plan itself.
    if result["status"] == "queued":
        supervisor.enqueue_run(run_id)
    return {
        key: result[key]
        for key in ("run_id", "status", "run_version", "requires_user_decision", "pending_task_ids")
        if key in result
    }


@router.post("/runs/{run_id}/confirm", status_code=202)
def confirm_run_side_effect(request: Request, run_id: str, body: ConfirmRequest):
    """Approve exactly one compiler-hashed side effect and enqueue its Task."""
    runtime, _artifacts, supervisor = _resources(request)
    run = runtime.get_run(run_id)
    if run is None:
        return _error(request, 404, "RUN_NOT_FOUND", "run does not exist")
    payload = body.model_dump(mode="json")
    digest = _command_hash(payload)
    existing = runtime.get_run_command(body.command_id)
    if existing:
        if existing.get("run_id") != run_id:
            return _error(request, 409, "COMMAND_ID_CONFLICT", "command id belongs to another run")
        saved = _decode_json(existing.get("payload_json"), {})
        if saved.get("request_hash") != digest:
            return _error(request, 409, "COMMAND_ID_CONFLICT", "command id was already used with a different request")
        current = runtime.get_run(run_id)
        return {"run_id": run_id, "status": current["status"], "run_version": current["run_version"]}
    try:
        result = runtime.confirm_task(
            run_id,
            task_id=body.task_id,
            action_hash=body.action_hash.lower(),
            command_id=body.command_id,
            request_hash=digest,
            expected_run_version=body.expected_run_version,
        )
    except RuntimeError as exc:
        code = str(exc)
        if code == "RUN_VERSION_CONFLICT":
            current = runtime.get_run(run_id)
            return _error(request, 409, code, "run state changed; refresh before retrying", {
                "expected": body.expected_run_version, "actual": current["run_version"] if current else None,
            })
        if code == "COMMAND_ID_CONFLICT":
            return _error(request, 409, code, "command id was already used with a different request")
        return _error(request, 409, code, "run cannot accept this confirmation")
    except ValueError as exc:
        code = str(exc)
        if code in {"CONFIRMATION_NOT_PENDING", "ACTION_HASH_MISMATCH"}:
            return _error(request, 409, code, "confirmation does not match a pending side effect")
        return _error(request, 422, "INVALID_CONFIRMATION", code)
    supervisor.enqueue_run(run_id)
    return {"run_id": run_id, "status": result["status"], "run_version": result["run_version"]}


@router.post("/runs/{run_id}/recover")
def recover_run(request: Request, run_id: str):
    runtime, _artifacts, supervisor = _resources(request)
    if runtime.get_run(run_id) is None:
        return _error(request, 404, "RUN_NOT_FOUND", "run does not exist")
    recovered = supervisor.queue.recover_expired_leases(run_id=run_id)
    status = supervisor._finalize_run_if_terminal(run_id)
    return {"run_id": run_id, "recovered_task_ids": recovered, "status": status or runtime.get_run(run_id)["status"]}


@router.get("/artifacts/{artifact_id}")
def get_artifact(request: Request, artifact_id: str):
    runtime, _artifacts, _supervisor = _resources(request)
    artifact = _artifact_visible(runtime, artifact_id)
    if artifact is None:
        return _error(request, 404, "ARTIFACT_NOT_FOUND", "artifact does not exist or is not owned")
    return _public_artifact(artifact)


@router.get("/artifacts/{artifact_id}/content")
def get_artifact_content(request: Request, artifact_id: str, range_header: str | None = Header(default=None, alias="Range")):
    runtime, artifacts, _supervisor = _resources(request)
    artifact = _artifact_visible(runtime, artifact_id)
    if artifact is None:
        return _error(request, 404, "ARTIFACT_NOT_FOUND", "artifact does not exist or is not owned")
    try:
        path = artifacts.path_for(artifact_id)
    except ValueError:
        return _error(request, 409, "ARTIFACT_INVALID", "artifact cannot be consumed")
    size = path.stat().st_size
    headers = {"ETag": f'"{artifact["content_hash"]}"', "Accept-Ranges": "bytes", "Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"}
    # A File may contain arbitrary user-provided text, and its stored MIME is
    # supplied by the uploader. Never render it inline on the API origin.
    is_download = artifact.get("type") == "File"
    if is_download:
        file_schema = (artifact.get("schema") or {}).get("file") or {}
        raw_filename = file_schema.get("filename") if isinstance(file_schema, dict) else None
        safe_filename = re.sub(r"[^A-Za-z0-9._-]", "_", Path(str(raw_filename or "artifact.bin")).name)[:120].strip("._") or "artifact.bin"
        headers["Content-Disposition"] = f'attachment; filename="{safe_filename}"'
    media_type = "application/octet-stream" if is_download else artifact.get("media_type") or "application/octet-stream"
    if range_header:
        try:
            value = range_header.removeprefix("bytes=").split("-", 1)
            if len(value) != 2:
                raise ValueError
            if value[0] == "":
                suffix = int(value[1])
                if suffix <= 0:
                    raise ValueError
                start, end = max(size - suffix, 0), size - 1
            else:
                start = int(value[0])
                end = int(value[1]) if value[1] else size - 1
            if start < 0 or end < start or end >= size:
                raise ValueError
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            return StreamingResponse(_iter_file_range(path, start, end), status_code=206, media_type=media_type, headers={**headers, "Content-Length": str(end - start + 1)})
        except (TypeError, ValueError):
            return _error(request, 416, "ARTIFACT_RANGE_INVALID", "invalid byte range")
    return FileResponse(Path(path), media_type=media_type, filename=None, headers=headers)


@router.post("/artifacts/uploads", status_code=201)
async def upload_runtime_artifact(
    request: Request,
    file: UploadFile = File(...),
    declared_crs: str | None = Form(default=None),
    declared_bbox: str | None = Form(default=None),
):
    """Publish an upload directly into the runtime CAS.

    The response is the public artifact projection; the logical CAS URI is
    deliberately never returned to the browser.
    """
    runtime, artifacts, _supervisor = _resources(request)
    try:
        crs = _decode_upload_json(declared_crs, "declared_crs")
        if crs is not None:
            validate_crs_value(crs)
        bbox = _validate_declared_bbox(_decode_upload_json(declared_bbox, "declared_bbox"))
    except (TypeError, ValueError) as exc:
        return _error(request, 422, "ARTIFACT_INVALID", str(exc))
    filename = Path(file.filename or "upload.bin").name or "upload.bin"
    media_type = file.content_type or "application/octet-stream"
    file_schema: dict[str, Any] = {
        "filename": filename,
        "media_type": media_type,
        "extension": Path(filename).suffix.lower(),
        "download_disposition": "attachment",
    }
    if crs is not None:
        file_schema["declared_crs"] = {"value": crs, "source": "user"}
    if bbox is not None:
        file_schema["declared_bbox"] = {"value": bbox, "source": "user"}
    staged = artifacts.create_staging_path(filename)
    max_size = int(settings.UPLOAD_MAX_SIZE) * 1024 * 1024
    size = 0
    try:
        with staged.open("wb") as destination:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_size:
                    raise OverflowError(f"uploaded file exceeds {settings.UPLOAD_MAX_SIZE} MB limit")
                destination.write(chunk)
        if size == 0:
            # Use the same staging cleanup path as other rejected uploads.
            raise ValueError("uploaded file is empty")
        from app.runtime.upload_inspection import inspect_upload, validate_declarations
        inspection = await run_in_threadpool(
            inspect_upload, staged, filename,
            max_total=int(settings.UPLOAD_ZIP_MAX_TOTAL_SIZE) * 1024 * 1024,
            max_count=int(settings.UPLOAD_ZIP_MAX_FILE_COUNT),
        )
        validate_declarations(inspection, crs, bbox)
        artifact = artifacts.publish_staged(
            staged,
            origin="upload",
            artifact_type="File",
            media_type=media_type,
            schema={"file": file_schema, "inspection": inspection, "fields": inspection.get("fields", {})},
            crs=inspection.get("crs"),
        )
    except OverflowError as exc:
        staged.unlink(missing_ok=True)
        return _error(request, 413, "ARTIFACT_TOO_LARGE", str(exc))
    except (OSError, ValueError) as exc:
        staged.unlink(missing_ok=True)
        return _error(request, 422, "ARTIFACT_INVALID", str(exc))
    return JSONResponse(status_code=201, content=_public_artifact(artifact), headers={"X-Request-ID": _request_id(request)})
