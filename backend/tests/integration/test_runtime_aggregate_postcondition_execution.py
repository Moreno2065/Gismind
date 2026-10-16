"""Operation result semantics are enforced before durable output publication."""

import json

import pytest
from shapely.geometry import Point, box, mapping

from app.runtime import (
    ArtifactRef, ArtifactStore, Compiler, PlannerDraft, PlannerStepDraft,
    RuntimeDB, RuntimeStore, RuntimeSupervisor, ToolContractRegistry, tool_handlers,
)


def feature(geometry, **properties):
    return {"type": "Feature", "geometry": mapping(geometry), "properties": properties}


@pytest.mark.parametrize("operation", ["vector.dissolve", "vector.join_nearest", "vector.count_points"])
@pytest.mark.parametrize("corrupt", [False, True])
def test_aggregate_result_is_published_only_when_its_semantics_pass(tmp_path, monkeypatch, operation, corrupt):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    if operation == "vector.dissolve":
        rows = {"input": [feature(box(0, 0, 1, 1), group="a"), feature(box(.5, 0, 1.5, 1), group="a")]}
        params = {"by": "group"}
    elif operation == "vector.join_nearest":
        rows = {"input": [feature(Point(0, 0), name="left")],
                "other": [feature(Point(.01, 0), name="near"), feature(Point(.02, 0), name="far")]}
        params = {}
    else:
        rows = {"points": [feature(Point(.5, .5))], "polygons": [feature(box(0, 0, 1, 1), name="zone")]}
        params = {}
    crs = {"kind": "EPSG", "code": 4326}
    bindings, envelopes = {}, {}
    for port, features in rows.items():
        payload = {"type": "FeatureCollection", "features": features}
        artifact = artifacts.publish_staged(artifacts.stage_bytes(json.dumps(payload).encode()),
                                            origin="upload", artifact_type="Vector", crs=crs, schema={})
        bindings[port] = ArtifactRef(artifact_id=artifact["artifact_id"], type="Vector", crs=crs)
        envelopes[port] = {"type": "Vector", "data": payload, "crs": crs}
    registry = ToolContractRegistry.with_builtins()
    plan = Compiler(registry, artifact_lookup=runtime.get_artifact).compile(PlannerDraft(
        run_id="r1", input_revision=1,
        tasks=[PlannerStepDraft(task_key="operation", op=operation, inputs=bindings, params=params)],
    ))
    runtime.create_plan("p1", "r1", plan=plan)
    runtime.create_task("operation", "p1", "operation", tool_name=operation)
    if corrupt:
        handler_name = "handle_" + operation.replace(".", "_")
        incorrect = getattr(tool_handlers, handler_name)(**envelopes, **params)
        assert incorrect["status"] == "success", incorrect
        if operation == "vector.dissolve":
            incorrect["data"]["features"] = rows["input"]
        elif operation == "vector.join_nearest":
            incorrect["data"]["features"][0]["properties"]["name_right"] = "far"
        else:
            incorrect["data"]["features"][0]["properties"]["count"] += 1
        monkeypatch.setattr(tool_handlers, handler_name, lambda **_kwargs: incorrect)
    supervisor = RuntimeSupervisor(runtime, artifacts, registry=registry, isolate=False, autostart=False)
    assert supervisor.worker_loop("worker-a", run_id="r1", max_tasks=1, poll_interval=.001) == 1
    status = runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='operation'").fetchone()[0])
    output_count = runtime._read(lambda c: c.execute("SELECT COUNT(*) FROM task_outputs WHERE task_id='operation'").fetchone()[0])
    assert status == ("failed" if corrupt else "succeeded")
    assert output_count == (0 if corrupt else 1)
    if corrupt:
        failures = [event for event in runtime.list_events("r1") if event["event_type"] == "task.failed"]
        assert failures and "VALIDATION_FAILED" in json.dumps(failures)
