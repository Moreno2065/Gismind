"""Runtime-only durable queue/worker probes (no Dispatcher or Redis)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import threading
import json

import pytest

from app.runtime import ArtifactRef, ArtifactStore, Compiler, PlannerDraft, PlannerStepDraft, RuntimeDB, RuntimeQueue, RuntimeStore, RuntimeSupervisor, TaskOutputRef, ToolContractRegistry
from app.runtime.queue import _ipc_result_manifest


def test_isolated_worker_ipc_manifest_never_contains_handler_payload():
    artifact = {
        "artifact_id": "art_output", "type": "Vector", "validation_status": "valid",
        "content_hash": "a" * 64, "schema": {"fields": ["name"]},
    }
    result = {
        "artifacts": [artifact], "artifact": artifact,
        "result": {"status": "success", "data": "x" * (8 * 1024 * 1024)},
        "content_hash": artifact["content_hash"],
    }
    manifest = _ipc_result_manifest(result)
    assert "result" not in manifest
    assert manifest["artifact"]["artifact_id"] == "art_output"
    assert len(json.dumps(manifest)) < 4096


def _runtime(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    return runtime


def test_queue_claim_is_idempotent_and_fenced(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_task("t1", "p1", "one")
    queue = RuntimeQueue(runtime, lease_seconds=20)
    first = queue.claim_next("worker-a", run_id="r1")
    assert first is not None
    assert queue.claim_next("worker-b", run_id="r1") is None
    renewed = queue.heartbeat(first)
    assert renewed.lease_expires_at > first.lease_expires_at
    with pytest.raises(RuntimeError, match="STALE_LEASE"):
        queue.heartbeat(replace(first, token="stale-token"))


def test_queue_heartbeat_keeps_validating_claim_alive(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_task("t1", "p1", "one")
    queue = RuntimeQueue(runtime, lease_seconds=20)
    claim = queue.claim_next("worker-a", run_id="r1")
    assert claim is not None
    runtime.update_task_status("t1", "running", fence=claim)
    runtime.update_task_status("t1", "validating", fence=claim)

    renewed = queue.heartbeat(claim)

    assert renewed.lease_expires_at > claim.lease_expires_at
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='t1'").fetchone()[0]) == "validating"


def test_same_owner_claim_returns_existing_active_claim(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_task("t1", "p1", "one")
    queue = RuntimeQueue(runtime, lease_seconds=20)
    first = queue.claim_next("worker-a", run_id="r1")
    assert first is not None
    again = queue.claim_next("worker-a", run_id="r1")
    assert again == first
    assert runtime._read(lambda c: c.execute("SELECT COUNT(*) FROM task_attempts WHERE task_id='t1'").fetchone()[0]) == 1


def test_supervisor_closes_queue_attempt_after_executor_success(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    payload = b'{"type":"FeatureCollection","features":[{"type":"Feature","geometry":{"type":"Point","coordinates":[116.4,39.9]},"properties":{}}]}'
    source = artifacts.publish_staged(artifacts.stage_bytes(payload), origin="upload", artifact_type="Vector", crs={"kind": "EPSG", "code": 4326}, schema={})
    ref = {"artifact_id": source["artifact_id"], "type": "Vector", "crs": {"kind": "EPSG", "code": 4326}}
    draft = PlannerDraft(run_id="r1", input_revision=1, tasks=[PlannerStepDraft(task_key="buffer", op="vector.buffer", inputs={"vector": ref}, params={"distance_m": 1})])
    plan = Compiler(ToolContractRegistry.with_builtins(), artifact_lookup=runtime.get_artifact).compile(draft)
    runtime.create_plan("p1", "r1", plan=plan)
    runtime.create_task("buffer", "p1", "buffer", tool_name="vector.buffer")
    RuntimeSupervisor(runtime, artifacts).run_once("worker-a", run_id="r1")
    assert runtime._read(lambda c: c.execute("SELECT status FROM task_attempts WHERE task_id='buffer'").fetchone()[0]) == "succeeded"


def test_supervisor_isolated_worker_does_not_run_executor_in_parent(tmp_path, monkeypatch):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    payload = b'{"type":"FeatureCollection","features":[{"type":"Feature","geometry":{"type":"Point","coordinates":[116.4,39.9]},"properties":{}}]}'
    source = artifacts.publish_staged(artifacts.stage_bytes(payload), origin="upload", artifact_type="Vector", crs={"kind": "EPSG", "code": 4326}, schema={})
    ref = {"artifact_id": source["artifact_id"], "type": "Vector", "crs": {"kind": "EPSG", "code": 4326}}
    draft = PlannerDraft(run_id="r1", input_revision=1, tasks=[PlannerStepDraft(task_key="buffer", op="vector.buffer", inputs={"vector": ref}, params={"distance_m": 1})])
    plan = Compiler(ToolContractRegistry.with_builtins(), artifact_lookup=runtime.get_artifact).compile(draft)
    runtime.create_plan("p1", "r1", plan=plan)
    runtime.create_task("buffer", "p1", "buffer", tool_name="vector.buffer")
    from app.runtime.executor import RuntimeExecutor
    monkeypatch.setattr(RuntimeExecutor, "execute", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("parent executed GIS")))
    supervisor = RuntimeSupervisor(runtime, artifacts, isolate=True, heartbeat_interval=0.01)
    heartbeat_calls = []
    original_heartbeat = supervisor.queue.heartbeat
    monkeypatch.setattr(supervisor.queue, "heartbeat", lambda claim: (heartbeat_calls.append(claim.task_id), original_heartbeat(claim))[1])
    result = supervisor.run_once("worker-a", run_id="r1")
    assert result is not None and result["artifact"]["validation_status"] == "valid"
    assert heartbeat_calls


def test_expired_claim_recovers_to_ready_and_records_event(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_task("t1", "p1", "one")
    queue = RuntimeQueue(runtime, lease_seconds=20)
    claim = queue.claim_next("worker-a", run_id="r1")
    assert claim is not None
    old = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    runtime._write(lambda c: c.execute("UPDATE tasks SET lease_expires_at=? WHERE task_id=?", (old, "t1")))
    assert queue.recover_expired_leases() == ["t1"]
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='t1'").fetchone()[0]) == "ready"
    assert any(event["event_type"] == "task.lease_expired" for event in runtime.list_events("r1"))


def test_expired_worker_cannot_write_after_reclaim(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_task("t1", "p1", "one")
    queue = RuntimeQueue(runtime, lease_seconds=20)
    old_claim = queue.claim_next("worker-a", run_id="r1")
    assert old_claim is not None
    old = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    runtime._write(lambda c: c.execute("UPDATE tasks SET lease_expires_at=? WHERE task_id=?", (old, "t1")))
    queue.recover_expired_leases()
    new_claim = queue.claim_next("worker-b", run_id="r1")
    assert new_claim is not None and new_claim.lease_generation > old_claim.lease_generation
    with pytest.raises(RuntimeError, match="STALE_LEASE"):
        queue.assert_current(old_claim)


def test_executor_claim_fence_rejects_old_worker_before_commit(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_task("t1", "p1", "one")
    queue = RuntimeQueue(runtime, lease_seconds=20)
    old_claim = queue.claim_next("worker-a", run_id="r1")
    assert old_claim is not None
    old = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    runtime._write(lambda c: c.execute("UPDATE tasks SET lease_expires_at=? WHERE task_id=?", (old, "t1")))
    queue.recover_expired_leases()
    assert queue.claim_next("worker-b", run_id="r1") is not None
    from app.runtime.executor import RuntimeExecutor
    with pytest.raises(RuntimeError, match="STALE_LEASE"):
        RuntimeExecutor(runtime, ArtifactStore(tmp_path / "data", runtime)).assert_claim_current(old_claim)


def test_validating_lease_recovery_does_not_leave_task_validating(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_task("t1", "p1", "one", status="blocked")
    runtime.update_task_status("t1", "ready")
    claim = RuntimeQueue(runtime, lease_seconds=20).claim_next("worker-a", run_id="r1")
    assert claim is not None
    runtime.update_task_status("t1", "running", fence=claim)
    runtime.update_task_status("t1", "validating", fence=claim)
    old = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    runtime._write(lambda c: c.execute("UPDATE tasks SET lease_expires_at=? WHERE task_id=?", (old, "t1")))
    RuntimeQueue(runtime).recover_expired_leases()
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='t1'").fetchone()[0]) == "ready"


def test_queue_selects_only_ready_tasks_without_unmet_dependencies(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_task("up", "p1", "up")
    runtime.create_task("down", "p1", "down", status="blocked")
    runtime.add_task_dependency("down", "up")
    runtime.create_task_attempt("up-attempt", "up", 0)
    runtime.update_task_status("up", "leased")
    runtime.update_task_status("up", "running")
    runtime.update_task_status("up", "validating")
    runtime.update_task_attempt_status("up-attempt", "succeeded")
    runtime.update_task_status("up", "succeeded")
    if runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='down'").fetchone()[0]) != "ready":
        runtime.update_task_status("down", "ready")
    claim = RuntimeQueue(runtime).claim_next("worker-a", run_id="r1")
    assert claim is not None and claim.task_id == "down"


def test_upstream_success_automatically_unblocks_downstream(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_task("up", "p1", "up")
    runtime.create_task("down", "p1", "down", status="blocked")
    runtime.add_task_dependency("down", "up")
    runtime.create_task_attempt("up-attempt", "up", 0)
    runtime.update_task_status("up", "leased")
    runtime.update_task_status("up", "running")
    runtime.update_task_status("up", "validating")
    runtime.update_task_attempt_status("up-attempt", "succeeded")
    runtime.update_task_status("up", "succeeded")
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='down'").fetchone()[0]) == "ready"


def test_supervisor_run_once_executes_persisted_plan_and_completes_run(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    payload = b'{"type":"FeatureCollection","features":[{"type":"Feature","geometry":{"type":"Point","coordinates":[116.4,39.9]},"properties":{}}]}'
    source = artifacts.publish_staged(artifacts.stage_bytes(payload), origin="upload", artifact_type="Vector", crs={"kind": "EPSG", "code": 4326}, schema={})
    ref = {"artifact_id": source["artifact_id"], "type": "Vector", "crs": {"kind": "EPSG", "code": 4326}}
    draft = PlannerDraft(run_id="r1", input_revision=1, tasks=[PlannerStepDraft(task_key="buffer", op="vector.buffer", inputs={"vector": ref}, params={"distance_m": 1})])
    plan = Compiler(ToolContractRegistry.with_builtins(), artifact_lookup=runtime.get_artifact).compile(draft)
    runtime.create_plan("p1", "r1", plan=plan)
    runtime.create_task("buffer", "p1", "buffer", tool_name="vector.buffer")
    result = RuntimeSupervisor(runtime, artifacts).run_once("worker-a", run_id="r1")
    assert result is not None and result["artifact"]["validation_status"] == "valid"
    assert runtime.get_run("r1")["status"] == "completed"
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='buffer'").fetchone()[0]) == "succeeded"


def test_worker_keeps_running_and_marks_run_failed_after_a_task_business_error(tmp_path, monkeypatch):
    """A handler error is a durable task/run outcome, never a worker crash."""
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    payload = b'{"type":"FeatureCollection","features":[{"type":"Feature","geometry":{"type":"Point","coordinates":[116.4,39.9]},"properties":{}}]}'
    source = artifacts.publish_staged(
        artifacts.stage_bytes(payload), origin="upload", artifact_type="Vector",
        crs={"kind": "EPSG", "code": 4326}, schema={},
    )
    registry = ToolContractRegistry.with_builtins()
    plan = Compiler(registry, artifact_lookup=runtime.get_artifact).compile(
        PlannerDraft(
            run_id="r1", input_revision=1,
            tasks=[PlannerStepDraft(
                task_key="buffer", op="vector.buffer",
                inputs={"vector": ArtifactRef(
                    artifact_id=source["artifact_id"], type="Vector",
                    crs={"kind": "EPSG", "code": 4326},
                )},
                params={"distance_m": 1},
            )],
        )
    )
    runtime.create_plan("p1", "r1", plan=plan)
    runtime.create_task("buffer", "p1", "buffer", tool_name="vector.buffer")
    from app.runtime import tool_handlers
    monkeypatch.setattr(tool_handlers, "handle_vector_buffer", lambda **_kwargs: {"status": "error", "message": "synthetic GIS failure"})

    supervisor = RuntimeSupervisor(runtime, artifacts, registry=registry, isolate=False, autostart=False)

    assert supervisor.worker_loop("worker-a", run_id="r1", max_tasks=1, poll_interval=0.001) == 1
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='buffer'").fetchone()[0]) == "failed"
    assert runtime._read(lambda c: c.execute("SELECT status FROM task_attempts WHERE task_id='buffer'").fetchone()[0]) == "failed"
    assert runtime.get_run("r1")["status"] == "failed"
    assert any(event["event_type"] == "run.failed" for event in runtime.list_events("r1"))


def test_worker_loop_auto_unblocks_and_claims_real_buffer_to_map_chain(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    payload = b'{"type":"FeatureCollection","features":[{"type":"Feature","geometry":{"type":"Point","coordinates":[116.4,39.9]},"properties":{}}]}'
    source = artifacts.publish_staged(artifacts.stage_bytes(payload), origin="upload", artifact_type="Vector", crs={"kind": "EPSG", "code": 4326}, schema={})
    draft = PlannerDraft(
        run_id="r1", input_revision=1, tasks=[
            PlannerStepDraft(task_key="buffer", op="vector.buffer", inputs={"vector": ArtifactRef(artifact_id=source["artifact_id"], type="Vector", crs={"kind": "EPSG", "code": 4326})}, params={"distance_m": 1}),
            PlannerStepDraft(task_key="map", op="map.build", inputs={"vector": TaskOutputRef(task_key="buffer", port="vector")}),
        ],
    )
    registry = ToolContractRegistry.with_builtins()
    plan = Compiler(registry, artifact_lookup=runtime.get_artifact).compile(draft)
    runtime.create_plan("p1", "r1", plan=plan)
    runtime.create_task("buffer", "p1", "buffer", tool_name="vector.buffer")
    runtime.create_task("map", "p1", "map", tool_name="map.build", status="blocked")
    runtime.add_task_dependency("map", "buffer")
    runtime.bind_task_input("map-vector", "map", "vector", source_task_id="buffer", source_port="vector")
    supervisor = RuntimeSupervisor(runtime, artifacts, registry=registry, isolate=False)
    assert supervisor.worker_loop("worker-a", run_id="r1", max_tasks=2, poll_interval=0.001) == 2
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='map'").fetchone()[0]) == "succeeded"
    assert runtime._read(lambda c: c.execute("SELECT COUNT(*) FROM task_inputs WHERE task_id='map'").fetchone()[0]) == 1


def test_isolated_buffer_map_chain_is_stable_for_ten_rounds(tmp_path):
    """Probe watchdog, CPU sampling and teardown races across repeated runs."""
    for round_no in range(10):
        root = tmp_path / f"round-{round_no}"
        runtime = RuntimeStore(RuntimeDB(root / "runtime.db"))
        runtime.initialize()
        runtime.create_session("s1")
        run_id = f"r{round_no}"
        runtime.create_run(run_id, "s1")
        artifacts = ArtifactStore(root / "data", runtime)
        payload = b'{"type":"FeatureCollection","features":[{"type":"Feature","geometry":{"type":"Point","coordinates":[116.4,39.9]},"properties":{}}]}'
        source = artifacts.publish_staged(artifacts.stage_bytes(payload), origin="upload", artifact_type="Vector", crs={"kind": "EPSG", "code": 4326}, schema={})
        draft = PlannerDraft(
            run_id=run_id, input_revision=1, tasks=[
                PlannerStepDraft(task_key="buffer", op="vector.buffer", inputs={"vector": ArtifactRef(artifact_id=source["artifact_id"], type="Vector", crs={"kind": "EPSG", "code": 4326})}, params={"distance_m": 1}),
                PlannerStepDraft(task_key="map", op="map.build", inputs={"vector": TaskOutputRef(task_key="buffer", port="vector")}),
            ],
        )
        registry = ToolContractRegistry.with_builtins()
        plan = Compiler(registry, artifact_lookup=runtime.get_artifact).compile(draft)
        runtime.create_plan("p1", run_id, plan=plan)
        runtime.create_task("buffer", "p1", "buffer", tool_name="vector.buffer")
        runtime.create_task("map", "p1", "map", tool_name="map.build", status="blocked")
        runtime.add_task_dependency("map", "buffer")
        runtime.bind_task_input("map-vector", "map", "vector", source_task_id="buffer", source_port="vector")
        supervisor = RuntimeSupervisor(runtime, artifacts, registry=registry, isolate=True, heartbeat_interval=0.1)
        assert supervisor.worker_loop("worker-a", run_id=run_id, max_tasks=2, poll_interval=0.001) == 2
        statuses = runtime._read(lambda c: c.execute("SELECT task_id,status,lease_owner,lease_token FROM tasks ORDER BY task_id").fetchall())
        assert [(row[0], row[1]) for row in statuses] == [("buffer", "succeeded"), ("map", "succeeded")]
        assert all(row[2] is None and row[3] is None for row in statuses)


def test_cancel_run_cancels_queued_tasks(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_task("t1", "p1", "one")
    RuntimeSupervisor(runtime, ArtifactStore(tmp_path / "data", runtime)).cancel_run("r1")
    assert runtime.get_run("r1")["status"] == "cancelled"
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='t1'").fetchone()[0]) == "cancelled"


def test_cancel_run_records_command_and_cancel_events(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_task("t1", "p1", "one")
    RuntimeSupervisor(runtime, ArtifactStore(tmp_path / "data", runtime)).cancel_run("r1")
    event_types = [event["event_type"] for event in runtime.list_events("r1")]
    assert "run.cancel_requested" in event_types
    assert "run.cancelled" in event_types
    command = runtime._read(lambda c: c.execute("SELECT command_type FROM run_commands WHERE run_id='r1'").fetchone())
    assert command is not None and command[0] == "cancel"


def test_cancel_active_claim_cancels_attempt_and_releases_fence(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_task("t1", "p1", "one")
    queue = RuntimeQueue(runtime, lease_seconds=20)
    assert queue.claim_next("worker-a", run_id="r1") is not None
    RuntimeSupervisor(runtime, ArtifactStore(tmp_path / "data", runtime)).cancel_run("r1")
    row = runtime._read(lambda c: c.execute("SELECT status FROM task_attempts WHERE task_id='t1'").fetchone())
    task = runtime._read(lambda c: c.execute("SELECT status,lease_owner,lease_token FROM tasks WHERE task_id='t1'").fetchone())
    assert row[0] == "cancelled"
    assert task[0] == "cancelled" and task[1] is None and task[2] is None


def test_resident_worker_loop_honors_shutdown_signal_after_recovery(tmp_path):
    runtime = _runtime(tmp_path)
    supervisor = RuntimeSupervisor(runtime, ArtifactStore(tmp_path / "data", runtime))
    stop = threading.Event()
    stop.set()
    assert supervisor.worker_loop("worker-a", stop_event=stop, poll_interval=0.001) == 0


def test_queue_claim_uses_run_active_plan_only(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_plan("p2", "r1", plan_version=2)
    runtime.set_active_plan("r1", "p1")
    runtime.create_task("inactive", "p2", "inactive")
    assert RuntimeQueue(runtime).claim_next("worker-a", run_id="r1") is None


def test_retry_is_durable_and_not_claimable_before_backoff(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_task("t1", "p1", "one")
    queue = RuntimeQueue(runtime, lease_seconds=20)
    claim = queue.claim_next("worker-a", run_id="r1")
    assert claim is not None
    retry_at = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat().replace("+00:00", "Z")
    queue.schedule_retry(claim, error_code="WORKER_LOST", next_retry_at=retry_at)
    row = runtime._read(lambda c: c.execute("SELECT status,next_retry_at FROM tasks WHERE task_id='t1'").fetchone())
    attempt = runtime._read(lambda c: c.execute("SELECT status,next_retry_at,error_code FROM task_attempts WHERE task_id='t1'").fetchone())
    assert row[0] == "retry_wait" and row[1] == retry_at
    assert attempt[0] == "retry_wait" and attempt[1] == retry_at and attempt[2] == "WORKER_LOST"
    assert queue.claim_next("worker-b", run_id="r1") is None


def test_retry_becomes_new_fenced_attempt_after_backoff(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.create_task("t1", "p1", "one")
    queue = RuntimeQueue(runtime, lease_seconds=20)
    claim = queue.claim_next("worker-a", run_id="r1")
    assert claim is not None
    old = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    queue.schedule_retry(claim, error_code="RETRYABLE", next_retry_at=old)
    new_claim = queue.claim_next("worker-b", run_id="r1")
    assert new_claim is not None and new_claim.lease_generation > claim.lease_generation
    assert new_claim.attempt_id != claim.attempt_id


def test_resource_limit_probe_is_available_for_isolated_process(tmp_path):
    from app.runtime.process import enforce_resource_limits, process_resource_exceeded

    assert enforce_resource_limits(None, memory_mb=1, cpu_weight=1) is None
    assert process_resource_exceeded(None, memory_mb=1, cpu_weight=1) is False
