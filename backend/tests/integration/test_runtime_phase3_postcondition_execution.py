"""RuntimeExecutor regressions proving semantic failures are durable."""

from __future__ import annotations

import json

from app.runtime import ArtifactStore, ArtifactRef, Compiler, PlannerDraft, PlannerStepDraft, RuntimeDB, RuntimeStore, RuntimeSupervisor, ToolContractRegistry


def test_malformed_buffer_payload_fails_before_artifact_publish(tmp_path, monkeypatch):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    source = artifacts.publish_staged(
        artifacts.stage_bytes(json.dumps({
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [0, 0]}, "properties": {}}],
        }).encode()),
        origin="upload", artifact_type="Vector", crs={"kind": "EPSG", "code": 4326}, schema={},
    )
    registry = ToolContractRegistry.with_builtins()
    plan = Compiler(registry, artifact_lookup=runtime.get_artifact).compile(PlannerDraft(
        run_id="r1", input_revision=1,
        tasks=[PlannerStepDraft(task_key="buffer", op="vector.buffer", inputs={"vector": ArtifactRef(artifact_id=source["artifact_id"], type="Vector", crs={"kind": "EPSG", "code": 4326})}, params={"distance_m": 10})],
    ))
    runtime.create_plan("p1", "r1", plan=plan)
    runtime.create_task("buffer", "p1", "buffer", tool_name="vector.buffer")

    from app.runtime import tool_handlers
    monkeypatch.setattr(tool_handlers, "handle_vector_buffer", lambda **_kwargs: {
        "status": "success",
        "data": {"type": "FeatureCollection", "features": []},
    })

    supervisor = RuntimeSupervisor(runtime, artifacts, registry=registry, isolate=False, autostart=False)
    assert supervisor.worker_loop("worker-a", run_id="r1", max_tasks=1, poll_interval=0.001) == 1
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='buffer'").fetchone()[0]) == "failed"
    assert runtime._read(lambda c: c.execute("SELECT COUNT(*) FROM task_outputs WHERE task_id='buffer'").fetchone()[0]) == 0
    assert any(event["event_type"] == "task.empty" for event in runtime.list_events("r1"))


def test_allowed_empty_result_is_published_only_when_contract_empty_policy_allows(tmp_path, monkeypatch):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    source = artifacts.publish_staged(
        artifacts.stage_bytes(json.dumps({"type": "FeatureCollection", "features": []}).encode()),
        origin="upload", artifact_type="Vector", crs={"kind": "EPSG", "code": 4326}, schema={},
    )
    registry = ToolContractRegistry.with_builtins()
    plan = Compiler(registry, artifact_lookup=runtime.get_artifact).compile(PlannerDraft(
        run_id="r1", input_revision=1,
        tasks=[PlannerStepDraft(task_key="summary", op="io.summarize", inputs={"input": ArtifactRef(artifact_id=source["artifact_id"], type="Vector", crs={"kind": "EPSG", "code": 4326})}, params={})],
    ))
    runtime.create_plan("p1", "r1", plan=plan)
    runtime.create_task("summary", "p1", "summary", tool_name="io.summarize")
    from app.runtime import tool_handlers
    monkeypatch.setattr(tool_handlers, "handle_io_summarize", lambda **_kwargs: {"status": "empty", "data": []})
    supervisor = RuntimeSupervisor(runtime, artifacts, registry=registry, isolate=False, autostart=False)
    assert supervisor.worker_loop("worker-a", run_id="r1", max_tasks=1, poll_interval=0.001) == 1
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='summary'").fetchone()[0]) == "succeeded"
