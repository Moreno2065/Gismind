"""Durable SQLite task queue primitives for the stage-three worker boundary."""

from __future__ import annotations

import hashlib
import json
import math
import multiprocessing
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from queue import Empty
from typing import Any

from .contracts import CompiledTask, PlannerDraft, RetryPolicy, ToolContractRegistry
from .compiler import AwaitingInputDiagnostic, CompileError, Compiler
from .db import utc_now
from .errors import bounded_error_message, execution_error_code
from .llm_planner import (
    OpenAICompatiblePlanner, PlannerCall, PlannerError, PlannerRequest,
    _ambiguous_rename_source, _ambiguous_rename_target, _inspected_vector_fields,
    _first_unavailable_artifact_ref, _semantic_fact_mismatch,
    _verified_buffer_distance_m, candidate_catalog, controlled_repair_catalog,
)
from .process import check_resource_budget, enforce_resource_limits, kill_process_tree, release_resource_limits
from .planning_control import PlanningBudget, current_planning_budget


def _planning_diagnostic(exc: Exception) -> dict[str, Any]:
    """Use one JSON-safe diagnostic for repair, attempts, and rejection events."""
    def safe_dict(value):
        if not isinstance(value, dict):
            return {}
        try:
            return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
        except (TypeError, ValueError, OverflowError):
            return {}

    diagnostic = safe_dict(getattr(exc, "diagnostics", None))
    diagnostic.setdefault("code", execution_error_code(exc, "PLAN_COMPILE_FAILED"))
    diagnostic.setdefault("path", getattr(exc, "path", "planner_response" if isinstance(exc, PlannerError) else "draft"))
    diagnostic.setdefault("message", getattr(exc, "message", str(exc)))
    diagnostic.setdefault("error", str(exc))
    diagnostic.setdefault("details", safe_dict(getattr(exc, "details", None)))
    return diagnostic


def _ipc_result_manifest(result: dict[str, Any]) -> dict[str, Any]:
    """Return only bounded completion facts for child-to-parent IPC.

    Handler payloads and Artifact bytes are already durable in SQLite/CAS by
    the time this function runs.  Sending them through multiprocessing.Queue
    duplicates large GIS results and can block child teardown on its feeder
    pipe, so the Queue carries identifiers and terminal metadata only.
    """
    allowed = (
        "artifact_id", "type", "validation_status", "content_hash",
        "producer_task_id", "byte_size", "media_type",
    )

    def compact(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        return {key: value.get(key) for key in allowed if key in value}

    artifacts = [item for item in (compact(value) for value in result.get("artifacts", [])) if item]
    primary = compact(result.get("artifact"))
    return {
        "artifacts": artifacts,
        "artifact": primary,
        "content_hash": result.get("content_hash"),
    }


def _terminate_process_tree(process) -> None:
    """Kill a worker and descendants, using psutil when available."""
    kill_process_tree(process)


def _isolated_executor_worker(db_path: str, data_root: str, artifact_dir: str, staging_dir: str, quarantine_dir: str, upload_dir: str, task_payload: dict[str, Any], run_id: str, task_id: str, claim: dict[str, Any], result_queue) -> None:
    """Run one persisted task in a killable worker process.

    Only durable paths and the persisted task payload cross the process
    boundary.  The child rebuilds the stores and resolves the public builtin
    registry itself, so the supervisor never executes GIS code.
    """
    try:
        from .artifacts import ArtifactStore
        from .db import RuntimeDB
        from .executor import RuntimeExecutor
        from .store import RuntimeStore

        runtime = RuntimeStore(RuntimeDB(db_path))
        runtime.initialize()
        artifacts = ArtifactStore(
            data_root,
            runtime,
            artifact_dir=artifact_dir,
            staging_dir=staging_dir,
            quarantine_dir=quarantine_dir,
            upload_dir=upload_dir,
        )
        registry = ToolContractRegistry.with_builtins()
        result = RuntimeExecutor(runtime, artifacts, registry).execute(
            CompiledTask.model_validate(task_payload), run_id=run_id, task_id=task_id,
            claim=claim, started_by_supervisor=True,
        )
        # SQLite/CAS already owns the complete result.  IPC carries only a
        # bounded completion manifest so a large vector/raster cannot block
        # the child process while its Queue feeder waits for the parent.
        result_queue.put(("ok_json", json.dumps(_ipc_result_manifest(result), ensure_ascii=False, sort_keys=True, default=str)))
    except Exception as exc:  # parent converts this to a worker failure
        result_queue.put(("error", type(exc).__name__, bounded_error_message(exc), execution_error_code(exc)))


def _expiry(seconds: float) -> str:
    if seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    return (datetime.now(timezone.utc) + timedelta(seconds=float(seconds))).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc_instant(value: Any) -> datetime | None:
    """Parse an RFC3339 UTC instant; a trailing letter alone is not enough."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        return None
    return parsed.astimezone(timezone.utc)


def _require_live_expiry(value: Any, error: str) -> datetime:
    parsed = _parse_utc_instant(value)
    now = _parse_utc_instant(utc_now())
    if parsed is None or now is None or parsed <= now:
        raise RuntimeError(error)
    return parsed


def _process_creation_time(pid: int) -> float | None:
    """Read a finite process creation timestamp without optional psutil."""
    try:
        import psutil
        value = float(psutil.Process(pid).create_time())
        return value if math.isfinite(value) and value > 0 else None
    except Exception:
        pass
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = (
            wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        )
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user = wintypes.FILETIME()
            ok = kernel.GetProcessTimes(
                wintypes.HANDLE(handle), ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(kernel_time), ctypes.byref(user),
            )
            if not ok:
                return None
            ticks = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
            value = ticks / 10_000_000.0 - 11644473600.0
            return value if math.isfinite(value) and value > 0 else None
        finally:
            kernel.CloseHandle(wintypes.HANDLE(handle))
    except Exception:
        return None


class _WindowsProcessHandle:
    """Minimal Process-compatible handle used when psutil is unavailable."""

    # OpenProcess documents ERROR_INVALID_PARAMETER for a PID that no longer
    # identifies a process.  That is the only failed-open result we treat as
    # positive evidence that a persisted child has exited.  Access denied and
    # every other failure remain unresolved because the process may still be
    # running and cancellation must fail closed.
    _ERROR_INVALID_PARAMETER = 87
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _SYNCHRONIZE = 0x00100000
    _STILL_ACTIVE = 259
    _WAIT_OBJECT_0 = 0
    _WAIT_TIMEOUT = 258

    def __init__(self, handle, pid: int):
        self._handle = handle
        self.pid = pid

    @classmethod
    def open(cls, pid: int):
        if os.name != "nt":
            return None
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            cls._PROCESS_TERMINATE | cls._PROCESS_QUERY_LIMITED_INFORMATION | cls._SYNCHRONIZE,
            False,
            pid,
        )
        if not handle:
            get_last_error = getattr(kernel32, "GetLastError", None)
            if callable(get_last_error):
                error_code = int(get_last_error())
            else:
                error_code = int(ctypes.get_last_error())
            if error_code == cls._ERROR_INVALID_PARAMETER:
                return None
            raise OSError(error_code, f"OpenProcess failed for pid {pid}")
        return cls(handle, pid)

    def _close(self) -> None:
        if self._handle:
            try:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(self._handle)
            except (AttributeError, OSError, TypeError):
                pass
            self._handle = None

    def is_running(self) -> bool:
        if not self._handle:
            return False
        try:
            import ctypes
            code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(self._handle, ctypes.byref(code)):
                return False
            return int(code.value) == self._STILL_ACTIVE
        except (AttributeError, OSError, TypeError):
            return False

    def kill(self) -> None:
        if not self._handle:
            return
        import ctypes
        if not ctypes.windll.kernel32.TerminateProcess(self._handle, 1):
            # Keep the handle open so callers can still observe that the
            # process was not terminated and classify cancellation safely.
            raise OSError("TerminateProcess failed")
        self._close()

    def wait(self, timeout: float | None = None) -> None:
        if not self._handle:
            return
        try:
            import ctypes
            milliseconds = 0xFFFFFFFF if timeout is None else max(0, int(float(timeout) * 1000))
            result = ctypes.windll.kernel32.WaitForSingleObject(self._handle, milliseconds)
            if result == self._WAIT_TIMEOUT:
                raise TimeoutError("process wait timed out")
        finally:
            self._close()

    def children(self, recursive: bool = False) -> list:
        # The persisted identity is used only after a restart.  The process
        # tree is already covered by Job Objects for newly spawned workers;
        # there is no safe ctypes-only child enumeration to claim here.
        return []


def _frozen_recovery_policy(task_payload: dict[str, Any]) -> tuple[bool, bool, RetryPolicy] | None:
    """Return side-effect/idempotency/retry facts persisted with CompiledTask."""
    if not task_payload:
        return None
    try:
        policy = RetryPolicy.model_validate(task_payload.get("retry_policy", task_payload.get("retry", {})))
        return bool(task_payload.get("side_effect", False)), bool(task_payload.get("idempotent", True)), policy
    except Exception:
        return None


@dataclass(frozen=True)
class TaskClaim:
    task_id: str
    task_key: str
    run_id: str
    plan_id: str
    owner: str
    token: str
    lease_generation: int
    lease_expires_at: str
    attempt_id: str = ""


@dataclass(frozen=True)
class RunClaim:
    run_id: str
    owner: str
    token: str
    generation: int
    lease_expires_at: str


class RuntimeQueue:
    """Claim ready tasks durably, with owner/token fencing and expiry recovery."""

    def __init__(self, runtime, *, lease_seconds: float = 30.0, registry: ToolContractRegistry | None = None):
        self.runtime = runtime
        if isinstance(lease_seconds, bool) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.lease_seconds = float(lease_seconds)
        self.registry = registry

    def enqueue_run(self, run_id: str) -> int:
        """Make a run visible to workers and return its currently runnable count."""
        if self.runtime.get_run(run_id) is None:
            raise ValueError(f"unknown run: {run_id}")
        plan_ids = self.runtime._read(lambda c: [row[0] for row in c.execute("SELECT plan_id FROM plans WHERE run_id=?", (run_id,)).fetchall()])
        for plan_id in plan_ids:
            try:
                self.runtime.resolve_task_inputs(plan_id)
            except ValueError:
                # A run with unresolved bindings is still queueable; those
                # tasks remain blocked until their upstream is complete.
                continue
        return self.runtime._read(
            lambda c: c.execute(
                """
                SELECT COUNT(*) FROM tasks t JOIN plans p ON p.plan_id=t.plan_id
                WHERE p.run_id=? AND t.status='ready'
                  AND (p.plan_id=(SELECT active_plan_id FROM runs WHERE run_id=p.run_id)
                       OR (SELECT active_plan_id FROM runs WHERE run_id=p.run_id) IS NULL)
                  AND NOT EXISTS (SELECT 1 FROM task_attempts a WHERE a.task_id=t.task_id
                                  AND a.status IN ('queued','leased','running','validating','retry_wait'))
                """,
                (run_id,),
            ).fetchone()[0]
        )

    @staticmethod
    def _promote_due_retries_conn(conn, now: str) -> None:
        """Turn due retry records into claimable tasks and close old attempts."""
        now_dt = _parse_utc_instant(now)
        rows = []
        for candidate in conn.execute("SELECT task_id,next_retry_at FROM tasks WHERE status='retry_wait' AND next_retry_at IS NOT NULL ORDER BY task_id").fetchall():
            retry_dt = _parse_utc_instant(candidate[1])
            if retry_dt is not None and now_dt is not None and retry_dt <= now_dt:
                rows.append(candidate)
        for row in rows:
            task_id = row[0]
            conn.execute(
                "UPDATE task_attempts SET status='failed',finished_at=?,next_retry_at=NULL WHERE task_id=? AND status='retry_wait'",
                (now, task_id),
            )
            conn.execute(
                "UPDATE tasks SET status='ready',next_retry_at=NULL,blocked_reason=NULL,updated_at=? WHERE task_id=? AND status='retry_wait'",
                (now, task_id),
            )
            run = conn.execute(
                "SELECT p.run_id FROM tasks t JOIN plans p ON p.plan_id=t.plan_id WHERE t.task_id=?",
                (task_id,),
            ).fetchone()
            if run is None:
                continue
            seq = int(conn.execute("SELECT last_event_seq FROM runs WHERE run_id=?", (run[0],)).fetchone()[0]) + 1
            conn.execute(
                "INSERT INTO events(run_id,seq,event_type,payload_json,created_at) VALUES (?,?,?,?,?)",
                (run[0], seq, "task.ready", json.dumps({"task_id": task_id, "reason": "RETRY_DUE"}, sort_keys=True), now),
            )
            conn.execute("UPDATE runs SET last_event_seq=?,updated_at=? WHERE run_id=?", (seq, now, run[0]))

    def schedule_retry(self, claim: TaskClaim, *, error_code: str, next_retry_at: str | None = None, retry_policy: RetryPolicy | None = None) -> str:
        """Persist a retry decision; a later claim creates a new attempt."""
        if not isinstance(error_code, str) or not error_code:
            raise ValueError("retry error_code is required")
        now = utc_now()
        with self.runtime.transaction() as conn:
            row = conn.execute(
                "SELECT lease_owner,lease_token,lease_generation,lease_expires_at,status FROM tasks WHERE task_id=?",
                (claim.task_id,),
            ).fetchone()
            expiry = _parse_utc_instant(row[3]) if row is not None else None
            if row is None or tuple(row[:3]) != (claim.owner, claim.token, claim.lease_generation) or expiry is None or expiry <= _parse_utc_instant(now) or row[4] not in {"leased", "running", "validating"}:
                raise RuntimeError("STALE_LEASE")
            attempt = conn.execute(
                "SELECT attempt_id FROM task_attempts WHERE attempt_id=? AND task_id=? AND status IN ('leased','running','validating')",
                (claim.attempt_id, claim.task_id),
            ).fetchone()
            if attempt is None:
                raise RuntimeError("STALE_LEASE")
            if retry_policy is not None:
                if error_code not in set(retry_policy.retryable_error_codes):
                    raise ValueError("error code is not retryable")
                count = int(conn.execute("SELECT COUNT(*) FROM task_attempts WHERE task_id=?", (claim.task_id,)).fetchone()[0])
                if count >= int(retry_policy.max_attempts):
                    raise ValueError("MAX_ATTEMPTS")
                index = max(0, count - 1)
                delay = float(retry_policy.backoff_seconds[index]) if index < len(retry_policy.backoff_seconds) else 0.0
                next_retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="microseconds").replace("+00:00", "Z")
            if _parse_utc_instant(next_retry_at) is None:
                raise ValueError("next_retry_at must be a UTC RFC3339 timestamp")
            conn.execute(
                "UPDATE task_attempts SET status='retry_wait',next_retry_at=?,error_code=? WHERE attempt_id=?",
                (next_retry_at, error_code, claim.attempt_id),
            )
            conn.execute(
                "UPDATE tasks SET status='retry_wait',next_retry_at=?,blocked_reason=?,updated_at=? WHERE task_id=?",
                (next_retry_at, error_code, now, claim.task_id),
            )
            seq = int(conn.execute("SELECT last_event_seq FROM runs WHERE run_id=?", (claim.run_id,)).fetchone()[0]) + 1
            conn.execute(
                "INSERT INTO events(run_id,seq,event_type,payload_json,created_at) VALUES (?,?,?,?,?)",
                (claim.run_id, seq, "task.retry_wait", json.dumps({"task_id": claim.task_id, "error_code": error_code, "next_retry_at": next_retry_at}, sort_keys=True), now),
            )
            conn.execute("UPDATE runs SET last_event_seq=?,updated_at=? WHERE run_id=?", (seq, now, claim.run_id))
        return next_retry_at

    def claim_planning_run(self, owner: str, *, run_id: str | None = None) -> RunClaim | None:
        """Claim one queued/expired planning Run with a durable fencing token."""
        if not isinstance(owner, str) or not owner:
            raise ValueError("worker owner is required")
        now = utc_now()
        expires = _expiry(self.lease_seconds)
        token = uuid.uuid4().hex
        with self.runtime.transaction() as conn:
            existing = conn.execute(
                "SELECT run_id,planner_lease_token,planner_lease_expires_at,planner_generation FROM runs WHERE planner_lease_owner=? AND status='planning' AND planner_lease_expires_at>? ORDER BY created_at LIMIT 1",
                (owner, now),
            ).fetchone()
            if existing is not None and (run_id is None or existing[0] == run_id):
                return RunClaim(existing[0], owner, existing[1], int(existing[3]), existing[2])
            # A queued Run with an already persisted plan belongs to the task
            # scheduler (including recovery fixtures and a crashed activation
            # hand-off), not the LLM planner.  Fresh public Runs have no plan
            # and are the only queued rows eligible for a planning lease.
            where = "(status='queued' AND NOT EXISTS (SELECT 1 FROM plans p WHERE p.run_id=runs.run_id)) OR (status='planning' AND planner_lease_expires_at<=?)"
            args: list[Any] = [now]
            if run_id is not None:
                where = f"({where}) AND run_id=?"
                args.append(run_id)
            row = conn.execute(
                f"SELECT run_id,planner_generation FROM runs WHERE {where} ORDER BY created_at,run_id LIMIT 1",
                tuple(args),
            ).fetchone()
            if row is None:
                return None
            generation = int(row[1]) + 1
            abandoned = conn.execute(
                "SELECT attempt_id FROM planner_attempts WHERE run_id=? AND lease_generation<? AND status='requested'",
                (row[0], generation),
            ).fetchall()
            for old_attempt in abandoned:
                conn.execute(
                    "UPDATE planner_attempts SET status='failed',error_code='STALE_PLANNER_LEASE',error='STALE_PLANNER_LEASE',finished_at=? WHERE attempt_id=? AND status='requested'",
                    (now, old_attempt[0]),
                )
                self.runtime._append_event_conn(conn, row[0], "planner.failed", {
                    "attempt_id": old_attempt[0], "status": "failed", "error_code": "STALE_PLANNER_LEASE",
                })
            conn.execute(
                "UPDATE runs SET status='planning',planner_lease_owner=?,planner_lease_token=?,planner_lease_expires_at=?,planner_generation=?,run_version=run_version+1,updated_at=? WHERE run_id=?",
                (owner, token, expires, generation, now, row[0]),
            )
            run_version = int(conn.execute("SELECT run_version FROM runs WHERE run_id=?", (row[0],)).fetchone()[0])
            seq = int(conn.execute("SELECT last_event_seq FROM runs WHERE run_id=?", (row[0],)).fetchone()[0]) + 1
            conn.execute("INSERT INTO events(run_id,seq,event_type,payload_json,created_at) VALUES (?,?,?,?,?)", (row[0], seq, "run.planning_claimed", json.dumps({"owner": owner, "generation": generation, "run_version": run_version}, sort_keys=True), now))
            conn.execute("UPDATE runs SET last_event_seq=? WHERE run_id=?", (seq, row[0]))
            return RunClaim(row[0], owner, token, generation, expires)

    def heartbeat_planning(self, claim: RunClaim) -> RunClaim:
        _require_live_expiry(claim.lease_expires_at, "STALE_PLANNER_LEASE")
        expires = _expiry(self.lease_seconds)
        with self.runtime.transaction() as conn:
            row = conn.execute(
                "SELECT planner_lease_expires_at FROM runs WHERE run_id=? AND planner_lease_owner=? AND planner_lease_token=? AND planner_generation=? AND status='planning'",
                (claim.run_id, claim.owner, claim.token, claim.generation),
            ).fetchone()
            _require_live_expiry(row[0] if row is not None else None, "STALE_PLANNER_LEASE")
            updated = conn.execute(
                "UPDATE runs SET planner_lease_expires_at=?,updated_at=? WHERE run_id=? AND planner_lease_owner=? AND planner_lease_token=? AND planner_generation=? AND status='planning'",
                (expires, utc_now(), claim.run_id, claim.owner, claim.token, claim.generation),
            )
            if updated.rowcount != 1:
                raise RuntimeError("STALE_PLANNER_LEASE")
        return RunClaim(claim.run_id, claim.owner, claim.token, claim.generation, expires)

    def release_planning(self, claim: RunClaim, *, status: str = "queued") -> None:
        if status not in {"queued", "planning", "awaiting_input", "ready", "running", "failed"}:
            raise ValueError("invalid planning release status")
        with self.runtime.transaction() as conn:
            row = conn.execute(
                "SELECT planner_lease_expires_at FROM runs WHERE run_id=? AND planner_lease_owner=? AND planner_lease_token=? AND planner_generation=?",
                (claim.run_id, claim.owner, claim.token, claim.generation),
            ).fetchone()
            _require_live_expiry(claim.lease_expires_at, "STALE_PLANNER_LEASE")
            _require_live_expiry(row[0] if row is not None else None, "STALE_PLANNER_LEASE")
            updated = conn.execute(
                "UPDATE runs SET status=?,planner_lease_owner=NULL,planner_lease_token=NULL,planner_lease_expires_at=NULL,updated_at=? WHERE run_id=? AND planner_lease_owner=? AND planner_lease_token=? AND planner_generation=?",
                (status, utc_now(), claim.run_id, claim.owner, claim.token, claim.generation),
            )
            if updated.rowcount != 1:
                raise RuntimeError("STALE_PLANNER_LEASE")

    def recover_expired_leases(self, *, run_id: str | None = None) -> list[str]:
        now = utc_now()
        recovered: list[str] = []
        affected_runs: set[str] = set()
        with self.runtime.transaction() as conn:
            self._promote_due_retries_conn(conn, now)
            query = "SELECT task_id,lease_owner,lease_token,plan_id,task_key,lease_generation,(SELECT attempt_id FROM task_attempts a WHERE a.task_id=tasks.task_id AND a.lease_generation=tasks.lease_generation AND a.status IN ('queued','leased','running','validating','retry_wait') ORDER BY a.started_at DESC LIMIT 1),(SELECT status FROM task_attempts a WHERE a.task_id=tasks.task_id AND a.lease_generation=tasks.lease_generation AND a.status IN ('queued','leased','running','validating','retry_wait') ORDER BY a.started_at DESC LIMIT 1) FROM tasks WHERE status IN ('leased','running','validating') AND lease_expires_at IS NOT NULL AND (plan_id=(SELECT active_plan_id FROM runs WHERE run_id=(SELECT run_id FROM plans WHERE plan_id=tasks.plan_id)) OR (SELECT active_plan_id FROM runs WHERE run_id=(SELECT run_id FROM plans WHERE plan_id=tasks.plan_id)) IS NULL)"
            params: tuple[Any, ...] = ()
            if run_id is not None:
                query += " AND plan_id IN (SELECT plan_id FROM plans WHERE run_id=?)"
                params = (run_id,)
            rows = conn.execute(query, params).fetchall()
            for row in rows:
                lease_expires = conn.execute("SELECT lease_expires_at FROM tasks WHERE task_id=?", (row[0],)).fetchone()
                expiry = _parse_utc_instant(lease_expires[0] if lease_expires else None)
                now_instant = _parse_utc_instant(now)
                if expiry is None or now_instant is None or expiry > now_instant:
                    continue
                recovered_attempt_id = row[6]
                previous_attempt = conn.execute(
                    "SELECT error_code FROM task_attempts WHERE attempt_id=? AND task_id=?",
                    (recovered_attempt_id, row[0]),
                ).fetchone()
                failure_code = (
                    previous_attempt[0] if previous_attempt is not None and previous_attempt[0]
                    else "LEASE_EXPIRED" if row[7] in {"queued", "leased"}
                    else "WORKER_LOST"
                )
                if recovered_attempt_id is not None:
                    conn.execute("UPDATE task_attempts SET status='failed',finished_at=?,error=?,error_code=? WHERE attempt_id=? AND task_id=? AND status IN ('queued','leased','running','validating','retry_wait')", (now, failure_code, failure_code, recovered_attempt_id, row[0]))
                recovery_status = "ready"
                recovery_reason = "LEASE_EXPIRED"
                retry_at = None
                task_row = conn.execute("SELECT task_json FROM tasks WHERE task_id=?", (row[0],)).fetchone()
                try:
                    task_payload = json.loads(task_row[0]) if task_row is not None and task_row[0] else {}
                    frozen = _frozen_recovery_policy(task_payload)
                    if frozen is not None:
                        side_effect, idempotent, retry_policy = frozen
                        # Pre-v1C hand-authored fixtures may persist only the
                        # operation identity. Preserve their compatibility
                        # while any complete CompiledTask remains authoritative
                        # and never consults the live registry.
                        if (
                            self.registry is not None
                            and "retry_policy" not in task_payload
                            and "retry" not in task_payload
                            and task_payload.get("op")
                        ):
                            legacy_contract = self.registry.get(task_payload["op"], task_payload.get("contract_version"))
                            if legacy_contract is not None:
                                retry_policy = legacy_contract.retry_policy
                        if side_effect:
                            recovery_status = "blocked"
                            recovery_reason = "SIDE_EFFECT_OUTCOME_UNKNOWN"
                        elif not idempotent:
                            recovery_status = "failed"
                            recovery_reason = f"NON_IDEMPOTENT_{failure_code}"
                        else:
                            attempts = int(conn.execute("SELECT COUNT(*) FROM task_attempts WHERE task_id=?", (row[0],)).fetchone()[0])
                            retryable_codes = set(retry_policy.retryable_error_codes)
                            # Preserve the historical WORKER_LOST retry
                            # policy for a claim that expired before its
                            # worker entered the running state; the durable
                            # diagnostic remains the more precise
                            # LEASE_EXPIRED classification.
                            retryable = failure_code in retryable_codes or (
                                failure_code == "LEASE_EXPIRED" and "WORKER_LOST" in retryable_codes
                            )
                            if not retryable:
                                recovery_status = "failed"
                                recovery_reason = "NON_RETRYABLE_ERROR"
                            elif attempts >= int(retry_policy.max_attempts):
                                recovery_status = "failed"
                                recovery_reason = "MAX_ATTEMPTS"
                            else:
                                index = max(0, attempts - 1)
                                delay = float(retry_policy.backoff_seconds[index]) if index < len(retry_policy.backoff_seconds) else 0.0
                                retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="microseconds").replace("+00:00", "Z")
                                recovery_status = "retry_wait"
                                recovery_reason = failure_code
                except (KeyError, StopIteration, TypeError, ValueError):
                    recovery_status = "blocked"
                    recovery_reason = "WORKER_LOST_PLAN_INVALID"
                conn.execute(
                    "UPDATE tasks SET status=?,next_retry_at=?,lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,blocked_reason=?,updated_at=? WHERE task_id=?",
                    (recovery_status, retry_at, recovery_reason, now, row[0]),
                )
                if recovery_status == "retry_wait":
                    conn.execute("UPDATE task_attempts SET status='retry_wait',finished_at=NULL,error=NULL,error_code=?,next_retry_at=? WHERE attempt_id=? AND task_id=? AND status='failed'", (failure_code, retry_at, recovered_attempt_id, row[0]))
                plan = conn.execute("SELECT run_id FROM plans WHERE plan_id=(SELECT plan_id FROM tasks WHERE task_id=?)", (row[0],)).fetchone()
                if plan:
                    affected_runs.add(plan[0])
                    seq = conn.execute("SELECT last_event_seq FROM runs WHERE run_id=?", (plan[0],)).fetchone()
                    if seq:
                        event_seq = int(seq[0]) + 1
                        side_effect_unknown = recovery_status == "blocked" and recovery_reason == "SIDE_EFFECT_OUTCOME_UNKNOWN"
                        if side_effect_unknown:
                            current_run = conn.execute("SELECT status FROM runs WHERE run_id=?", (plan[0],)).fetchone()
                            if current_run is not None and current_run[0] in {"queued", "planning", "ready", "running"}:
                                conn.execute("UPDATE runs SET status='awaiting_input',run_version=run_version+1,updated_at=? WHERE run_id=?", (now, plan[0]))
                                conn.execute("INSERT INTO events(run_id,seq,event_type,payload_json,created_at) VALUES (?,?,?,?,?)", (plan[0], event_seq, "run.awaiting_input", json.dumps({"kind": "side_effect_outcome_unknown", "task_id": row[0], "reason": recovery_reason}, sort_keys=True), now))
                                event_seq += 1
                        event_type = "task.awaiting_input" if side_effect_unknown else ("task.retry_wait" if recovery_status == "retry_wait" else "task.lease_expired")
                        event_payload = {"task_id": row[0], "owner": row[1], "reason": recovery_reason}
                        if retry_at is not None:
                            event_payload["next_retry_at"] = retry_at
                        conn.execute("INSERT INTO events(run_id,seq,event_type,payload_json,created_at) VALUES (?,?,?,?,?)", (plan[0], event_seq, event_type, json.dumps(event_payload, sort_keys=True), now))
                        conn.execute("UPDATE runs SET last_event_seq=?,updated_at=? WHERE run_id=?", (event_seq, now, plan[0]))
                recovered.append(row[0])
            # Recovery is a single durable transaction: once an expired task
            # has been classified, project the active plan's complete task
            # facts onto its Run before releasing the SQLite write lock.
            for affected_run_id in sorted(affected_runs):
                self.runtime.aggregate_run_terminal(affected_run_id)
        return recovered

    def claim_next(self, owner: str, *, run_id: str | None = None) -> TaskClaim | None:
        if not isinstance(owner, str) or not owner:
            raise ValueError("worker owner is required")
        self.recover_expired_leases()
        now = utc_now()
        expires = _expiry(self.lease_seconds)
        token = uuid.uuid4().hex
        with self.runtime.transaction() as conn:
            # A retry from the same worker is idempotent while its lease is
            # still live.  Returning the persisted fencing token is important:
            # inventing a fresh token here would make the first invocation
            # stale even though no competing claim occurred.
            active_args: tuple[Any, ...] = (owner, now)
            active_run = ""
            if run_id is not None:
                active_run = "AND p.run_id=?"
                active_args += (run_id,)
            existing = conn.execute(
                f"""
                SELECT t.task_id,t.task_key,t.plan_id,p.run_id,t.lease_generation,
                       t.lease_token,t.lease_expires_at,
                       (SELECT attempt_id FROM task_attempts a WHERE a.task_id=t.task_id
                        AND a.lease_generation=t.lease_generation
                        AND a.status IN ('leased','running','validating')
                        ORDER BY a.started_at DESC LIMIT 1)
                FROM tasks t JOIN plans p ON p.plan_id=t.plan_id
                WHERE t.lease_owner=? AND t.status IN ('leased','running')
                  AND (SELECT status FROM runs WHERE run_id=p.run_id) NOT IN ('cancelling','awaiting_input','completed','failed','cancelled')
                  AND t.lease_token IS NOT NULL AND t.lease_expires_at>? {active_run}
                  AND (p.plan_id=(SELECT active_plan_id FROM runs WHERE run_id=p.run_id)
                       OR (SELECT active_plan_id FROM runs WHERE run_id=p.run_id) IS NULL)
                ORDER BY t.created_at,t.task_id LIMIT 1
                """,
                active_args,
            ).fetchone()
            if existing is not None:
                return TaskClaim(existing[0], existing[1], existing[3], existing[2], owner, existing[5], int(existing[4]), existing[6], existing[7] or "")
            where_run = "AND p.run_id=?" if run_id is not None else ""
            args: tuple[Any, ...] = (now,)
            if run_id is not None:
                args += (run_id,)
            row = conn.execute(
                f"""
                SELECT t.task_id,t.task_key,t.plan_id,p.run_id,t.lease_generation
                FROM tasks t JOIN plans p ON p.plan_id=t.plan_id
                WHERE t.status='ready' {where_run}
                  AND (SELECT status FROM runs WHERE run_id=p.run_id) NOT IN ('cancelling','awaiting_input','completed','failed','cancelled')
                  AND (p.plan_id=(SELECT active_plan_id FROM runs WHERE run_id=p.run_id)
                       OR (SELECT active_plan_id FROM runs WHERE run_id=p.run_id) IS NULL)
                  AND NOT EXISTS (SELECT 1 FROM task_dependencies d JOIN tasks dep ON dep.task_id=d.depends_on_task_id
                                  WHERE d.task_id=t.task_id AND dep.status <> 'succeeded')
                  AND NOT EXISTS (SELECT 1 FROM task_attempts a WHERE a.task_id=t.task_id
                                  AND a.status IN ('queued','leased','running','validating','retry_wait'))
                ORDER BY t.created_at,t.task_id LIMIT 1
                """,
                args[1:] if run_id is not None else (),
            ).fetchone()
            if row is None:
                return None
            generation = int(row[4]) + 1
            conn.execute("UPDATE tasks SET status='leased',lease_owner=?,lease_token=?,lease_expires_at=?,lease_generation=?,updated_at=? WHERE task_id=? AND status='ready'", (owner, token, expires, generation, now, row[0]))
            attempt_id = f"{row[0]}:{generation}:{token}"
            conn.execute("INSERT INTO task_attempts(attempt_id,task_id,attempt_no,status,lease_generation,started_at,executor_identity) VALUES (?,?,?,?,?,?,?)", (attempt_id, row[0], generation, "leased", generation, now, f"{owner}:{attempt_id}"))
            seq = int(conn.execute("SELECT last_event_seq FROM runs WHERE run_id=?", (row[3],)).fetchone()[0]) + 1
            conn.execute("INSERT INTO events(run_id,seq,event_type,payload_json,created_at) VALUES (?,?,?,?,?)", (row[3], seq, "task.leased", json.dumps({"task_id": row[0], "owner": owner, "lease_generation": generation}, sort_keys=True), now))
            conn.execute("UPDATE runs SET last_event_seq=?,updated_at=? WHERE run_id=?", (seq, now, row[3]))
            return TaskClaim(row[0], row[1], row[3], row[2], owner, token, generation, expires, attempt_id)

    def heartbeat(self, claim: TaskClaim) -> TaskClaim:
        _require_live_expiry(claim.lease_expires_at, "STALE_LEASE")
        expires = _expiry(self.lease_seconds)
        now = utc_now()
        with self.runtime.transaction() as conn:
            row = conn.execute(
                """SELECT t.lease_expires_at,a.attempt_id,a.lease_generation,a.status,a.executor_identity
                   FROM tasks t LEFT JOIN task_attempts a ON a.attempt_id=? AND a.task_id=t.task_id
                   WHERE t.task_id=? AND t.lease_owner=? AND t.lease_token=? AND t.lease_generation=?
                     AND t.status IN ('leased','running','validating')""",
                (claim.attempt_id, claim.task_id, claim.owner, claim.token, claim.lease_generation),
            ).fetchone()
            _require_live_expiry(row[0] if row is not None else None, "STALE_LEASE")
            if row is None or not claim.attempt_id or row[1] != claim.attempt_id or int(row[2]) != int(claim.lease_generation) or row[3] not in {"leased", "running", "validating"} or not row[4] or not str(row[4]).strip():
                raise RuntimeError("STALE_LEASE")
            updated = conn.execute(
                "UPDATE tasks SET lease_expires_at=?,updated_at=? WHERE task_id=? AND lease_owner=? AND lease_token=? AND lease_generation=? AND status IN ('leased','running','validating')",
                (expires, now, claim.task_id, claim.owner, claim.token, claim.lease_generation),
            )
            if updated.rowcount != 1:
                raise RuntimeError("STALE_LEASE")
        return TaskClaim(claim.task_id, claim.task_key, claim.run_id, claim.plan_id, claim.owner, claim.token, claim.lease_generation, expires, claim.attempt_id)

    def assert_current(self, claim: TaskClaim) -> None:
        """Fail closed when a worker attempts a fenced write after reclaim."""
        row = self.runtime._read(
            lambda c: c.execute(
                """SELECT t.lease_owner,t.lease_token,t.lease_generation,t.lease_expires_at,t.status,
                          a.attempt_id,a.lease_generation,a.status,a.executor_identity
                   FROM tasks t LEFT JOIN task_attempts a ON a.attempt_id=? AND a.task_id=t.task_id
                   WHERE t.task_id=?""",
                (claim.attempt_id, claim.task_id),
            ).fetchone()
        )
        now = _parse_utc_instant(utc_now())
        expiry = _parse_utc_instant(row[3]) if row is not None else None
        if row is None or row[0] != claim.owner or row[1] != claim.token or int(row[2]) != claim.lease_generation or row[3] is None or expiry is None or now is None or expiry <= now or row[4] not in {"leased", "running", "validating"} or row[5] != claim.attempt_id or int(row[6]) != claim.lease_generation or row[7] not in {"leased", "running", "validating"} or not row[8] or not str(row[8]).strip():
            raise RuntimeError("STALE_LEASE")

    def release(self, claim: TaskClaim) -> None:
        _require_live_expiry(claim.lease_expires_at, "STALE_LEASE")
        with self.runtime.transaction() as conn:
            now = utc_now()
            row = conn.execute(
                """SELECT t.lease_expires_at,t.status,a.attempt_id,a.lease_generation,a.status,a.executor_identity
                   FROM tasks t LEFT JOIN task_attempts a ON a.attempt_id=? AND a.task_id=t.task_id
                   WHERE t.task_id=? AND t.lease_owner=? AND t.lease_token=? AND t.lease_generation=?""",
                (claim.attempt_id, claim.task_id, claim.owner, claim.token, claim.lease_generation),
            ).fetchone()
            _require_live_expiry(row[0] if row is not None else None, "STALE_LEASE")
            if row is None or not claim.attempt_id or row[2] != claim.attempt_id or int(row[3]) != int(claim.lease_generation) or not row[5] or not str(row[5]).strip():
                raise RuntimeError("STALE_LEASE")
            if row[1] in {"leased", "running", "validating"}:
                # Clearing a live lease is not a state transition.  Refuse
                # this public primitive unless the caller has first durably
                # terminalized the Task (or entered retry_wait through the
                # queue retry decision), otherwise it creates an active
                # no-lease orphan that no worker can claim.
                raise RuntimeError("TASK_NOT_TERMINAL")
            if row[1] == "retry_wait" and row[4] != "retry_wait":
                raise RuntimeError("TASK_NOT_TERMINAL")
            if row[1] in {"succeeded", "failed", "cancelled"} and row[4] in {"queued", "leased", "running", "validating", "retry_wait"}:
                raise RuntimeError("TASK_NOT_TERMINAL")
            updated = conn.execute("UPDATE tasks SET lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE task_id=? AND lease_owner=? AND lease_token=? AND lease_generation=?", (now, claim.task_id, claim.owner, claim.token, claim.lease_generation))
            if updated.rowcount != 1:
                raise RuntimeError("STALE_LEASE")

    def abandon_worker_claims(self, owner: str, *, reason: str = "WORKER_LOST") -> list[str]:
        """Reconcile every live claim owned by a stopping worker atomically."""
        if not isinstance(owner, str) or not owner:
            raise ValueError("worker owner is required")
        now = utc_now()
        abandoned: list[str] = []
        with self.runtime.transaction() as conn:
            rows = conn.execute(
                """SELECT t.task_id,p.run_id,t.status
                   FROM tasks t JOIN plans p ON p.plan_id=t.plan_id
                   WHERE t.lease_owner=? AND t.status IN ('leased','running','validating')""",
                (owner,),
            ).fetchall()
            for row in rows:
                run = conn.execute("SELECT status FROM runs WHERE run_id=?", (row[1],)).fetchone()
                if run is not None and run[0] in {"cancelling", "cancelled", "completed", "failed"}:
                    conn.execute("UPDATE task_attempts SET status='cancelled',finished_at=?,error=?,error_code=? WHERE task_id=? AND status IN ('queued','leased','running','validating')", (now, reason, reason, row[0]))
                    conn.execute("UPDATE tasks SET status='cancelled',lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,blocked_reason=?,updated_at=? WHERE task_id=?", ("RUN_CANCELLED", now, row[0]))
                    self.runtime._append_event_conn(conn, row[1], "task.cancelled", {"task_id": row[0], "reason": reason, "owner": owner})
                else:
                    # Route worker shutdown through the same persisted-policy
                    # recovery state machine as lease expiry. This preserves
                    # idempotency, retry and side-effect safety decisions.
                    conn.execute("UPDATE task_attempts SET error_code=?,error=? WHERE task_id=? AND status IN ('queued','leased','running','validating')", (reason, reason, row[0]))
                    conn.execute("UPDATE tasks SET lease_expires_at=?,updated_at=? WHERE task_id=? AND lease_owner=?", (now, now, row[0], owner))
                abandoned.append(row[0])
        self.recover_expired_leases()
        return abandoned

    def cancel_run(self, run_id: str) -> None:
        # Worker-initiated cancellation uses the same atomic command path as
        # the public API.  This prevents a command/event/status split when a
        # process exits between individual writes.
        self.runtime.apply_cancel_command(
            run_id,
            command_id=f"cancel:{run_id}",
            request_hash=hashlib.sha256(f"worker_cancel:{run_id}".encode()).hexdigest(),
        )


class RuntimeSupervisor:
    """Small public service facade used by the later FastAPI integration."""

    def __init__(self, runtime, artifacts, *, registry: ToolContractRegistry | None = None, planner=None,
                 lease_seconds: float = 30.0, isolate: bool = True, timeout_seconds: float | None = None,
                 heartbeat_interval: float | None = None, autostart: bool = True):
        self.runtime = runtime
        self.artifacts = artifacts
        self.autostart = bool(autostart)
        self._registry_supplied = registry is not None
        self.isolate = bool(isolate)
        self.timeout_seconds = timeout_seconds
        self.heartbeat_interval = float(heartbeat_interval) if heartbeat_interval is not None else min(float(lease_seconds) / 3.0, 5.0)
        if self.heartbeat_interval <= 0:
            raise ValueError("heartbeat_interval must be positive")
        self._active_processes: dict[str, Any] = {}
        self._active_jobs: dict[str, Any] = {}
        self._shutdown_event = threading.Event()
        self._wake_event = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._worker_id: str | None = None
        self._planner_claim_refresh: dict[str, RunClaim] = {}
        self._prefer_task: dict[str, bool] = {}
        self._worker_stop_events: dict[str, Any] = {}
        self.registry = registry or ToolContractRegistry.with_builtins()
        # The default is a real provider boundary, not a deterministic route
        # table.  Tests inject a contract-shaped planner explicitly.
        self.planner = planner or OpenAICompatiblePlanner.from_settings(registry=self.registry)
        self.queue = RuntimeQueue(runtime, lease_seconds=lease_seconds, registry=self.registry)

    def enqueue_run(self, run_id: str) -> int:
        count = self.queue.enqueue_run(run_id)
        self._wake_event.set()
        return count

    def recover_expired_leases(self) -> list[str]:
        return self.queue.recover_expired_leases()

    def recover_cancelling_runs(self) -> list[str]:
        """Finish or pause cancellation reservations left by a prior worker.

        Cancellation is deliberately a two-phase durable protocol.  A worker
        restart must revisit the reservation before claiming new work so a
        ``cancelling`` Run cannot remain stranded indefinitely.
        """
        run_ids = self.runtime._read(
            lambda c: [row[0] for row in c.execute(
                "SELECT run_id FROM runs WHERE status='cancelling' ORDER BY updated_at,run_id"
            ).fetchall()]
        )
        converged: list[str] = []
        for run_id in run_ids:
            command = self.runtime._read(
                lambda c: c.execute(
                    "SELECT command_id FROM run_commands WHERE run_id=? AND command_type='cancel' ORDER BY created_at DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
            )
            command_id = command[0] if command is not None else f"cancel:recovery:{run_id}"
            if command is None:
                self.runtime.create_run_command(
                    command_id, run_id, "cancel",
                    {"request_hash": hashlib.sha256(command_id.encode()).hexdigest()},
                    idempotency_key=command_id,
                )
            unresolved = self.terminate_run_processes(run_id) or []
            if unresolved:
                try:
                    self.runtime.mark_cancel_indeterminate(
                        run_id, command_id=command_id,
                    )
                except RuntimeError as exc:
                    # Another recovery worker may have converged the same
                    # reservation between the initial read and this write.
                    # The durable Run state is authoritative; tolerate only
                    # the expected idempotent race.
                    if str(exc) != "RUN_NOT_CANCELLING":
                        raise
            else:
                try:
                    self.runtime.finalize_cancel_command(
                        run_id, command_id=command_id,
                    )
                except RuntimeError as exc:
                    if str(exc) != "RUN_NOT_CANCELLING":
                        raise
            converged.append(run_id)
        return converged

    def cancel_run(self, run_id: str) -> None:
        command_id = f"cancel:{run_id}"
        self.runtime.apply_cancel_command(
            run_id,
            command_id=command_id,
            request_hash=hashlib.sha256(f"worker_cancel:{run_id}".encode()).hexdigest(),
            finalize=False,
            defer_active=True,
        )
        unresolved = self.terminate_run_processes(run_id) or []
        if unresolved:
            self.runtime.mark_cancel_indeterminate(run_id, command_id=command_id)
        else:
            self.runtime.finalize_cancel_command(run_id, command_id=command_id, resolved=True)
        self._wake_event.set()

    @staticmethod
    def _persisted_process(identity: str):
        """Resolve a PID only when its persisted creation identity matches."""
        if not isinstance(identity, str) or not identity.strip():
            return None, False
        if identity.startswith("store:"):
            return None, True
        try:
            value = json.loads(identity)
            raw_pid = value["pid"]
            # Do not coerce bools/floats/strings into a PID.  A persisted
            # process identity is a strict positive JSON integer.
            if type(raw_pid) is not int or raw_pid <= 0:
                return None, False
            pid = raw_pid
            raw_expected = value["create_time"]
            if isinstance(raw_expected, bool) or not isinstance(raw_expected, (int, float)):
                return None, False
            expected = float(raw_expected)
            if pid == os.getpid() or not math.isfinite(expected) or expected <= 0:
                return None, False
            psutil_module = None
            try:
                import psutil as psutil_module
                # Tests and stripped deployments may expose a null module
                # sentinel to indicate that the optional dependency is not
                # installed.  Treat it exactly like ImportError.
                if psutil_module is None:
                    raise ImportError
                process = psutil_module.Process(pid)
            except (ImportError, AttributeError):
                process = _WindowsProcessHandle.open(pid)
                if process is None:
                    return None, True
            actual = _process_creation_time(pid)
            if actual is None or abs(actual - expected) > 0.01:
                close = getattr(process, "_close", None)
                if callable(close):
                    close()
                return None, False
            return process, True
        except (KeyError, TypeError, ValueError, OverflowError, ImportError):
            return None, False
        except Exception as exc:
            no_such_process = getattr(psutil_module, "NoSuchProcess", None)
            if isinstance(no_such_process, type) and isinstance(exc, no_such_process):
                return None, True
            return None, False

    def terminate_run_processes(self, run_id: str) -> list[str]:
        """Stop children using in-memory or safely persisted process identity."""
        active = self.runtime._read(
            lambda c: [tuple(row) for row in c.execute(
                """SELECT t.task_id,a.status,a.executor_identity
                   FROM tasks t JOIN plans p ON p.plan_id=t.plan_id
                   LEFT JOIN task_attempts a ON a.attempt_id=(
                     SELECT a2.attempt_id FROM task_attempts a2 WHERE a2.task_id=t.task_id
                     AND a2.status IN ('queued','leased','running','validating')
                     ORDER BY a2.started_at DESC LIMIT 1)
                   WHERE p.run_id=? AND t.status IN ('leased','running','validating')""",
                (run_id,),
            ).fetchall()]
        )
        unresolved: list[str] = []
        for task_id, attempt_status, identity in active:
            process = self._active_processes.get(task_id)
            if process is not None and process.is_alive():
                _terminate_process_tree(process)
                process.join(5)
                if process.is_alive():
                    unresolved.append(task_id)
                continue
            if attempt_status == "leased":
                # The durable queue identity is not an execution process.
                continue
            process, safe = self._persisted_process(identity)
            if not safe:
                unresolved.append(task_id)
                continue
            if process is not None:
                try:
                    for child in reversed(process.children(recursive=True)):
                        child.kill()
                    process.kill()
                    process.wait(timeout=5)
                except Exception:
                    # A failed TerminateProcess (including access denied or
                    # an invalid handle) is not proof that the child stopped.
                    # Keep the cancellation reservation unresolved rather
                    # than falsely committing a terminal cancelled Run.
                    unresolved.append(task_id)
                    close = getattr(process, "_close", None)
                    if callable(close):
                        close()
        return unresolved

    def _cleanup_active_process(self, task_id: str) -> None:
        """Release supervisor references after any isolated-task exception.

        Timeout/cancel/lease errors can leave ``_run_isolated`` before its
        normal teardown tail.  Always kill a still-live child and close its
        Job Object handle here so a failed task cannot leak a process or
        resource handle into later Runs.
        """
        process = self._active_processes.pop(task_id, None)
        self._active_jobs.pop(task_id, None)
        if process is None:
            return
        try:
            if process.is_alive():
                _terminate_process_tree(process)
                process.join(5)
        finally:
            release_resource_limits(process)

    def _finalize_run_if_terminal(self, run_id: str) -> str | None:
        """Project durable task facts onto one durable Run terminal state.

        A task failure must not strand its Run in ``running``.  Resolve
        dependency fallout first so descendants of a failed task become
        explicitly cancelled rather than remaining blocked forever; then
        derive the Run outcome from the persisted Task statuses.  This method
        deliberately reads/writes only the Runtime Store, so it is safe after
        an in-process handler error as well as after an isolated child error.
        """
        current = self.runtime.get_run(run_id)
        if current is None or current["status"] in {"completed", "failed", "cancelled", "cancelling"}:
            return current["status"] if current is not None else None
        # ``resolve_task_inputs`` is also the durable dependency reconciler:
        # it changes blocked descendants with a failed/cancelled prerequisite
        # to ``cancelled`` in the same SQLite transaction as its event.
        self.queue.enqueue_run(run_id)
        return self.runtime.aggregate_run_terminal(run_id)

    def _recover_terminal_runs(self) -> None:
        """Close Runs whose final Task commit outlived the previous parent."""
        run_ids = self.runtime._read(lambda c: [row[0] for row in c.execute(
            """SELECT DISTINCT r.run_id FROM runs r
               JOIN plans p ON p.run_id=r.run_id
               JOIN tasks t ON t.plan_id=p.plan_id
               WHERE r.status IN ('queued','ready','running')
                 AND (r.active_plan_id=p.plan_id OR r.active_plan_id IS NULL)
                 AND t.status IN ('succeeded','failed','cancelled')"""
        ).fetchall()])
        for run_id in run_ids:
            self._finalize_run_if_terminal(run_id)

    def _planner_request(self, run_id: str) -> PlannerRequest:
        run = self.runtime.get_run(run_id)
        if run is None:
            raise PlannerError("RUN_NOT_FOUND", "run does not exist")
        rows = self.runtime._read(lambda c: list(c.execute(
            "SELECT revision,input_json FROM run_input_revisions WHERE run_id=? ORDER BY revision", (run_id,)
        ).fetchall()))
        if not rows:
            raise PlannerError("PLAN_INPUT_MISSING", "run has no immutable input revision")
        decoded: list[dict[str, Any]] = []
        for row in rows:
            try:
                value = json.loads(row["input_json"])
            except (TypeError, ValueError) as exc:
                raise PlannerError("PLAN_INPUT_INVALID", "run input revision is invalid JSON") from exc
            if not isinstance(value, dict):
                raise PlannerError("PLAN_INPUT_INVALID", "run input revision must be an object")
            decoded.append(value)
        initial = decoded[0]
        goal = initial.get("prompt")
        if not isinstance(goal, str) or not goal.strip():
            raise PlannerError("PLAN_INPUT_MISSING", "run needs a non-empty natural-language message")
        artifact_ids = initial.get("input_artifact_ids", [])
        if not isinstance(artifact_ids, list) or not all(isinstance(item, str) and item for item in artifact_ids):
            raise PlannerError("PLAN_INPUT_INVALID", "input_artifact_ids must be a list of artifact IDs")
        input_artifacts = []
        for artifact_id in artifact_ids:
            artifact = self.runtime.get_artifact(artifact_id)
            if artifact is None or artifact.get("validation_status") != "valid":
                raise PlannerError("ARTIFACT_NOT_FOUND", f"input artifact is unavailable: {artifact_id}")
            input_artifacts.append({
                "artifact_id": artifact_id, "type": artifact.get("type", "File"),
                "crs": artifact.get("crs"), "schema": artifact.get("schema", {}),
                "content_hash": artifact.get("content_hash"), "media_type": artifact.get("media_type"),
            })
        # Give the model a bounded conversational view while keeping all
        # executable data flow explicit through Artifact IDs.  The message
        # that created this Run is already represented by user_goal and is
        # omitted to avoid duplicate instructions.
        message_rows = self.runtime._read(lambda c: list(c.execute(
            "SELECT role,content,metadata_json FROM messages WHERE session_id=? "
            "ORDER BY message_index DESC LIMIT 24", (run["session_id"],)
        ).fetchall()))
        newest_messages: list[dict[str, str]] = []
        remaining_chars = 16000
        for row in message_rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            if isinstance(metadata, dict) and metadata.get("run_id") == run_id:
                continue
            role = str(row["role"])
            if role not in {"user", "assistant"}:
                continue
            content = str(row["content"])[:4000]
            if not content or remaining_chars <= 0:
                continue
            content = content[:remaining_chars]
            remaining_chars -= len(content)
            newest_messages.append({"role": role, "content": content})
        session_messages = list(reversed(newest_messages))

        current_ids = set(artifact_ids)
        prior_ids: list[tuple[str, str]] = []
        linked_rows = self.runtime._read(lambda c: list(c.execute(
            "SELECT DISTINCT ma.artifact_id FROM message_artifacts ma "
            "JOIN messages m ON m.message_id=ma.message_id "
            "JOIN artifacts a ON a.artifact_id=ma.artifact_id "
            "WHERE m.session_id=? AND a.validation_status='valid' "
            "ORDER BY m.message_index DESC LIMIT 16", (run["session_id"],)
        ).fetchall()))
        prior_ids.extend((str(row[0]), "message") for row in linked_rows)
        output_rows = self.runtime._read(lambda c: list(c.execute(
            "SELECT DISTINCT o.artifact_id FROM task_outputs o "
            "JOIN tasks t ON t.task_id=o.task_id JOIN plans p ON p.plan_id=t.plan_id "
            "JOIN runs r ON r.run_id=p.run_id JOIN artifacts a ON a.artifact_id=o.artifact_id "
            "WHERE r.session_id=? AND r.run_id<>? AND r.status='completed' "
            "AND a.validation_status='valid' ORDER BY r.updated_at DESC LIMIT 16",
            (run["session_id"], run_id),
        ).fetchall()))
        prior_ids.extend((str(row[0]), "run_output") for row in output_rows)
        prior_artifacts: list[dict[str, Any]] = []
        seen_prior: set[str] = set()
        for artifact_id, source in prior_ids:
            if artifact_id in current_ids or artifact_id in seen_prior or len(prior_artifacts) >= 16:
                continue
            artifact = self.runtime.get_artifact(artifact_id)
            if artifact is None or artifact.get("validation_status") != "valid":
                continue
            seen_prior.add(artifact_id)
            prior_artifacts.append({
                "artifact_id": artifact_id, "type": artifact.get("type", "File"),
                "crs": artifact.get("crs"), "schema": artifact.get("schema", {}),
                "content_hash": artifact.get("content_hash"), "media_type": artifact.get("media_type"),
                "source": source,
            })
        patch: dict[str, Any] = {}
        for value in decoded[1:]:
            patch.update(value)
        return PlannerRequest(
            run_id=run_id, input_revision=int(run["input_revision"]), user_goal=goal.strip(),
            input_artifacts=tuple(input_artifacts), revision_patch=patch,
            session_messages=tuple(session_messages), prior_artifacts=tuple(prior_artifacts),
        )

    def _planner_identity(self, request: PlannerRequest | None = None) -> tuple[str, str, str, str, str]:
        provider = str(getattr(self.planner, "provider", type(self.planner).__name__))
        model = str(getattr(self.planner, "model", "test-double"))
        prompt_version = str(getattr(self.planner, "prompt_version", "injected-planner-v1"))
        catalog_hash = candidate_catalog(self.registry, request).catalog_hash if request is not None else hashlib.sha256(json.dumps(
            self.registry.public_operations_manifest(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        return provider, model, prompt_version, catalog_hash, ""

    @staticmethod
    def _coerce_planner_call(value: PlannerDraft | PlannerCall, request: PlannerRequest,
                             *, fallback: tuple[str, str, str, str, str]) -> tuple[PlannerDraft, dict[str, Any]]:
        provider, model, prompt_version, catalog_hash, _unused = fallback
        if isinstance(value, PlannerCall):
            return value.draft, {
                "provider": value.provider, "model": value.model, "prompt_version": value.prompt_version,
                "catalog_hash": value.catalog_hash, "request_hash": value.request_hash,
                "response_hash": value.response_hash, "usage": value.usage, "latency_ms": value.latency_ms,
            }
        if not isinstance(value, PlannerDraft):
            raise PlannerError("LLM_DRAFT_INVALID", "planner did not return a PlannerDraft")
        request_hash = hashlib.sha256(json.dumps({
            "run_id": request.run_id, "input_revision": request.input_revision,
            "goal": request.user_goal, "provider": provider,
        }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        return value, {
            "provider": provider, "model": model, "prompt_version": prompt_version,
            "catalog_hash": catalog_hash, "request_hash": request_hash,
            "response_hash": hashlib.sha256(value.model_dump_json().encode("utf-8")).hexdigest(),
            "usage": {}, "latency_ms": 0,
        }

    def _call_planner_with_lease_heartbeat(self, claim: RunClaim, callback):
        """Run one potentially slow provider call without losing its Run lease.

        Planner calls are external I/O and routinely exceed a small local
        lease.  The heartbeat owns no planning semantics: it only renews the
        owner/token/generation fence while the synchronous provider call is
        outstanding.  A heartbeat failure is surfaced before its output can
        be committed, preserving the at-least-once claim boundary.
        """
        stop = threading.Event()
        failures: list[Exception] = []
        budget = current_planning_budget.get()
        interval = max(0.001, min(self.heartbeat_interval, self.queue.lease_seconds / 3.0))
        poll_interval = min(interval, 0.1)

        def renew() -> None:
            next_renewal = time.monotonic() + interval
            while not stop.wait(poll_interval):
                try:
                    if self._shutdown_event.is_set() and budget is not None:
                        budget.cancel("WORKER_SHUTDOWN")
                        return
                    if time.monotonic() >= next_renewal:
                        current[0] = self.queue.heartbeat_planning(current[0])
                        next_renewal = time.monotonic() + interval
                    else:
                        # Cancellation checks are read-only; responsiveness
                        # must not multiply the configured durable lease writes.
                        self.runtime._read(lambda conn: self.runtime._assert_planner_fence_conn(
                            conn, claim.run_id, current[0],
                        ))
                except Exception as exc:  # the owner may have gone stale
                    failures.append(exc)
                    if budget is not None:
                        budget.cancel("STALE_PLANNER_LEASE")
                    return

        # Renew synchronously before handing control to an external provider;
        # very short local leases must not expire while the heartbeat thread
        # is being scheduled.
        current = [self.queue.heartbeat_planning(claim)]

        thread = threading.Thread(
            target=renew, name=f"gismind-planner-lease-{claim.run_id}", daemon=True,
        )
        thread.start()
        try:
            if budget is not None:
                budget.check()
            value = callback()
            if budget is not None:
                budget.check()
            # A provider can return just after the periodic heartbeat thread
            # missed its final interval (especially with very short test or
            # local leases).  Renew synchronously at the hand-off boundary so
            # the subsequent fenced planner-attempt/PlanIR commits have a
            # fresh lease, while still surfacing a genuinely expired claim.
            if not failures:
                current[0] = self.queue.heartbeat_planning(current[0])
        finally:
            stop.set()
            thread.join(timeout=max(1.0, interval * 2.0))
            self._planner_claim_refresh[claim.run_id] = current[0]
        if failures:
            raise RuntimeError("STALE_PLANNER_LEASE") from failures[0]
        return value

    @staticmethod
    def _require_executable_draft(draft: PlannerDraft) -> None:
        """Keep clarification a Run state, never an empty PlanIR shortcut."""
        if draft.origin == "needs_clarification":
            question = draft.clarification
            patch_schema = {"type": "object", "minProperties": 1}
            if question is not None:
                patch_schema = {
                    "type": "object",
                    "properties": {"clarification": {
                        "type": "string", "title": "补充信息", "description": question,
                        "minLength": 1, "maxLength": 2000,
                    }},
                    "required": ["clarification"], "additionalProperties": False,
                }
            raise AwaitingInputDiagnostic(
                "CLARIFICATION_REQUIRED", "planner.origin",
                question if question is not None else "planner requires additional user input before creating a plan",
                {"patch_schema": patch_schema},
            )

    @staticmethod
    def _require_visible_artifact_refs(draft: PlannerDraft, request: PlannerRequest) -> None:
        unavailable = _first_unavailable_artifact_ref(draft, request)
        if unavailable is not None:
            path, artifact_id = unavailable
            raise CompileError(
                "ARTIFACT_NOT_VISIBLE", path,
                "ArtifactRef is outside this Run's input_artifacts and explicit prior_artifacts",
                {"artifact_id": artifact_id},
            )

    @classmethod
    def _guard_buffer_distance(cls, request: PlannerRequest, draft: PlannerDraft) -> None:
        buffers = [step for step in draft.tasks if step.op == "vector.buffer"]
        if not buffers:
            return
        from .goal_requirements import build_goal_requirements
        requirements = build_goal_requirements(request.user_goal,
            (*request.input_artifacts, *request.prior_artifacts), request.revision_patch)
        ordered_buffers = [item for stage in requirements.get('ordered_stages', [])
                           for item in stage['requirements'] if item['op'] == 'vector.buffer']
        grouped_buffers = [item for group in requirements.get('instruction_groups', [])
                           for item in group.get('operations', []) if item['op'] == 'vector.buffer']
        if len(ordered_buffers) > 1 or (len(grouped_buffers) > 1 and all(
                'distance_m' in item.get('params', {}) for item in grouped_buffers)):
            # Multiple occurrences are checked against their own stage/group
            # facts by the compiler, including scoped corrections and missing
            # facts. An op-wide scalar would overwrite unrelated distances.
            return
        expected_m = _verified_buffer_distance_m(request)
        if expected_m is None:
            if grouped_buffers and all("distance_m" in item.get("params", {}) for item in grouped_buffers):
                # Distinct, explicitly grounded instructions are not one
                # ambiguous distance. The compiler checks each group's exact
                # parameters AND its own final dataflow before activation.
                return
            question = "请明确本次缓冲距离及单位（米或公里）；如有多个距离，请说明用于缓冲的那个。"
            raise cls._rename_clarification("planner.buffer_distance", question, "缓冲距离及单位")
        for step in buffers:
            actual_m = step.params.get("distance_m")
            if isinstance(actual_m, bool) or not isinstance(actual_m, (int, float)) or not math.isfinite(actual_m):
                continue  # The ToolContract reports malformed or missing params.
            if not math.isclose(float(actual_m), expected_m, rel_tol=1e-9, abs_tol=1e-6):
                raise CompileError(
                    "BUFFER_DISTANCE_MISMATCH", f"tasks.{step.task_key}.params.distance_m",
                    "buffer distance differs from the explicit user-supplied distance",
                    {"expected_distance_m": expected_m, "planned_distance_m": actual_m},
                )

    @staticmethod
    def _guard_semantic_facts(request: PlannerRequest, draft: PlannerDraft) -> None:
        mismatch = _semantic_fact_mismatch(request, draft.tasks)
        if mismatch is not None:
            code, path, message, details = mismatch
            raise CompileError(code, path, message, details)

    @staticmethod
    def _clarified_name(answer: Any, prefixes: tuple[str, ...]) -> str | None:
        if isinstance(answer, str):
            normalized = answer.strip().strip("。！!？?，,；; ")
            for prefix in prefixes:
                if normalized.startswith(prefix):
                    normalized = normalized[len(prefix):].strip()
                    break
            if len(normalized) >= 2 and normalized[0] in "“‘\"'" and normalized[-1] in "”’\"'":
                normalized = normalized[1:-1]
            return normalized or None
        return None

    @staticmethod
    def _rename_clarification(path: str, question: str, title: str) -> AwaitingInputDiagnostic:
        return AwaitingInputDiagnostic(
            "CLARIFICATION_REQUIRED", path, question,
            {"patch_schema": {
                "type": "object",
                "properties": {"clarification": {
                    "type": "string", "title": title, "description": question,
                    "minLength": 1, "maxLength": 2000,
                }},
                "required": ["clarification"], "additionalProperties": False,
            }},
        )

    @classmethod
    def _guard_ambiguous_rename(cls, request: PlannerRequest, draft: PlannerDraft) -> None:
        """A model proposal cannot settle a physical-field or suffix ambiguity."""
        answer = request.revision_patch.get("clarification")
        renames = [step for step in draft.tasks if step.op == "attribute.rename_field"]
        source_ambiguity = _ambiguous_rename_source(request)
        if source_ambiguity is not None:
            candidate, target = source_ambiguity
            selected_source = cls._clarified_name(answer, (
                "原字段名是", "原列名是", "源字段是", "字段名是", "列名是", "原字段是", "就是",
            ))
            if selected_source not in _inspected_vector_fields(request):
                question = f"“字段名称”是指现有的“{candidate}”列，还是其他列？请只回复要更名的原字段名。"
                raise cls._rename_clarification("planner.rename_source", question, "原字段名")
            if len(renames) != 1 or renames[0].params.get("old_name") != selected_source or renames[0].params.get("new_name") != target:
                raise CompileError(
                    "RENAME_SOURCE_MISMATCH", "tasks",
                    "the rename draft does not match the user's explicit source-field clarification",
                    {"expected_old_name": selected_source, "expected_new_name": target},
                )
        target_ambiguity = _ambiguous_rename_target(request)
        if target_ambiguity is not None:
            source, full_name, short_name = target_ambiguity
            selected_target = cls._clarified_name(answer, (
                "目标字段名是", "目标列名是", "字段名是", "列名是", "目标是", "改成", "改为", "就是", "请用", "用",
            ))
            if selected_target not in {full_name, short_name}:
                question = f"请确认把“{source}”列改成“{short_name}”还是“{full_name}”？请只回复目标字段名。"
                raise cls._rename_clarification("planner.rename_target", question, "目标字段名")
            if len(renames) != 1 or renames[0].params.get("old_name") != source or renames[0].params.get("new_name") != selected_target:
                raise CompileError(
                    "RENAME_TARGET_MISMATCH", "tasks",
                    "the rename draft does not match the user's explicit target-field clarification",
                    {"expected_old_name": source, "expected_new_name": selected_target},
                )

    def _plan_once(self, worker_id: str, *, run_id: str | None = None) -> dict[str, Any] | None:
        claim = self.queue.claim_planning_run(worker_id, run_id=run_id)
        if claim is None:
            return None
        budget = PlanningBudget(getattr(self.planner, "total_timeout_seconds", 90.0),
                                getattr(self.planner, "max_http_calls", 6),
                                stop_event=self._worker_stop_events.get(worker_id, self._shutdown_event))
        budget_token = current_planning_budget.set(budget)
        attempt = None
        repair_used = False
        attempt_response: dict[str, Any] = {}
        try:
            request = self._planner_request(claim.run_id)
            identity = self._planner_identity(request)
            request_catalog = candidate_catalog(self.registry, request)
            request_hash = hashlib.sha256(json.dumps({
                "run_id": request.run_id, "input_revision": request.input_revision,
                "goal": request.user_goal, "inputs": list(request.input_artifacts),
                "prior_artifacts": list(request.prior_artifacts),
                "session_messages": list(request.session_messages),
                "patch": request.revision_patch,
            }, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
            attempt = self.runtime.create_planner_attempt(
                claim.run_id, planner_claim=claim, input_revision=request.input_revision, attempt_no=1,
                phase="initial", provider=identity[0], model=identity[1], prompt_version=identity[2],
                catalog_hash=identity[3], request_hash=request_hash,
            )
            repair = getattr(self.planner, "repair_draft", None)
            try:
                raw = self._call_planner_with_lease_heartbeat(
                    claim, lambda: self.planner.create_draft(request),
                )
                claim = self._planner_claim_refresh.pop(claim.run_id, claim)
            except PlannerError as initial_error:
                # A syntactically/schema-invalid provider reply has no usable
                # PlannerDraft to compile, but it is still a bounded repairable
                # front-end failure.  Persist the failed initial attempt before
                # asking the same provider once for corrected JSON; this keeps
                # the retry observable and prevents an unrecorded hidden call.
                if initial_error.code != "LLM_DRAFT_INVALID" or not callable(repair):
                    raise
                repair_used = True
                claim = self._planner_claim_refresh.pop(claim.run_id, claim)
                diagnostic = _planning_diagnostic(initial_error)
                self.runtime.finish_planner_attempt(
                    attempt["attempt_id"], planner_claim=claim, status="failed",
                    diagnostics=diagnostic,
                    error_code=initial_error.code, error=str(initial_error),
                )
                # Response evidence belongs only to the attempt that returned it.
                attempt_response = {}
                repaired_attempt = self.runtime.create_planner_attempt(
                    claim.run_id, planner_claim=claim, input_revision=request.input_revision, attempt_no=2,
                    phase="repair", provider=identity[0], model=identity[1], prompt_version=identity[2],
                    catalog_hash=identity[3], request_hash=request_hash,
                )
                attempt = repaired_attempt
                raw = self._call_planner_with_lease_heartbeat(
                    claim,
                    lambda: repair(request, rejected_draft=None, diagnostic=diagnostic),
                )
                claim = self._planner_claim_refresh.pop(claim.run_id, claim)
                attempt = repaired_attempt
            draft, metadata = self._coerce_planner_call(raw, request, fallback=identity)
            # A provider cannot cross the persistence boundary with a stale
            # run/revision identity; the worker owns those fields.
            # Runtime owns the planner catalog identity.  Injecting these
            # fields for deterministic test doubles also ensures every
            # planner action crosses the same bounded boundary as live LLM
            # calls; a provider cannot silently widen it in its response.
            draft = draft.model_copy(update={
                "run_id": claim.run_id, "input_revision": request.input_revision,
                "user_goal": request.user_goal,
                "allowed_operation_ids": list(request_catalog.operation_ids),
                "catalog_hash": request_catalog.catalog_hash,
            })
            attempt_response = {
                "response_hash": metadata["response_hash"], "draft": draft.model_dump(mode="json"),
                "usage": metadata["usage"], "latency_ms": metadata["latency_ms"],
            }
            self._require_executable_draft(draft)
            compiler = Compiler(
                self.registry, artifact_lookup=self.runtime.get_artifact,
                policy={"trusted_user_goal": request.user_goal,
                        "visible_artifacts": [*request.input_artifacts, *request.prior_artifacts],
                        "revision_patch": request.revision_patch},
            )
            try:
                self._require_visible_artifact_refs(draft, request)
                self._guard_ambiguous_rename(request, draft)
                self._guard_buffer_distance(request, draft)
                self._guard_semantic_facts(request, draft)
                plan = compiler.compile(draft)
            except AwaitingInputDiagnostic:
                # Missing user facts are not malformed model output. Never
                # ask repair to invent a CRS or other required business value.
                raise
            except (CompileError, ValueError) as compile_error:
                if not callable(repair) or repair_used:
                    raise
                repair_used = True
                diagnostic = _planning_diagnostic(compile_error)
                self.runtime.finish_planner_attempt(
                    attempt["attempt_id"], planner_claim=claim, status="failed",
                    response_hash=metadata["response_hash"], draft=draft.model_dump(mode="json"),
                    diagnostics=diagnostic,
                    usage=metadata["usage"], latency_ms=metadata["latency_ms"],
                    error_code=getattr(compile_error, "code", "PLAN_COMPILE_FAILED"), error=str(compile_error),
                )
                attempt_response = {}
                repair_catalog = request_catalog
                repair_options: dict[str, Any] = {}
                if isinstance(self.planner, OpenAICompatiblePlanner):
                    repair_catalog = controlled_repair_catalog(self.registry, request, diagnostic, base=request_catalog)
                    additions = tuple(name for name in repair_catalog.operation_ids if name not in request_catalog.operation_ids)
                    if additions:
                        repair_options["catalog_expansion"] = additions
                    elif diagnostic.get("code") == "GOAL_REQUIREMENT_MISSING":
                        required = diagnostic.get("details", {}).get("requirement", {}).get("op")
                        if required and required not in request_catalog.operation_ids:
                            # The original diagnostic is already durable. A
                            # second call cannot satisfy an absent mandatory
                            # operation without a permitted bounded expansion.
                            raise compile_error
                repaired_attempt = self.runtime.create_planner_attempt(
                    claim.run_id, planner_claim=claim, input_revision=request.input_revision, attempt_no=2,
                    phase="repair", provider=identity[0], model=identity[1], prompt_version=identity[2],
                    catalog_hash=repair_catalog.catalog_hash, request_hash=request_hash,
                )
                attempt = repaired_attempt
                repaired_raw = self._call_planner_with_lease_heartbeat(
                    claim,
                    lambda: repair(request, rejected_draft=draft, diagnostic=diagnostic, **repair_options),
                )
                claim = self._planner_claim_refresh.pop(claim.run_id, claim)
                draft, metadata = self._coerce_planner_call(repaired_raw, request, fallback=identity)
                if isinstance(self.planner, OpenAICompatiblePlanner) and isinstance(repaired_raw, PlannerCall) and metadata["catalog_hash"] != repair_catalog.catalog_hash:
                    attempt_response = {
                        "response_hash": metadata["response_hash"], "draft": draft.model_dump(mode="json"),
                        "usage": metadata["usage"], "latency_ms": metadata["latency_ms"],
                    }
                    raise PlannerError("CATALOG_HASH_MISMATCH", "repair response catalog does not match its authorized prompt catalog")
                request_catalog = repair_catalog
                draft = draft.model_copy(update={
                    "run_id": claim.run_id, "input_revision": request.input_revision, "user_goal": request.user_goal,
                    "allowed_operation_ids": list(request_catalog.operation_ids), "catalog_hash": request_catalog.catalog_hash,
                })
                attempt_response = {
                    "response_hash": metadata["response_hash"], "draft": draft.model_dump(mode="json"),
                    "usage": metadata["usage"], "latency_ms": metadata["latency_ms"],
                }
                self._require_executable_draft(draft)
                self._require_visible_artifact_refs(draft, request)
                self._guard_ambiguous_rename(request, draft)
                self._guard_buffer_distance(request, draft)
                self._guard_semantic_facts(request, draft)
                plan = compiler.compile(draft)
                attempt = repaired_attempt
            budget.check()
            self.runtime.finish_planner_attempt(
                attempt["attempt_id"], planner_claim=claim, status="succeeded",
                response_hash=metadata["response_hash"], draft=draft.model_dump(mode="json"),
                usage=metadata["usage"], latency_ms=metadata["latency_ms"],
            )
            self.runtime.activate_compiled_plan(claim.run_id, plan, planner_claim=claim)
            self.enqueue_run(claim.run_id)
            return {"status": "planned", "run_id": claim.run_id}
        except AwaitingInputDiagnostic as exc:
            claim = self._planner_claim_refresh.pop(claim.run_id, claim)
            if attempt is not None:
                try:
                    self.runtime.finish_planner_attempt(
                        attempt["attempt_id"], planner_claim=claim, status="failed",
                        diagnostics=exc.details, error_code=exc.code, error=exc.message,
                        **attempt_response,
                    )
                except (RuntimeError, ValueError):
                    pass
            try:
                self.runtime.reject_planning(
                    claim.run_id, planner_claim=claim, code=exc.code,
                    diagnostics={"path": exc.path, "message": exc.message, "patch_schema": exc.details.get("patch_schema", {"type": "object"})},
                    awaiting_input=True,
                )
            except RuntimeError as fence_error:
                if str(fence_error) != "STALE_PLANNER_LEASE":
                    raise
                return {"status": "planning_lease_lost", "run_id": claim.run_id}
            return {"status": "awaiting_input", "run_id": claim.run_id}
        except Exception as exc:
            claim = self._planner_claim_refresh.pop(claim.run_id, claim)
            code = getattr(exc, "code", None) or "PLAN_COMPILE_FAILED"
            diagnostic = _planning_diagnostic(exc)
            if attempt is not None:
                try:
                    self.runtime.finish_planner_attempt(
                        attempt["attempt_id"], planner_claim=claim, status="failed",
                        diagnostics=diagnostic, error_code=code, error=str(exc),
                        **attempt_response,
                    )
                except (RuntimeError, ValueError):
                    pass
            try:
                if code == "WORKER_SHUTDOWN":
                    self.queue.release_planning(claim, status="queued")
                    return {"status": "planning_interrupted", "run_id": claim.run_id}
                self.runtime.reject_planning(
                    claim.run_id, planner_claim=claim, code=code,
                    diagnostics=diagnostic,
                )
            except RuntimeError as fence_error:
                # A lease lost to another worker is intentionally not turned
                # into a failed run by this stale owner.
                if str(fence_error) != "STALE_PLANNER_LEASE":
                    raise
                return {"status": "planning_lease_lost", "run_id": claim.run_id}
            return {"status": "planning_failed", "run_id": claim.run_id}
        finally:
            self._planner_claim_refresh.pop(claim.run_id, None)
            current_planning_budget.reset(budget_token)

    def run_once(self, worker_id: str, *, run_id: str | None = None) -> dict[str, Any] | None:
        claim = None
        if self._prefer_task.get(worker_id, False):
            claim = self.queue.claim_next(worker_id, run_id=run_id)
        if claim is None:
            planned = self._plan_once(worker_id, run_id=run_id)
            if planned is not None:
                self._prefer_task[worker_id] = True
                return planned
            claim = self.queue.claim_next(worker_id, run_id=run_id)
        if claim is None:
            return None
        self._prefer_task[worker_id] = False
        start_won = False
        try:
            from .executor import RuntimeExecutor

            row = self.runtime._read(lambda c: c.execute("SELECT ir_json FROM plans WHERE plan_id=?", (claim.plan_id,)).fetchone())
            if row is None or not row[0]:
                raise ValueError("queued plan has no persisted PlanIR")
            payload = json.loads(row[0])
            candidate = next((item for item in payload.get("tasks", []) if item.get("task_key") == claim.task_key), None)
            if candidate is None:
                raise ValueError("queued task is absent from persisted PlanIR")
            compiled = CompiledTask.model_validate(candidate)
            executor = RuntimeExecutor(self.runtime, self.artifacts, self.registry)
            # Begin the claimed attempt before crossing the process boundary;
            # recovery can now distinguish a lease that never started from a
            # worker that had entered execution.
            executor._ensure_run_running(claim.run_id, claim=claim)
            try:
                executor._ensure_task_running(claim.task_id, claim=claim)
            except RuntimeError as exc:
                if str(exc) in {"TASK_ALREADY_STARTED", "STALE_LEASE"}:
                    # An owner may retry a claim while another invocation is
                    # already executing it, including after the winner has
                    # advanced from running to validating. The loser has no
                    # attempt of its own to fail, release, or clean up.
                    return None
                raise
            start_won = True
            # A shared in-memory SQLite URI cannot be reopened by a spawned
            # process; retain the explicit in-process compatibility path for
            # ephemeral unit fixtures. Durable file-backed runs always use a
            # child worker.
            can_isolate = self.isolate and not self._registry_supplied and not getattr(self.runtime.db, "_memory_uri", None)
            if can_isolate:
                result, claim = self._run_isolated(claim, compiled)
            else:
                result = executor.execute(
                    compiled, run_id=claim.run_id, task_id=claim.task_id,
                    claim=claim, started_by_supervisor=True,
                )
            executor.assert_claim_current(claim, allow_terminal=True)
            # The queue owns the fenced attempt.  RuntimeExecutor deliberately
            # cannot create a second active attempt under the partial unique
            # index, so close the claim's attempt only after all outputs have
            # been committed successfully.
            attempt = self.runtime._read(
                lambda c: c.execute(
                    "SELECT attempt_id FROM task_attempts WHERE task_id=? AND status IN ('leased','running','validating','retry_wait') ORDER BY started_at DESC LIMIT 1",
                    (claim.task_id,),
                ).fetchone()
            )
            if attempt is not None:
                self.runtime.update_task_attempt_status(attempt[0], "succeeded", fence=claim)
            self.queue.release(claim)
            self._finalize_run_if_terminal(claim.run_id)
            return result
        except Exception as exc:
            if not start_won:
                # No executor start was won by this invocation. In particular,
                # a same-owner claim can refer to another invocation's child;
                # its process and attempt must never be cleaned up here.
                raise
            self._cleanup_active_process(claim.task_id)
            # A child can commit its full SQLite/CAS transaction and then die
            # before its small IPC completion manifest reaches this process.
            # The fenced, terminal DB rows are authoritative; losing IPC must
            # neither replay the handler nor convert a committed success into
            # WORKER_LOST or retry_wait.
            committed = self.runtime._read(lambda c: c.execute(
                """SELECT t.status,a.status FROM tasks t
                   JOIN task_attempts a ON a.task_id=t.task_id AND a.attempt_id=?
                   WHERE t.task_id=? AND t.lease_generation=?""",
                (claim.attempt_id, claim.task_id, claim.lease_generation),
            ).fetchone())
            if committed is not None and tuple(committed) == ("succeeded", "succeeded"):
                try:
                    self.queue.release(claim)
                except RuntimeError:
                    pass
                self._finalize_run_if_terminal(claim.run_id)
                return {"status": "succeeded", "task_id": claim.task_id, "recovered_from": "durable_task_commit"}
            claim_current = True
            try:
                from .executor import RuntimeExecutor
                # A business error is durably reflected on the Task before
                # control returns here.  Keep the original live lease fence
                # valid long enough for Queue to close its one claimed
                # TaskAttempt, while still rejecting reclaimed/expired claims.
                RuntimeExecutor(self.runtime, self.artifacts, self.registry).assert_claim_current(claim, allow_terminal=True)
            except RuntimeError:
                claim_current = False
            task_state = self.runtime._read(lambda c: c.execute("SELECT status,next_retry_at FROM tasks WHERE task_id=?", (claim.task_id,)).fetchone())
            if task_state is not None and task_state[0] == "retry_wait":
                try:
                    self.queue.release(claim)
                except RuntimeError:
                    pass
                return {"status": "retry_wait", "task_id": claim.task_id, "next_retry_at": task_state[1]}
            error_code = execution_error_code(exc)
            if claim_current and "compiled" in locals() and error_code not in {"WORKER_SHUTDOWN", "CANCELLED"}:
                retry_policy = compiled.retry_policy
                attempt_count = self.runtime._read(
                    lambda c: c.execute("SELECT COUNT(*) FROM task_attempts WHERE task_id=?", (claim.task_id,)).fetchone()[0]
                )
                if compiled.idempotent and error_code in set(retry_policy.retryable_error_codes) and attempt_count < int(retry_policy.max_attempts):
                    try:
                        retry_at = self.queue.schedule_retry(claim, error_code=error_code, retry_policy=retry_policy)
                        self.queue.release(claim)
                        return {"status": "retry_wait", "task_id": claim.task_id, "next_retry_at": retry_at}
                    except (RuntimeError, ValueError) as schedule_error:
                        # A concurrent attempt may consume the final slot
                        # between the count check and the fenced write.  In
                        # that case MAX_ATTEMPTS is a terminal task failure,
                        # not an exception that strands the worker loop.
                        if isinstance(schedule_error, ValueError):
                            if str(schedule_error) != "MAX_ATTEMPTS":
                                raise
                        else:
                            claim_current = False
            attempt = self.runtime._read(
                lambda c: c.execute(
                    "SELECT attempt_id,status FROM task_attempts WHERE task_id=? AND lease_generation=? AND status IN ('leased','running','validating','retry_wait') ORDER BY started_at DESC LIMIT 1",
                    (claim.task_id, claim.lease_generation),
                ).fetchone()
            )
            if claim_current and attempt is not None:
                try:
                    self.runtime.update_task_attempt_status(attempt[0], "failed", fence=claim, error=str(exc), error_code=error_code)
                except ValueError:
                    pass
                try:
                    if task_state is not None and task_state[0] in {"leased", "running", "validating"}:
                        self.runtime.update_task_status(claim.task_id, "failed", blocked_reason=error_code, fence=claim)
                        self.runtime.append_event(claim.run_id, "task.failed", {"task_id": claim.task_id, "message": str(exc), "error_code": error_code})
                except (RuntimeError, ValueError):
                    pass
            try:
                self.queue.release(claim)
            except RuntimeError:
                pass
            current_run = self.runtime.get_run(claim.run_id)
            if current_run is not None and current_run["status"] in {"cancelling", "cancelled"}:
                return {"status": "cancelled", "error_code": "CANCELLED"}
            if claim_current:
                self._finalize_run_if_terminal(claim.run_id)
            # The loop needs the exact durable Task row to decide whether an
            # exception was already converted into a business outcome.  Keep
            # this narrow context on the exception instead of guessing from
            # the newest terminal task in a multi-run Worker.
            try:
                exc.error_code = error_code
                exc.runtime_task_id = claim.task_id
                exc.runtime_run_id = claim.run_id
            except Exception:
                pass
            raise

    def _run_isolated(self, claim: TaskClaim, compiled: CompiledTask) -> tuple[dict[str, Any], TaskClaim]:
        """Execute a builtin task in a killable child process."""
        ctx = multiprocessing.get_context("spawn")
        result_queue = ctx.Queue()
        process = ctx.Process(
            target=_isolated_executor_worker,
            args=(
                str(self.runtime.db.path),
                str(self.artifacts.data_root),
                str(self.artifacts.artifacts_dir),
                str(self.artifacts.staging_dir),
                str(self.artifacts.quarantine_dir),
                str(self.artifacts.uploads_dir),
                compiled.model_dump(mode="json"),
                claim.run_id,
                claim.task_id,
                {
                    "task_id": claim.task_id,
                    "owner": claim.owner,
                    "token": claim.token,
                    "lease_generation": claim.lease_generation,
                    "attempt_id": claim.attempt_id,
                },
                result_queue,
            ),
        )
        process.start()
        self._active_processes[claim.task_id] = process
        self._active_jobs[claim.task_id] = enforce_resource_limits(
            process,
            memory_mb=int(compiled.resource_budget.memory_mb),
            cpu_weight=int(compiled.resource_budget.cpu_weight),
        )
        create_time = _process_creation_time(process.pid)
        if create_time is None or not math.isfinite(create_time) or create_time <= 0:
            _terminate_process_tree(process)
            process.join(5)
            release_resource_limits(process)
            error = RuntimeError("executor creation identity is unavailable")
            error.error_code = "EXECUTOR_IDENTITY_UNAVAILABLE"
            raise error
        identity = {"pid": process.pid, "create_time": create_time, "job": bool(self._active_jobs[claim.task_id])}
        try:
            self.runtime.set_executor_identity(claim, json.dumps(identity, sort_keys=True))
        except Exception:
            _terminate_process_tree(process)
            process.join(5)
            raise
        timeout = self.timeout_seconds or float(compiled.resource_budget.timeout_seconds)
        deadline = time.monotonic() + max(0.1, timeout)
        child_started = time.monotonic()
        cpu_over_limit_samples = 0
        next_heartbeat = time.monotonic() + self.heartbeat_interval
        live_claim = claim
        while process.is_alive():
            if self._shutdown_event.is_set():
                _terminate_process_tree(process)
                process.join(5)
                error = RuntimeError("WORKER_SHUTDOWN")
                error.error_code = "WORKER_SHUTDOWN"
                raise error
            current_run = self.runtime.get_run(claim.run_id)
            if current_run is not None and current_run["status"] in {"cancelling", "cancelled"}:
                _terminate_process_tree(process)
                process.join(5)
                error = RuntimeError("RUN_CANCELLED")
                error.error_code = "CANCELLED"
                raise error
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process_tree(process)
                process.join(5)
                raise TimeoutError("isolated worker exceeded task timeout")
            process.join(min(0.2, remaining))
            budget = check_resource_budget(
                process,
                memory_mb=int(compiled.resource_budget.memory_mb),
                cpu_weight=int(compiled.resource_budget.cpu_weight),
                sample_interval=min(max(self.heartbeat_interval / 2.0, 0.05), 0.2),
            )
            # Process CPU accounting is noisy during interpreter start-up
            # (especially on Windows). Let the first sample settle while
            # retaining memory enforcement from the first heartbeat.
            startup_grace = max(0.25, min(self.heartbeat_interval, 2.0))
            if budget.get("reason") == "cpu":
                if time.monotonic() - child_started < startup_grace:
                    cpu_over_limit_samples = 0
                    budget["exceeded"] = False
                else:
                    cpu_over_limit_samples += 1
                    budget["exceeded"] = cpu_over_limit_samples >= 2
            else:
                cpu_over_limit_samples = 0
            if process.is_alive() and budget.get("exceeded"):
                _terminate_process_tree(process)
                process.join(5)
                self.runtime.append_event(claim.run_id, "task.resource_limit_exceeded", {"task_id": claim.task_id, **budget})
                error = RuntimeError(f"isolated worker exceeded {budget.get('reason')} resource budget")
                error.error_code = "RESOURCE_LIMIT"
                raise error
            if process.is_alive() and time.monotonic() >= next_heartbeat:
                try:
                    live_claim = self.queue.heartbeat(live_claim)
                except RuntimeError as exc:
                    terminal = self.runtime._read(
                        lambda c: c.execute("SELECT status,lease_owner,lease_token,lease_generation FROM tasks WHERE task_id=?", (claim.task_id,)).fetchone()
                    )
                    if terminal is not None and terminal[0] in {"succeeded", "failed", "cancelled"}:
                        # The child may have committed its terminal state a
                        # few milliseconds before Process.is_alive() observes
                        # exit.  Do not treat that normal teardown race as a
                        # stale worker.
                        next_heartbeat = time.monotonic() + self.heartbeat_interval
                        continue
                    _terminate_process_tree(process)
                    process.join(5)
                    raise RuntimeError("STALE_LEASE") from exc
                next_heartbeat = time.monotonic() + self.heartbeat_interval
        self._active_processes.pop(claim.task_id, None)
        self._active_jobs.pop(claim.task_id, None)
        release_resource_limits(process)
        if process.is_alive():
            _terminate_process_tree(process)
            process.join(5)
            raise TimeoutError("isolated worker exceeded task timeout")
        try:
            message = result_queue.get(timeout=2)
        except Empty as exc:
            error = RuntimeError(f"isolated worker exited without a result (exitcode={process.exitcode})")
            error.error_code = "WORKER_LOST"
            raise error from exc
        except Exception as exc:
            error = RuntimeError("isolated worker result transport failed")
            error.error_code = "WORKER_PROTOCOL_ERROR"
            raise error from exc
        finally:
            result_queue.close()
        try:
            if not isinstance(message, tuple) or not message:
                raise ValueError("invalid IPC envelope")
            if message[0] == "ok_json" and len(message) == 2:
                result = json.loads(message[1])
                if not isinstance(result, dict):
                    raise ValueError("invalid completion manifest")
                return result, live_claim
            if (message[0] != "error" or len(message) != 4
                    or not isinstance(message[2], str) or not isinstance(message[3], str)
                    or not message[3].strip()):
                raise ValueError("invalid error manifest")
        except (ValueError, TypeError) as exc:
            error = RuntimeError("isolated worker returned an invalid result message")
            error.error_code = "WORKER_PROTOCOL_ERROR"
            raise error from exc
        error = RuntimeError(message[2])
        error.error_code = message[3]
        raise error

    def worker_loop(self, worker_id: str, *, run_id: str | None = None, max_tasks: int | None = None, poll_interval: float = 0.25, stop_event=None, shutdown_event=None) -> int:
        if isinstance(poll_interval, bool) or poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        stop_event = shutdown_event or stop_event or self._shutdown_event
        self.recover_cancelling_runs()
        self.queue.recover_expired_leases()
        self._recover_terminal_runs()
        next_terminal_recovery = time.monotonic() + 1.0
        completed = 0
        self._worker_stop_events[worker_id] = stop_event
        try:
            while not stop_event.is_set() and (max_tasks is None or completed < max_tasks):
                try:
                    result = self.run_once(worker_id, run_id=run_id)
                except Exception as exc:
                    # ``run_once`` persists a terminal TaskAttempt/Task
                    # outcome before raising the operational exception.  The
                    # supervisor must keep polling after such a business
                    # failure; otherwise one malformed data set kills the
                    # only local Worker and strands unrelated Runs.
                    task_id = getattr(exc, "runtime_task_id", None)
                    task_state = self.runtime._read(
                        lambda c: c.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                    ) if isinstance(task_id, str) and task_id else None
                    if task_state is not None and task_state[0] in {"succeeded", "failed", "cancelled"}:
                        failed_run_id = getattr(exc, "runtime_run_id", None)
                        if isinstance(failed_run_id, str) and failed_run_id:
                            self._finalize_run_if_terminal(failed_run_id)
                        completed += 1
                        continue
                    if isinstance(exc, RuntimeError) and str(exc) in {"WORKER_SHUTDOWN", "RUN_CANCELLED", "STALE_LEASE", "STALE_PLANNER_LEASE", "CANCELLED"}:
                        continue
                    raise
                if result is not None:
                    completed += 1
                    continue
                if max_tasks is not None:
                    break
                if time.monotonic() >= next_terminal_recovery:
                    self._recover_terminal_runs()
                    next_terminal_recovery = time.monotonic() + 1.0
                self._wake_event.wait(poll_interval)
                self._wake_event.clear()
            return completed
        finally:
            self._worker_stop_events.pop(worker_id, None)
            for process in list(self._active_processes.values()):
                if process.is_alive():
                    _terminate_process_tree(process)
                join = getattr(process, "join", None)
                if callable(join):
                    join(5)
            for task_id, process in list(self._active_processes.items()):
                if not process.is_alive():
                    release_resource_limits(process)
                    self._active_processes.pop(task_id, None)
                    self._active_jobs.pop(task_id, None)
            # A normal stop may happen before a lease expires. Reconcile the
            # worker-owned rows now so another worker can claim them safely.
            affected_runs = self.runtime._read(
                lambda c: [row[0] for row in c.execute(
                    "SELECT DISTINCT p.run_id FROM tasks t JOIN plans p ON p.plan_id=t.plan_id WHERE t.lease_owner=? AND t.status IN ('leased','running','validating')",
                    (worker_id,),
                ).fetchall()]
            )
            self.queue.abandon_worker_claims(worker_id, reason="WORKER_LOST")
            self.queue.recover_expired_leases()
            for affected_run in affected_runs:
                self._finalize_run_if_terminal(affected_run)

    def start(self, worker_id: str, *, run_id: str | None = None, poll_interval: float = 0.25) -> threading.Thread:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            raise RuntimeError("worker is already running")
        # Converge persisted cancellation reservations before accepting new
        # work; worker_loop repeats this defensively for direct callers.
        self.recover_cancelling_runs()
        self._shutdown_event.clear()
        self._worker_id = worker_id
        self._worker_thread = threading.Thread(
            target=self.worker_loop,
            kwargs={"worker_id": worker_id, "run_id": run_id, "poll_interval": poll_interval},
            name=f"gismind-runtime-worker-{worker_id}",
            daemon=True,
        )
        self._worker_thread.start()
        return self._worker_thread

    def stop(self, *, timeout: float | None = None) -> None:
        self._shutdown_event.set()
        self._wake_event.set()
        if self._worker_thread is not None and self._worker_thread is not threading.current_thread():
            self._worker_thread.join(timeout)

    def join(self, timeout: float | None = None) -> None:
        if self._worker_thread is not None:
            self._worker_thread.join(timeout)

    def health_snapshot(self) -> dict[str, Any]:
        active = self.runtime._read(
            lambda c: c.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('leased','running','validating')").fetchone()[0]
        )
        return {
            "worker_id": self._worker_id,
            "running": bool(self._worker_thread and self._worker_thread.is_alive()),
            "active_tasks": int(active),
            "active_processes": sum(1 for process in self._active_processes.values() if process.is_alive()),
            "shutdown_requested": self._shutdown_event.is_set(),
            "resource_limits": self.resource_health(),
        }

    def resource_health(self) -> dict[str, Any]:
        """Report whether hard child limits are available on this worker."""
        import os

        if os.name != "nt":
            return {"status": "degraded", "hard_limits": False, "reason": "platform_has_no_job_object"}
        try:
            import win32job  # noqa: F401
        except ImportError:
            return {"status": "degraded", "hard_limits": False, "reason": "pywin32_unavailable"}
        return {"status": "ok", "hard_limits": True, "job_object": True}


__all__ = ["RunClaim", "RuntimeQueue", "RuntimeSupervisor", "TaskClaim"]
