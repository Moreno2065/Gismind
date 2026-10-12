"""Compiler repair receives exactly the structured diagnostic persisted by queue."""

import json

import pytest


@pytest.mark.parametrize("scenario", ["ports", "missing_parameter", "unexpected_parameter"])
def test_compile_details_survive_initial_repair_and_terminal_persistence(tmp_path, scenario):
    from app.runtime import ArtifactRef, ArtifactStore, PlannerDraft, PlannerStepDraft, RuntimeDB, RuntimeStore, RuntimeSupervisor

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    source = artifacts.publish_staged(artifacts.stage_bytes(b'{"type":"FeatureCollection","features":[]}'), origin="upload", artifact_type="Vector", crs={"kind": "EPSG", "code": 4326}, schema={})
    ref = ArtifactRef(artifact_id=source["artifact_id"], type="Vector", crs={"kind": "EPSG", "code": 4326})

    class Planner:
        calls = []

        def create_draft(self, request):
            self.calls.append("initial")
            return PlannerDraft(run_id=request.run_id, tasks=[PlannerStepDraft(task_key="op", op="vector.simplify" if scenario == "missing_parameter" else "vector.dissolve", inputs={"vector" if scenario == "ports" else "input": ref}, params={"group_by": "name"} if scenario == "unexpected_parameter" else {})])

        def repair_draft(self, request, *, rejected_draft, diagnostic):
            self.calls.append("repair")
            self.diagnostic = diagnostic
            return rejected_draft

    planner = Planner()
    runtime.add_input_revision("r1", 1, {"prompt": "简化图形并融合 dissolve simplify", "input_artifact_ids": [source["artifact_id"]]})
    assert RuntimeSupervisor(runtime, artifacts, planner=planner, isolate=False).run_once("planner", run_id="r1")["status"] == "planning_failed"
    assert planner.calls == ["initial", "repair"]
    diagnostic = planner.diagnostic
    assert diagnostic["path"]
    assert diagnostic["message"]
    assert diagnostic["error"]
    details = diagnostic["details"]
    if scenario == "ports":
        assert details["allowed_ports"] == ["input"]
        assert details["provided_ports"] == ["vector"]
        assert details["missing_ports"] == ["input"]
        assert details["unexpected_ports"] == ["vector"]
    elif scenario == "missing_parameter":
        assert details["missing_parameters"] == ["tolerance"]
        assert "tolerance" in details["parameter_schema"]["properties"]
    else:
        assert details["unexpected_parameters"] == ["group_by"]
        assert "by" in details["parameter_schema"]["properties"]
    attempts = runtime._read(lambda conn: [dict(row) for row in conn.execute("SELECT * FROM planner_attempts ORDER BY attempt_no")])
    assert len(attempts) == 2
    assert [row["phase"] for row in attempts] == ["initial", "repair"]
    assert all(row["status"] == "failed" for row in attempts)
    assert all(json.loads(row["diagnostics_json"]) == diagnostic for row in attempts)
    rejection = next(event for event in runtime.list_events("r1") if event["event_type"] == "plan.rejected")
    assert json.loads(rejection["payload_json"])["diagnostic"] == diagnostic
    assert runtime.get_run("r1")["status"] == "failed"


def test_provider_schema_diagnostic_is_preserved_for_repair_and_storage(tmp_path):
    from app.runtime import ArtifactStore, RuntimeDB, RuntimeStore, RuntimeSupervisor
    from app.runtime.llm_planner import PlannerError

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.add_input_revision("r1", 1, {"prompt": "查询南京", "input_artifact_ids": []})
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    details = {"code": "LLM_DRAFT_INVALID", "path": "tasks.0.params", "message": "invalid parameter", "details": {"required": ["address"]}}

    class Planner:
        def create_draft(self, request):
            raise PlannerError("LLM_DRAFT_INVALID", "bad schema", diagnostics=details)

        def repair_draft(self, request, *, rejected_draft, diagnostic):
            assert rejected_draft is None
            self.diagnostic = diagnostic
            raise PlannerError("LLM_DRAFT_INVALID", "bad schema", diagnostics=details)

    planner = Planner()
    assert RuntimeSupervisor(runtime, artifacts, planner=planner, isolate=False).run_once("planner", run_id="r1")["status"] == "planning_failed"
    assert all(planner.diagnostic[key] == value for key, value in details.items())
    attempts = runtime._read(lambda conn: [dict(row) for row in conn.execute("SELECT * FROM planner_attempts ORDER BY attempt_no")])
    assert len(attempts) == 2
    assert all(json.loads(row["diagnostics_json"]) == planner.diagnostic for row in attempts)
