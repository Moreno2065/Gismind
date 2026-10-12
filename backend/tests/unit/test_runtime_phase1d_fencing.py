from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone

from app.runtime import ArtifactStore, Compiler, PlannerDraft, PlannerStepDraft, RuntimeDB, RuntimeQueue, RuntimeStore, RuntimeSupervisor, ToolContractRegistry
from app.runtime.llm_planner import PlannerError


def _runtime(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one")
    runtime.create_task("t2", "p1", "two")
    return runtime


def test_fenced_task_status_cannot_target_a_different_task(tmp_path):
    runtime = _runtime(tmp_path)
    claim = RuntimeQueue(runtime).claim_next("worker-a", run_id="r1")
    assert claim is not None and claim.task_id == "t1"

    with pytest.raises(RuntimeError, match="STALE_LEASE"):
        runtime.update_task_status("t2", "leased", fence=claim)

    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='t2'").fetchone()[0]) == "ready"


def test_fenced_attempt_status_cannot_target_a_different_attempt(tmp_path):
    runtime = _runtime(tmp_path)
    queue = RuntimeQueue(runtime)
    first = queue.claim_next("worker-a", run_id="r1")
    second = queue.claim_next("worker-b", run_id="r1")
    assert first is not None and second is not None and first.task_id != second.task_id

    with pytest.raises(RuntimeError, match="STALE_LEASE"):
        runtime.update_task_attempt_status(second.attempt_id, "failed", fence=first)


def test_fenced_artifact_reference_cannot_claim_another_producer(tmp_path):
    runtime = _runtime(tmp_path)
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    source = artifacts.publish_staged(
        artifacts.stage_bytes(b"source"), origin="upload", artifact_type="File",
    )
    claim = RuntimeQueue(runtime).claim_next("worker-a", run_id="r1")
    assert claim is not None and claim.task_id == "t1"

    with pytest.raises(RuntimeError, match="STALE_LEASE"):
        runtime.attach_artifact_references(
            source["artifact_id"], output_name="out", producer_task_id="t2", fence=claim,
        )


def test_planner_fence_with_attempt_identity_fails_closed_without_column_error(tmp_path):
    runtime = _runtime(tmp_path)
    queue = RuntimeQueue(runtime)
    claim = queue.claim_planning_run("planner-a", run_id="r1")
    assert claim is None

    runtime.create_run("r2", "s1")
    claim = queue.claim_planning_run("planner-a", run_id="r2")
    assert claim is not None
    forged = {
        "run_id": claim.run_id,
        "owner": claim.owner,
        "token": claim.token,
        "generation": claim.generation,
        "attempt_id": "planner-attempt",
    }
    with pytest.raises(RuntimeError, match="STALE_PLANNER_LEASE"):
        runtime.set_active_plan("r2", "missing", planner_fence=forged)


def test_recovery_ignores_expired_claims_from_demoted_plan(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_plan("p2", "r1", plan_version=2)
    runtime.set_active_plan("r1", "p1")
    runtime.create_task("old", "p2", "old")

    runtime._write(lambda c: c.execute(
        "UPDATE tasks SET status='leased',lease_owner='worker-a',lease_token='old-token',lease_generation=1,lease_expires_at='2000-01-01T00:00:00Z' WHERE task_id='old'"
    ))
    assert RuntimeQueue(runtime).recover_expired_leases(run_id="r1") == []
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='old'").fetchone()[0]) == "leased"


def test_recovery_aggregates_failed_task_to_terminal_run_in_same_transaction(tmp_path):
    runtime = _runtime(tmp_path)
    runtime._write(lambda c: c.execute("DELETE FROM tasks WHERE plan_id='p1'"))
    runtime.create_task("fatal", "p1", "fatal", task={"idempotent": False})
    runtime.update_run_status("r1", "planning")
    runtime.update_run_status("r1", "ready")
    runtime.update_run_status("r1", "running")
    queue = RuntimeQueue(runtime)
    claim = queue.claim_next("worker-a", run_id="r1")
    assert claim is not None
    expired = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    runtime._write(lambda c: c.execute("UPDATE tasks SET lease_expires_at=? WHERE task_id=?", (expired, "fatal")))

    assert queue.recover_expired_leases(run_id="r1") == ["fatal"]
    assert runtime.get_run("r1")["status"] == "failed"


def test_recovery_distinguishes_prestart_expiry_from_started_worker_loss(tmp_path):
    runtime = _runtime(tmp_path)
    runtime._write(lambda c: c.execute("DELETE FROM tasks WHERE plan_id='p1'"))
    runtime.create_task("prestart", "p1", "prestart")
    queue = RuntimeQueue(runtime)
    claim = queue.claim_next("worker-a", run_id="r1")
    assert claim is not None
    expired = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    runtime._write(lambda c: c.execute("UPDATE tasks SET lease_expires_at=? WHERE task_id=?", (expired, "prestart")))

    queue.recover_expired_leases(run_id="r1")
    assert runtime._read(lambda c: c.execute("SELECT error_code FROM task_attempts WHERE attempt_id=?", (claim.attempt_id,)).fetchone()[0]) == "LEASE_EXPIRED"


def test_failed_planner_repair_call_is_terminalized(tmp_path):
    class BrokenRepairPlanner:
        def create_draft(self, request):
            return PlannerDraft(
                run_id=request.run_id, input_revision=request.input_revision,
                user_goal=request.user_goal,
                tasks=[PlannerStepDraft(task_key="bad", op="not.a.runtime.operation")],
            )

        def repair_draft(self, request, *, rejected_draft, diagnostic):
            raise PlannerError("LLM_REPAIR_FAILED", "repair provider unavailable")

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.add_input_revision("r1", 1, {"prompt": "分析", "input_artifact_ids": []})
    supervisor = RuntimeSupervisor(runtime, ArtifactStore(tmp_path / "data", runtime), planner=BrokenRepairPlanner(), isolate=False, autostart=False)

    assert supervisor.run_once("planner-a", run_id="r1") == {"status": "planning_failed", "run_id": "r1"}
    attempts = runtime._read(lambda c: [tuple(row) for row in c.execute(
        "SELECT phase,status FROM planner_attempts WHERE run_id='r1' ORDER BY attempt_no"
    ).fetchall()])
    assert attempts == [("initial", "failed"), ("repair", "failed")]


def test_store_cannot_enter_retry_wait_without_queue_decision(tmp_path):
    runtime = _runtime(tmp_path)
    claim = RuntimeQueue(runtime).claim_next("worker-a", run_id="r1")
    assert claim is not None
    runtime.update_task_status("t1", "running", fence=claim)

    with pytest.raises(ValueError, match="schedule_retry"):
        runtime.update_task_status("t1", "retry_wait", next_retry_at="2099-01-01T00:00:00Z")


def test_retry_decision_rejects_an_expired_claim(tmp_path):
    runtime = _runtime(tmp_path)
    queue = RuntimeQueue(runtime)
    claim = queue.claim_next("worker-a", run_id="r1")
    assert claim is not None
    expired = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    runtime._write(lambda c: c.execute("UPDATE tasks SET lease_expires_at=? WHERE task_id=?", (expired, claim.task_id)))

    with pytest.raises(RuntimeError, match="STALE_LEASE"):
        queue.schedule_retry(claim, error_code="WORKER_LOST", next_retry_at="2099-01-01T00:00:00Z")


def test_heartbeat_rejects_an_expired_claim_instead_of_reviving_it(tmp_path):
    runtime = _runtime(tmp_path)
    queue = RuntimeQueue(runtime)
    claim = queue.claim_next("worker-a", run_id="r1")
    assert claim is not None
    expired = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    runtime._write(lambda c: c.execute("UPDATE tasks SET lease_expires_at=? WHERE task_id=?", (expired, claim.task_id)))

    with pytest.raises(RuntimeError, match="STALE_LEASE"):
        queue.heartbeat(claim)


def test_supervisor_reserves_cancellation_before_terminating_children(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    supervisor = RuntimeSupervisor(runtime, ArtifactStore(tmp_path / "data", runtime), isolate=False, autostart=False)
    observed = []

    def terminate(run_id):
        observed.append(runtime.get_run(run_id)["status"])

    monkeypatch.setattr(supervisor, "terminate_run_processes", terminate)
    supervisor.cancel_run("r1")

    assert observed == ["cancelling"]
    assert runtime.get_run("r1")["status"] == "cancelled"


def test_duplicate_confirmation_replays_the_original_response(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.set_active_plan("r1", "p1")
    runtime._write(lambda c: c.execute("UPDATE tasks SET status='blocked' WHERE task_id='t1'"))
    runtime.create_task_confirmation("confirmation-1", "t1", action_hash="a" * 64)

    first = runtime.confirm_task(
        "r1", task_id="t1", action_hash="a" * 64,
        command_id="confirm-1", request_hash="b" * 64,
    )
    runtime._write(lambda c: c.execute("UPDATE runs SET run_version=run_version+1 WHERE run_id='r1'"))
    replay = runtime.confirm_task(
        "r1", task_id="t1", action_hash="a" * 64,
        command_id="confirm-1", request_hash="b" * 64,
    )

    assert replay == first


def test_compiler_rejects_an_empty_executable_plan(tmp_path):
    with pytest.raises(Exception, match="empty plan"):
        Compiler(ToolContractRegistry.with_builtins()).compile(
            PlannerDraft(run_id="r1", input_revision=1, user_goal="do work", tasks=[])
        )


def test_activating_a_new_plan_demotes_the_previous_active_plan(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.add_input_revision("r1", 1, {"prompt": "查询", "input_artifact_ids": []})
    queue = RuntimeQueue(runtime)
    first_claim = queue.claim_planning_run("planner-a", run_id="r1")
    assert first_claim is not None
    plan = Compiler(ToolContractRegistry.with_builtins()).compile(
        PlannerDraft(run_id="r1", input_revision=1, user_goal="查询", tasks=[
            PlannerStepDraft(task_key="geo", op="geo.geocode", params={"address": "南京"}),
        ])
    )
    runtime.activate_compiled_plan("r1", plan, planner_claim=first_claim)
    runtime._write(lambda c: c.execute(
        "UPDATE runs SET status='planning',planner_lease_owner='old',planner_lease_token='old',planner_lease_expires_at='2000-01-01T00:00:00Z' WHERE run_id='r1'"
    ))
    second_claim = queue.claim_planning_run("planner-b", run_id="r1")
    assert second_claim is not None
    runtime.activate_compiled_plan("r1", plan, planner_claim=second_claim)

    statuses = runtime._read(lambda c: [row[0] for row in c.execute(
        "SELECT status FROM plans WHERE run_id='r1' ORDER BY plan_version"
    ).fetchall()])
    assert statuses == ["superseded", "active"]
