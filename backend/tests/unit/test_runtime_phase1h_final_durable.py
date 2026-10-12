from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

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
    runtime.apply_cancel_command("r1", command_id="cancel-1", request_hash="a" * 64, finalize=False, defer_active=True)
    runtime.mark_cancel_indeterminate("r1", command_id="cancel-1")
    return claim


def test_blocked_cancellation_decision_requires_explicit_retry_and_normal_resume_stays_paused(tmp_path):
    runtime = _runtime(tmp_path)
    _indeterminate(runtime)
    runtime._write(lambda c: c.execute(
        "UPDATE tasks SET status='blocked',blocked_reason='CANCELLATION_DECISION_REQUIRED' WHERE task_id='t1'"
    ))

    normal = runtime.apply_resume_command(
        "r1", command_id="resume-normal", request_hash="b" * 64,
        patch={}, expected_run_version=runtime.get_run("r1")["run_version"],
    )
    assert normal["status"] == "awaiting_input"
    assert runtime.get_run("r1")["status"] == "awaiting_input"
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='t1'").fetchone()[0]) == "blocked"

    retry = runtime.apply_resume_command(
        "r1", command_id="resume-retry", request_hash="c" * 64,
        patch={"task_decisions": {"t1": "retry"}},
        expected_run_version=runtime.get_run("r1")["run_version"],
    )
    assert retry["status"] == "queued"
    state = runtime._read(lambda c: c.execute(
        "SELECT t.status,a.status,a.error_code FROM tasks t JOIN task_attempts a ON a.task_id=t.task_id WHERE t.task_id='t1' ORDER BY a.started_at DESC LIMIT 1"
    ).fetchone())
    assert tuple(state) == ("ready", "failed", "CANCELLATION_OUTCOME_UNKNOWN")


def test_windows_fallback_requests_terminate_right_and_failed_termination_is_unresolved(monkeypatch):
    import app.runtime.queue as queue_module

    class Handle:
        def __init__(self, value, pid):
            self._handle = value
            self.pid = pid
            self.closed = False
            self.kill_failed = True

        def is_running(self):
            return True

        def children(self, recursive=False):
            return []

        def kill(self):
            raise OSError("TerminateProcess failed")

        def wait(self, timeout=None):
            raise AssertionError("must not classify failed kill as cancelled")

    monkeypatch.setitem(sys.modules, "psutil", None)
    monkeypatch.setattr(queue_module.os, "name", "nt")
    monkeypatch.setattr(queue_module, "_process_creation_time", lambda pid: 123.5)
    monkeypatch.setattr(queue_module._WindowsProcessHandle, "open", staticmethod(lambda pid: Handle(10, pid)))
    supervisor = object.__new__(RuntimeSupervisor)
    process, safe = supervisor._persisted_process(json.dumps({"pid": 4242, "create_time": 123.5}))
    assert safe is True
    with pytest.raises(OSError, match="TerminateProcess failed"):
        process.kill()


def test_failed_persisted_process_termination_stays_unresolved(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    claim = RuntimeQueue(runtime).claim_next("worker-a", run_id="r1")
    assert claim is not None
    runtime.executor_start_task(claim)

    class Process:
        def children(self, recursive=False):
            return []

        def kill(self):
            raise OSError("TerminateProcess failed")

        def is_running(self):
            return True

    supervisor = RuntimeSupervisor(runtime, ArtifactStore(tmp_path / "data", runtime), isolate=False, autostart=False)
    monkeypatch.setattr(supervisor, "_persisted_process", staticmethod(lambda identity: (Process(), True)))
    assert supervisor.terminate_run_processes("r1") == ["t1"]


def test_cancel_replay_reads_current_terminal_run_projection(tmp_path):
    from app.main import create_app

    runtime = _runtime(tmp_path)
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    supervisor = RuntimeSupervisor(runtime, artifacts, isolate=False, autostart=False)
    request = {"command_id": "cancel-1", "expected_run_version": 1}
    import hashlib
    digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    runtime.apply_cancel_command("r1", command_id="cancel-1", request_hash=digest, finalize=False, defer_active=True)
    runtime.finalize_cancel_command("r1", command_id="cancel-1")
    runtime._write(lambda c: c.execute(
        "UPDATE run_commands SET payload_json=? WHERE command_id='cancel-1'",
        (json.dumps({"request_hash": digest, "response": {"run_id": "r1", "status": "cancelling", "run_version": 2}}),),
    ))
    app = create_app(runtime_store=runtime, artifact_store=artifacts, runtime_supervisor=supervisor)
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        response = client.post("/api/runs/r1/cancel", json=request)
    assert response.status_code == 202
    assert response.json()["status"] == "cancelled"
    assert response.json()["run_version"] == runtime.get_run("r1")["run_version"]


def test_resource_budget_uses_injected_sampler_without_psutil(monkeypatch):
    from app.runtime import process

    monkeypatch.setitem(sys.modules, "psutil", None)
    sample = lambda proc, **kwargs: {"memory_mb": 80.0, "cpu_percent": 1.0}
    result = process.check_resource_budget(SimpleNamespace(is_alive=lambda: True), memory_mb=64, cpu_weight=1, sampler=sample)
    assert result["exceeded"] is True
    assert result["reason"] == "memory"
    assert result["available"] is True


def test_unfenced_artifact_write_rejects_expired_active_task_before_recovery(tmp_path):
    runtime = _runtime(tmp_path)
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    claim = RuntimeQueue(runtime).claim_next("worker-a", run_id="r1")
    assert claim is not None
    runtime._write(lambda c: c.execute("UPDATE tasks SET lease_expires_at='2000-01-01T00:00:00Z' WHERE task_id='t1'"))
    staged = artifacts.stage_bytes(b"output")
    with pytest.raises(RuntimeError, match="STALE_LEASE"):
        artifacts.publish_staged(staged, origin="task", producer_task_id="t1", fence=None)
