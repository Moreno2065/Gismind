"""Progress metadata is a durable projection, not process-local trace state."""
import json

from app.api.runtime import _snapshot
from app.runtime import ArtifactRef, ArtifactStore, Compiler, PlannerDraft, PlannerStepDraft, RuntimeDB, RuntimeStore, TaskOutputRef
from app.runtime.queue import RuntimeQueue


def store(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s")
    runtime.create_run("r", "s")
    return runtime


def test_snapshot_restores_ordered_task_operations_and_exact_latest_attempt_error(tmp_path):
    runtime = store(tmp_path)
    runtime.create_plan("p", "r")
    runtime.create_task("z-first", "p", "buffer", tool_name="vector.buffer")
    runtime.create_task("a-second", "p", "export", tool_name="file.export")
    runtime.create_task_attempt("attempt", "z-first", 1)
    runtime.update_task_attempt_status("attempt", "failed", error_code="INVALID_CRS", error="图层缺少坐标参考系")
    runtime._write(lambda c: c.execute("UPDATE plans SET ir_json=? WHERE plan_id='p'", (json.dumps({"tasks": [{"task_key": "buffer"}, {"task_key": "export"}]}),)))
    runtime._write(lambda c: c.execute("UPDATE runs SET active_plan_id='p',status='failed' WHERE run_id='r'"))
    runtime._write(lambda c: c.execute("UPDATE tasks SET created_at='same',status='failed' WHERE task_id='z-first'"))
    runtime._write(lambda c: c.execute("UPDATE tasks SET created_at='same' WHERE task_id='a-second'"))
    snapshot = _snapshot(runtime, "r")
    assert [task["task_key"] for task in snapshot["tasks"]] == ["buffer", "export"]
    assert snapshot["tasks"][0]["op"] == "vector.buffer"
    assert snapshot["tasks"][0]["error_code"] == "INVALID_CRS"
    assert snapshot["tasks"][0]["error"] == "图层缺少坐标参考系"


def test_planning_repair_phase_and_error_survive_snapshot_and_are_in_events(tmp_path):
    runtime = store(tmp_path)
    runtime.add_input_revision("r", 1, {"prompt": "缓冲然后导出", "input_artifact_ids": []})
    claim = RuntimeQueue(runtime).claim_planning_run("worker")
    fields = dict(planner_claim=claim, input_revision=1, provider="offline", model="offline", prompt_version="1", catalog_hash="catalog", request_hash="request")
    first = runtime.create_planner_attempt("r", attempt_no=1, phase="initial", **fields)
    runtime.finish_planner_attempt(first["attempt_id"], planner_claim=claim, status="failed", error_code="LLM_DRAFT_INVALID", error="计划缺少任务")
    runtime.create_planner_attempt("r", attempt_no=2, phase="repair", **fields)
    attempts = _snapshot(runtime, "r")["planning_attempts"]
    assert [(item["phase"], item["status"]) for item in attempts] == [("initial", "failed"), ("repair", "requested")]
    assert attempts[0]["error_code"] == "LLM_DRAFT_INVALID"
    assert attempts[0]["error"] == "计划缺少任务"
    failed = next(json.loads(e["payload_json"]) for e in runtime.list_events("r") if e["event_type"] == "planner.failed")
    assert failed["phase"] == "initial"
    assert failed["message"] == "计划缺少任务"
    assert json.loads(runtime.list_events("r")[-1]["payload_json"])["attempt_no"] == 2
    runtime._write(lambda c: c.execute("UPDATE runs SET planner_generation=planner_generation+1 WHERE run_id='r'"))
    assert _snapshot(runtime, "r")["planning_attempts"] == []


def test_dependency_display_order_matches_snapshot_and_events_without_changing_canonical_plan(tmp_path):
    runtime = store(tmp_path)
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    source = artifacts.publish_staged(artifacts.stage_bytes(b'{"type":"FeatureCollection","features":[]}', filename="source.geojson"),
                                     origin="upload", artifact_type="File", crs={"kind": "EPSG", "code": 4326})
    draft = PlannerDraft(run_id="r", input_revision=1, tasks=[
        PlannerStepDraft(task_key="load", op="io.load_vector", inputs={"file": ArtifactRef(artifact_id=source["artifact_id"], type="File", crs=source["crs"])}),
        PlannerStepDraft(task_key="buffer", op="vector.buffer", inputs={"vector": TaskOutputRef(task_key="load", port="vector")}, params={"distance_m": 500}),
        PlannerStepDraft(task_key="map", op="map.build", inputs={"vector": TaskOutputRef(task_key="buffer", port="vector")}),
        PlannerStepDraft(task_key="export", op="file.export", inputs={"input": TaskOutputRef(task_key="buffer", port="vector")}, params={"format": "geojson"}),
    ])
    plan = Compiler(artifact_lookup=runtime.get_artifact).compile(draft)
    canonical = plan.canonical_json
    assert [task["task_key"] for task in json.loads(canonical)["tasks"]] == ["buffer", "export", "load", "map"]
    claim = RuntimeQueue(runtime).claim_planning_run("worker")
    runtime.activate_compiled_plan("r", plan, planner_claim=claim)
    snapshot = _snapshot(runtime, "r")
    assert [task["task_key"] for task in snapshot["tasks"]] == ["load", "buffer", "export", "map"]
    assert [task["display_order"] for task in snapshot["tasks"]] == [0, 1, 2, 3]
    events = [json.loads(event["payload_json"]) for event in runtime.list_events("r") if event["event_type"] == "task.created"]
    assert [(task["task_key"], task["display_order"]) for task in events] == [(task["task_key"], task["display_order"]) for task in snapshot["tasks"]]
    stored = runtime.get_run_snapshot("r")["plan"]
    assert stored["ir_json"] == canonical
    assert stored["plan_hash"] == plan.plan_hash


def test_snapshot_task_details_are_durable_bounded_and_do_not_expose_execution_secrets(tmp_path):
    runtime = store(tmp_path)
    runtime.create_plan("p", "r")
    runtime.create_task("source", "p", "读取数据", tool_name="io.load_vector")
    runtime.create_task("buffer", "p", "生成缓冲", tool_name="vector.buffer", task={
        "params": {"distance_m": 500, "options": {"api_key": "secret-value", "password": "password-value"},
                   "code": "print('private code')", "body": "private request", "description": "x" * 10000,
                   "endpoint": "https://user:password@example.test/?token=secret"},
        "retry_policy": {"max_attempts": 3}, "code": {"source": "private source"},
    })
    runtime.add_task_dependency("buffer", "source")
    runtime.bind_task_input("binding", "buffer", "vector", source_task_id="source", source_port="vector", source_ordinal=0)
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    output = artifacts.publish_staged(artifacts.stage_bytes(b"result"), origin="task", producer_task_id="buffer")
    runtime.attach_task_output("buffer", "vector", output["artifact_id"])
    runtime.create_task_attempt("private-lease-token", "buffer", 1)
    runtime.update_task_attempt_status("private-lease-token", "failed", error_code="INVALID_CRS", error="CRS missing")
    runtime._write(lambda c: c.execute("UPDATE runs SET active_plan_id='p' WHERE run_id='r'"))

    snapshot = _snapshot(RuntimeStore(RuntimeDB(tmp_path / "runtime.db")), "r")
    detail = next(task for task in snapshot["tasks"] if task["task_id"] == "buffer")["details"]
    assert detail["parameters"]["distance_m"] == 500
    assert detail["inputs"] == [{"name": "vector", "ordinal": 0, "artifact_id": None,
                                  "source_task_id": "source", "source_task_key": "读取数据", "source_port": "vector",
                                  "source_ordinal": 0, "selection": "one"}]
    assert detail["dependencies"] == [{"task_id": "source", "task_key": "读取数据"}]
    assert detail["outputs"] == [{"name": "vector", "ordinal": 0, "artifact_id": output["artifact_id"]}]
    assert detail["latest_attempt"]["attempt_no"] == 1
    assert detail["latest_attempt"]["status"] == "failed"
    assert detail["latest_attempt"]["started_at"] and detail["latest_attempt"]["finished_at"]
    assert detail["max_attempts"] == 3
    serialized = json.dumps(detail)
    for private in ("secret-value", "password-value", "private code", "private request", "private source", "private-lease-token", "user:password", "token=secret"):
        assert private not in serialized
    assert len(serialized) < 4000


def test_created_events_share_the_persisted_task_explanation(tmp_path):
    runtime = store(tmp_path)
    plan = Compiler().compile(PlannerDraft(run_id="r", input_revision=1, tasks=[
        PlannerStepDraft(task_key="search", op="geo.geocode", params={"address": "南京"}),
    ]))
    runtime.activate_compiled_plan("r", plan, planner_claim=RuntimeQueue(runtime).claim_planning_run("worker"))
    task = _snapshot(runtime, "r")["tasks"][0]
    created = next(json.loads(e["payload_json"]) for e in runtime.list_events("r") if e["event_type"] == "task.created")
    assert created["details"] == task["details"]
    assert created["details"]["parameters"]["address"] == "南京"


def test_task_parameter_projection_limits_nested_payloads_and_alternate_secret_keys(tmp_path):
    runtime = store(tmp_path)
    runtime.create_plan("p", "r")
    runtime.create_task("t", "p", "task", task={"params": {
        "private_key": "private-credential", "auth": "Bearer hidden", "python_code": "private-script",
        "nested": {str(index): {str(child): "a" * 240 for child in range(20)} for index in range(20)},
    }})
    runtime._write(lambda c: c.execute("UPDATE runs SET active_plan_id='p' WHERE run_id='r'"))
    parameters = _snapshot(runtime, "r")["tasks"][0]["details"]["parameters"]
    serialized = json.dumps(parameters)
    assert "private-credential" not in serialized
    assert "Bearer hidden" not in serialized
    assert "private-script" not in serialized
    assert len(serialized) < 6000


def test_task_parameter_projection_preserves_only_valid_typed_epsg_codes(tmp_path):
    runtime = store(tmp_path)
    runtime.create_plan("p", "r")
    runtime.create_task("t", "p", "project", task={"params": {
        "target_crs": {"kind": "EPSG", "code": 3857}, "code": 4326, "source_code": "private script",
        "bad_crs": {"kind": "EPSG", "code": "print('private')"},
        "bool_crs": {"kind": "EPSG", "code": True}, "negative_crs": {"kind": "EPSG", "code": -1},
        "extra_crs": {"kind": "EPSG", "code": 4326, "extra": "metadata"},
    }})
    runtime._write(lambda c: c.execute("UPDATE runs SET active_plan_id='p' WHERE run_id='r'"))
    params = _snapshot(runtime, "r")["tasks"][0]["details"]["parameters"]
    assert params["target_crs"] == {"kind": "EPSG", "code": 3857}
    assert params["code"] == params["source_code"] == "[content omitted]"
    for name in ("bad_crs", "bool_crs", "negative_crs", "extra_crs"):
        assert params[name]["code"] == "[content omitted]"
