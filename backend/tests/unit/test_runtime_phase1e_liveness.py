from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from app.runtime import ArtifactStore, RuntimeDB, RuntimeQueue, RuntimeStore, RuntimeSupervisor


def _runtime(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one", task={
        "idempotent": True,
        "retry_policy": {"max_attempts": 2, "retryable_error_codes": ["WORKER_LOST", "LEASE_EXPIRED"], "backoff_seconds": [0]},
    })
    return runtime


def _expired():
    return (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")


def test_fenced_task_start_marks_attempt_running_atomically(tmp_path):
    runtime = _runtime(tmp_path)
    queue = RuntimeQueue(runtime)
    claim = queue.claim_next("worker-a", run_id="r1")
    assert claim is not None

    runtime.executor_start_task(claim)

    state = runtime._read(lambda c: c.execute(
        "SELECT t.status,a.status FROM tasks t JOIN task_attempts a ON a.task_id=t.task_id WHERE t.task_id='t1'"
    ).fetchone())
    assert tuple(state) == ("running", "running")


def test_a_second_executor_cannot_start_the_same_claim(tmp_path):
    runtime = _runtime(tmp_path)
    claim = RuntimeQueue(runtime).claim_next("api", run_id="r1")
    assert claim is not None

    runtime.executor_start_task(claim)
    with pytest.raises(RuntimeError, match="TASK_ALREADY_STARTED"):
        runtime.executor_start_task(claim)

    attempt = runtime._read(lambda c: c.execute(
        "SELECT status FROM task_attempts WHERE attempt_id=?", (claim.attempt_id,)
    ).fetchone()[0])
    assert attempt == "running"


def test_stale_worker_cannot_start_reclaimed_task(tmp_path):
    runtime = _runtime(tmp_path)
    queue = RuntimeQueue(runtime, lease_seconds=20)
    old_claim = queue.claim_next("worker-a", run_id="r1")
    assert old_claim is not None
    runtime._write(lambda c: c.execute("UPDATE tasks SET lease_expires_at=? WHERE task_id='t1'", (_expired(),)))
    queue.recover_expired_leases()
    new_claim = queue.claim_next("worker-b", run_id="r1")
    assert new_claim is not None

    with pytest.raises(RuntimeError, match="STALE_LEASE"):
        runtime.executor_start_task(old_claim)
    assert runtime._read(lambda c: c.execute("SELECT status FROM task_attempts WHERE attempt_id=?", (old_claim.attempt_id,)).fetchone()[0]) == "failed"


@pytest.mark.parametrize("expiry", ["not-a-timestamp", _expired()])
def test_task_heartbeat_rejects_malformed_or_expired_claim_expiry(tmp_path, expiry):
    runtime = _runtime(tmp_path)
    queue = RuntimeQueue(runtime)
    claim = queue.claim_next("worker-a", run_id="r1")
    assert claim is not None

    with pytest.raises(RuntimeError, match="STALE_LEASE"):
        queue.heartbeat(replace(claim, lease_expires_at=expiry))


def test_task_heartbeat_and_release_require_active_attempt_identity(tmp_path):
    runtime = _runtime(tmp_path)
    queue = RuntimeQueue(runtime)
    claim = queue.claim_next("worker-a", run_id="r1")
    assert claim is not None
    forged = replace(claim, attempt_id="other-attempt")

    with pytest.raises(RuntimeError, match="STALE_LEASE"):
        queue.heartbeat(forged)
    with pytest.raises(RuntimeError, match="STALE_LEASE"):
        queue.release(forged)


def test_planner_heartbeat_rejects_malformed_or_expired_claim_expiry(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    queue = RuntimeQueue(runtime)
    claim = queue.claim_planning_run("planner-a", run_id="r1")
    assert claim is not None

    for expiry in ("bad", _expired()):
        with pytest.raises(RuntimeError, match="STALE_PLANNER_LEASE"):
            queue.heartbeat_planning(replace(claim, lease_expires_at=expiry))


def test_restart_cancel_with_unresolvable_started_process_is_indeterminate(tmp_path):
    runtime = _runtime(tmp_path)
    queue = RuntimeQueue(runtime)
    claim = queue.claim_next("worker-a", run_id="r1")
    assert claim is not None
    runtime._write(lambda c: c.execute(
        "UPDATE tasks SET status='running' WHERE task_id='t1'"
    ))
    runtime._write(lambda c: c.execute(
        "UPDATE task_attempts SET status='running',executor_identity=? WHERE attempt_id=?",
        (json.dumps({"pid": 999999}), claim.attempt_id),
    ))

    restarted = RuntimeSupervisor(runtime, ArtifactStore(tmp_path / "data", runtime), autostart=False)
    restarted.cancel_run("r1")

    assert runtime.get_run("r1")["status"] == "awaiting_input"
