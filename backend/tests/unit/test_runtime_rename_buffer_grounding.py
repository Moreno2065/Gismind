"""Offline regressions for browser rename/buffer grounding, not live evidence."""
import json
from dataclasses import replace

import pytest

from app.runtime.llm_planner import (
    OpenAICompatiblePlanner, PlannerError, PlannerRequest,
    _verified_rename_binding, candidate_catalog,
)
from tests.browser.runtime_chinese_corpus import PHRASES


FIELDS = {"经度": "float64", "纬度": "float64", "编号": "int64",
          "名称": "object", "数值": "float64", "类别": "object"}


def request(phrase=3, kind="csv", goal=None):
    if kind == "csv":
        artifact = {"artifact_id": "source", "type": "File", "crs": None,
            "schema": {"inspection": {"source": "physical_file", "format": "csv",
                "artifact_type": "Table", "fields": FIELDS}, "fields": FIELDS}}
    else:
        artifact = {"artifact_id": "source", "type": "Vector", "crs": {"kind": "EPSG", "code": 4326},
                    "schema": {"fields": FIELDS}}
    if goal is None:
        goal = ("请用“经度”“纬度”两列生成点，坐标列可以作为原属性保留。"
                "先只保留数值严格大于18的站点，等于阈值的不要。"
                + PHRASES["rename_buffer"][phrase].format(distance_m=200)
                + "在地图上展示最终结果，并将同一结果导出为地理包空间数据库文件供下载。"
                  "保留未要求修改的原始属性。不同站点的面不要合并。")
    return PlannerRequest(run_id="rename-buffer", input_revision=1, user_goal=goal,
                          input_artifacts=(artifact,), revision_patch={})


def planner():
    return OpenAICompatiblePlanner(api_key="offline-test", base_url="https://unused.invalid",
        model="offline-test", timeout_seconds=1, max_tokens=1000, max_retries=0)


@pytest.mark.parametrize("kind", ["Vector", "csv"])
@pytest.mark.parametrize("phrase", range(len(PHRASES["rename_buffer"])))
def test_explicit_rename_is_grounded_in_all_frozen_phrasings(phrase, kind):
    req = request(phrase, kind)
    assert _verified_rename_binding(req) == ("名称", "新名")
    frontend = planner()
    catalog = candidate_catalog(frontend.registry, req)
    assert "attribute.rename_field" in catalog.operation_ids
    payload = json.loads(frontend._messages(req, catalog)[-1]["content"])
    assert payload["verified_request_facts"]["rename"] == {"old_name": "名称", "new_name": "新名"}


@pytest.mark.parametrize("goal", [
    "请用“经度”“纬度”生成点。不要把“名称”字段重命名为“新名”。",
    "先保留“名称”字段，再把“建筑”字段重命名为“新名”。",
    "将“名称”字段重命名为“类别”。",
    "将名称列换成新名列。",
    "把“名称”和“数值”字段重命名为“新名”。",
    "把“名称”字段的属性值改成“新名”。",
    "把“名称”字段改成“新名”，不，改为“正式名”。",
    "把“名称”字段改成“新名”，然后将数值列改名为金额。",
    "把“名称”字段改成“新名”或“正式名”，任选一个。",
    "把“名称”或“数值”字段改成“新名”。",
])
def test_csv_rename_grounding_preserves_ambiguity_and_source_guards(goal):
    assert _verified_rename_binding(request(goal=goal)) is None


def test_csv_declared_fields_without_physical_inspection_do_not_ground_rename():
    req = request()
    artifact = {**req.input_artifacts[0], "schema": {"fields": FIELDS}}
    assert _verified_rename_binding(replace(req, input_artifacts=(artifact,))) is None


@pytest.mark.parametrize("kind", ["Vector", "csv"])
def test_wrong_model_rename_target_is_rejected_with_facts_for_bounded_repair(monkeypatch, kind):
    req = request(kind=kind)
    frontend = planner()
    response = {"tasks": [{"task_key": "rename", "op": "attribute.rename_field",
        "inputs": {}, "params": {"old_name": "名称", "new_name": "错误名称"}}]}
    monkeypatch.setattr(frontend, "_call", lambda messages: (
        {"choices": [{"message": {"content": json.dumps(response)}}]}, 1))
    with pytest.raises(PlannerError) as failure:
        frontend.create_draft(req)
    assert failure.value.code == "LLM_DRAFT_INVALID"
    assert failure.value.diagnostics["verified_rename_binding"] == {"old_name": "名称", "new_name": "新名"}


def test_repair_prompt_retains_exact_rename_and_original_request():
    req = request()
    frontend = planner()
    repair = {"diagnostic": {"code": "GOAL_REQUIREMENT_MISSING", "details": {
        "requirement": {"op": "attribute.rename_field"}}},
        "rejected_draft": {"tasks": [{"task_key": "rename_name", "op": "attribute.keep_fields",
            "inputs": {}, "params": {"fields": ["编号", "新名", "数值", "类别"]}}]}}
    payload = json.loads(frontend._messages(req, candidate_catalog(frontend.registry, req), repair=repair)[-1]["content"])
    assert payload["user_goal"] == req.user_goal
    assert payload["verified_request_facts"]["rename"] == {"old_name": "名称", "new_name": "新名"}
    assert "attribute.rename_field" in payload["operation_catalog"]["operation_ids"]
    assert payload["repair"]["rejected_draft"] == repair["rejected_draft"]
