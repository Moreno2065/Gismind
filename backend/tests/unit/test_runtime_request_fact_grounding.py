"""Explicit user facts must survive the LLM-to-draft boundary."""

import json
import runpy
from pathlib import Path

import pytest

from app.runtime import ArtifactRef, ArtifactStore, PlannerDraft, PlannerStepDraft, RuntimeDB, RuntimeStore, RuntimeSupervisor
from app.runtime.llm_planner import (
    OpenAICompatiblePlanner, PlannerError, PlannerRequest, _verified_buffer_distance_m,
    _verified_semantic_facts, candidate_catalog,
)


FIELDS = {"编号": "integer", "名称": "string", "数值": "integer", "类别": "string"}
CORPUS = runpy.run_path(str(Path(__file__).resolve().parents[1] / "live" / "prompt_corpus_2000.py"))


def _request(goal: str, *, schema=None) -> PlannerRequest:
    return PlannerRequest(
        run_id="fact-run", input_revision=1, user_goal=goal,
        input_artifacts=({
            "artifact_id": "art-vector", "type": "Vector",
            "crs": {"kind": "EPSG", "code": 3857},
            "schema": {"fields": FIELDS} if schema is None else schema,
            "content_hash": "a" * 64,
        },), revision_patch={},
    )


def _planner(monkeypatch, op: str, params: dict):
    planner = OpenAICompatiblePlanner(
        api_key="test", base_url="https://example.test/v1", model="fake-model",
        timeout_seconds=1, max_tokens=512, max_retries=0,
    )
    draft = {
        "draft_version": "1", "origin": "llm", "tasks": [{
            "task_key": "work", "op": op,
            "inputs": {"vector" if op == "vector.reproject" else "input": {
                "kind": "artifact", "artifact_id": "art-vector", "type": "Vector",
                "selection": "single", "ordinal": 0,
            }},
            "params": params,
        }],
    }
    response = {"choices": [{"message": {"content": json.dumps(draft, ensure_ascii=False)}}]}
    monkeypatch.setattr(planner, "_call", lambda messages: (response, 1))
    return planner


def _assert_planner_rejects(monkeypatch, goal, op, params):
    planner = _planner(monkeypatch, op, params)
    with pytest.raises(PlannerError) as error:
        planner.create_draft(_request(goal))
    assert error.value.code == "LLM_DRAFT_INVALID"


def test_planner_rejects_wrong_explicit_target_crs(monkeypatch):
    _assert_planner_rejects(
        monkeypatch, "附件采用网页墨卡托投影；请重投影到 WGS84（EPSG:4326）。",
        "vector.reproject", {"target_crs": {"kind": "EPSG", "code": 3857}},
    )


def test_planner_rejects_wrong_explicit_keep_field_set(monkeypatch):
    _assert_planner_rejects(
        monkeypatch, "属性只保留编号和名称，几何不要改变。",
        "attribute.keep_fields", {"fields": ["编号", "类别"]},
    )


def test_planner_rejects_wrong_explicit_filter_threshold(monkeypatch):
    _assert_planner_rejects(
        monkeypatch, "只保留数值大于20的要素。",
        "attribute.filter", {"field": "数值", "operator": ">", "value": 10},
    )


@pytest.mark.parametrize(("goal", "op", "params", "code"), [
    ("请重投影到 WGS84（EPSG:4326）。", "vector.reproject",
     {"target_crs": {"kind": "EPSG", "code": 3857}}, "REQUEST_TARGET_CRS_MISMATCH"),
    ("属性只保留编号和名称。", "attribute.keep_fields",
     {"fields": ["编号", "类别"]}, "REQUEST_KEEP_FIELDS_MISMATCH"),
    ("只保留数值大于20的要素。", "attribute.filter",
     {"field": "编号", "operator": ">", "value": 20}, "REQUEST_FILTER_MISMATCH"),
])
def test_supervisor_does_not_activate_plan_with_wrong_request_facts(tmp_path, goal, op, params, code):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s-facts")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    source = artifacts.publish_staged(
        artifacts.stage_bytes(b'{"type":"FeatureCollection","features":[]}', filename="source.geojson"),
        origin="upload", artifact_type="Vector", media_type="application/geo+json",
        crs={"kind": "EPSG", "code": 3857}, schema={"fields": FIELDS},
    )

    class FactPlanner:
        def create_draft(self, request):
            return PlannerDraft(
                run_id=request.run_id, input_revision=request.input_revision, user_goal=request.user_goal,
                tasks=[PlannerStepDraft(
                    task_key="work", op=op,
                    inputs={"vector" if op == "vector.reproject" else "input": ArtifactRef(
                        artifact_id=source["artifact_id"], type="Vector", crs=source["crs"],
                    )}, params=params,
                )],
            )

    runtime.create_run("run-facts", "s-facts")
    runtime.add_input_revision("run-facts", 1, {"prompt": goal, "input_artifact_ids": [source["artifact_id"]]})
    supervisor = RuntimeSupervisor(runtime, artifacts, isolate=False, planner=FactPlanner(), autostart=False)
    assert supervisor.run_once("planner-facts", run_id="run-facts")["status"] == "planning_failed"
    assert runtime._read(lambda c: c.execute(
        "SELECT COUNT(*) FROM plans WHERE run_id=?", ("run-facts",),
    ).fetchone()[0]) == 0
    attempt = runtime._read(lambda c: c.execute(
        "SELECT error_code FROM planner_attempts WHERE run_id=?", ("run-facts",),
    ).fetchone())
    assert attempt[0] == code


@pytest.mark.parametrize("paraphrase", range(10))
def test_frozen_filter_wordings_bind_the_exact_field_operator_and_threshold(paraphrase):
    case = next(case for case in CORPUS["load_cases"]() if case.family == "filter" and case.profile == 0 and case.paraphrase == paraphrase)
    assert _verified_semantic_facts(_request(case.prompt))["attribute.filter"] == {
        "field": "数值", "operator": ">", "value": 10.0,
    }


@pytest.mark.parametrize("paraphrase", range(10))
def test_frozen_reproject_wordings_bind_only_explicit_target_crs(paraphrase):
    case = next(case for case in CORPUS["load_cases"]() if case.family == "reproject" and case.profile == 0 and case.paraphrase == paraphrase)
    target = _verified_semantic_facts(_request(case.prompt)).get("vector.reproject")
    if paraphrase in {1, 4, 8}:
        assert target is None
    else:
        assert target == {"target_crs": {"kind": "EPSG", "code": 4326}}


@pytest.mark.parametrize("paraphrase", range(10))
def test_frozen_keep_wordings_bind_the_complete_field_set(paraphrase):
    case = next(case for case in CORPUS["load_cases"]() if case.family == "keep" and case.profile == 0 and case.paraphrase == paraphrase)
    assert _verified_semantic_facts(_request(case.prompt))["attribute.keep_fields"] == {"fields": ["名称", "编号"]}


def test_later_user_distance_overrides_original_distance_and_guides_catalog():
    original = _request("请对附件的点做 100 米缓冲，距离我稍后再确认。")
    request = PlannerRequest(
        run_id=original.run_id, input_revision=2, user_goal=original.user_goal,
        input_artifacts=original.input_artifacts,
        revision_patch={"clarification": "这次明确改为 500 米缓冲。"},
    )
    assert _verified_buffer_distance_m(request) == 500
    assert "vector.buffer" in candidate_catalog(OpenAICompatiblePlanner(
        api_key="offline", base_url="https://api.deepseek.com", model="deepseek-flash",
        timeout_seconds=1, max_tokens=512, max_retries=0,
    ).registry, request).operation_ids


def test_later_user_target_crs_overrides_original_and_is_verified():
    original = _request("请把图层重投影到 EPSG:4326；目标坐标系我稍后再确认。")
    request = PlannerRequest(
        run_id=original.run_id, input_revision=2, user_goal=original.user_goal,
        input_artifacts=original.input_artifacts,
        revision_patch={"clarification": "目标坐标系是 EPSG:3857。"},
    )
    assert _verified_semantic_facts(request)["vector.reproject"] == {
        "target_crs": {"kind": "EPSG", "code": 3857},
    }
