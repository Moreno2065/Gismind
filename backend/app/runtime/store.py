"""Small repository API over the runtime relational model."""

from __future__ import annotations

import json
import hashlib
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .db import RUN_STATUSES, TASK_ATTEMPT_STATUSES, TASK_STATUSES, RuntimeDB, utc_now

RUN_TRANSITIONS = {
    "queued": {"planning", "failed", "cancelling"},
    "planning": {"awaiting_input", "ready", "failed", "cancelling"},
    "awaiting_input": {"queued", "ready", "running", "cancelling"},
    "ready": {"running", "failed", "cancelling"},
    "running": {"completed", "failed", "cancelling", "awaiting_input"},
    "cancelling": {"cancelled"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
}
TASK_TRANSITIONS = {
    "blocked": {"ready", "cancelled"},
    "ready": {"leased", "succeeded", "cancelled"},
    "leased": {"running", "retry_wait", "failed", "cancelled"},
    "running": {"validating", "retry_wait", "failed", "cancelled"},
    "validating": {"succeeded", "retry_wait", "failed", "cancelled"},
    "retry_wait": {"ready", "cancelled"},
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
}
TASK_ATTEMPT_TRANSITIONS = {
    "queued": {"leased", "failed", "cancelled"},
    "leased": {"running", "retry_wait", "failed", "cancelled"},
    # A queue-owned attempt may close directly after its handler has returned;
    # ``validating`` remains available for the explicit postcondition path.
    "running": {"validating", "succeeded", "retry_wait", "failed", "cancelled"},
    "validating": {"succeeded", "retry_wait", "failed", "cancelled"},
    "retry_wait": {"failed"},
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
}
_MISSING_EXECUTOR_IDENTITY = object()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


def _row(row):
    return dict(row) if row is not None else None


class RuntimeStore:
    def __init__(self, db: RuntimeDB):
        self.db = db
        self._artifact_root = None

    def set_artifact_root(self, root) -> None:
        self._artifact_root = __import__("pathlib").Path(root).resolve()

    @staticmethod
    def _valid_hash(value: Any) -> bool:
        return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))

    def _validate_artifact_record(self, artifact: dict, *, require_file: bool) -> None:
        if not isinstance(artifact.get("type"), str) or not artifact["type"]:
            raise ValueError("artifact type metadata is required")
        for key in ("crs", "schema"):
            if key not in artifact or (artifact[key] is not None and not isinstance(artifact[key], dict)):
                raise ValueError(f"artifact {key} metadata is required and must be an object")
        if not self._valid_hash(artifact.get("content_hash")):
            raise ValueError("artifact content_hash must be a SHA-256 hex digest")
        try:
            size = int(artifact["size_bytes"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("artifact size_bytes must be a non-negative integer") from exc
        if size < 0:
            raise ValueError("artifact size_bytes must be non-negative")
        storage_uri = artifact.get("storage_uri")
        expected_uri = f"artifacts/sha256/{artifact['content_hash'][:2]}/{artifact['content_hash']}"
        if not storage_uri or storage_uri != expected_uri:
            raise ValueError("valid artifact must use the canonical CAS storage URI")
        if not require_file:
            return
        if self._artifact_root is None:
            raise ValueError("valid artifact requires a configured artifact root")
        path = (self._artifact_root / "sha256" / artifact["content_hash"][:2] / artifact["content_hash"]).resolve()
        try:
            path.relative_to(self._artifact_root)
        except ValueError as exc:
            raise ValueError("artifact storage path escapes artifact root") from exc
        if not path.is_file() or path.stat().st_size != size:
            raise ValueError("artifact CAS file is missing or has an unexpected size")
        if hashlib.sha256(path.read_bytes()).hexdigest() != artifact["content_hash"]:
            raise ValueError("artifact CAS content hash mismatch")

    def _require_valid_artifact(self, c, artifact_id: str) -> dict:
        row = c.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        if row is None or row["validation_status"] != "valid":
            raise ValueError("only valid artifacts may be referenced")
        artifact = self._decode_artifact_metadata(dict(row))
        self._validate_artifact_record(artifact, require_file=True)
        return artifact

    def _require_valid_artifact_metadata(self, c, artifact_id: str) -> dict:
        """Validate canonical DB metadata without reading the CAS payload.

        GC candidate generation/confirmation verifies bytes outside its short
        SQLite write transaction, then uses this method while recording the
        protective lease.  The second DB snapshot still prevents stale rows
        from being leased, while large CAS reads never hold the write lock.
        """
        row = c.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        if row is None or row["validation_status"] != "valid":
            raise ValueError("only valid artifacts may be referenced")
        artifact = self._decode_artifact_metadata(dict(row))
        self._validate_artifact_record(artifact, require_file=False)
        return artifact

    @staticmethod
    def _assert_task_fence_conn(c, fence, *, target_task_id: str | None = None,
                                target_attempt_id: str | None = None) -> None:
        """Check owner/token/generation/attempt while the write txn is open."""
        if fence is None:
            return
        get = fence.get if isinstance(fence, dict) else lambda name, default=None: getattr(fence, name, default)
        task_id = get("task_id")
        attempt_id = get("attempt_id", "")
        if target_task_id is None:
            raise RuntimeError("STALE_LEASE")
        if target_task_id is not None and task_id != target_task_id:
            raise RuntimeError("STALE_LEASE")
        if target_attempt_id is not None and attempt_id != target_attempt_id:
            raise RuntimeError("STALE_LEASE")
        row = c.execute(
            """
            SELECT t.lease_owner,t.lease_token,t.lease_generation,t.lease_expires_at,
                   t.status,a.attempt_id,a.lease_generation,a.status,a.executor_identity
            FROM tasks t LEFT JOIN task_attempts a
              ON a.attempt_id=? AND a.task_id=t.task_id
            WHERE t.task_id=?
            """,
            (attempt_id, task_id),
        ).fetchone()
        try:
            lease_expired = row is None or datetime.fromisoformat(str(row[3]).replace("Z", "+00:00")) <= datetime.now(timezone.utc)
        except (TypeError, ValueError):
            lease_expired = True
        if row is None or row[0] != get("owner") or row[1] != get("token") or int(row[2]) != int(get("lease_generation", -1)) or lease_expired or row[4] not in {"leased", "running", "validating", "succeeded", "failed", "cancelled"}:
            raise RuntimeError("STALE_LEASE")
        if row[5] != get("attempt_id", "") or int(row[6]) != int(get("lease_generation", -1)) or row[7] not in {"queued", "leased", "running", "validating", "retry_wait", "succeeded", "failed", "cancelled"} or not row[8] or not str(row[8]).strip():
            raise RuntimeError("STALE_LEASE")

    @staticmethod
    def _reject_unfenced_active_task_conn(c, task_id: str, fence) -> None:
        """Do not let an unfenced caller mutate a leased task or attempt."""
        if fence is not None:
            return
        row = c.execute(
            "SELECT lease_owner,lease_token,lease_expires_at,status FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None or not row[0] or not row[1] or row[3] not in {"leased", "running", "validating"}:
            return
        # An expired lease remains an active, unrecovered claim until the
        # queue's recovery transaction classifies and clears it.  Allowing an
        # unfenced artifact write in that interval can publish output from a
        # stale worker before recovery gets a chance to fence it.
        raise RuntimeError("STALE_LEASE")

    @staticmethod
    def _reject_unfenced_active_run_conn(c, run_id: str, fence) -> None:
        """Require a matching maintenance/worker fence for live Run leases."""
        if fence is not None:
            return
        planner = c.execute(
            "SELECT planner_lease_owner,planner_lease_token,planner_lease_expires_at,status FROM runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if planner is not None and planner[0] and planner[1] and planner[3] == "planning":
            try:
                expiry = datetime.fromisoformat(str(planner[2]).replace("Z", "+00:00"))
                if expiry.tzinfo is not None and expiry > datetime.now(timezone.utc):
                    raise RuntimeError("STALE_PLANNER_LEASE")
            except (TypeError, ValueError):
                pass
        active = c.execute(
            """SELECT t.lease_owner,t.lease_token,t.lease_expires_at
               FROM tasks t JOIN plans p ON p.plan_id=t.plan_id
               WHERE p.run_id=? AND t.status IN ('leased','running','validating')
                 AND t.lease_owner IS NOT NULL AND t.lease_token IS NOT NULL
               LIMIT 1""",
            (run_id,),
        ).fetchone()
        if active is None:
            return
        try:
            expiry = datetime.fromisoformat(str(active[2]).replace("Z", "+00:00"))
            live = expiry.tzinfo is not None and expiry > datetime.now(timezone.utc)
        except (TypeError, ValueError):
            live = False
        if live:
            raise RuntimeError("STALE_LEASE")

    @staticmethod
    def _assert_planner_fence_conn(c, run_id: str, fence, *, mode: str = "normal") -> None:
        """Check planner owner/token/generation while the write txn is open."""
        if fence is None:
            active = c.execute(
                "SELECT planner_lease_owner,planner_lease_token,planner_lease_expires_at,status FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if mode != "bootstrap" and active is not None and active[0] and active[1] and active[2] and active[3] == "planning":
                raise RuntimeError("STALE_PLANNER_LEASE")
            return
        get = fence.get if isinstance(fence, dict) else lambda name, default=None: getattr(fence, name, default)
        claim_run = get("run_id")
        row = c.execute(
            "SELECT planner_lease_owner,planner_lease_token,planner_lease_expires_at,planner_generation,status FROM runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        try:
            expiry = datetime.fromisoformat(str(row[2]).replace("Z", "+00:00")) if row is not None else None
            live = expiry is not None and expiry.tzinfo is not None and expiry.utcoffset() == timezone.utc.utcoffset(datetime.now(timezone.utc)) and expiry > datetime.now(timezone.utc)
        except (TypeError, ValueError):
            live = False
        if (
            row is None
            or claim_run != run_id
            or row[0] != get("owner")
            or row[1] != get("token")
            or row[2] is None
            or not live
            or int(row[3]) != int(get("generation", -1))
            or row[4] != "planning"
        ):
            raise RuntimeError("STALE_PLANNER_LEASE")
        # Planner claims fence the Run lease.  Task-attempt columns do not
        # belong to this query (planner attempts are a separate table), so a
        # task attempt identity on a forged/mixed claim must fail closed.
        if get("attempt_id", ""):
            raise RuntimeError("STALE_PLANNER_LEASE")

    @staticmethod
    def _decode_artifact_metadata(artifact: dict) -> dict:
        for key in ("crs", "schema"):
            value = artifact.get(key)
            if isinstance(value, str):
                try:
                    artifact[key] = json.loads(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"artifact {key} metadata is invalid") from exc
        return artifact

    @staticmethod
    def _require_live_session(c, session_id: str) -> None:
        row = c.execute("SELECT deleted_at FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown session: {session_id}")
        if row[0] is not None:
            raise ValueError("SESSION_DELETED")

    def initialize(self) -> None:
        self.db.initialize()

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self.db.transaction() as conn:
            yield conn

    def _write(self, fn):
        active = self.db.active_connection
        if active is not None:
            return fn(active)
        with self.db.transaction() as conn:
            return fn(conn)

    def _read(self, fn):
        active = self.db.active_connection
        if active is not None:
            return fn(active)
        with self.db.connection() as conn:
            return fn(conn)

    def create_session(self, session_id: str, *, session_version: int = 1) -> dict:
        now = utc_now()
        self._write(lambda c: c.execute(
            "INSERT INTO sessions(session_id,session_version,created_at,updated_at) VALUES (?,?,?,?)",
            (session_id, session_version, now, now),
        ))
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict | None:
        return self._read(lambda c: _row(c.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()))

    def delete_session(self, session_id: str) -> None:
        now = utc_now()
        def op(c):
            c.execute("UPDATE sessions SET deleted_at=?,session_version=session_version+1,updated_at=? WHERE session_id=?", (now, now, session_id))
            c.execute("DELETE FROM message_artifacts WHERE message_id IN (SELECT message_id FROM messages WHERE session_id=?)", (session_id,))
        self._write(op)

    def update_session(self, session_id: str, *, session_version: int | None = None) -> dict:
        now = utc_now()
        def op(c):
            if session_version is None:
                c.execute("UPDATE sessions SET session_version=session_version+1,updated_at=? WHERE session_id=?", (now, session_id))
            else:
                c.execute("UPDATE sessions SET session_version=?,updated_at=? WHERE session_id=?", (session_version, now, session_id))
        self._write(op)
        return self.get_session(session_id)

    def append_message(self, session_id: str, role: str, content: str, *, message_id: str | None = None, metadata: Any = None) -> dict:
        message_id = message_id or str(uuid.uuid4())
        now = utc_now()
        def op(c):
            self._require_live_session(c, session_id)
            row = c.execute("SELECT COALESCE(MAX(message_index), 0) + 1 FROM messages WHERE session_id=?", (session_id,)).fetchone()
            index = row[0]
            c.execute("INSERT INTO messages(message_id,session_id,role,content,message_index,metadata_json,created_at) VALUES (?,?,?,?,?,?,?)", (message_id, session_id, role, content, index, _json(metadata), now))
            c.execute("UPDATE sessions SET updated_at=? WHERE session_id=?", (now, session_id))
        self._write(op)
        return self._read(lambda c: _row(c.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()))

    def link_message_artifact(self, message_id: str, artifact_id: str) -> None:
        def op(c):
            message = c.execute("SELECT session_id FROM messages WHERE message_id=?", (message_id,)).fetchone()
            if message is None:
                raise ValueError(f"unknown message: {message_id}")
            self._require_live_session(c, message[0])
            self._require_valid_artifact(c, artifact_id)
            c.execute("INSERT INTO message_artifacts(message_id,artifact_id) VALUES (?,?)", (message_id, artifact_id))
        self._write(op)

    def create_run(self, run_id: str, session_id: str, *, run_version: int = 1, status: str = "queued", input_revision: int = 1) -> dict:
        if status != "queued":
            raise ValueError("runs must be created in queued status")
        now = utc_now()
        def op(c):
            self._require_live_session(c, session_id)
            c.execute("INSERT INTO runs(run_id,session_id,run_version,status,input_revision,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", (run_id, session_id, run_version, status, input_revision, now, now))
        self._write(op)
        return self.get_run(run_id)

    def create_run_with_input(self, run_id: str, session_id: str, *, message: str,
                              input_artifact_ids: list[str], command_id: str,
                              idempotency_key: str, command_payload: Any) -> dict:
        """Commit the public create command, message, Run and revision together.

        The API has already authenticated/validated the artifact IDs.  This
        method repeats the DB validity checks in the same transaction so a
        concurrent quarantine/delete cannot leave a command pointing at a
        partial Run.
        """
        if not isinstance(message, str) or not message.strip():
            raise ValueError("run message is required")
        if len(set(input_artifact_ids)) != len(input_artifact_ids):
            raise ValueError("input artifact IDs must be unique")
        now = utc_now()
        message_id = f"message_{uuid.uuid4().hex}"
        revision_id = f"revision_{uuid.uuid4().hex}"

        def op(c):
            self._require_live_session(c, session_id)
            for artifact_id in input_artifact_ids:
                self._require_valid_artifact(c, artifact_id)
            existing = c.execute(
                """SELECT r.run_id,rc.payload_json FROM run_commands rc
                   JOIN runs r ON r.run_id=rc.run_id
                   WHERE r.session_id=? AND rc.command_type='create' AND rc.idempotency_key=?""",
                (session_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                try:
                    prior = json.loads(existing[1])
                except (TypeError, ValueError):
                    prior = {}
                if prior.get("request_hash") != command_payload.get("request_hash"):
                    raise RuntimeError("CLIENT_REQUEST_CONFLICT")
                return {"idempotent": True, "run_id": existing[0], "response": prior.get("response")}
            index = int(c.execute(
                "SELECT COALESCE(MAX(message_index), 0) + 1 FROM messages WHERE session_id=?", (session_id,)
            ).fetchone()[0])
            c.execute(
                "INSERT INTO messages(message_id,session_id,role,content,message_index,metadata_json,created_at) VALUES (?,?,?,?,?,?,?)",
                (message_id, session_id, "user", message, index, _json({"run_id": run_id}), now),
            )
            c.execute(
                "INSERT INTO runs(run_id,session_id,run_version,status,input_revision,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (run_id, session_id, 1, "queued", 1, now, now),
            )
            c.execute(
                "INSERT INTO run_input_revisions(revision_id,run_id,revision,input_json,created_at) VALUES (?,?,?,?,?)",
                (revision_id, run_id, 1, _json({"prompt": message, "input_artifact_ids": input_artifact_ids}), now),
            )
            c.execute(
                "INSERT INTO run_commands(command_id,run_id,command_type,payload_json,idempotency_key,created_at) VALUES (?,?,?,?,?,?)",
                (command_id, run_id, "create", _json(command_payload), idempotency_key, now),
            )
            self._append_event_conn(c, run_id, "run.created", {
                "session_id": session_id, "message_id": message_id, "input_revision": 1,
            })
            c.execute("UPDATE sessions SET updated_at=? WHERE session_id=?", (now, session_id))
            return {"idempotent": False, "run_id": run_id, "message_id": message_id, "revision_id": revision_id}

        result = self._write(op)
        effective_run_id = result.get("run_id", run_id)
        return {"run": self.get_run(effective_run_id), **result}

    def create_run_command(self, command_id: str, run_id: str, command_type: str, payload: Any = None, *, idempotency_key: str | None = None) -> dict:
        now = utc_now()
        self._write(lambda c: c.execute("INSERT INTO run_commands(command_id,run_id,command_type,payload_json,idempotency_key,created_at) VALUES (?,?,?,?,?,?)", (command_id, run_id, command_type, _json(payload), idempotency_key, now)))
        return self.get_run_command(command_id)

    def get_run_command(self, command_id: str) -> dict | None:
        return self._read(lambda c: _row(c.execute("SELECT * FROM run_commands WHERE command_id=?", (command_id,)).fetchone()))

    def get_run(self, run_id: str) -> dict | None:
        return self._read(lambda c: _row(c.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()))

    def get_run_snapshot(self, run_id: str) -> dict | None:
        """Read a Run projection and its event high-water mark atomically.

        The API snapshot is a replay cursor, so reading the Run first and
        querying events later can pair an old status with a newer cursor.  A
        single SQLite read transaction gives every projection component one
        consistent snapshot while allowing concurrent writers to commit after
        this method returns.
        """
        with self.transaction() as c:
            run_row = c.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run_row is None:
                return None
            run = dict(run_row)
            plan_row = c.execute(
                "SELECT * FROM plans WHERE plan_id=?", (run.get("active_plan_id"),)
            ).fetchone() if run.get("active_plan_id") else None
            task_rows = c.execute(
                """SELECT t.task_id,t.task_key,t.status,
                          (SELECT COUNT(*) FROM task_attempts a WHERE a.task_id=t.task_id),t.blocked_reason
                   FROM tasks t JOIN plans p ON p.plan_id=t.plan_id
                   WHERE p.run_id=? AND p.plan_id=(SELECT active_plan_id FROM runs WHERE run_id=?)
                   ORDER BY t.created_at,t.task_id""",
                (run_id, run_id),
            ).fetchall()
            input_revision = c.execute(
                "SELECT revision_id,revision FROM run_input_revisions WHERE run_id=? ORDER BY revision DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            output_rows = c.execute(
                """SELECT DISTINCT a.* FROM task_outputs o
                   JOIN tasks t ON t.task_id=o.task_id JOIN plans p ON p.plan_id=t.plan_id
                   JOIN artifacts a ON a.artifact_id=o.artifact_id
                   WHERE p.run_id=? AND p.plan_id=(SELECT active_plan_id FROM runs WHERE run_id=?)
                   ORDER BY o.created_at,o.artifact_id""",
                (run_id, run_id),
            ).fetchall()
            last_event = c.execute(
                "SELECT event_id FROM events WHERE run_id=? ORDER BY event_id DESC LIMIT 1", (run_id,)
            ).fetchone()
            awaiting = None
            if run.get("status") == "awaiting_input":
                awaiting = c.execute(
                    "SELECT payload_json FROM events WHERE run_id=? AND event_type='run.awaiting_input' ORDER BY event_id DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
            confirmation_rows = c.execute(
                """SELECT tc.confirmation_id,tc.task_id,t.task_key,tc.action_hash
                   FROM task_confirmations tc JOIN tasks t ON t.task_id=tc.task_id
                   JOIN plans p ON p.plan_id=t.plan_id
                   WHERE p.run_id=? AND p.plan_id=(SELECT active_plan_id FROM runs WHERE run_id=?)
                     AND tc.status='pending' ORDER BY tc.created_at,tc.confirmation_id""",
                (run_id, run_id),
            ).fetchall()
            awaiting_payload = None
            if awaiting is not None:
                try:
                    awaiting_payload = json.loads(awaiting[0])
                except (TypeError, ValueError):
                    awaiting_payload = None
            return {
                "run": run,
                "plan": dict(plan_row) if plan_row is not None else None,
                "tasks": [
                    {"task_id": row[0], "task_key": row[1], "status": row[2], "attempts": int(row[3] or 0), "error": row[4]}
                    for row in task_rows
                ],
                "input_revision": (input_revision[0], input_revision[1]) if input_revision else None,
                "output_artifacts": [self._decode_artifact_metadata(dict(row)) for row in output_rows],
                "last_event_id": int(last_event[0]) if last_event else 0,
                "awaiting_input": awaiting_payload,
                "pending_confirmations": [
                    {"confirmation_id": row[0], "task_id": row[1], "task_key": row[2], "action_hash": row[3]}
                    for row in confirmation_rows
                ],
            }

    def get_input_revision(self, run_id: str, revision: int | None = None) -> dict | None:
        """Return one immutable Run input revision with decoded JSON content."""
        def query(c):
            if revision is None:
                row = c.execute(
                    "SELECT * FROM run_input_revisions WHERE run_id=? ORDER BY revision DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT * FROM run_input_revisions WHERE run_id=? AND revision=?",
                    (run_id, revision),
                ).fetchone()
            value = _row(row)
            if value is not None:
                try:
                    value["input"] = json.loads(value.pop("input_json"))
                except (TypeError, ValueError) as exc:
                    raise ValueError("run input revision contains invalid JSON") from exc
            return value
        return self._read(query)

    def create_plan(self, plan_id: str, run_id: str, *, plan_version: int = 1, status: str = "draft", plan: Any = None, planner_fence=None, planner_claim=None, fence=None, mode: str = "normal") -> dict:
        now = utc_now()
        fields = {"input_revision_id": None, "ir_version": None, "compiler_version": None, "plan_hash": None, "ir_json": None}
        plan_json = plan
        if plan is not None:
            try:
                from .contracts import PlanIR
                ir = plan if isinstance(plan, PlanIR) else PlanIR.model_validate(plan)
                expected = hashlib.sha256(ir.canonical_json.encode("utf-8")).hexdigest()
                if not ir.canonical_json or ir.plan_hash != expected:
                    raise ValueError("PlanIR plan_hash does not match canonical_json")
                dumped = ir.model_dump(mode="json")
                plan_json = dumped
                fields.update(input_revision_id=str(ir.input_revision), ir_version=ir.ir_version, compiler_version=ir.compiler_version, plan_hash=ir.plan_hash, ir_json=ir.canonical_json)
            except ValueError:
                raise
            except Exception as exc:
                raise ValueError("plan must be a valid immutable PlanIR") from exc
        def op(c):
            self._assert_planner_fence_conn(c, run_id, planner_fence or planner_claim or fence, mode=mode)
            c.execute("INSERT INTO plans(plan_id,run_id,plan_version,status,plan_json,input_revision_id,ir_version,compiler_version,plan_hash,ir_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (plan_id, run_id, plan_version, status, _json(plan_json), fields["input_revision_id"], fields["ir_version"], fields["compiler_version"], fields["plan_hash"], fields["ir_json"], now))
        self._write(op)
        return self._read(lambda c: _row(c.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()))

    def activate_compiled_plan(self, run_id: str, plan: Any, *, planner_claim) -> dict:
        """Atomically publish a compiler-validated PlanIR behind a planner fence.

        This is intentionally the sole Runtime write that turns an LLM draft
        into executable tasks.  A crash can leave either the old run state or
        a fully linked active plan; it cannot leave a partly created DAG.
        """
        from .contracts import ArtifactRef, PlanIR, TaskOutputRef

        ir = plan if isinstance(plan, PlanIR) else PlanIR.model_validate(plan)
        expected_hash = hashlib.sha256(ir.canonical_json.encode("utf-8")).hexdigest()
        if ir.plan_hash != expected_hash:
            raise ValueError("PlanIR plan_hash does not match canonical_json")
        plan_id = f"plan_{uuid.uuid4().hex}"
        now = utc_now()

        def references(value):
            return value if isinstance(value, list) else [value]

        def op(c):
            self._assert_planner_fence_conn(c, run_id, planner_claim)
            current = c.execute(
                "SELECT input_revision,status FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if current is None:
                raise ValueError(f"unknown run: {run_id}")
            if int(current[0]) != int(ir.input_revision):
                raise RuntimeError("STALE_INPUT_REVISION")
            if current[1] != "planning":
                raise RuntimeError("STALE_PLANNER_LEASE")
            version = int(c.execute(
                "SELECT COALESCE(MAX(plan_version), 0) + 1 FROM plans WHERE run_id=?", (run_id,)
            ).fetchone()[0])
            c.execute(
                """INSERT INTO plans(plan_id,run_id,plan_version,status,plan_json,input_revision_id,ir_version,compiler_version,plan_hash,ir_json,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    plan_id, run_id, version, "active", _json(ir.model_dump(mode="json")),
                    str(ir.input_revision), ir.ir_version, ir.compiler_version,
                    ir.plan_hash, ir.canonical_json, now,
                ),
            )
            # Only one plan is executable for a Run. Preserve historical
            # plans for audit, but explicitly demote the previous active plan
            # before publishing the new active_plan_id.
            c.execute(
                "UPDATE plans SET status='superseded' WHERE run_id=? AND status='active' AND plan_id<>?",
                (run_id, plan_id),
            )
            task_ids = {task.task_key: f"task_{uuid.uuid4().hex}" for task in ir.tasks}
            confirmation_ids: dict[str, str] = {}
            for task in ir.tasks:
                has_task_dependency = any(
                    isinstance(ref, TaskOutputRef)
                    for value in task.inputs.values()
                    for ref in references(value)
                )
                requires_confirmation = task.confirmation_policy.mode == "required"
                status = "blocked" if has_task_dependency or requires_confirmation else "ready"
                blocked_reason = (
                    "BOTH" if has_task_dependency and requires_confirmation
                    else "CONFIRMATION_REQUIRED" if requires_confirmation
                    else "DEPENDENCIES" if has_task_dependency
                    else None
                )
                c.execute(
                    """INSERT INTO tasks(task_id,plan_id,task_key,tool_name,status,blocked_reason,task_json,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (task_ids[task.task_key], plan_id, task.task_key, task.op, status, blocked_reason,
                     _json(task.model_dump(mode="json")), now, now),
                )
                if requires_confirmation:
                    if not task.action_hash:
                        raise ValueError("confirmation-required task has no action hash")
                    confirmation_id = f"confirmation_{uuid.uuid4().hex}"
                    confirmation_ids[task.task_key] = confirmation_id
                    c.execute(
                        """INSERT INTO task_confirmations(confirmation_id,task_id,status,action_hash,created_at)
                           VALUES (?,?,?,?,?)""",
                        (confirmation_id, task_ids[task.task_key], "pending", task.action_hash, now),
                    )
            for task in ir.tasks:
                target_id = task_ids[task.task_key]
                for input_name, value in task.inputs.items():
                    for ordinal, ref in enumerate(references(value)):
                        if isinstance(ref, ArtifactRef):
                            self._require_valid_artifact(c, ref.artifact_id)
                            c.execute(
                                """INSERT INTO task_input_bindings(binding_id,task_id,input_name,source_artifact_id,required,ordinal,source_all)
                                   VALUES (?,?,?,?,?,?,0)""",
                                (str(uuid.uuid4()), target_id, input_name, ref.artifact_id, 1, ordinal),
                            )
                            c.execute(
                                "INSERT INTO task_inputs(task_id,input_name,artifact_id,ordinal) VALUES (?,?,?,?)",
                                (target_id, input_name, ref.artifact_id, ordinal),
                            )
                        elif isinstance(ref, TaskOutputRef):
                            source_id = task_ids[ref.task_key]
                            c.execute(
                                "INSERT INTO task_dependencies(task_id,depends_on_task_id) VALUES (?,?)",
                                (target_id, source_id),
                            )
                            c.execute(
                                """INSERT INTO task_input_bindings(binding_id,task_id,input_name,source_task_id,required,ordinal,source_port,source_ordinal,source_all)
                                   VALUES (?,?,?,?,?,?,?,?,?)""",
                                (
                                    str(uuid.uuid4()), target_id, input_name, source_id, 1, ordinal,
                                    ref.port, ref.ordinal, int(ref.selection == "all"),
                                ),
                            )
                        else:  # PlanIR validation should make this unreachable.
                            raise ValueError("compiled task contains an invalid input reference")
            self._append_event_conn(c, run_id, "plan.created", {
                "plan_id": plan_id, "plan_version": version, "input_revision": ir.input_revision,
                "plan_hash": ir.plan_hash, "task_count": len(ir.tasks),
            })
            for task in ir.tasks:
                has_dependency = any(
                    isinstance(ref, TaskOutputRef)
                    for value in task.inputs.values()
                    for ref in references(value)
                )
                requires_confirmation = task.task_key in confirmation_ids
                self._append_event_conn(c, run_id, "task.created", {
                    "task_id": task_ids[task.task_key], "task_key": task.task_key,
                    "op": task.op, "initial_status": "blocked" if has_dependency or requires_confirmation else "ready",
                })
            # Event append increments updated_at/last_event_seq.  Set the
            # state and clear the lease in the same transaction, then append
            # the stable status projection and readiness event.
            c.execute(
                """UPDATE runs SET active_plan_id=?,status='ready',planner_lease_owner=NULL,
                   planner_lease_token=NULL,planner_lease_expires_at=NULL,
                   run_version=run_version+1,updated_at=? WHERE run_id=?""",
                (plan_id, now, run_id),
            )
            self._append_event_conn(c, run_id, "run.status_changed", {"run_id": run_id, "status": "ready"})
            self._append_event_conn(c, run_id, "run.ready", {
                "plan_id": plan_id,
                "ready_task_count": sum(
                    task.confirmation_policy.mode != "required"
                    and not any(isinstance(ref, TaskOutputRef) for value in task.inputs.values() for ref in references(value))
                    for task in ir.tasks
                ),
            })
            for task in ir.tasks:
                confirmation_id = confirmation_ids.get(task.task_key)
                if confirmation_id is not None:
                    self._append_event_conn(c, run_id, "run.confirmation_required", {
                        "confirmation_id": confirmation_id,
                        "task_id": task_ids[task.task_key],
                        "task_key": task.task_key,
                        "action_hash": task.action_hash,
                    })
            return {"plan_id": plan_id, "task_ids": task_ids, "plan_version": version}

        return self._write(op)

    def create_planner_attempt(self, run_id: str, *, planner_claim, input_revision: int, attempt_no: int,
                               phase: str, provider: str, model: str, prompt_version: str,
                               catalog_hash: str, request_hash: str) -> dict:
        if phase not in {"initial", "repair"} or attempt_no < 1:
            raise ValueError("planner attempt phase or ordinal is invalid")
        attempt_id = f"planner_{uuid.uuid4().hex}"
        now = utc_now()

        def op(c):
            self._assert_planner_fence_conn(c, run_id, planner_claim)
            c.execute(
                """INSERT INTO planner_attempts(attempt_id,run_id,input_revision,lease_generation,attempt_no,phase,status,provider,model,prompt_version,catalog_hash,request_hash,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (attempt_id, run_id, input_revision, planner_claim.generation, attempt_no, phase,
                 "requested", provider, model, prompt_version, catalog_hash, request_hash, now),
            )
            self._append_event_conn(c, run_id, "planner.requested", {
                "attempt_id": attempt_id, "phase": phase, "input_revision": input_revision,
                "provider": provider, "model": model,
            })
        self._write(op)
        return self._read(lambda c: _row(c.execute("SELECT * FROM planner_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()))

    def finish_planner_attempt(self, attempt_id: str, *, planner_claim, status: str,
                               response_hash: str | None = None, draft: Any = None,
                               diagnostics: Any = None, usage: Any = None,
                               latency_ms: int | None = None, error_code: str | None = None,
                               error: str | None = None) -> dict:
        if status not in {"succeeded", "failed"}:
            raise ValueError("planner attempt status is invalid")

        def op(c):
            row = c.execute("SELECT run_id,status FROM planner_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None:
                raise ValueError("unknown planner attempt")
            self._assert_planner_fence_conn(c, row[0], planner_claim)
            if row[1] != "requested":
                raise ValueError("planner attempt is already finalized")
            c.execute(
                """UPDATE planner_attempts SET status=?,response_hash=?,draft_json=?,diagnostics_json=?,usage_json=?,latency_ms=?,error_code=?,error=?,finished_at=?
                   WHERE attempt_id=?""",
                (status, response_hash, _json(draft) if draft is not None else None,
                 _json(diagnostics), _json(usage), latency_ms, error_code, error, utc_now(), attempt_id),
            )
            self._append_event_conn(c, row[0], "planner.responded" if status == "succeeded" else "planner.failed", {
                "attempt_id": attempt_id, "status": status, "error_code": error_code,
            })
        self._write(op)
        return self._read(lambda c: _row(c.execute("SELECT * FROM planner_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()))

    def reject_planning(self, run_id: str, *, planner_claim, code: str, diagnostics: Any,
                        awaiting_input: bool = False) -> dict:
        """Finalize a planner lease with a durable diagnostic and no live lease."""
        target = "awaiting_input" if awaiting_input else "failed"
        if target not in RUN_TRANSITIONS["planning"]:
            raise ValueError("invalid planning terminal state")

        def op(c):
            self._assert_planner_fence_conn(c, run_id, planner_claim)
            event_type = "run.awaiting_input" if awaiting_input else "plan.rejected"
            payload = {"code": code, "diagnostic": diagnostics}
            if awaiting_input:
                detail = diagnostics if isinstance(diagnostics, dict) else {}
                payload = {
                    "kind": "planning_input", "code": code,
                    "path": detail.get("path", "draft"), "message": detail.get("message", str(code)),
                    "diagnostic": {"code": code, **detail},
                    "patch_schema": detail.get("patch_schema", {"type": "object"}),
                }
            self._append_event_conn(c, run_id, event_type, payload)
            c.execute(
                """UPDATE runs SET status=?,planner_lease_owner=NULL,planner_lease_token=NULL,
                   planner_lease_expires_at=NULL,run_version=run_version+1,updated_at=? WHERE run_id=?""",
                (target, utc_now(), run_id),
            )
            self._append_event_conn(c, run_id, "run.status_changed", {"run_id": run_id, "status": target})
            if target == "failed":
                self._append_event_conn(c, run_id, "run.failed", {"run_id": run_id, "status": target})
        self._write(op)
        return self.get_run(run_id)

    def set_active_plan(self, run_id: str, plan_id: str, *, planner_fence=None, planner_claim=None, fence=None, mode: str = "normal") -> None:
        def op(c):
            self._assert_planner_fence_conn(c, run_id, planner_fence or planner_claim or fence, mode=mode)
            if c.execute("SELECT 1 FROM plans WHERE plan_id=? AND run_id=?", (plan_id, run_id)).fetchone() is None:
                raise ValueError("plan does not belong to run")
            c.execute("UPDATE runs SET active_plan_id=?,updated_at=? WHERE run_id=?", (plan_id, utc_now(), run_id))
        self._write(op)

    def update_run_status(self, run_id: str, status: str, *, fence=None) -> dict:
        if status not in RUN_STATUSES:
            raise ValueError(f"invalid run status: {status}")
        def op(c):
            self._reject_unfenced_active_run_conn(c, run_id, fence)
            if fence is not None:
                get = fence.get if isinstance(fence, dict) else lambda name, default=None: getattr(fence, name, default)
                task_id = get("task_id")
                if not task_id:
                    raise RuntimeError("STALE_LEASE")
                self._assert_task_fence_conn(c, fence, target_task_id=task_id)
                belongs = c.execute(
                    "SELECT 1 FROM tasks t JOIN plans p ON p.plan_id=t.plan_id WHERE t.task_id=? AND p.run_id=?",
                    (task_id, run_id),
                ).fetchone()
                if belongs is None:
                    raise RuntimeError("STALE_LEASE")
            current = c.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if current is None:
                raise ValueError(f"unknown run: {run_id}")
            if status not in RUN_TRANSITIONS[current[0]]:
                raise ValueError(f"illegal run status transition: {current[0]} -> {status}")
            now = utc_now()
            c.execute("UPDATE runs SET status=?,run_version=run_version+1,updated_at=? WHERE run_id=?", (status, now, run_id))
            self._append_event_conn(c, run_id, "run.status_changed", {"run_id": run_id, "status": status})
            # Public Runtime consumers subscribe to stable terminal vocabulary;
            # emit exactly one named terminal fact alongside the generic state
            # projection event.
            terminal_type = {"completed": "run.completed", "failed": "run.failed", "cancelled": "run.cancelled"}.get(status)
            if terminal_type and c.execute("SELECT 1 FROM events WHERE run_id=? AND event_type=? LIMIT 1", (run_id, terminal_type)).fetchone() is None:
                self._append_event_conn(c, run_id, terminal_type, {"run_id": run_id, "status": status})
        self._write(op)
        return self.get_run(run_id)

    def create_task(self, task_id: str, plan_id: str, task_key: str, *, agent_role: str | None = None, tool_name: str | None = None, status: str = "ready", task: Any = None) -> dict:
        if status not in {"blocked", "ready"}:
            raise ValueError("tasks must be created in blocked or ready status")
        now = utc_now()
        self._write(lambda c: c.execute("INSERT INTO tasks(task_id,plan_id,task_key,agent_role,tool_name,status,task_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (task_id, plan_id, task_key, agent_role, tool_name, status, _json(task), now, now)))
        return self._read(lambda c: _row(c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()))

    def update_task_status(self, task_id: str, status: str, *, blocked_reason: str | None = None, next_retry_at: str | None = None, cache_hit: bool = False, cache_key: str | None = None, execution_fingerprint: str | None = None, compiler_version: str | None = None, contract_version: str | None = None, contract_identity: str | None = None, input_fingerprint: Any = None, validated: bool = False, fence=None) -> dict:
        if status not in TASK_STATUSES:
            raise ValueError(f"invalid task status: {status}")
        if status == "retry_wait":
            raise ValueError("retry_wait must be entered via queue.schedule_retry")
        def op(c):
            self._reject_unfenced_active_task_conn(c, task_id, fence)
            self._assert_task_fence_conn(c, fence, target_task_id=task_id)
            current = c.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if current is None:
                raise ValueError(f"unknown task: {task_id}")
            event_type = "task.status_changed"
            event_payload = {"task_id": task_id, "status": status}
            if status == "succeeded" and current[0] == "ready":
                if not cache_hit:
                    raise ValueError("ready to succeeded requires an explicit cache hit")
                self._validate_cache_hit_conn(c, cache_key, execution_fingerprint=execution_fingerprint, compiler_version=compiler_version, contract_version=contract_version, contract_identity=contract_identity, input_fingerprint=input_fingerprint)
                event_type = "task.cache_hit"
                event_payload["cache_key"] = cache_key
            elif cache_hit or cache_key is not None:
                raise ValueError("cache hit metadata is only valid for ready to succeeded")
            if status == "succeeded" and current[0] == "validating":
                if not self._has_verified_task_submission(c, task_id):
                    raise ValueError("validating to succeeded requires a valid output or succeeded attempt")
                event_type = "task.completed"
            elif status == "succeeded" and current[0] != "ready" and validated:
                # The explicit flag selects the commit event shape, but it
                # never bypasses the same submission gate above.
                if not self._has_verified_task_submission(c, task_id):
                    raise ValueError("validated commit requires a valid output or succeeded attempt")
                event_type = "task.completed"
            if status == "ready":
                unmet = c.execute(
                    "SELECT 1 FROM task_dependencies td JOIN tasks dep ON dep.task_id=td.depends_on_task_id WHERE td.task_id=? AND dep.status <> 'succeeded' LIMIT 1",
                    (task_id,),
                ).fetchone()
                if unmet is not None:
                    raise ValueError("cannot ready task with unmet dependency")
                pending_confirmation = c.execute(
                    "SELECT 1 FROM task_confirmations WHERE task_id=? AND status='pending' LIMIT 1",
                    (task_id,),
                ).fetchone()
                if pending_confirmation is not None:
                    raise ValueError("cannot ready task with pending confirmation")
                unresolved_binding = c.execute(
                    """
                    SELECT 1
                    FROM task_input_bindings b
                    WHERE b.task_id=? AND b.required=1
                      AND NOT EXISTS (
                          SELECT 1 FROM task_inputs i
                          WHERE i.task_id=b.task_id
                            AND i.input_name=b.input_name
                            AND i.ordinal=b.ordinal
                            AND (b.source_artifact_id IS NULL OR i.artifact_id=b.source_artifact_id)
                      )
                    LIMIT 1
                    """,
                    (task_id,),
                ).fetchone()
                if unresolved_binding is not None:
                    raise ValueError("cannot ready task with unresolved input binding")
            if status not in TASK_TRANSITIONS[current[0]]:
                raise ValueError(f"illegal task status transition: {current[0]} -> {status}")
            self._set_task_status_conn(c, task_id, status, blocked_reason=blocked_reason, event_type=event_type, event_payload=event_payload)
            if status == "retry_wait":
                c.execute("UPDATE tasks SET next_retry_at=? WHERE task_id=?", (next_retry_at, task_id))
            elif next_retry_at is not None:
                raise ValueError("next_retry_at is only valid for retry_wait")
            if status == "succeeded":
                self._unblock_dependents_conn(c, task_id)
        self._write(op)
        return self._read(lambda c: _row(c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()))

    def executor_start_task(self, claim) -> None:
        """Atomically enter execution for a claimed Task and its Attempt."""
        get = claim.get if isinstance(claim, dict) else lambda name, default=None: getattr(claim, name, default)
        task_id = get("task_id")
        attempt_id = get("attempt_id", "")
        if not task_id or not attempt_id:
            raise RuntimeError("STALE_LEASE")
        now = utc_now()
        def op(c):
            self._assert_task_fence_conn(c, claim, target_task_id=task_id, target_attempt_id=attempt_id)
            task = c.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            attempt = c.execute("SELECT status FROM task_attempts WHERE attempt_id=? AND task_id=?", (attempt_id, task_id)).fetchone()
            if task is None or attempt is None:
                raise RuntimeError("STALE_LEASE")
            if task[0] == "leased" and attempt[0] == "leased":
                c.execute("UPDATE tasks SET status='running',updated_at=? WHERE task_id=?", (now, task_id))
                run_id = get("run_id")
                if not run_id:
                    run_row = c.execute(
                        "SELECT p.run_id FROM tasks t JOIN plans p ON p.plan_id=t.plan_id WHERE t.task_id=?",
                        (task_id,),
                    ).fetchone()
                    run_id = run_row[0] if run_row is not None else None
                if run_id:
                    self._append_event_conn(c, run_id, "task.started", {"task_id": task_id, "attempt_id": attempt_id})
                c.execute("UPDATE task_attempts SET status='running',started_at=? WHERE attempt_id=?", (now, attempt_id))
            elif task[0] == "running" and attempt[0] == "running":
                raise RuntimeError("TASK_ALREADY_STARTED")
            else:
                raise RuntimeError("STALE_LEASE")
        self._write(op)

    def set_executor_identity(self, claim, identity: str) -> None:
        """Persist child identity only while the owning claim is live."""
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError("executor identity is required")
        get = claim.get if isinstance(claim, dict) else lambda name, default=None: getattr(claim, name, default)
        def op(c):
            self._assert_task_fence_conn(c, claim, target_task_id=get("task_id"), target_attempt_id=get("attempt_id", ""))
            updated = c.execute("UPDATE task_attempts SET executor_identity=? WHERE attempt_id=? AND task_id=?", (identity, get("attempt_id"), get("task_id")))
            if updated.rowcount != 1:
                raise RuntimeError("STALE_LEASE")
        self._write(op)

    def _unblock_dependents_conn(self, c, upstream_task_id: str) -> None:
        """Resolve dependency readiness in the same transaction as success."""
        dependents = c.execute(
            "SELECT DISTINCT task_id FROM task_dependencies WHERE depends_on_task_id=? ORDER BY task_id",
            (upstream_task_id,),
        ).fetchall()
        for row in dependents:
            task_id = row[0]
            current = c.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if current is None or current[0] != "blocked":
                continue
            if c.execute(
                "SELECT 1 FROM task_dependencies td JOIN tasks dep ON dep.task_id=td.depends_on_task_id WHERE td.task_id=? AND dep.status <> 'succeeded' LIMIT 1",
                (task_id,),
            ).fetchone() is not None:
                continue
            if c.execute("SELECT 1 FROM task_confirmations WHERE task_id=? AND status='pending' LIMIT 1", (task_id,)).fetchone() is not None:
                continue
            # Materialize TaskOutputRefs as concrete task_inputs in this same
            # success transaction. This makes the downstream claimable
            # without a second enqueue/resolve pass.
            bindings = c.execute(
                "SELECT * FROM task_input_bindings WHERE task_id=? AND source_task_id IS NOT NULL ORDER BY ordinal,binding_id",
                (task_id,),
            ).fetchall()
            for binding in bindings:
                if binding["source_all"]:
                    outputs = c.execute(
                        "SELECT artifact_id FROM task_outputs WHERE task_id=? AND (? IS NULL OR output_name=?) ORDER BY ordinal",
                        (binding["source_task_id"], binding["source_port"], binding["source_port"]),
                    ).fetchall()
                    for index, output in enumerate(outputs):
                        c.execute(
                            "INSERT OR IGNORE INTO task_inputs(task_id,input_name,artifact_id,ordinal) VALUES (?,?,?,?)",
                            (task_id, binding["input_name"], output[0], binding["ordinal"] + index),
                        )
                else:
                    wanted = binding["source_ordinal"] if binding["source_ordinal"] is not None else 0
                    output = c.execute(
                        "SELECT artifact_id FROM task_outputs WHERE task_id=? AND (? IS NULL OR output_name=?) AND ordinal=?",
                        (binding["source_task_id"], binding["source_port"], binding["source_port"], wanted),
                    ).fetchone()
                    if output is not None:
                        c.execute(
                            "INSERT OR IGNORE INTO task_inputs(task_id,input_name,artifact_id,ordinal) VALUES (?,?,?,?)",
                            (task_id, binding["input_name"], output[0], binding["ordinal"]),
                        )
            unresolved = c.execute(
                """
                SELECT 1 FROM task_input_bindings b
                WHERE b.task_id=? AND b.required=1
                  AND NOT EXISTS (
                      SELECT 1 FROM task_inputs i
                      WHERE i.task_id=b.task_id AND i.input_name=b.input_name
                        AND i.ordinal=b.ordinal
                        AND (b.source_artifact_id IS NULL OR i.artifact_id=b.source_artifact_id)
                  )
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if unresolved is not None:
                continue
            task_row = c.execute("SELECT p.run_id FROM tasks t JOIN plans p ON p.plan_id=t.plan_id WHERE t.task_id=?", (task_id,)).fetchone()
            if task_row is None:
                continue
            now = utc_now()
            c.execute("UPDATE tasks SET status='ready',blocked_reason=NULL,updated_at=? WHERE task_id=? AND status='blocked'", (now, task_id))
            self._append_event_conn(c, task_row[0], "task.ready", {"task_id": task_id, "reason": "dependencies_succeeded"})

    def add_task_dependency(self, task_id: str, depends_on_task_id: str) -> None:
        def op(c):
            if task_id == depends_on_task_id:
                raise ValueError("task dependency cannot be self-referential")
            tasks = c.execute(
                "SELECT task_id,plan_id FROM tasks WHERE task_id IN (?,?)",
                (task_id, depends_on_task_id),
            ).fetchall()
            if len(tasks) != 2:
                raise ValueError("task dependency references an unknown task")
            plans = {row[1] for row in tasks}
            if len(plans) != 1:
                raise ValueError("task dependencies must stay within one plan")
            cycle = c.execute(
                """
                WITH RECURSIVE reach(task_id) AS (
                    SELECT depends_on_task_id FROM task_dependencies WHERE task_id=?
                    UNION
                    SELECT td.depends_on_task_id FROM task_dependencies td JOIN reach r ON td.task_id=r.task_id
                )
                SELECT 1 FROM reach WHERE task_id=? LIMIT 1
                """,
                (depends_on_task_id, task_id),
            ).fetchone()
            if cycle is not None:
                raise ValueError("task dependency would create a cycle")
            c.execute("INSERT INTO task_dependencies(task_id,depends_on_task_id) VALUES (?,?)", (task_id, depends_on_task_id))
        self._write(op)

    def bind_task_input(self, binding_id: str, task_id: str, input_name: str, *, source_task_id: str | None = None, source_artifact_id: str | None = None, source_port: str | None = None, source_ordinal: int | None = None, source_all: bool = False, ordinal: int = 0, required: bool = True) -> dict:
        if (source_task_id is None) == (source_artifact_id is None):
            raise ValueError("task input binding needs exactly one source")
        if ordinal < 0 or (source_ordinal is not None and source_ordinal < 0):
            raise ValueError("ordinals must be non-negative")
        if source_all and source_ordinal is not None:
            raise ValueError("source_all bindings cannot specify source_ordinal")
        # Older callers omitted the output port. Keep that API source-compatible
        # while persisting an explicit selector required by the schema.
        if source_task_id is not None and source_port is None:
            source_port = "default"
        if source_task_id is not None and not source_all and source_ordinal is None:
            source_ordinal = 0
        if source_task_id is None and (source_port is not None or source_ordinal is not None or source_all):
            raise ValueError("artifact bindings cannot specify task output selectors")
        def op(c):
            if source_task_id is not None:
                owner = c.execute(
                    "SELECT t.plan_id,p.run_id FROM tasks t JOIN plans p ON p.plan_id=t.plan_id WHERE t.task_id=?",
                    (task_id,),
                ).fetchone()
                source = c.execute(
                    "SELECT t.plan_id,p.run_id FROM tasks t JOIN plans p ON p.plan_id=t.plan_id WHERE t.task_id=?",
                    (source_task_id,),
                ).fetchone()
                if owner is None or source is None:
                    raise ValueError("task binding references an unknown task")
                if owner[0] != source[0] or owner[1] != source[1]:
                    raise ValueError("task input binding source and target must share a plan")
            else:
                self._require_valid_artifact(c, source_artifact_id)
            c.execute("INSERT INTO task_input_bindings(binding_id,task_id,input_name,source_task_id,source_artifact_id,required,ordinal,source_port,source_ordinal,source_all) VALUES (?,?,?,?,?,?,?,?,?,?)", (binding_id, task_id, input_name, source_task_id, source_artifact_id, int(required), ordinal, source_port, source_ordinal, int(source_all)))
        self._write(op)
        return self._read(lambda c: _row(c.execute("SELECT * FROM task_input_bindings WHERE binding_id=?", (binding_id,)).fetchone()))

    def resolve_task_inputs(self, plan_id: str) -> dict[str, int]:
        """Resolve immutable binding rows into concrete inputs in one transaction."""
        conflict_task_id: str | None = None
        def op(c):
            nonlocal conflict_task_id
            resolved: dict[str, int] = {}
            task_rows = c.execute("SELECT * FROM tasks WHERE plan_id=? ORDER BY task_id", (plan_id,)).fetchall()
            for task in task_rows:
                task_id = task["task_id"]
                if task["status"] in {"succeeded", "failed", "cancelled"}:
                    continue
                bindings = c.execute("SELECT * FROM task_input_bindings WHERE task_id=? ORDER BY ordinal,binding_id", (task_id,)).fetchall()
                dep_rows = c.execute("SELECT td.depends_on_task_id,t.status FROM task_dependencies td JOIN tasks t ON t.task_id=td.depends_on_task_id WHERE td.task_id=?", (task_id,)).fetchall()
                if any(row["status"] in {"failed", "cancelled"} for row in dep_rows):
                    self._set_task_status_conn(c, task_id, "cancelled", blocked_reason="UPSTREAM_FAILED")
                    continue
                all_ready = all(row["status"] == "succeeded" for row in dep_rows)
                if not all_ready:
                    if task["status"] in {"blocked", "ready"}:
                        self._set_task_status_conn(c, task_id, "blocked", blocked_reason="DEPENDENCY_NOT_READY")
                    continue
                count = 0
                for binding in bindings:
                    outputs = []
                    if binding["source_artifact_id"] is not None:
                        if c.execute("SELECT 1 FROM artifacts WHERE artifact_id=? AND validation_status='valid'", (binding["source_artifact_id"],)).fetchone():
                            self._require_valid_artifact(c, binding["source_artifact_id"])
                            outputs = [(binding["source_artifact_id"], 0)]
                    else:
                        if binding["source_all"]:
                            outputs = [(row["artifact_id"], row["ordinal"]) for row in c.execute("SELECT artifact_id,ordinal FROM task_outputs WHERE task_id=? AND (? IS NULL OR output_name=?) ORDER BY ordinal", (binding["source_task_id"], binding["source_port"], binding["source_port"]))]
                        else:
                            wanted = binding["source_ordinal"] if binding["source_ordinal"] is not None else 0
                            row = c.execute("SELECT artifact_id,ordinal FROM task_outputs WHERE task_id=? AND (? IS NULL OR output_name=?) AND ordinal=?", (binding["source_task_id"], binding["source_port"], binding["source_port"], wanted)).fetchone()
                            outputs = [(row["artifact_id"], row["ordinal"])] if row else []
                        source_status = c.execute("SELECT status FROM tasks WHERE task_id=?", (binding["source_task_id"],)).fetchone()
                        if source_status is None or source_status[0] != "succeeded":
                            outputs = []
                    if not outputs:
                        if binding["required"]:
                            all_ready = False
                        continue
                    for index, (artifact_id, source_ordinal) in enumerate(outputs):
                        self._require_valid_artifact(c, artifact_id)
                        target_ordinal = binding["ordinal"] + index
                        existing = c.execute(
                            "SELECT artifact_id FROM task_inputs WHERE task_id=? AND input_name=? AND ordinal=?",
                            (task_id, binding["input_name"], target_ordinal),
                        ).fetchone()
                        if existing is not None and existing[0] != artifact_id:
                            conflict_task_id = task_id
                            raise ValueError("conflicting materialized task input")
                        c.execute("INSERT OR IGNORE INTO task_inputs(task_id,input_name,artifact_id,ordinal) VALUES (?,?,?,?)", (task_id, binding["input_name"], artifact_id, target_ordinal))
                        count += 1
                if all_ready and task["status"] == "blocked":
                    self._set_task_status_conn(c, task_id, "ready", blocked_reason=None)
                elif not all_ready and task["status"] == "blocked" and task["blocked_reason"] != "INPUTS_NOT_READY":
                    self._set_task_status_conn(c, task_id, "blocked", blocked_reason="INPUTS_NOT_READY")
                if count:
                    resolved[task_id] = count
            return resolved
        try:
            return self._write(op)
        except ValueError as exc:
            if "conflicting materialized task input" not in str(exc):
                raise
            conflict_error = exc
            # A conflict invalidates any previously optimistic ready state.
            # The resolution transaction has rolled back its partial inserts;
            # persist the blocked marker in a short compensating transaction.
            def mark_conflict(c):
                if conflict_task_id is None:
                    raise conflict_error
                task_row = c.execute("SELECT p.run_id FROM tasks t JOIN plans p ON p.plan_id=t.plan_id WHERE t.task_id=?", (conflict_task_id,)).fetchone()
                if task_row is None:
                    raise conflict_error
                now = utc_now()
                c.execute("UPDATE tasks SET status='blocked',blocked_reason='INPUT_BINDING_CONFLICT',updated_at=? WHERE task_id=? AND status IN ('ready','blocked')", (now, conflict_task_id))
                self._append_event_conn(c, task_row[0], "task.input_conflict", {"task_id": conflict_task_id, "reason": "conflicting materialized task input"})
            self._write(mark_conflict)
            raise

    resolve_dependencies = resolve_task_inputs

    def confirm_task(self, run_id: str, *, task_id: str, action_hash: str, command_id: str,
                     request_hash: str, expected_run_version: int | None = None) -> dict:
        """Atomically approve one compiler-bound side effect and release its Task.

        The method deliberately receives the content-derived action hash rather
        than an operation name or a free-form approval.  It is the sole write
        path from a user command to a pending side-effect Task becoming ready.
        """
        if not self._valid_hash(action_hash):
            raise ValueError("action_hash must be a SHA-256 hex digest")
        now = utc_now()

        def op(c):
            run = c.execute("SELECT run_version,status,active_plan_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise ValueError("RUN_NOT_FOUND")
            if expected_run_version is not None and int(run[0]) != int(expected_run_version):
                raise RuntimeError("RUN_VERSION_CONFLICT")
            if run[1] in {"completed", "failed", "cancelled", "cancelling"}:
                raise RuntimeError("RUN_NOT_CONFIRMABLE")
            existing = c.execute("SELECT run_id,payload_json FROM run_commands WHERE command_id=?", (command_id,)).fetchone()
            if existing is not None:
                try:
                    prior = json.loads(existing[1])
                except (TypeError, ValueError):
                    prior = {}
                if existing[0] != run_id or prior.get("request_hash") != request_hash:
                    raise RuntimeError("COMMAND_ID_CONFLICT")
                confirmation = c.execute(
                    "SELECT status FROM task_confirmations WHERE task_id=? AND action_hash=?", (task_id, action_hash)
                ).fetchone()
                if isinstance(prior.get("response"), dict):
                    return dict(prior["response"])
                return {"idempotent": True, "task_status": None, "confirmation_status": confirmation[0] if confirmation else None}
            task = c.execute(
                """SELECT t.status,t.plan_id FROM tasks t WHERE t.task_id=?
                   AND t.plan_id=(SELECT active_plan_id FROM runs WHERE run_id=?)""",
                (task_id, run_id),
            ).fetchone()
            if task is None:
                raise ValueError("CONFIRMATION_NOT_PENDING")
            confirmation = c.execute(
                "SELECT confirmation_id,status,action_hash FROM task_confirmations WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if confirmation is None or confirmation[1] != "pending":
                raise ValueError("CONFIRMATION_NOT_PENDING")
            if confirmation[2] != action_hash:
                raise ValueError("ACTION_HASH_MISMATCH")
            c.execute(
                "INSERT INTO run_commands(command_id,run_id,command_type,payload_json,idempotency_key,created_at) VALUES (?,?,?,?,?,?)",
                (command_id, run_id, "confirm", _json({"request_hash": request_hash, "task_id": task_id, "action_hash": action_hash}), command_id, now),
            )
            c.execute(
                "UPDATE task_confirmations SET status='confirmed',confirmed_by='local',confirmed_at=? WHERE confirmation_id=? AND status='pending'",
                (now, confirmation[0]),
            )
            c.execute("UPDATE runs SET run_version=run_version+1,updated_at=? WHERE run_id=?", (now, run_id))
            new_version = int(c.execute("SELECT run_version FROM runs WHERE run_id=?", (run_id,)).fetchone()[0])
            self._append_event_conn(c, run_id, "run.confirmed", {
                "confirmation_id": confirmation[0], "task_id": task_id, "action_hash": action_hash,
                "command_id": command_id, "run_version": new_version,
            })
            unmet = c.execute(
                """SELECT 1 FROM task_dependencies d JOIN tasks upstream ON upstream.task_id=d.depends_on_task_id
                   WHERE d.task_id=? AND upstream.status <> 'succeeded' LIMIT 1""",
                (task_id,),
            ).fetchone()
            if unmet is None and task[0] == "blocked":
                c.execute("UPDATE tasks SET status='ready',blocked_reason=NULL,updated_at=? WHERE task_id=?", (now, task_id))
                self._append_event_conn(c, run_id, "task.ready", {"task_id": task_id, "reason": "confirmation_approved"})
                task_status = "ready"
            else:
                task_status = task[0]
            response = {
                "run_id": run_id, "status": run[1], "run_version": new_version,
                "idempotent": False, "task_status": task_status,
                "confirmation_status": "confirmed",
            }
            c.execute(
                "UPDATE run_commands SET payload_json=?,applied_at=? WHERE command_id=?",
                (_json({"request_hash": request_hash, "task_id": task_id, "action_hash": action_hash, "response": response}), now, command_id),
            )
            return {key: response[key] for key in ("idempotent", "task_status", "confirmation_status", "run_version")}

        result = self._write(op)
        run = self.get_run(run_id)
        return {"run_id": run_id, "status": run["status"], "run_version": run["run_version"], **result}

    def apply_resume_command(self, run_id: str, *, command_id: str, request_hash: str,
                             patch: Any, expected_run_version: int | None = None) -> dict:
        """Atomically deduplicate and apply a paused-Run resume command."""
        now = utc_now()

        def op(c):
            run = c.execute("SELECT run_version,status,input_revision FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise ValueError("RUN_NOT_FOUND")
            existing = c.execute("SELECT run_id,payload_json FROM run_commands WHERE command_id=?", (command_id,)).fetchone()
            if existing is not None:
                try:
                    prior = json.loads(existing[1])
                except (TypeError, ValueError):
                    prior = {}
                if existing[0] != run_id or prior.get("request_hash") != request_hash:
                    raise RuntimeError("COMMAND_ID_CONFLICT")
                saved = prior.get("response")
                if isinstance(saved, dict):
                    return dict(saved)
                return {"idempotent": True, "run_version": int(run[0]), "status": run[1]}
            if run[1] != "awaiting_input":
                raise RuntimeError("RUN_NOT_AWAITING_INPUT")
            if expected_run_version is not None and int(run[0]) != int(expected_run_version):
                raise RuntimeError("RUN_VERSION_CONFLICT")

            # A cancellation reservation may have cleared the process lease
            # while the child outcome was still unknown.  Releasing the lease
            # must never leave an active Task stranded: an idempotent,
            # side-effect-free task can safely receive a fresh attempt, while
            # every side-effect/non-idempotent task needs an explicit user
            # decision before it can be retried.  Do this in the same
            # transaction as the resume command so Task, Attempt and Run
            # cannot diverge.
            decisions = patch.get("task_decisions", {}) if isinstance(patch, dict) else {}
            if not isinstance(decisions, dict):
                decisions = {}
            recovery_rows = c.execute(
                """SELECT t.task_id,t.status,t.task_json,t.blocked_reason
                   FROM tasks t JOIN plans p ON p.plan_id=t.plan_id
                   WHERE p.run_id=? AND (
                         t.status IN ('leased','running','validating')
                         OR (t.status='blocked' AND t.blocked_reason IN (?,?))
                   )
                   ORDER BY t.created_at,t.task_id""",
                (run_id, "CANCELLATION_OUTCOME_UNKNOWN", "CANCELLATION_DECISION_REQUIRED"),
            ).fetchall()
            pending_decisions: list[str] = []
            for row in recovery_rows:
                try:
                    payload = json.loads(row[2]) if row[2] else {}
                except (TypeError, ValueError):
                    payload = {}
                safe_retry = (
                    row[1] != "blocked"
                    and payload.get("idempotent") is True
                    and payload.get("side_effect") is not True
                )
                decision = decisions.get(row[0])
                if not safe_retry and decision != "retry":
                    pending_decisions.append(row[0])

            for row in recovery_rows:
                task_id = row[0]
                try:
                    payload = json.loads(row[2]) if row[2] else {}
                except (TypeError, ValueError):
                    payload = {}
                safe_retry = (
                    row[1] != "blocked"
                    and payload.get("idempotent") is True
                    and payload.get("side_effect") is not True
                )
                decision = decisions.get(task_id)
                if not safe_retry and decision != "retry":
                    c.execute(
                        """UPDATE task_attempts
                           SET status='failed',finished_at=?,error='CANCELLATION_OUTCOME_UNKNOWN',
                               error_code='CANCELLATION_OUTCOME_UNKNOWN',next_retry_at=NULL
                           WHERE task_id=? AND status IN ('queued','leased','running','validating','retry_wait')""",
                        (now, task_id),
                    )
                    c.execute(
                        """UPDATE tasks SET status='blocked',next_retry_at=NULL,
                           lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                           blocked_reason='CANCELLATION_DECISION_REQUIRED',updated_at=?
                           WHERE task_id=?""",
                        (now, task_id),
                    )
                    self._append_event_conn(c, run_id, "task.awaiting_input", {
                        "task_id": task_id,
                        "reason": "CANCELLATION_DECISION_REQUIRED",
                    })
                    continue
                c.execute(
                    """UPDATE task_attempts
                       SET status='failed',finished_at=?,error='CANCELLATION_OUTCOME_UNKNOWN',
                           error_code='CANCELLATION_OUTCOME_UNKNOWN',next_retry_at=NULL
                       WHERE task_id=? AND status IN ('queued','leased','running','validating','retry_wait')""",
                    (now, task_id),
                )
                c.execute(
                    """UPDATE tasks SET status='ready',next_retry_at=NULL,
                       lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                       blocked_reason=NULL,updated_at=? WHERE task_id=?""",
                    (now, task_id),
                )
                self._append_event_conn(c, run_id, "task.ready", {
                    "task_id": task_id,
                    "reason": "CANCELLATION_RESUME_RETRY",
                })

            if pending_decisions:
                response = {
                    "run_id": run_id,
                    "status": "awaiting_input",
                    "run_version": int(run[0]),
                    "idempotent": False,
                    "requires_user_decision": True,
                    "pending_task_ids": pending_decisions,
                }
                c.execute(
                    """INSERT INTO run_commands(command_id,run_id,command_type,payload_json,idempotency_key,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (command_id, run_id, "resume", _json({"request_hash": request_hash, "request": {"patch": patch}, "response": response}), command_id, now),
                )
                self._append_event_conn(c, run_id, "run.awaiting_input", {
                    "kind": "cancellation_decision_required",
                    "task_ids": pending_decisions,
                    "patch_schema": {
                        "type": "object",
                        "properties": {
                            "task_decisions": {
                                "type": "object",
                                "description": "Set each pending task to retry only after accepting duplicate side-effect risk.",
                            },
                        },
                    },
                })
                return response
            revision = int(run[2]) + 1
            revision_id = f"revision_{uuid.uuid4().hex}"
            payload = {"request_hash": request_hash, "request": {"patch": patch}}
            c.execute(
                "INSERT INTO run_commands(command_id,run_id,command_type,payload_json,idempotency_key,created_at) VALUES (?,?,?,?,?,?)",
                (command_id, run_id, "resume", _json(payload), command_id, now),
            )
            c.execute(
                "INSERT INTO run_input_revisions(revision_id,run_id,revision,input_json,created_at) VALUES (?,?,?,?,?)",
                (revision_id, run_id, revision, _json(patch), now),
            )
            c.execute(
                "UPDATE runs SET input_revision=?,status='queued',run_version=run_version+1,updated_at=? WHERE run_id=?",
                (revision, now, run_id),
            )
            new_version = int(c.execute("SELECT run_version FROM runs WHERE run_id=?", (run_id,)).fetchone()[0])
            self._append_event_conn(c, run_id, "run.resumed", {
                "input_revision_id": revision_id, "new_run_version": new_version,
            })
            self._append_event_conn(c, run_id, "run.status_changed", {"run_id": run_id, "status": "queued"})
            response = {"run_id": run_id, "status": "queued", "run_version": new_version, "idempotent": False}
            c.execute("UPDATE run_commands SET payload_json=?,applied_at=? WHERE command_id=?", (_json({"request_hash": request_hash, "request": {"patch": patch}, "response": response}), now, command_id))
            return {**response, "revision_id": revision_id}

        result = self._write(op)
        return {"run_id": run_id, "status": result["status"], "run_version": result["run_version"], **result}

    def apply_cancel_command(self, run_id: str, *, command_id: str, request_hash: str,
                             expected_run_version: int | None = None,
                             finalize: bool = True, defer_active: bool = False) -> dict:
        """Atomically apply cancellation, task fencing, and terminal events."""
        now = utc_now()

        def op(c):
            run = c.execute("SELECT run_version,status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise ValueError("RUN_NOT_FOUND")
            existing = c.execute("SELECT run_id,payload_json FROM run_commands WHERE command_id=?", (command_id,)).fetchone()
            if existing is not None:
                try:
                    prior = json.loads(existing[1])
                except (TypeError, ValueError):
                    prior = {}
                if existing[0] != run_id or prior.get("request_hash") != request_hash:
                    raise RuntimeError("COMMAND_ID_CONFLICT")
                saved = prior.get("response")
                if isinstance(saved, dict):
                    return dict(saved)
                return {"idempotent": True, "run_version": int(run[0]), "status": run[1]}
            if expected_run_version is not None and int(run[0]) != int(expected_run_version):
                raise RuntimeError("RUN_VERSION_CONFLICT")
            c.execute(
                "INSERT INTO run_commands(command_id,run_id,command_type,payload_json,idempotency_key,created_at) VALUES (?,?,?,?,?,?)",
                (command_id, run_id, "cancel", _json({"request_hash": request_hash}), command_id, now),
            )
            status = run[1]
            if status not in {"completed", "failed", "cancelled"}:
                self._append_event_conn(c, run_id, "run.cancel_requested", {"run_id": run_id})
                if status != "cancelling":
                    c.execute(
                        """UPDATE runs SET status='cancelling',run_version=run_version+1,
                           planner_lease_owner=NULL,planner_lease_token=NULL,
                           planner_lease_expires_at=NULL,updated_at=? WHERE run_id=?""",
                        (now, run_id),
                    )
                rows = c.execute(
                    "SELECT t.task_id,t.status FROM tasks t JOIN plans p ON p.plan_id=t.plan_id WHERE p.run_id=?",
                    (run_id,),
                ).fetchall()
                for row in rows:
                    if row[1] in {"succeeded", "failed", "cancelled"}:
                        continue
                    if defer_active and row[1] in {"leased", "running", "validating"}:
                        continue
                    self._set_task_status_conn(c, row[0], "cancelled", blocked_reason="RUN_CANCELLED")
                    c.execute(
                        "UPDATE task_attempts SET status='cancelled',finished_at=? WHERE task_id=? AND status IN ('queued','leased','running','validating','retry_wait')",
                        (now, row[0]),
                    )
                    c.execute(
                        "UPDATE tasks SET lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL WHERE task_id=?",
                        (row[0],),
                    )
                if finalize:
                    c.execute("UPDATE runs SET status='cancelled',run_version=run_version+1,updated_at=? WHERE run_id=?", (now, run_id))
                    self._append_event_conn(c, run_id, "run.cancelled", {"run_id": run_id})
                    status = "cancelled"
                else:
                    status = "cancelling"
            new_version = int(c.execute("SELECT run_version FROM runs WHERE run_id=?", (run_id,)).fetchone()[0])
            response = {"run_id": run_id, "status": status, "run_version": new_version, "idempotent": False}
            c.execute("UPDATE run_commands SET payload_json=?,applied_at=? WHERE command_id=?", (_json({"request_hash": request_hash, "response": response}), now, command_id))
            return response

        result = self._write(op)
        return {"run_id": run_id, "status": result["status"], "run_version": result["run_version"], **result}

    def finalize_cancel_command(self, run_id: str, *, command_id: str, resolved: bool = True) -> dict:
        """Complete a cancellation reservation after child termination."""
        now = utc_now()
        def op(c):
            run = c.execute("SELECT run_version,status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise ValueError("RUN_NOT_FOUND")
            if run[1] == "cancelled":
                current = c.execute("SELECT payload_json FROM run_commands WHERE command_id=? AND run_id=?", (command_id, run_id)).fetchone()
                if current is not None:
                    try:
                        saved = json.loads(current[0]).get("response")
                    except (TypeError, ValueError):
                        saved = None
                    if isinstance(saved, dict):
                        return saved
            if run[1] != "cancelling":
                raise RuntimeError("RUN_NOT_CANCELLING")
            if resolved:
                rows = c.execute(
                    "SELECT t.task_id,t.status FROM tasks t JOIN plans p ON p.plan_id=t.plan_id WHERE p.run_id=?",
                    (run_id,),
                ).fetchall()
                for row in rows:
                    if row[1] in {"succeeded", "failed", "cancelled"}:
                        continue
                    self._set_task_status_conn(c, row[0], "cancelled", blocked_reason="RUN_CANCELLED")
                    c.execute(
                        "UPDATE task_attempts SET status='cancelled',finished_at=? WHERE task_id=? AND status IN ('queued','leased','running','validating','retry_wait')",
                        (now, row[0]),
                    )
                    c.execute(
                        "UPDATE tasks SET lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL WHERE task_id=?",
                        (row[0],),
                    )
            c.execute("UPDATE runs SET status='cancelled',run_version=run_version+1,updated_at=? WHERE run_id=?", (now, run_id))
            self._append_event_conn(c, run_id, "run.cancelled", {"run_id": run_id})
            new_version = int(c.execute("SELECT run_version FROM runs WHERE run_id=?", (run_id,)).fetchone()[0])
            response = {"run_id": run_id, "status": "cancelled", "run_version": new_version, "idempotent": False}
            command = c.execute("SELECT 1 FROM run_commands WHERE command_id=? AND run_id=?", (command_id, run_id)).fetchone()
            if command is None:
                raise ValueError("CANCEL_COMMAND_NOT_FOUND")
            payload = json.loads(c.execute("SELECT payload_json FROM run_commands WHERE command_id=?", (command_id,)).fetchone()[0])
            payload["response"] = response
            c.execute("UPDATE run_commands SET payload_json=?,applied_at=? WHERE command_id=?", (_json(payload), now, command_id))
            return response
        result = self._write(op)
        return {"run_id": run_id, "status": result["status"], "run_version": result["run_version"], **result}

    def mark_cancel_indeterminate(self, run_id: str, *, command_id: str, reason: str = "CANCELLATION_OUTCOME_UNKNOWN") -> dict:
        """Fence unresolved child work and pause cancellation for user input."""
        now = utc_now()
        def op(c):
            run = c.execute("SELECT run_version,status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise ValueError("RUN_NOT_FOUND")
            if run[1] == "awaiting_input":
                return {"run_id": run_id, "status": "awaiting_input", "run_version": int(run[0]), "idempotent": True}
            if run[1] != "cancelling":
                raise RuntimeError("RUN_NOT_CANCELLING")
            rows = c.execute(
                "SELECT t.task_id FROM tasks t JOIN plans p ON p.plan_id=t.plan_id WHERE p.run_id=? AND t.status IN ('leased','running','validating')",
                (run_id,),
            ).fetchall()
            for row in rows:
                c.execute(
                    "UPDATE task_attempts SET error=?,error_code=? WHERE task_id=? AND status IN ('leased','running','validating')",
                    (reason, reason, row[0]),
                )
                c.execute(
                    "UPDATE tasks SET lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,blocked_reason=?,updated_at=? WHERE task_id=?",
                    (reason, now, row[0]),
                )
            c.execute("UPDATE runs SET status='awaiting_input',run_version=run_version+1,updated_at=? WHERE run_id=?", (now, run_id))
            self._append_event_conn(c, run_id, "run.awaiting_input", {"kind": "cancellation_outcome_unknown", "reason": reason})
            new_version = int(c.execute("SELECT run_version FROM runs WHERE run_id=?", (run_id,)).fetchone()[0])
            command = c.execute("SELECT payload_json FROM run_commands WHERE command_id=? AND run_id=?", (command_id, run_id)).fetchone()
            if command is not None:
                payload = json.loads(command[0])
                payload["response"] = {"run_id": run_id, "status": "awaiting_input", "run_version": new_version, "idempotent": False}
                c.execute("UPDATE run_commands SET payload_json=?,applied_at=? WHERE command_id=?", (_json(payload), now, command_id))
            return {"run_id": run_id, "status": "awaiting_input", "run_version": new_version, "idempotent": False}
        return self._write(op)

    def create_task_confirmation(self, confirmation_id: str, task_id: str, *, status: str = "pending", action_hash: str | None = None) -> dict:
        if status not in {"pending", "confirmed", "rejected", "cancelled"}:
            raise ValueError("invalid task confirmation status")
        if action_hash is not None and not self._valid_hash(action_hash):
            raise ValueError("action_hash must be a SHA-256 hex digest")
        now = utc_now()
        self._write(lambda c: c.execute("INSERT INTO task_confirmations(confirmation_id,task_id,status,action_hash,created_at) VALUES (?,?,?,?,?)", (confirmation_id, task_id, status, action_hash, now)))
        return self._read(lambda c: _row(c.execute("SELECT * FROM task_confirmations WHERE confirmation_id=?", (confirmation_id,)).fetchone()))

    def update_task_confirmation_status(self, confirmation_id: str, status: str) -> dict:
        if status not in {"pending", "confirmed", "rejected", "cancelled"}:
            raise ValueError("invalid task confirmation status")
        def op(c):
            row = c.execute("SELECT status FROM task_confirmations WHERE confirmation_id=?", (confirmation_id,)).fetchone()
            if row is None:
                raise ValueError(f"unknown task confirmation: {confirmation_id}")
            if row[0] != "pending":
                raise ValueError("task confirmation is already finalized")
            confirmed_at = utc_now() if status == "confirmed" else None
            c.execute("UPDATE task_confirmations SET status=?,confirmed_at=? WHERE confirmation_id=?", (status, confirmed_at, confirmation_id))
        self._write(op)
        return self._read(lambda c: _row(c.execute("SELECT * FROM task_confirmations WHERE confirmation_id=?", (confirmation_id,)).fetchone()))

    def create_task_attempt(self, attempt_id: str, task_id: str, attempt_no: int, *, lease_generation: int = 0, status: str = "running", executor_identity=_MISSING_EXECUTOR_IDENTITY) -> dict:
        if status not in TASK_ATTEMPT_STATUSES:
            raise ValueError("invalid task attempt status")
        if attempt_no < 0:
            raise ValueError("attempt number must be non-negative")
        if lease_generation < 0:
            raise ValueError("lease generation must be non-negative")
        if executor_identity is _MISSING_EXECUTOR_IDENTITY:
            # Legacy/internal callers do not know the eventual executor yet;
            # still persist a durable identity so the NOT NULL invariant is
            # never relaxed. Queue claims overwrite this with PID/job data.
            executor_identity = f"store:{attempt_id}"
        elif not isinstance(executor_identity, str) or not executor_identity.strip():
            raise ValueError("executor_identity is required for active attempts")
        now = utc_now()
        self._write(lambda c: c.execute("INSERT INTO task_attempts(attempt_id,task_id,attempt_no,status,lease_generation,started_at,executor_identity) VALUES (?,?,?,?,?,?,?)", (attempt_id, task_id, attempt_no, status, lease_generation, now, executor_identity)))
        return self._read(lambda c: _row(c.execute("SELECT * FROM task_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()))

    def update_task_attempt_status(self, attempt_id: str, status: str, *, fence=None, error: str | None = None, error_code: str | None = None, next_retry_at: str | None = None) -> dict:
        allowed = TASK_ATTEMPT_STATUSES
        if status not in allowed:
            raise ValueError("invalid task attempt status")
        if status == "retry_wait":
            raise ValueError("retry_wait must be entered via queue.schedule_retry")
        def op(c):
            attempt_row = c.execute("SELECT task_id FROM task_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if attempt_row is None:
                raise ValueError(f"unknown task attempt: {attempt_id}")
            self._reject_unfenced_active_task_conn(c, attempt_row[0], fence)
            self._assert_task_fence_conn(c, fence, target_task_id=attempt_row[0], target_attempt_id=attempt_id)
            row = c.execute("SELECT status FROM task_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row[0] in {"succeeded", "failed", "cancelled"}:
                raise ValueError("task attempt is terminal")
            if status not in TASK_ATTEMPT_TRANSITIONS.get(row[0], set()):
                raise ValueError(f"illegal task attempt status transition: {row[0]} -> {status}")
            finished_at = utc_now() if status in {"succeeded", "failed", "cancelled"} else None
            c.execute("UPDATE task_attempts SET status=?,finished_at=?,error=?,error_code=?,next_retry_at=? WHERE attempt_id=?", (status, finished_at, error, error_code, next_retry_at, attempt_id))
        self._write(op)
        return self._read(lambda c: _row(c.execute("SELECT * FROM task_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()))

    def aggregate_run_terminal(self, run_id: str) -> str | None:
        """Atomically derive and commit a Run terminal outcome from its Tasks."""
        now = utc_now()
        def op(c):
            run = c.execute("SELECT status,active_plan_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise ValueError(f"unknown run: {run_id}")
            if run[0] in {"completed", "failed", "cancelled", "cancelling", "awaiting_input"}:
                return run[0]
            rows = c.execute(
                "SELECT t.status FROM tasks t JOIN plans p ON p.plan_id=t.plan_id "
                "WHERE p.run_id=? AND (p.plan_id=? OR ? IS NULL)",
                (run_id, run[1], run[1]),
            ).fetchall()
            statuses = [row[0] for row in rows]
            if not statuses or any(value not in {"succeeded", "failed", "cancelled"} for value in statuses):
                return run[0]
            target = "failed" if "failed" in statuses else "completed" if all(value == "succeeded" for value in statuses) else None
            if target is None:
                return run[0]
            updated = c.execute(
                "UPDATE runs SET status=?,run_version=run_version+1,updated_at=? "
                "WHERE run_id=? AND status NOT IN ('completed','failed','cancelled','cancelling','awaiting_input')",
                (target, now, run_id),
            )
            if updated.rowcount == 1:
                self._append_event_conn(c, run_id, "run.status_changed", {"run_id": run_id, "status": target})
                self._append_event_conn(c, run_id, f"run.{target}", {"run_id": run_id, "status": target})
            return c.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
        return self._write(op)

    def delete_task(self, task_id: str) -> None:
        self._write(lambda c: c.execute("DELETE FROM tasks WHERE task_id=?", (task_id,)))

    def add_input_revision(self, run_id: str, revision: int, value: Any, *, revision_id: str | None = None) -> dict:
        revision_id = revision_id or str(uuid.uuid4())
        now = utc_now()
        self._write(lambda c: c.execute("INSERT INTO run_input_revisions(revision_id,run_id,revision,input_json,created_at) VALUES (?,?,?,?,?)", (revision_id, run_id, revision, _json(value), now)))
        return self._read(lambda c: _row(c.execute("SELECT * FROM run_input_revisions WHERE revision_id=?", (revision_id,)).fetchone()))

    def create_artifact_record(self, artifact_id: str, *, origin: str, content: bytes, producer_task_id: str | None = None) -> dict:
        if origin not in {"upload", "task"}:
            raise ValueError("artifact origin must be upload or task")
        import hashlib
        digest = hashlib.sha256(content).hexdigest()
        if artifact_id != digest:
            # Test and repository callers may choose a logical ID; CAS callers
            # use the digest itself. The content hash remains authoritative.
            content_hash = digest
        else:
            content_hash = artifact_id
        artifact = {"artifact_id": artifact_id, "origin": origin, "producer_task_id": producer_task_id, "content_hash": content_hash, "storage_uri": "", "media_type": None, "size_bytes": len(content), "validation_status": "staged"}
        return self.insert_artifact(artifact)

    def attach_task_input(self, task_id: str, input_name: str, artifact_id: str, *, ordinal: int = 0) -> None:
        if ordinal < 0:
            raise ValueError("ordinal must be non-negative")
        def op(c):
            self._require_valid_artifact(c, artifact_id)
            c.execute("INSERT INTO task_inputs(task_id,input_name,artifact_id,ordinal) VALUES (?,?,?,?)", (task_id, input_name, artifact_id, ordinal))
        self._write(op)

    def attach_task_output(self, task_id: str, output_name: str, artifact_id: str, *, ordinal: int = 0, fence=None) -> None:
        if ordinal < 0:
            raise ValueError("ordinal must be non-negative")
        def op(c):
            self._reject_unfenced_active_task_conn(c, task_id, fence)
            self._assert_task_fence_conn(c, fence, target_task_id=task_id)
            artifact = self._require_valid_artifact(c, artifact_id)
            if artifact.get("producer_task_id") != task_id:
                raise ValueError("task output producer must match task")
            c.execute("INSERT INTO task_outputs(task_id,output_name,artifact_id,ordinal,created_at) VALUES (?,?,?,?,?)", (task_id, output_name, artifact_id, ordinal, utc_now()))
        self._write(op)

    def pin_artifact(self, artifact_id: str, pin_key: str) -> None:
        def op(c):
            self._require_valid_artifact(c, artifact_id)
            c.execute("INSERT INTO artifact_pins(artifact_id,pin_key,created_at) VALUES (?,?,?)", (artifact_id, pin_key, utc_now()))
        self._write(op)

    def lease_artifact(self, artifact_id: str | None, lease_key: str, *, expires_at: str, purpose: str = "gc", content_hash: str | None = None, _validate_file: bool = True) -> None:
        if (artifact_id is None) == (content_hash is None):
            raise ValueError("lease needs exactly one artifact_id or content_hash")
        if purpose not in {"commit", "download", "backup", "gc"}:
            raise ValueError(f"invalid lease purpose: {purpose}")
        expires_at = self._normalize_expiry(expires_at)
        if content_hash is not None and (len(content_hash) != 64 or any(ch not in "0123456789abcdef" for ch in content_hash)):
            raise ValueError("content_hash must be a SHA-256 hex digest")
        def op(c):
            now = utc_now()
            target_artifact_id = artifact_id
            resolved_hash = content_hash
            if target_artifact_id is not None:
                if _validate_file:
                    self._require_valid_artifact(c, target_artifact_id)
                else:
                    self._require_valid_artifact_metadata(c, target_artifact_id)
                source = c.execute("SELECT content_hash FROM artifacts WHERE artifact_id=?", (target_artifact_id,)).fetchone()
                if source is None:
                    raise ValueError(f"unknown artifact: {target_artifact_id}")
                resolved_hash = source[0]
            else:
                source = c.execute("SELECT artifact_id FROM artifacts WHERE content_hash=?", (content_hash,)).fetchone()
                if source is not None:
                    target_artifact_id = source[0]
                    if _validate_file:
                        self._require_valid_artifact(c, target_artifact_id)
                    else:
                        self._require_valid_artifact_metadata(c, target_artifact_id)
            conflict = c.execute("SELECT lease_key FROM artifact_leases WHERE lease_key<>? AND expires_at>? AND ((? IS NOT NULL AND artifact_id=?) OR (? IS NOT NULL AND content_hash=?))", (lease_key, now, target_artifact_id, target_artifact_id, resolved_hash, resolved_hash)).fetchone()
            if conflict:
                raise RuntimeError("artifact lease is held by another purpose")
            existing = c.execute("SELECT artifact_id,content_hash,purpose,expires_at FROM artifact_leases WHERE lease_key=?", (lease_key,)).fetchone()
            if existing is not None:
                if existing[3] > now:
                    if existing[0] == target_artifact_id and existing[1] == resolved_hash and existing[2] == purpose:
                        return
                    raise RuntimeError("lease key is already held")
                c.execute("DELETE FROM artifact_leases WHERE lease_key=?", (lease_key,))
            c.execute("INSERT INTO artifact_leases(artifact_id,content_hash,lease_key,purpose,expires_at,created_at) VALUES (?,?,?,?,?,?)", (target_artifact_id, resolved_hash, lease_key, purpose, expires_at, now))
        self._write(op)

    @staticmethod
    def _normalize_expiry(expires_at: str) -> str:
        if not isinstance(expires_at, str):
            raise ValueError("expires_at must be an RFC3339 UTC timestamp")
        try:
            parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError("expires_at must be an RFC3339 UTC timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("expires_at must include a timezone")
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    acquire_artifact_lease = lease_artifact

    def release_artifact_lease(self, lease_key: str) -> None:
        self._write(lambda c: c.execute("DELETE FROM artifact_leases WHERE lease_key=?", (lease_key,)))

    def put_cache(self, cache_key: str, *, expires_at: str | None = None, metadata: Any = None, contract_version: str | None = None, execution_fingerprint: str | None = None, input_fingerprint: Any = None) -> None:
        metadata = dict(metadata or {})
        contract_version = contract_version or metadata.get("contract_version")
        execution_fingerprint = execution_fingerprint or metadata.get("execution_fingerprint")
        input_fingerprint = input_fingerprint if input_fingerprint is not None else metadata.get("input_fingerprint")
        self._write(lambda c: c.execute("INSERT INTO cache_entries(cache_key,created_at,expires_at,metadata_json,contract_version,execution_fingerprint,input_fingerprint) VALUES (?,?,?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET expires_at=excluded.expires_at,metadata_json=excluded.metadata_json,contract_version=excluded.contract_version,execution_fingerprint=excluded.execution_fingerprint,input_fingerprint=excluded.input_fingerprint", (cache_key, utc_now(), expires_at, _json(metadata), contract_version, execution_fingerprint, _json(input_fingerprint) if input_fingerprint is not None else None)))

    def attach_cache_artifact(self, cache_key: str, artifact_id: str) -> None:
        def op(c):
            self._require_valid_artifact(c, artifact_id)
            c.execute("INSERT INTO cache_artifacts(cache_key,artifact_id) VALUES (?,?)", (cache_key, artifact_id))
        self._write(op)

    def append_event(self, run_id: str, event_type: str, payload: Any = None, *, status: str | None = None) -> dict:
        if status is not None and status not in RUN_STATUSES:
            raise ValueError(f"invalid run status: {status}")
        def op(c):
            run = c.execute("SELECT last_event_seq,run_version FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise ValueError(f"unknown run: {run_id}")
            seq = int(run[0]) + 1
            now = utc_now()
            if isinstance(payload, dict) and "run_version" not in payload:
                payload_value = {**payload, "run_version": int(run[1])}
            else:
                payload_value = payload
            cur = c.execute("INSERT INTO events(run_id,seq,event_type,payload_json,created_at) VALUES (?,?,?,?,?)", (run_id, seq, event_type, _json(payload_value), now))
            if status is None:
                c.execute("UPDATE runs SET last_event_seq=?,updated_at=? WHERE run_id=?", (seq, now, run_id))
            else:
                current = c.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
                if status not in RUN_TRANSITIONS[current]:
                    raise ValueError(f"illegal run status transition: {current} -> {status}")
                c.execute("UPDATE runs SET last_event_seq=?,status=?,run_version=run_version+1,updated_at=? WHERE run_id=?", (seq, status, now, run_id))
            row = c.execute("SELECT * FROM events WHERE event_id=?", (cur.lastrowid,)).fetchone()
            return _row(row)
        return self._write(op)

    def list_events(self, run_id: str) -> list[dict]:
        return self._read(lambda c: [dict(r) for r in c.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,))])

    def get_artifact(self, artifact_id: str) -> dict | None:
        def query(c):
            row = _row(c.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone())
            return self._decode_artifact_metadata(row) if row is not None else None
        return self._read(query)

    def has_artifact_with_content_hash(self, content_hash: str) -> bool:
        """Return whether a CAS blob is referenced by any Artifact metadata."""
        return bool(self._read(lambda c: c.execute(
            "SELECT 1 FROM artifacts WHERE content_hash=? LIMIT 1", (content_hash,)
        ).fetchone()))

    def list_lineage(self, artifact_id: str) -> list[dict]:
        return self._read(lambda c: [dict(r) for r in c.execute("SELECT * FROM artifact_lineage WHERE artifact_id=? ORDER BY lineage_id", (artifact_id,))])

    def insert_artifact(self, artifact: dict, *, parents: list[str] | None = None, output_name: str | None = None, output_ordinal: int = 0, cache_key: str | None = None, event: tuple[str, str, Any] | None = None, fence=None) -> dict:
        artifact = dict(artifact)
        artifact.setdefault("type", "Artifact")
        artifact.setdefault("crs", None)
        artifact.setdefault("schema", {})
        if artifact.get("origin") not in {"upload", "task"}:
            raise ValueError("artifact origin must be upload or task")
        status = artifact.get("validation_status")
        if status not in {"valid", "staged"}:
            raise ValueError("artifact is invalid or quarantined")
        if status == "valid":
            self._validate_artifact_record(artifact, require_file=True)
        else:
            if artifact.get("storage_uri"):
                raise ValueError("staged artifacts cannot have a published storage URI")
            if not self._valid_hash(artifact.get("content_hash")) or int(artifact.get("size_bytes", -1)) < 0:
                raise ValueError("staged artifact metadata is invalid")
        parents = parents or []
        now = artifact.get("created_at") or utc_now()
        def op(c):
            producer_task_id = artifact.get("producer_task_id") if artifact["origin"] == "task" else None
            self._reject_unfenced_active_task_conn(c, producer_task_id, fence)
            self._assert_task_fence_conn(c, fence, target_task_id=producer_task_id)
            if artifact["origin"] == "task":
                producer = producer_task_id
                if not producer or c.execute("SELECT 1 FROM tasks WHERE task_id=?", (producer,)).fetchone() is None:
                    raise ValueError("task artifact producer must reference an existing task")
            if status == "valid":
                self._validate_artifact_record(artifact, require_file=True)
            for parent in parents:
                self._require_valid_artifact(c, parent)
            c.execute("INSERT INTO artifacts(artifact_id,origin,producer_task_id,content_hash,storage_uri,media_type,type,crs,schema,size_bytes,validation_status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (artifact["artifact_id"], artifact["origin"], artifact.get("producer_task_id"), artifact["content_hash"], artifact["storage_uri"], artifact.get("media_type"), artifact["type"], _json(artifact["crs"]) if artifact["crs"] is not None else "null", _json(artifact["schema"]), artifact["size_bytes"], artifact["validation_status"], now))
            for parent in parents:
                c.execute("INSERT INTO artifact_lineage(lineage_id,artifact_id,parent_artifact_id,relation,created_at) VALUES (?,?,?,?,?)", (str(uuid.uuid4()), artifact["artifact_id"], parent, "derived_from", now))
            if output_name is not None:
                if not artifact.get("producer_task_id"):
                    raise ValueError("task output requires producer_task_id")
                if output_ordinal < 0:
                    raise ValueError("output ordinal must be non-negative")
                c.execute("INSERT INTO task_outputs(task_id,output_name,artifact_id,ordinal,created_at) VALUES (?,?,?,?,?)", (artifact["producer_task_id"], output_name, artifact["artifact_id"], output_ordinal, now))
            if cache_key is not None:
                c.execute("INSERT INTO cache_entries(cache_key,created_at) VALUES (?,?) ON CONFLICT(cache_key) DO NOTHING", (cache_key, now))
                c.execute("INSERT INTO cache_artifacts(cache_key,artifact_id) VALUES (?,?)", (cache_key, artifact["artifact_id"]))
            if event is not None:
                run_id, event_type, payload = event
                self._append_event_conn(c, run_id, event_type, payload)
            return artifact
        return self._write(op)

    def attach_artifact_references(self, artifact_id: str, *, parents: list[str] | None = None, output_name: str | None = None, producer_task_id: str | None = None, ordinal: int = 0, cache_key: str | None = None, event: tuple[str, str, Any] | None = None, fence=None) -> dict:
        """Attach transactional references to an already CAS-deduplicated artifact."""
        parents = parents or []
        def op(c):
            producer = producer_task_id
            if producer is None:
                row = c.execute("SELECT producer_task_id FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
                producer = row[0] if row is not None else None
            self._reject_unfenced_active_task_conn(c, producer, fence)
            self._assert_task_fence_conn(c, fence, target_task_id=producer)
            artifact = self._require_valid_artifact(c, artifact_id)
            now = utc_now()
            for parent in parents:
                self._require_valid_artifact(c, parent)
                c.execute("INSERT OR IGNORE INTO artifact_lineage(lineage_id,artifact_id,parent_artifact_id,relation,created_at) VALUES (?,?,?,?,?)", (str(uuid.uuid4()), artifact_id, parent, "derived_from", now))
            if output_name is not None:
                if ordinal < 0:
                    raise ValueError("ordinal must be non-negative")
                producer = producer_task_id or artifact["producer_task_id"]
                if not producer:
                    raise ValueError("task output requires producer_task_id")
                if c.execute("SELECT 1 FROM tasks WHERE task_id=?", (producer,)).fetchone() is None:
                    raise ValueError("unknown producer task")
                c.execute("INSERT OR IGNORE INTO task_outputs(task_id,output_name,artifact_id,ordinal,link_origin,created_at) VALUES (?,?,?,?,?,?)", (producer, output_name, artifact_id, ordinal, "dedup" if producer_task_id else "producer", now))
            if cache_key is not None:
                self._require_valid_artifact(c, artifact_id)
                c.execute("INSERT INTO cache_entries(cache_key,created_at) VALUES (?,?) ON CONFLICT(cache_key) DO NOTHING", (cache_key, now))
                c.execute("INSERT OR IGNORE INTO cache_artifacts(cache_key,artifact_id) VALUES (?,?)", (cache_key, artifact_id))
            if event is not None:
                self._append_event_conn(c, *event)
            return _row(artifact)
        return self._write(op)

    @staticmethod
    def _append_event_conn(c, run_id: str, event_type: str, payload: Any) -> dict:
        row = c.execute("SELECT last_event_seq,run_version FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown run: {run_id}")
        seq = int(row[0]) + 1
        if isinstance(payload, dict) and "run_version" not in payload:
            payload = {**payload, "run_version": int(row[1])}
        now = utc_now()
        cur = c.execute("INSERT INTO events(run_id,seq,event_type,payload_json,created_at) VALUES (?,?,?,?,?)", (run_id, seq, event_type, _json(payload), now))
        c.execute("UPDATE runs SET last_event_seq=?,updated_at=? WHERE run_id=?", (seq, now, run_id))
        return _row(c.execute("SELECT * FROM events WHERE event_id=?", (cur.lastrowid,)).fetchone())

    def _set_task_status_conn(self, c, task_id: str, status: str, *, blocked_reason: str | None, event_type: str = "task.status_changed", event_payload: dict | None = None) -> None:
        row = c.execute(
            "SELECT t.status,p.run_id FROM tasks t JOIN plans p ON p.plan_id=t.plan_id WHERE t.task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown task: {task_id}")
        now = utc_now()
        c.execute("UPDATE tasks SET status=?,blocked_reason=?,updated_at=? WHERE task_id=?", (status, blocked_reason, now, task_id))
        self._append_event_conn(c, row["run_id"], event_type, event_payload or {"task_id": task_id, "status": status})

    def _validate_cache_hit_conn(self, c, cache_key: str | None, *, execution_fingerprint: str | None, compiler_version: str | None, contract_version: str | None, contract_identity: str | None, input_fingerprint: Any) -> None:
        if not cache_key:
            raise ValueError("cache_key is required for a cache hit")
        now = utc_now()
        entry = c.execute(
            "SELECT metadata_json FROM cache_entries WHERE cache_key=? AND (expires_at IS NULL OR expires_at>?)",
            (cache_key, now),
        ).fetchone()
        if entry is None:
            raise ValueError("cache entry is missing or expired")
        try:
            metadata = json.loads(entry[0])
        except (TypeError, ValueError) as exc:
            raise ValueError("cache entry metadata is invalid") from exc
        expected = {
            "execution_fingerprint": execution_fingerprint,
            "compiler_version": compiler_version,
            "contract_version": contract_version,
            "contract_identity": contract_identity,
        }
        if any(value is None for value in expected.values()):
            raise ValueError("cache hit execution metadata is required")
        if any(metadata.get(key) != value for key, value in expected.items()):
            raise ValueError("cache hit execution fingerprint or contract identity does not match")
        if input_fingerprint is None or metadata.get("input_fingerprint") != input_fingerprint:
            raise ValueError("cache hit input fingerprint does not match")
        artifacts = c.execute("SELECT artifact_id FROM cache_artifacts WHERE cache_key=? ORDER BY artifact_id", (cache_key,)).fetchall()
        if not artifacts:
            raise ValueError("cache entry has no artifacts")
        for row in artifacts:
            self._require_valid_artifact(c, row[0])

    def _has_verified_task_submission(self, c, task_id: str) -> bool:
        output_rows = c.execute(
            "SELECT artifact_id FROM task_outputs WHERE task_id=? ORDER BY output_name,ordinal",
            (task_id,),
        ).fetchall()
        found = False
        for row in output_rows:
            self._require_valid_artifact(c, row[0])
            found = True
        attempt = c.execute(
            "SELECT 1 FROM task_attempts WHERE task_id=? AND status='succeeded' LIMIT 1",
            (task_id,),
        ).fetchone()
        return found or attempt is not None

    def commit_validated_task(self, task_id: str, *, fence=None) -> dict:
        """Commit a validated result under the owning task claim fence."""
        if fence is None:
            active = self._read(
                lambda c: c.execute(
                    "SELECT status FROM tasks WHERE task_id=?", (task_id,)
                ).fetchone()
            )
            if active is not None and active[0] in {"leased", "running", "validating"}:
                raise ValueError("validated commit requires a claim fence, output, or succeeded attempt")
        return self.update_task_status(task_id, "succeeded", validated=True, fence=fence)

    def reachable_artifact_ids(self) -> set[str]:
        def query(c):
            now = utc_now()
            roots = {r[0] for r in c.execute("SELECT artifact_id FROM artifact_pins UNION SELECT artifact_id FROM task_inputs UNION SELECT artifact_id FROM task_outputs UNION SELECT artifact_id FROM message_artifacts UNION SELECT artifact_id FROM artifact_leases WHERE artifact_id IS NOT NULL AND expires_at > ? UNION SELECT a.artifact_id FROM artifacts a JOIN artifact_leases l ON l.content_hash=a.content_hash WHERE l.expires_at > ? UNION SELECT ca.artifact_id FROM cache_artifacts ca JOIN cache_entries ce ON ce.cache_key=ca.cache_key WHERE ce.expires_at IS NULL OR ce.expires_at > ?", (now, now, now))}
            changed = True
            while changed:
                rows = c.execute("SELECT parent_artifact_id FROM artifact_lineage WHERE artifact_id IN ({})".format(",".join("?" * len(roots)) if roots else "NULL"), tuple(roots)).fetchall() if roots else []
                previous = len(roots)
                roots.update(r[0] for r in rows)
                changed = len(roots) != previous
            return roots
        return self._read(query)

    def is_artifact_reachable(self, artifact_id: str) -> bool:
        if artifact_id in self.reachable_artifact_ids():
            return True
        artifact = self.get_artifact(artifact_id)
        return bool(artifact and self.is_content_hash_reachable(artifact["content_hash"]))

    def reachable_content_hashes(self) -> set[str]:
        """Return CAS blobs retained by any reachable semantic Artifact."""
        artifact_ids = self.reachable_artifact_ids()
        def query(c):
            retained = {
                row[0] for row in c.execute(
                    "SELECT DISTINCT content_hash FROM artifact_leases WHERE expires_at>?", (utc_now(),)
                )
            }
            if artifact_ids:
                placeholders = ",".join("?" for _ in artifact_ids)
                retained.update(row[0] for row in c.execute(
                    f"SELECT DISTINCT content_hash FROM artifacts WHERE artifact_id IN ({placeholders})",
                    tuple(artifact_ids),
                ))
            return retained
        return self._read(query)

    def is_content_hash_reachable(self, content_hash: str) -> bool:
        return content_hash in self.reachable_content_hashes()

    get_reachable_artifact_ids = reachable_artifact_ids


RuntimeRepository = RuntimeStore
