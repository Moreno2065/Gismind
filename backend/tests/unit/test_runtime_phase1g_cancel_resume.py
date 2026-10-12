from __future__ import annotations

import json
import math
import os

import pytest
from fastapi.testclient import TestClient

from app.runtime import ArtifactStore, RuntimeDB, RuntimeQueue, RuntimeStore, RuntimeSupervisor


def _runtime(tmp_path, *, task=None):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one", task=task or {
        "idempotent": True,
        "side_effect": False,
        "retry_policy": {"max_attempts": 2, "retryable_error_codes": ["CANCELLATION_OUTCOME_UNKNOWN"], "backoff_seconds": [0]},
    })
    return runtime


def _indeterminate(runtime):
    claim = RuntimeQueue(runtime).claim_next("worker-a", run_id="r1")
    assert claim is not None
    runtime.apply_cancel_command(
        "r1", command_id="cancel-1", request_hash="a" * 64,
        finalize=False, defer_active=True,
    )
    runtime.mark_cancel_indeterminate("r1", command_id="cancel-1")
    return claim


def test_resume_requeues_safe_indeterminate_task_and_terminalizes_prior_attempt(tmp_path):
    runtime = _runtime(tmp_path)
    prior = _indeterminate(runtime)

    result = runtime.apply_resume_command(
        "r1", command_id="resume-1", request_hash="b" * 64,
        patch={}, expected_run_version=runtime.get_run("r1")["run_version"],
    )

    assert result["status"] == "queued"
    state = runtime._read(lambda c: c.execute(
        "SELECT t.status,a.status,a.error_code,t.lease_owner "
        "FROM tasks t JOIN task_attempts a ON a.task_id=t.task_id "
        "WHERE t.task_id='t1' AND a.attempt_id=?",
        (prior.attempt_id,),
    ).fetchone())
    assert tuple(state) == ("ready", "failed", "CANCELLATION_OUTCOME_UNKNOWN", None)
    assert RuntimeQueue(runtime).claim_next("worker-b", run_id="r1") is not None


def test_resume_keeps_side_effect_task_awaiting_input_until_explicit_decision(tmp_path):
    runtime = _runtime(tmp_path, task={
        "idempotent": False,
        "side_effect": True,
        "retry_policy": {"max_attempts": 2, "retryable_error_codes": ["CANCELLATION_OUTCOME_UNKNOWN"], "backoff_seconds": [0]},
    })
    prior = _indeterminate(runtime)

    result = runtime.apply_resume_command(
        "r1", command_id="resume-1", request_hash="b" * 64,
        patch={}, expected_run_version=runtime.get_run("r1")["run_version"],
    )

    assert result["status"] == "awaiting_input"
    state = runtime._read(lambda c: c.execute(
        "SELECT t.status,a.status,a.error_code,t.lease_owner FROM tasks t "
        "JOIN task_attempts a ON a.task_id=t.task_id WHERE t.task_id='t1' AND a.attempt_id=?",
        (prior.attempt_id,),
    ).fetchone())
    assert tuple(state) == ("blocked", "failed", "CANCELLATION_OUTCOME_UNKNOWN", None)


@pytest.mark.parametrize("pid", [True, False, 1.0, 1.5, float("nan"), float("inf"), -1, 0])
def test_persisted_process_rejects_non_strict_positive_integer_pid(pid):
    process, safe = RuntimeSupervisor._persisted_process(
        json.dumps({"pid": pid, "create_time": 1.0}, allow_nan=True)
    )
    assert process is None
    assert safe is False


@pytest.mark.parametrize("create_time", [True, "NaN", "Infinity", float("nan"), float("inf"), 0, -1])
def test_persisted_process_rejects_nonfinite_or_nonpositive_creation_time(create_time):
    process, safe = RuntimeSupervisor._persisted_process(
        json.dumps({"pid": os.getpid() + 1000000, "create_time": create_time}, allow_nan=True)
    )
    assert process is None
    assert safe is False


def test_persisted_process_uses_windows_fallback_without_psutil(monkeypatch):
    import app.runtime.queue as queue_module

    class FakeWindowsProcess:
        pid = 4242

    monkeypatch.setitem(__import__("sys").modules, "psutil", None)
    monkeypatch.setattr(queue_module.os, "name", "nt")
    monkeypatch.setattr(queue_module, "_process_creation_time", lambda pid: 123.5)
    monkeypatch.setattr(queue_module._WindowsProcessHandle, "open", staticmethod(lambda pid: FakeWindowsProcess()))

    process, safe = RuntimeSupervisor._persisted_process(json.dumps({"pid": 4242, "create_time": 123.5}))

    assert isinstance(process, FakeWindowsProcess)
    assert safe is True


def test_release_rejects_active_task_without_terminal_transition(tmp_path):
    runtime = _runtime(tmp_path)
    claim = RuntimeQueue(runtime).claim_next("worker-a", run_id="r1")
    assert claim is not None

    with pytest.raises(RuntimeError, match="TASK_NOT_TERMINAL"):
        RuntimeQueue(runtime).release(claim)


def test_retry_wait_attempt_can_only_be_entered_by_schedule_retry(tmp_path):
    runtime = _runtime(tmp_path)
    claim = RuntimeQueue(runtime).claim_next("worker-a", run_id="r1")
    assert claim is not None

    with pytest.raises(ValueError, match="retry_wait must be entered via queue.schedule_retry"):
        runtime.update_task_attempt_status(claim.attempt_id, "retry_wait", fence=claim)


def test_cancel_api_rejects_internal_control_parameters(tmp_path):
    from app.main import create_app

    runtime = _runtime(tmp_path)
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    supervisor = RuntimeSupervisor(runtime, artifacts, isolate=False, autostart=False)
    app = create_app(runtime_store=runtime, artifact_store=artifacts, runtime_supervisor=supervisor)

    with TestClient(app) as client:
        response = client.post("/api/runs/r1/cancel", json={
            "command_id": "cancel-1", "expected_run_version": 1, "finalize": True,
        })

    assert response.status_code == 422


def test_resume_api_keeps_unresolved_side_effect_paused_and_exposes_decision_contract(tmp_path, monkeypatch):
    from app.main import create_app

    runtime = _runtime(tmp_path, task={
        "idempotent": False,
        "side_effect": True,
        "retry_policy": {"max_attempts": 2, "retryable_error_codes": ["CANCELLATION_OUTCOME_UNKNOWN"], "backoff_seconds": [0]},
    })
    _indeterminate(runtime)
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    supervisor = RuntimeSupervisor(runtime, artifacts, isolate=False, autostart=False)
    enqueued = []
    monkeypatch.setattr(supervisor, "enqueue_run", lambda run_id: enqueued.append(run_id))
    app = create_app(runtime_store=runtime, artifact_store=artifacts, runtime_supervisor=supervisor)

    with TestClient(app) as client:
        response = client.post("/api/runs/r1/resume", json={
            "command_id": "resume-1", "expected_run_version": runtime.get_run("r1")["run_version"], "patch": {},
        })

    assert response.status_code == 202
    assert response.json()["status"] == "awaiting_input"
    assert response.json()["requires_user_decision"] is True
    assert response.json()["pending_task_ids"] == ["t1"]
    assert enqueued == []


def test_cancelling_recovery_tolerates_concurrent_convergence(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    runtime.apply_cancel_command(
        "r1", command_id="cancel-1", request_hash="a" * 64,
        finalize=False, defer_active=True,
    )
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    supervisor = RuntimeSupervisor(runtime, artifacts, isolate=False, autostart=False)
    monkeypatch.setattr(supervisor, "terminate_run_processes", lambda run_id: [])
    monkeypatch.setattr(runtime, "finalize_cancel_command", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("RUN_NOT_CANCELLING")))
    assert supervisor.recover_cancelling_runs() == ["r1"]
    assert runtime.get_run("r1")["status"] == "cancelling"


def test_active_task_requires_fence_for_task_output_and_lineage_attachment(tmp_path):
    runtime = _runtime(tmp_path)
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    parent = artifacts.publish_staged(
        artifacts.stage_bytes(b"parent"), origin="upload", artifact_type="File",
    )
    produced = artifacts.publish_staged(
        artifacts.stage_bytes(b"produced"), origin="task", producer_task_id="t1", artifact_type="File",
    )
    claim = RuntimeQueue(runtime).claim_next("worker-a", run_id="r1")
    assert claim is not None

    with pytest.raises(RuntimeError, match="STALE_LEASE"):
        runtime.attach_task_output("t1", "output", produced["artifact_id"])
    with pytest.raises(RuntimeError, match="STALE_LEASE"):
        runtime.attach_artifact_references(produced["artifact_id"], parents=[parent["artifact_id"]])
