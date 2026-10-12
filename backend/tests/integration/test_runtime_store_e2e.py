"""Pure phase-one RuntimeStore + ArtifactStore integration coverage."""

import json

import pytest

from app.runtime import ArtifactStore, RuntimeDB, RuntimeStore


EMPTY_VECTOR = b'{"type":"FeatureCollection","features":[]}'


def test_runtime_store_artifact_pipeline_is_durable_and_cas_backed(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("producer", "p1", "producer", status="ready")
    runtime.create_task("consumer", "p1", "consumer", status="blocked")
    runtime.add_task_dependency("consumer", "producer")

    artifacts = ArtifactStore(tmp_path / "data", runtime)
    source = artifacts.publish_staged(artifacts.stage_bytes(b"source"), origin="upload")
    runtime.update_task_status("producer", "leased")
    runtime.update_task_status("producer", "running")
    runtime.update_task_status("producer", "validating")
    output = artifacts.publish_staged(
        artifacts.stage_bytes(b"derived"),
        origin="task",
        producer_task_id="producer",
        parents=[source["artifact_id"]],
        output_name="result",
        event=("r1", "artifact.published", {"artifact_id": "derived"}),
    )
    runtime.update_task_status("producer", "succeeded")
    runtime.bind_task_input(
        "binding", "consumer", "input", source_task_id="producer", source_port="result", source_ordinal=0
    )

    assert runtime.resolve_task_inputs("p1") == {"consumer": 1}
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='consumer'").fetchone()[0]) == "ready"
    assert runtime._read(lambda c: c.execute("SELECT artifact_id FROM task_inputs WHERE task_id='consumer'").fetchone()[0]) == output["artifact_id"]
    assert any(event["event_type"] == "task.ready" for event in runtime.list_events("r1"))
    assert runtime.list_lineage(output["artifact_id"])[0]["parent_artifact_id"] == source["artifact_id"]
    assert artifacts.path_for(output["artifact_id"]).read_bytes() == b"derived"


def test_compiled_runtime_task_dispatches_real_handler_and_publishes_artifact_event(tmp_path, monkeypatch):
    from app.runtime import ArtifactRef, Compiler, PlannerDraft, PlannerStepDraft, RuntimeExecutor, ToolContractRegistry

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    source_payload = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [120.0, 30.0]}, "properties": {}}]}
    source = artifacts.publish_staged(
        artifacts.stage_bytes(json.dumps(source_payload).encode("utf-8"), filename="source.geojson"),
        origin="upload", artifact_type="Vector", crs={"kind": "EPSG", "code": 4326}, schema={"fields": []},
    )
    draft = PlannerDraft(
        run_id="r1", input_revision=1, user_goal="buffer", origin="e2e",
        tasks=[PlannerStepDraft(task_key="buffer", op="vector.buffer", inputs={"vector": ArtifactRef(artifact_id=source["artifact_id"], type="Vector", crs={"kind": "EPSG", "code": 4326})}, params={"distance_m": 1})],
    )
    plan = Compiler(ToolContractRegistry.with_builtins(), artifact_lookup=runtime.get_artifact).compile(draft)
    runtime.create_plan("p2", "r1", plan=plan)
    runtime.create_task("buffer", "p2", "buffer", tool_name="vector.buffer")
    result = RuntimeExecutor(runtime, artifacts).execute(plan.tasks[0], run_id="r1", task_id="buffer")
    assert result["artifact"]["validation_status"] == "valid"
    assert result["artifact"]["producer_task_id"] == "buffer"
    assert artifacts.path_for(result["artifact"]["artifact_id"]).is_file()
    assert [row["parent_artifact_id"] for row in runtime.list_lineage(result["artifact"]["artifact_id"])] == [source["artifact_id"]]
    assert any(event["event_type"] == "task.completed" for event in runtime.list_events("r1"))


def test_executor_resolves_artifact_ref_to_cas_payload_and_synchronizes_states(tmp_path, monkeypatch):
    from app.runtime import ArtifactRef, Compiler, PlannerDraft, PlannerStepDraft, RuntimeExecutor, ToolContractRegistry
    import app.runtime.tool_handlers as handlers

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    source_payload = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [120.0, 30.0]}, "properties": {"x": 1}}]}
    source = artifacts.publish_staged(
        artifacts.stage_bytes(json.dumps(source_payload).encode("utf-8"), filename="source.geojson"), origin="upload", artifact_type="Vector",
        crs={"kind": "EPSG", "code": 4326}, schema={"fields": ["x"]},
    )
    called = []

    def fake_buffer(**kwargs):
        called.append(kwargs["vector"])
        assert kwargs["vector"]["data"] == source_payload
        assert kwargs["vector"]["schema"] == {"fields": ["x"]}
        assert kwargs["vector"]["crs"] == {"kind": "EPSG", "code": 4326}
        return {"status": "success", "data": {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[119.9, 29.9], [120.1, 29.9], [120.1, 30.1], [119.9, 30.1], [119.9, 29.9]]]}, "properties": {"x": 1}}]}}

    monkeypatch.setattr(handlers, "handle_vector_buffer", fake_buffer)
    registry = ToolContractRegistry.with_builtins()
    draft = PlannerDraft(
        run_id="r1", input_revision=1, user_goal="buffer", origin="e2e",
        tasks=[PlannerStepDraft(task_key="buffer", op="vector.buffer", inputs={"vector": ArtifactRef(artifact_id=source["artifact_id"], type="Vector", crs={"kind": "EPSG", "code": 4326})}, params={"distance_m": 1})],
    )
    plan = Compiler(registry, artifact_lookup=runtime.get_artifact).compile(draft)
    runtime.create_plan("p1", "r1", plan=plan)
    runtime.create_task("buffer", "p1", "buffer", tool_name="vector.buffer")
    result = RuntimeExecutor(runtime, artifacts, registry).execute(plan.tasks[0], run_id="r1", task_id="buffer")
    assert called and result["artifact"]["validation_status"] == "valid"
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='buffer'").fetchone()[0]) == "succeeded"
    assert runtime.get_run("r1")["status"] == "running"


def test_executor_rolls_back_all_output_metadata_events_and_links_when_later_output_fails(tmp_path, monkeypatch):
    from app.runtime import ArtifactRef, CompiledTask, RuntimeExecutor, ToolContractRegistry
    from app.runtime.contracts import OutputSpec
    import app.runtime.tool_handlers as handlers

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("buffer", "p1", "buffer", tool_name="vector.buffer")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    source = artifacts.publish_staged(
        artifacts.stage_bytes(EMPTY_VECTOR, filename="source.geojson"), origin="upload", artifact_type="Vector",
        crs={"kind": "EPSG", "code": 4326}, schema={},
    )
    monkeypatch.setattr(handlers, "handle_vector_buffer", lambda **_kwargs: {
        "status": "success", "data": {"first": {"n": 1}, "second": {"n": 2}},
    })
    compiled = CompiledTask(
        task_key="buffer", op="vector.buffer", contract_version="1.0.0", origin="planner",
        inputs={"vector": ArtifactRef(artifact_id=source["artifact_id"], type="Vector", crs={"kind": "EPSG", "code": 4326})},
        params={"distance_m": 1},
        outputs=[OutputSpec(port="first", artifact_type="File"), OutputSpec(port="second", artifact_type="File")],
    )
    executor = RuntimeExecutor(runtime, artifacts, ToolContractRegistry.with_builtins())
    monkeypatch.setattr(executor, "_validate_persisted_compiled", lambda *_args: None)
    original_publish = artifacts.publish_staged
    calls = 0

    def fail_second_publish(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated second output failure")
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(artifacts, "publish_staged", fail_second_publish)
    with pytest.raises(RuntimeError, match="second output"):
        executor.execute(compiled, run_id="r1", task_id="buffer")

    assert runtime._read(lambda c: c.execute("SELECT COUNT(*) FROM task_outputs WHERE task_id='buffer'").fetchone()[0]) == 0
    assert runtime._read(lambda c: c.execute("SELECT COUNT(*) FROM artifacts WHERE producer_task_id='buffer'").fetchone()[0]) == 0
    assert not [event for event in runtime.list_events("r1") if event["event_type"] == "artifact.published"]
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='buffer'").fetchone()[0]) == "failed"


def test_executor_empty_result_fails_task_and_never_publishes_valid_artifact(tmp_path, monkeypatch):
    from app.runtime import ArtifactRef, Compiler, PlannerDraft, PlannerStepDraft, RuntimeExecutor, ToolContractRegistry
    import app.runtime.tool_handlers as handlers

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    source = artifacts.publish_staged(artifacts.stage_bytes(EMPTY_VECTOR, filename="source.geojson"), origin="upload", artifact_type="Vector", crs={"kind": "EPSG", "code": 4326}, schema={})
    monkeypatch.setattr(handlers, "handle_vector_buffer", lambda **kwargs: {"status": "empty", "message": "no geometry"})
    registry = ToolContractRegistry.with_builtins()
    draft = PlannerDraft(run_id="r1", input_revision=1, user_goal="buffer", origin="e2e", tasks=[PlannerStepDraft(task_key="buffer", op="vector.buffer", inputs={"vector": ArtifactRef(artifact_id=source["artifact_id"], type="Vector", crs={"kind": "EPSG", "code": 4326})}, params={"distance_m": 1})])
    plan = Compiler(registry, artifact_lookup=runtime.get_artifact).compile(draft)
    runtime.create_plan("p1", "r1", plan=plan)
    runtime.create_task("buffer", "p1", "buffer", tool_name="vector.buffer")
    with pytest.raises(ValueError, match="empty"):
        RuntimeExecutor(runtime, artifacts, registry).execute(plan.tasks[0], run_id="r1", task_id="buffer")
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='buffer'").fetchone()[0]) == "failed"
    assert not runtime._read(lambda c: c.execute("SELECT 1 FROM task_outputs WHERE task_id='buffer'").fetchone())
    assert any(event["event_type"] == "task.empty" for event in runtime.list_events("r1"))


def test_code_executor_consumes_declared_artifact_bytes_and_publishes_its_result(tmp_path):
    from app.runtime import ArtifactRef, Compiler, PlannerDraft, PlannerStepDraft, RuntimeExecutor, ToolContractRegistry

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    source = artifacts.publish_staged(
        artifacts.stage_bytes(b"runtime input", filename="source.txt"), origin="upload", artifact_type="File",
    )
    plan = Compiler(ToolContractRegistry.with_builtins(), artifact_lookup=runtime.get_artifact).compile(
        PlannerDraft(
            user_goal="read a supplied file",
            tasks=[PlannerStepDraft(
                task_key="code", op="code.execute",
                inputs={"input": [ArtifactRef(artifact_id=source["artifact_id"], type="File")]},
                params={
                    "source_code": "__result__ = {'text': inputs['document'].decode(), 'id': artifact_metadata['document']['artifact_id']}",
                    "declared_inputs": ["document"],
                },
            )],
        )
    )
    runtime.create_plan("p1", "r1", plan=plan)
    runtime.create_task("code", "p1", "code", tool_name="code.execute")

    result = RuntimeExecutor(runtime, artifacts).execute(plan.tasks[0], run_id="r1", task_id="code")

    payload = json.loads(artifacts.path_for(result["artifact"]["artifact_id"]).read_text(encoding="utf-8"))
    assert payload == {"id": source["artifact_id"], "text": "runtime input"}
    assert result["artifact"]["type"] == "File"


def test_executor_none_result_is_error_and_never_publishes_valid_artifact(tmp_path, monkeypatch):
    from app.runtime import ArtifactRef, Compiler, PlannerDraft, PlannerStepDraft, RuntimeExecutor, ToolContractRegistry
    import app.runtime.tool_handlers as handlers

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    source = artifacts.publish_staged(artifacts.stage_bytes(EMPTY_VECTOR, filename="source.geojson"), origin="upload", artifact_type="Vector", crs={"kind": "WGS84"}, schema={})
    draft = PlannerDraft(run_id="r1", input_revision=1, tasks=[PlannerStepDraft(task_key="buffer", op="vector.buffer", params={"distance_m": 1}, inputs={"vector": ArtifactRef(artifact_id=source["artifact_id"], type="Vector", crs={"kind": "WGS84"})})])
    plan = Compiler(ToolContractRegistry.with_builtins()).compile(draft)
    runtime.create_plan("p1", "r1", plan=plan)
    runtime.create_task("buffer", "p1", "buffer", tool_name="vector.buffer")
    monkeypatch.setattr(handlers, "handle_vector_buffer", lambda **kwargs: None)
    with pytest.raises(ValueError, match="task result is error"):
        RuntimeExecutor(runtime, artifacts).execute(plan.tasks[0], run_id="r1", task_id="buffer")
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='buffer'").fetchone()[0]) == "failed"
    assert not runtime._read(lambda c: c.execute("SELECT 1 FROM task_outputs WHERE task_id='buffer'").fetchone())
    assert any(event["event_type"] == "task.failed" for event in runtime.list_events("r1"))


def test_executor_rejects_unconfirmed_side_effect_before_running_task(tmp_path):
    from app.runtime import ArtifactRef, Compiler, PlannerDraft, PlannerStepDraft, RuntimeExecutor, ToolContractRegistry

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    source = artifacts.publish_staged(artifacts.stage_bytes(b"file"), origin="upload", artifact_type="File", crs=None, schema={})
    registry = ToolContractRegistry.with_builtins()
    draft = PlannerDraft(run_id="r1", input_revision=1, user_goal="publish", origin="e2e", tasks=[PlannerStepDraft(task_key="publish", op="publish_file", inputs={"input": ArtifactRef(artifact_id=source["artifact_id"], type="File")}, params={"path": "result.geojson"})])
    plan = Compiler(registry, artifact_lookup=runtime.get_artifact, policy={"allowed_path_root": str(tmp_path)}).compile(draft)
    runtime.create_plan("p1", "r1", plan=plan)
    runtime.create_task("publish", "p1", "publish", tool_name="publish_file")
    with pytest.raises(ValueError, match="CONFIRMATION_REQUIRED"):
        RuntimeExecutor(runtime, artifacts, registry).execute(plan.tasks[0], run_id="r1", task_id="publish")
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='publish'").fetchone()[0]) == "ready"


def test_executor_rejects_task_id_from_another_run(tmp_path):
    from app.runtime import RuntimeExecutor, ToolContractRegistry

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_run("r2", "s1", run_version=2)
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "t1", tool_name="vector.buffer")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    with pytest.raises(ValueError, match="does not belong to run"):
        RuntimeExecutor(runtime, artifacts, ToolContractRegistry.with_builtins()).execute(
            {"task_key": "t1", "op": "vector.buffer", "contract_version": "1.0.0", "origin": "planner", "params": {"distance_m": 1}, "outputs": []},
            run_id="r2", task_id="t1",
        )


def test_executor_rejects_compiled_task_that_differs_from_persisted_plan(tmp_path):
    from app.runtime import ArtifactRef, Compiler, PlannerDraft, PlannerStepDraft, RuntimeExecutor, ToolContractRegistry

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    source = artifacts.publish_staged(artifacts.stage_bytes(EMPTY_VECTOR, filename="source.geojson"), origin="upload", artifact_type="Vector", crs={"kind": "EPSG", "code": 4326}, schema={})
    plan = Compiler(ToolContractRegistry.with_builtins(), artifact_lookup=runtime.get_artifact).compile(
        PlannerDraft(run_id="r1", input_revision=1, tasks=[PlannerStepDraft(task_key="buffer", op="vector.buffer", inputs={"vector": ArtifactRef(artifact_id=source["artifact_id"], type="Vector", crs={"kind": "EPSG", "code": 4326})}, params={"distance_m": 1})])
    )
    runtime.create_plan("p1", "r1", plan=plan)
    runtime.create_task("buffer", "p1", "buffer", tool_name="vector.buffer")
    forged = plan.tasks[0].model_copy(update={"params": {"distance_m": 999}})
    with pytest.raises(ValueError, match="persisted PlanIR"):
        RuntimeExecutor(runtime, artifacts).execute(forged, run_id="r1", task_id="buffer")
    assert runtime.get_run("r1")["status"] == "queued"


def test_task_output_selection_all_returns_all_ordinals_in_order(tmp_path):
    from app.runtime import ArtifactStore, RuntimeExecutor, TaskOutputRef

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("producer", "p1", "producer")
    runtime.create_task("consumer", "p1", "consumer")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    for ordinal, payload in enumerate((b"one", b"two")):
        artifact = artifacts.publish_staged(artifacts.stage_bytes(payload), origin="task", producer_task_id="producer", artifact_type="File")
        runtime.attach_task_output("producer", "out", artifact["artifact_id"], ordinal=ordinal)
    runtime.update_task_status("producer", "leased")
    runtime.update_task_status("producer", "running")
    runtime.update_task_status("producer", "validating")
    runtime.update_task_status("producer", "succeeded")
    values = RuntimeExecutor(runtime, artifacts)._resolve_input(TaskOutputRef(task_key="producer", port="out", selection="all", ordinal=None))
    assert [item["data"] for item in values] == [b"one", b"two"]


def test_task_confirmation_persists_and_matches_action_hash(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "t1")
    confirmation = runtime.create_task_confirmation("c1", "t1", action_hash="a" * 64)
    assert confirmation["action_hash"] == "a" * 64


def test_executor_cas_dedup_keeps_output_links_for_distinct_producers(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "t1")
    runtime.create_task("t2", "p1", "t2")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    first = artifacts.publish_staged(artifacts.stage_bytes(b"same"), origin="task", producer_task_id="t1", artifact_type="File", output_name="out")
    second = artifacts.publish_staged(artifacts.stage_bytes(b"same"), origin="task", producer_task_id="t2", artifact_type="File", output_name="out")
    assert first["artifact_id"] != second["artifact_id"]
    assert first["content_hash"] == second["content_hash"]
    links = runtime._read(lambda c: c.execute("SELECT task_id,artifact_id FROM task_outputs ORDER BY task_id").fetchall())
    assert [(row[0], row[1]) for row in links] == [("t1", first["artifact_id"]), ("t2", second["artifact_id"])]
