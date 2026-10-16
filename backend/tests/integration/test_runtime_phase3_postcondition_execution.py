"""RuntimeExecutor regressions proving semantic failures are durable."""

from __future__ import annotations

import json

import pytest

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


@pytest.mark.parametrize("operation,params", [
    ("attribute.rename_field", {"old_name": "old", "new_name": "new"}),
    ("attribute.keep_fields", {"fields": ["old"]}),
    ("attribute.filter", {"field": "old", "operator": ">", "value": 100}),
    ("vector.clip", {}), ("vector.extract_location", {}),
    ("vector.point_on_surface", {}), ("vector.buffer", {"distance_m": 100}),
])
def test_plausible_unchanged_result_fails_durably_before_publish(tmp_path, monkeypatch, operation, params):
    from shapely.geometry import box, mapping
    from app.runtime import tool_handlers

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    payload = {"type": "FeatureCollection", "features": [{
        "type": "Feature", "geometry": mapping(box(0, 0, 4, 4)), "properties": {"old": 7, "keep": "a"},
    }]}
    crs = {"kind": "EPSG", "code": 3857}
    source = artifacts.publish_staged(artifacts.stage_bytes(json.dumps(payload).encode()),
                                      origin="upload", artifact_type="Vector", crs=crs, schema={})
    bindings = {"vector" if operation == "vector.buffer" else "input":
                ArtifactRef(artifact_id=source["artifact_id"], type="Vector", crs=crs)}
    if operation in {"vector.clip", "vector.extract_location"}:
        mask_payload = {"type": "FeatureCollection", "features": [{
            "type": "Feature", "geometry": mapping(box(10, 10, 20, 20)), "properties": {},
        }]}
        mask = artifacts.publish_staged(artifacts.stage_bytes(json.dumps(mask_payload).encode()),
                                        origin="upload", artifact_type="Vector", crs=crs, schema={})
        bindings["mask"] = ArtifactRef(artifact_id=mask["artifact_id"], type="Vector", crs=crs)
    registry = ToolContractRegistry.with_builtins()
    plan = Compiler(registry, artifact_lookup=runtime.get_artifact).compile(PlannerDraft(
        run_id="r1", input_revision=1,
        tasks=[PlannerStepDraft(task_key="operation", op=operation, inputs=bindings, params=params)],
    ))
    runtime.create_plan("p1", "r1", plan=plan)
    runtime.create_task("operation", "p1", "operation", tool_name=operation)
    monkeypatch.setattr(tool_handlers, "handle_" + operation.replace(".", "_"),
                        lambda **_kwargs: {"status": "success", "data": payload})
    supervisor = RuntimeSupervisor(runtime, artifacts, registry=registry, isolate=False, autostart=False)
    assert supervisor.worker_loop("worker-a", run_id="r1", max_tasks=1, poll_interval=0.001) == 1
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='operation'").fetchone()[0]) == "failed"
    assert runtime._read(lambda c: c.execute("SELECT COUNT(*) FROM task_outputs WHERE task_id='operation'").fetchone()[0]) == 0
    failed = [event for event in runtime.list_events("r1") if event["event_type"] == "task.failed"]
    assert failed and "VALIDATION_FAILED" in json.dumps(failed)
