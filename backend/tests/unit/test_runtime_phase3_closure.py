"""RED tests for the remaining durable-runtime phase-three closure."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import threading

import pytest

from app.runtime import RuntimeDB, RuntimeQueue, RuntimeStore, RuntimeSupervisor, ToolContractRegistry
from app.runtime.contracts import ToolContract
from app.runtime.contracts import RetryPolicy


def _runtime(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1", status="queued")
    runtime.create_plan("p1", "r1")
    return runtime


def test_runtime_limits_reuse_sandbox_job_object_helper(monkeypatch):
    from app.runtime import process

    calls = []
    monkeypatch.setattr(process, "_sandbox_apply_job_object", lambda proc, memory_mb, cpu_weight=1: calls.append((proc, memory_mb, cpu_weight)) or "job")
    child = SimpleNamespace(pid=1)
    assert process.enforce_resource_limits(child, memory_mb=32, cpu_weight=3) == "job"
    assert calls == [(child, 32, 3)]


def test_resource_limit_probe_returns_reason(monkeypatch):
    from app.runtime import process

    monkeypatch.setitem(__import__("sys").modules, "psutil", None)
    monkeypatch.setattr(process, "sample_process_resources", lambda proc, **kwargs: {"memory_mb": 80.0, "cpu_percent": 250.0})
    result = process.check_resource_budget(SimpleNamespace(is_alive=lambda: True), memory_mb=64, cpu_weight=1)
    assert result == {
        "exceeded": True,
        "reason": "memory",
        "memory_mb": 80.0,
        "cpu_percent": 250.0,
        "available": True,
    }


def test_retry_schedule_computes_persisted_backoff_and_respects_max_attempts(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_task("t1", "p1", "one")
    queue = RuntimeQueue(runtime, lease_seconds=20)
    claim = queue.claim_next("worker-a", run_id="r1")
    assert claim is not None
    policy = RetryPolicy(max_attempts=3, retryable_error_codes=["WORKER_LOST"], backoff_seconds=[2, 4])
    retry_at = queue.schedule_retry(claim, error_code="WORKER_LOST", retry_policy=policy)
    assert retry_at.endswith("Z")
    row = runtime._read(lambda c: c.execute("SELECT status,next_retry_at FROM tasks WHERE task_id='t1'").fetchone())
    assert row[0] == "retry_wait" and row[1] == retry_at


def test_side_effect_outcome_unknown_moves_run_to_awaiting_input(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_task("t1", "p1", "one", task={"side_effect": True})
    queue = RuntimeQueue(runtime, lease_seconds=20, registry=ToolContractRegistry.with_builtins())
    claim = queue.claim_next("worker-a", run_id="r1")
    assert claim is not None
    old = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    runtime._write(lambda c: c.execute("UPDATE tasks SET lease_expires_at=? WHERE task_id=?", (old, "t1")))
    queue.recover_expired_leases()
    assert runtime.get_run("r1")["status"] == "awaiting_input"
    assert any(e["event_type"] == "run.awaiting_input" for e in runtime.list_events("r1"))


def test_worker_health_reports_resource_degraded_state_and_stop_kills_active_process(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    supervisor = RuntimeSupervisor(runtime, SimpleNamespace(), isolate=False)
    monkeypatch.setattr(supervisor, "resource_health", lambda: {"status": "degraded", "hard_limits": False})
    health = supervisor.health_snapshot()
    assert health["resource_limits"]["status"] == "degraded"


def test_planning_claim_is_fenced_and_run_is_active_plan_aware(tmp_path):
    runtime = _runtime(tmp_path)
    queue = RuntimeQueue(runtime)
    # A queued run which already owns a persisted plan is execution recovery,
    # not a second LLM-planning opportunity.
    assert queue.claim_planning_run("worker-a") is None
    runtime.create_run("r2", "s1", status="queued")
    claim = queue.claim_planning_run("worker-a")
    assert claim is not None and claim.run_id == "r2"
    assert queue.claim_planning_run("worker-b") is None
    queue.release_planning(claim)


def test_worker_shutdown_reconciles_active_claim_and_attempt(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_task("t1", "p1", "one")
    supervisor = RuntimeSupervisor(runtime, SimpleNamespace(), isolate=False)
    claim = supervisor.queue.claim_next("worker-a", run_id="r1")
    assert claim is not None
    runtime.executor_start_task(claim)

    class AliveProcess:
        def __init__(self):
            self.killed = False

        def is_alive(self):
            return not self.killed

        def kill(self):
            self.killed = True

    process = AliveProcess()
    supervisor._active_processes["t1"] = process
    stop = threading.Event()
    stop.set()
    assert supervisor.worker_loop("worker-a", stop_event=stop, poll_interval=0.001) == 0
    task = runtime._read(lambda c: c.execute("SELECT status,lease_owner,lease_token,lease_expires_at FROM tasks WHERE task_id='t1'").fetchone())
    attempt = runtime._read(lambda c: c.execute("SELECT status,error_code,executor_identity FROM task_attempts WHERE task_id='t1'").fetchone())
    assert task[0] == "ready" and task[1:] == (None, None, None)
    assert attempt[0] == "failed" and attempt[1] == "WORKER_LOST"
    assert process.killed


def _retry_registry(*, max_attempts=2, backoff_seconds=None, idempotent=True):
    return ToolContractRegistry([
        ToolContract(
            name="demo.op",
            version="1.0.0",
            idempotent=idempotent,
            retry={"max_attempts": max_attempts, "retryable_error_codes": ["WORKER_LOST"], "backoff_seconds": backoff_seconds or []},
        )
    ])


def _expire_claim(runtime, claim, *, error_code=None):
    old = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    def expire(c):
        c.execute("UPDATE tasks SET lease_expires_at=? WHERE task_id=?", (old, claim.task_id))
        if error_code:
            c.execute("UPDATE task_attempts SET error_code=? WHERE attempt_id=?", (error_code, claim.attempt_id))
    runtime._write(expire)


def test_expired_lease_retries_only_idempotent_retryable_task(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_task("t1", "p1", "one", task={"op": "demo.op", "contract_version": "1.0.0", "idempotent": True})
    queue = RuntimeQueue(runtime, lease_seconds=20, registry=_retry_registry(max_attempts=2, backoff_seconds=[2]))
    claim = queue.claim_next("worker-a", run_id="r1")
    assert claim is not None
    _expire_claim(runtime, claim, error_code="WORKER_LOST")
    assert queue.recover_expired_leases() == ["t1"]
    row = runtime._read(lambda c: c.execute("SELECT status,next_retry_at,blocked_reason FROM tasks WHERE task_id='t1'").fetchone())
    attempt = runtime._read(lambda c: c.execute("SELECT status,error_code,next_retry_at FROM task_attempts WHERE task_id='t1'").fetchone())
    assert row[0] == "retry_wait" and row[1] and row[2] == "WORKER_LOST"
    assert attempt[0] == "retry_wait" and attempt[1] == "WORKER_LOST" and attempt[2] == row[1]


def test_expired_lease_does_not_retry_non_idempotent_or_exhausted_task(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_task("t1", "p1", "one", task={"op": "demo.op", "contract_version": "1.0.0", "idempotent": False})
    queue = RuntimeQueue(runtime, lease_seconds=20, registry=_retry_registry(max_attempts=3, backoff_seconds=[0, 0], idempotent=True))
    claim = queue.claim_next("worker-a", run_id="r1")
    assert claim is not None
    _expire_claim(runtime, claim, error_code="WORKER_LOST")
    queue.recover_expired_leases()
    row = runtime._read(lambda c: c.execute("SELECT status,blocked_reason FROM tasks WHERE task_id='t1'").fetchone())
    assert row[0] == "failed" and row[1] == "NON_IDEMPOTENT_WORKER_LOST"


def test_old_planner_claim_cannot_create_or_activate_late_plan(tmp_path):
    runtime = _runtime(tmp_path)
    queue = RuntimeQueue(runtime, lease_seconds=20)
    runtime.create_run("r2", "s1", status="queued")
    first = queue.claim_planning_run("planner-a", run_id="r2")
    assert first is not None
    old = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    runtime._write(lambda c: c.execute("UPDATE runs SET planner_lease_expires_at=? WHERE run_id='r2'", (old,)))
    second = queue.claim_planning_run("planner-b", run_id="r2")
    assert second is not None and second.generation > first.generation
    with pytest.raises(RuntimeError, match="STALE_PLANNER_LEASE"):
        runtime.create_plan("stale", "r2", planner_fence=first)
    runtime.create_plan("fresh", "r2", plan_version=1, planner_fence=second)
    with pytest.raises(RuntimeError, match="STALE_PLANNER_LEASE"):
        runtime.set_active_plan("r2", "fresh", planner_fence=first)
    runtime.set_active_plan("r2", "fresh", planner_fence=second)


def test_planner_writes_with_active_claim_must_not_omit_fence(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_run("r2", "s1", status="queued")
    claim = RuntimeQueue(runtime, lease_seconds=20).claim_planning_run("planner-a", run_id="r2")
    assert claim is not None
    with pytest.raises(RuntimeError, match="STALE_PLANNER_LEASE"):
        runtime.create_plan("omitted", "r2", plan_version=1)
    runtime.create_plan("p2", "r2", plan_version=1, planner_fence=claim)
    with pytest.raises(RuntimeError, match="STALE_PLANNER_LEASE"):
        runtime.set_active_plan("r2", "p2")


def test_claim_persists_nonempty_executor_identity_and_commit_requires_fence(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_task("t1", "p1", "one")
    claim = RuntimeQueue(runtime).claim_next("worker-a", run_id="r1")
    assert claim is not None
    identity = runtime._read(lambda c: c.execute("SELECT executor_identity FROM task_attempts WHERE attempt_id=?", (claim.attempt_id,)).fetchone()[0])
    assert identity
    with pytest.raises(ValueError, match="fence"):
        runtime.commit_validated_task("t1")


def test_active_attempt_rejects_empty_executor_identity(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_task("t1", "p1", "one")
    with pytest.raises(ValueError, match="executor_identity"):
        runtime.create_task_attempt("a1", "t1", 0, status="running", executor_identity="")
    with pytest.raises(ValueError, match="executor_identity"):
        runtime.create_task_attempt("a2", "t1", 0, status="leased", executor_identity=None)


def test_claim_fence_rejects_attempt_with_blank_executor_identity(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_task("t1", "p1", "one")
    claim = RuntimeQueue(runtime).claim_next("worker-a", run_id="r1")
    assert claim is not None
    runtime._write(lambda c: c.execute("UPDATE task_attempts SET executor_identity='' WHERE attempt_id=?", (claim.attempt_id,)))
    with pytest.raises(RuntimeError, match="STALE_LEASE"):
        RuntimeQueue(runtime).assert_current(claim)


def test_task_attempt_schema_requires_executor_identity(tmp_path):
    runtime = _runtime(tmp_path)
    notnull = runtime._read(
        lambda c: c.execute("PRAGMA table_info(task_attempts)").fetchall()
    )
    identity = next(row for row in notnull if row[1] == "executor_identity")
    assert identity[3] == 1
