from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.runtime import ArtifactStore, RuntimeDB, RuntimeQueue, RuntimeStore, RuntimeSupervisor


def _runtime(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one")
    return runtime


def test_http_cancel_pauses_when_child_termination_is_unresolved(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    supervisor = RuntimeSupervisor(runtime, artifacts, isolate=False, autostart=False)
    claim = RuntimeQueue(runtime).claim_next("worker-a", run_id="r1")
    assert claim is not None
    observed = []

    def terminate(run_id):
        observed.append(runtime._read(lambda c: c.execute(
            "SELECT status,lease_owner,lease_token FROM tasks WHERE task_id='t1'"
        ).fetchone()))
        return ["t1"]

    monkeypatch.setattr(supervisor, "terminate_run_processes", terminate)
    app = create_app(runtime_store=runtime, artifact_store=artifacts, runtime_supervisor=supervisor)

    with TestClient(app) as client:
        response = client.post(
            "/api/runs/r1/cancel",
            json={"command_id": "cancel-1", "expected_run_version": 1},
        )

    assert response.status_code == 202
    assert response.json()["status"] == "awaiting_input"
    assert runtime.get_run("r1")["status"] == "awaiting_input"
    assert tuple(observed[0])[:2] == ("leased", "worker-a")


def test_worker_startup_converges_persisted_cancelling_run(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    queue = RuntimeQueue(runtime)
    claim = queue.claim_next("worker-a", run_id="r1")
    assert claim is not None
    runtime.apply_cancel_command(
        "r1", command_id="cancel-1", request_hash="a" * 64,
        finalize=False, defer_active=True,
    )
    supervisor = RuntimeSupervisor(runtime, artifacts, isolate=False, autostart=False)
    monkeypatch.setattr(supervisor, "terminate_run_processes", lambda run_id: [])

    supervisor.worker_loop("worker-b", max_tasks=0)

    assert runtime.get_run("r1")["status"] == "cancelled"


def test_worker_startup_pauses_persisted_cancelling_when_kill_unresolved(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    claim = RuntimeQueue(runtime).claim_next("worker-a", run_id="r1")
    assert claim is not None
    runtime.apply_cancel_command(
        "r1", command_id="cancel-1", request_hash="a" * 64,
        finalize=False, defer_active=True,
    )
    supervisor = RuntimeSupervisor(runtime, artifacts, isolate=False, autostart=False)
    monkeypatch.setattr(supervisor, "terminate_run_processes", lambda run_id: ["t1"])

    supervisor.start("worker-b")
    supervisor.stop(timeout=5)

    assert runtime.get_run("r1")["status"] == "awaiting_input"


def test_live_task_lease_requires_fence_for_status_mutation(tmp_path):
    runtime = _runtime(tmp_path)
    claim = RuntimeQueue(runtime).claim_next("worker-a", run_id="r1")
    assert claim is not None

    with pytest.raises(RuntimeError, match="STALE_LEASE"):
        runtime.update_task_status("t1", "running")


def test_live_task_attempt_requires_fence_for_status_mutation(tmp_path):
    runtime = _runtime(tmp_path)
    claim = RuntimeQueue(runtime).claim_next("worker-a", run_id="r1")
    assert claim is not None

    with pytest.raises(RuntimeError, match="STALE_LEASE"):
        runtime.update_task_attempt_status(claim.attempt_id, "running")


def test_live_task_lease_blocks_unfenced_run_status_mutation(tmp_path):
    runtime = _runtime(tmp_path)
    claim = RuntimeQueue(runtime).claim_next("worker-a", run_id="r1")
    assert claim is not None

    with pytest.raises(RuntimeError, match="STALE_LEASE"):
        runtime.update_run_status("r1", "planning")


def test_persisted_process_rejects_nonfinite_creation_time():
    process, safe = RuntimeSupervisor._persisted_process(
        json.dumps({"pid": os.getpid() + 1000000, "create_time": "NaN"})
    )

    assert process is None
    assert safe is False


def test_persisted_process_rejects_creation_time_drift(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    class FakeProcess:
        def create_time(self):
            return 100.0

    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(Process=lambda pid: FakeProcess()))
    process, safe = RuntimeSupervisor._persisted_process(
        json.dumps({"pid": os.getpid() + 1000000, "create_time": 100.1})
    )

    assert process is None
    assert safe is False


def test_attempt_status_obeys_legal_transition_table(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_task_attempt("a1", "t1", 0, status="queued")

    with pytest.raises(ValueError, match="illegal task attempt status transition"):
        runtime.update_task_attempt_status("a1", "succeeded")


def test_queue_claim_success_closes_attempt_inside_result_transaction(tmp_path, monkeypatch):
    from app.runtime import Compiler, PlannerDraft, PlannerStepDraft, ToolContractRegistry

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    source = artifacts.publish_staged(
        artifacts.stage_bytes(b'{"type":"FeatureCollection","features":[{"type":"Feature","geometry":{"type":"Point","coordinates":[118.7,32.0]},"properties":{}}]}'),
        origin="upload", artifact_type="Vector", crs={"kind": "EPSG", "code": 4326}, schema={},
    )
    ref = {"artifact_id": source["artifact_id"], "type": "Vector", "crs": {"kind": "EPSG", "code": 4326}}
    plan = Compiler(ToolContractRegistry.with_builtins(), artifact_lookup=runtime.get_artifact).compile(
        PlannerDraft(run_id="r1", input_revision=1, user_goal="buffer", tasks=[
            PlannerStepDraft(task_key="buffer", op="vector.buffer", inputs={"vector": ref}, params={"distance_m": 1}),
        ])
    )
    runtime.create_plan("p1", "r1", plan=plan)
    runtime.create_task("buffer", "p1", "buffer", tool_name="vector.buffer")
    observed = []
    original = runtime.update_task_attempt_status

    def wrapped(*args, **kwargs):
        if len(args) > 1 and args[1] == "succeeded":
            observed.append(runtime.db.active_connection is not None)
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime, "update_task_attempt_status", wrapped)
    RuntimeSupervisor(runtime, artifacts, isolate=False).run_once("worker-a", run_id="r1")

    assert observed == [True]
