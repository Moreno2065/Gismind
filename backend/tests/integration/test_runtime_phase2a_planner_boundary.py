from __future__ import annotations

import json

from app.runtime import ArtifactStore, PlannerDraft, PlannerStepDraft, RuntimeDB, RuntimeStore, RuntimeSupervisor
from app.runtime.llm_planner import candidate_catalog, PlannerRequest


class StaticPlanner:
    def create_draft(self, request):
        return PlannerDraft(
            run_id=request.run_id, input_revision=request.input_revision,
            user_goal=request.user_goal, tasks=[
                PlannerStepDraft(task_key="geocode", op="geo.geocode", params={"address": "南京市"}),
            ],
        )


def test_runtime_persists_bounded_catalog_identity_into_draft_and_plan(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s-phase2a")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    supervisor = RuntimeSupervisor(runtime, artifacts, planner=StaticPlanner(), isolate=False, autostart=False)
    runtime.create_run("run-phase2a", "s-phase2a")
    runtime.add_input_revision("run-phase2a", 1, {"prompt": "查询南京", "input_artifact_ids": []})

    assert supervisor.run_once("planner-phase2a", run_id="run-phase2a")["status"] == "planned"

    attempt = runtime._read(lambda c: c.execute(
        "SELECT catalog_hash,draft_json FROM planner_attempts WHERE run_id=?", ("run-phase2a",)
    ).fetchone())
    plan = runtime._read(lambda c: c.execute(
        "SELECT plan_json,ir_json FROM plans WHERE run_id=?", ("run-phase2a",)
    ).fetchone())
    draft = json.loads(attempt[1])
    plan_json = json.loads(plan[0])
    canonical = json.loads(plan[1])

    assert attempt[0] == draft["catalog_hash"] == plan_json["catalog_hash"]
    assert draft["allowed_operation_ids"] == plan_json["allowed_operation_ids"] == canonical["allowed_operation_ids"]
    assert "geo.geocode" in draft["allowed_operation_ids"]
