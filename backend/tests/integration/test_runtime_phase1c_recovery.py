from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading

import pytest

from app.api.runtime import _snapshot
from app.runtime import RuntimeDB, RuntimeQueue, RuntimeStore, RuntimeSupervisor, ToolContractRegistry
from app.runtime.contracts import RetryPolicy, ToolContract


def _runtime(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    return runtime


def _expired():
    return (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")


def test_recovery_uses_frozen_task_policy_without_live_registry(tmp_path):
    runtime = _runtime(tmp_path)
    persisted = {
        "task_key": "demo", "op": "demo.op", "contract_version": "1.0.0", "origin": "compiler",
        "idempotent": True,
        "retry_policy": {"max_attempts": 2, "retryable_error_codes": ["WORKER_LOST"], "backoff_seconds": [2]},
    }
    runtime.create_task("t1", "p1", "demo", task=persisted)
    queue = RuntimeQueue(runtime, lease_seconds=20, registry=ToolContractRegistry([
        ToolContract(name="demo.op", version="1.0.0", idempotent=False, retry_policy=RetryPolicy(max_attempts=1)),
    ]))
    claim = queue.claim_next("worker-a", run_id="r1")
    assert claim is not None
    runtime._write(lambda c: c.execute("UPDATE tasks SET lease_expires_at=?, blocked_reason=NULL WHERE task_id=?", (_expired(), "t1")))

    assert queue.recover_expired_leases() == ["t1"]
    row = runtime._read(lambda c: c.execute("SELECT status,next_retry_at FROM tasks WHERE task_id='t1'").fetchone())
    assert row[0] == "retry_wait"
    assert row[1] is not None


def test_recovery_transitions_only_expired_attempt_and_preserves_history(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_task("t1", "p1", "demo")
    queue = RuntimeQueue(runtime, lease_seconds=20)
    claim = queue.claim_next("worker-a", run_id="r1")
    assert claim is not None
    # A historical failed attempt must not be reopened as the recovered one.
    runtime._write(lambda c: c.execute(
        "INSERT INTO task_attempts(attempt_id,task_id,attempt_no,status,lease_generation,started_at,finished_at,executor_identity) VALUES (?,?,?,?,?,?,?,?)",
        ("history", "t1", 0, "failed", 0, "1999-01-01T00:00:00Z", "1999-01-01T00:00:01Z", "old-worker"),
    ))
    runtime._write(lambda c: c.execute("UPDATE tasks SET lease_expires_at=? WHERE task_id=?", (_expired(), "t1")))

    queue.recover_expired_leases()
    rows = runtime._read(lambda c: c.execute("SELECT attempt_id,status FROM task_attempts WHERE task_id=? ORDER BY attempt_id", ("t1",)).fetchall())
    assert [(row[0], row[1]) for row in rows] == [("history", "failed"), (claim.attempt_id, "failed")]


def test_retry_timestamp_requires_full_utc_iso_instant(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_task("t1", "p1", "demo")
    queue = RuntimeQueue(runtime, lease_seconds=20)
    claim = queue.claim_next("worker-a", run_id="r1")
    assert claim is not None
    with pytest.raises(ValueError, match="UTC RFC3339"):
        queue.schedule_retry(claim, error_code="WORKER_LOST", next_retry_at="tomorrowZ")


def test_snapshot_projects_active_plan_only_and_clears_old_diagnostic(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_plan("p2", "r1", plan_version=2)
    runtime.set_active_plan("r1", "p1")
    runtime.create_task("old", "p2", "old")
    runtime.create_task("new", "p1", "new")
    runtime.append_event("r1", "run.awaiting_input", {"diagnostic": {"code": "OLD"}, "patch_schema": {"type": "object"}})
    runtime.update_run_status("r1", "planning")
    runtime.update_run_status("r1", "ready")

    snapshot = _snapshot(runtime, "r1")
    assert [task["task_id"] for task in snapshot["tasks"]] == ["new"]
    assert snapshot["awaiting_input"] is None
    assert snapshot["diagnostic"] is None
    assert snapshot["patch_schema"] is None


def test_create_run_with_input_is_atomic_for_concurrent_same_client_request(tmp_path):
    runtime = _runtime(tmp_path)
    results = []
    errors = []

    def create(run_id):
        try:
            results.append(runtime.create_run_with_input(
                run_id, "s1", message="hello", input_artifact_ids=[],
                command_id=f"cmd-{run_id}", idempotency_key="same-client",
                command_payload={"request_hash": "a" * 64, "response": {"run_id": run_id}},
            ))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=create, args=(f"run-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len(results) == 2
    assert results[0]["run"]["run_id"] == results[1]["run"]["run_id"]
    assert runtime._read(lambda c: c.execute("SELECT COUNT(*) FROM runs WHERE session_id='s1'").fetchone()[0]) == 2


def test_duplicate_cancel_replays_stored_response(tmp_path):
    runtime = _runtime(tmp_path)
    first = runtime.apply_cancel_command("r1", command_id="cancel-1", request_hash="b" * 64)
    runtime._write(lambda c: c.execute("UPDATE runs SET run_version=run_version+1 WHERE run_id='r1'"))
    replay = runtime.apply_cancel_command("r1", command_id="cancel-1", request_hash="b" * 64)
    assert replay == first


def test_worker_shutdown_recovery_honors_frozen_retry_policy(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_task("t1", "p1", "demo", task={
        "op": "demo.op", "contract_version": "1.0.0", "origin": "compiler",
        "idempotent": True,
        "retry_policy": {"max_attempts": 2, "retryable_error_codes": ["WORKER_LOST"], "backoff_seconds": [2]},
    })
    supervisor = RuntimeSupervisor(runtime, object(), isolate=False, autostart=False)
    claim = supervisor.queue.claim_next("worker-a", run_id="r1")
    assert claim is not None
    stop = threading.Event()
    stop.set()

    supervisor.worker_loop("worker-a", run_id="r1", stop_event=stop, poll_interval=0.001)
    task = runtime._read(lambda c: c.execute("SELECT status,next_retry_at FROM tasks WHERE task_id='t1'").fetchone())
    assert task[0] == "retry_wait" and task[1]
