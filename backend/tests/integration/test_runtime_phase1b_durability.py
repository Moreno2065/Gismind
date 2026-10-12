"""Phase 1B regressions for durable retry, snapshots, and commands."""

from __future__ import annotations

import json

import pytest

from app.api.runtime import _snapshot
from app.runtime import (
    ArtifactStore,
    Compiler,
    PlannerDraft,
    PlannerStepDraft,
    RuntimeDB,
    RuntimeExecutor,
    RuntimeQueue,
    RuntimeStore,
    RuntimeSupervisor,
    ToolContractRegistry,
)
from app.runtime.contracts import PortContract, RetryPolicy, ToolContract


def _runtime(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    return runtime


def _retry_runtime(tmp_path):
    runtime = _runtime(tmp_path)
    registry = ToolContractRegistry([
        ToolContract(
            name="test.retry",
            version="1.0.0",
            output_ports={"out": PortContract(artifact_type="File")},
            retry_policy=RetryPolicy(max_attempts=2, retryable_error_codes=["TIMEOUT"], backoff_seconds=[0]),
        )
    ])
    plan = Compiler(registry).compile(PlannerDraft(
        run_id="r1", input_revision=1, user_goal="retry",
        tasks=[PlannerStepDraft(task_key="retry", op="test.retry")],
    ))
    runtime.create_plan("p1", "r1", plan=plan)
    runtime.create_task("retry", "p1", "retry", task=plan.tasks[0].model_dump(mode="json"))
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    return runtime, artifacts, registry, plan


def test_executor_performs_one_handler_call_and_never_schedules_retry(tmp_path, monkeypatch):
    runtime, artifacts, registry, plan = _retry_runtime(tmp_path)
    queue = RuntimeQueue(runtime, lease_seconds=20)
    claim = queue.claim_next("worker-a", run_id="r1")
    assert claim is not None
    calls = []

    def fail_once(*_args, **_kwargs):
        calls.append(1)
        raise TimeoutError("synthetic timeout")

    monkeypatch.setattr(RuntimeExecutor, "_invoke_handler", fail_once)
    with pytest.raises(TimeoutError):
        RuntimeExecutor(runtime, artifacts, registry).execute(
            plan.tasks[0], run_id="r1", task_id=claim.task_id, claim=claim
        )

    assert len(calls) == 1
    assert runtime.get_run("r1")["status"] == "running"
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='retry'").fetchone()[0]) == "running"
    assert not any(event["event_type"] == "task.retry_wait" for event in runtime.list_events("r1"))


def test_retry_limit_uses_persisted_attempts_and_finishes_run(tmp_path, monkeypatch):
    runtime, artifacts, registry, _plan = _retry_runtime(tmp_path)
    supervisor = RuntimeSupervisor(runtime, artifacts, registry=registry, isolate=False)
    calls = []

    def always_timeout(*_args, **_kwargs):
        calls.append(1)
        raise TimeoutError("synthetic timeout")

    monkeypatch.setattr(RuntimeExecutor, "_invoke_handler", always_timeout)
    first = supervisor.run_once("worker-a", run_id="r1")
    assert first and first["status"] == "retry_wait"
    assert len(calls) == 1
    runtime._write(lambda c: c.execute("UPDATE tasks SET next_retry_at='2000-01-01T00:00:00Z' WHERE task_id='retry'"))
    with pytest.raises(TimeoutError):
        supervisor.run_once("worker-a", run_id="r1")

    assert len(calls) == 2
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='retry'").fetchone()[0]) == "failed"
    assert runtime.get_run("r1")["status"] == "failed"
    assert runtime._read(lambda c: c.execute("SELECT COUNT(*) FROM task_attempts WHERE task_id='retry'").fetchone()[0]) == 2


def test_snapshot_does_not_pair_old_run_with_new_event_cursor(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    runtime.update_run_status("r1", "planning")
    original_read = runtime._read
    injected = {}
    calls = [0]

    def read_with_race(fn):
        calls[0] += 1
        value = original_read(fn)
        if calls[0] == 1:
            runtime.update_run_status("r1", "failed")
            injected["event_id"] = runtime.list_events("r1")[-1]["event_id"]
        return value

    monkeypatch.setattr(runtime, "_read", read_with_race)
    snapshot = _snapshot(runtime, "r1")

    assert snapshot is not None
    assert calls[0] == 0
    assert snapshot["status"] == "planning"


def _awaiting(runtime):
    runtime.create_run("awaiting", "s1")
    runtime.add_input_revision("awaiting", 1, {"prompt": "need CRS", "input_artifact_ids": []})
    runtime.update_run_status("awaiting", "planning")
    runtime.append_event("awaiting", "run.awaiting_input", {
        "code": "CRS_REQUIRED", "patch_schema": {"type": "object", "required": ["crs"]},
    })
    runtime.update_run_status("awaiting", "awaiting_input")


def test_resume_command_rolls_back_command_revision_and_events_on_failure(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    _awaiting(runtime)
    original = runtime._append_event_conn

    def fail_after_mutation(conn, run_id, event_type, payload):
        if event_type == "run.resumed":
            raise RuntimeError("injected write failure")
        return original(conn, run_id, event_type, payload)

    monkeypatch.setattr(runtime, "_append_event_conn", fail_after_mutation)
    with pytest.raises(RuntimeError, match="injected write failure"):
        runtime.apply_resume_command(
            "awaiting", command_id="resume-1", request_hash="a" * 64,
            patch={"crs": "EPSG:4326"}, expected_run_version=runtime.get_run("awaiting")["run_version"],
        )

    assert runtime.get_run_command("resume-1") is None
    assert runtime.get_input_revision("awaiting")["revision"] == 1
    assert runtime.get_run("awaiting")["status"] == "awaiting_input"
    assert not any(e["event_type"] == "run.resumed" for e in runtime.list_events("awaiting"))


def test_cancel_command_rolls_back_command_status_and_events_on_failure(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    original = runtime._append_event_conn

    def fail_terminal(conn, run_id, event_type, payload):
        if event_type == "run.cancelled":
            raise RuntimeError("injected cancel failure")
        return original(conn, run_id, event_type, payload)

    monkeypatch.setattr(runtime, "_append_event_conn", fail_terminal)
    with pytest.raises(RuntimeError, match="injected cancel failure"):
        runtime.apply_cancel_command(
            "r1", command_id="cancel-1", request_hash="b" * 64,
            expected_run_version=runtime.get_run("r1")["run_version"],
        )

    assert runtime.get_run_command("cancel-1") is None
    assert runtime.get_run("r1")["status"] == "queued"
    assert not any(e["event_type"] == "run.cancelled" for e in runtime.list_events("r1"))
