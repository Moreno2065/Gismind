"""Independent business-language recall checks; these do not invoke a provider."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.runtime.contracts import PortContract, ToolContract, ToolContractRegistry
from app.runtime.llm_planner import OpenAICompatiblePlanner, PlannerRequest, candidate_catalog


def request(goal, artifacts):
    return PlannerRequest(run_id="catalog-recall", input_revision=1, user_goal=goal, input_artifacts=tuple(artifacts), revision_patch={})


def inspected(kind="Vector", source="physical_file"):
    return {"artifact_id": "uploaded", "type": "File", "schema": {"inspection": {"version": "2", "source": source, "artifact_type": kind}}}


@pytest.mark.parametrize("goal,operation", [
    ("给每个地块生成与坐标轴平行的外框。", "vector.bounding_boxes"),
    ("需要各个对象的轴对齐矩形范围。", "vector.bounding_boxes"),
    ("这些离散岛屿属于同一个多部件对象，请逐一拆开。", "vector.multipart_to_singlepart"),
    ("一个位置出现了好几遍，移除重复点。", "vector.delete_duplicates"),
    ("将这份数据中的重复记录只留一份。", "vector.delete_duplicates"),
    ("留下人口大于500的地区，其他记录不要。", "attribute.filter"),
    ("按得分门槛过滤记录，阈值为80。", "attribute.filter"),
    ("输出属性只留名称和编号这两列。", "attribute.keep_fields"),
    ("把旧的区名列更名为行政区名称。", "attribute.rename_field"),
    ("增加一列，内容是各个地块面积的两倍。", "attribute.calculate"),
    ("给每条线加一列米数，以长度除以1000。", "attribute.calculate"),
    ("按所属县分组，把每组面连成一个区域。", "vector.dissolve"),
    ("两张面数据上下拼接，完整保留各自的记录。", "vector.merge"),
    ("把这两份数据合成一张图层，重叠地块仍各是一条。", "vector.merge"),
])
def test_inspected_vector_business_recall_keeps_loader(goal, operation):
    catalog = candidate_catalog(ToolContractRegistry.with_builtins(), request(goal, [inspected()]))
    assert {operation, "io.load_vector"}.issubset(catalog.operation_ids)
    assert "code.execute" not in catalog.operation_ids
    assert len(catalog) <= 18


def test_physical_inspection_improves_affinity_without_bypassing_file_loader():
    registry = ToolContractRegistry()
    for name, input_type, output_type in [("a.copy", "File", "File"), ("io.load_vector", "File", "Vector"), ("z.analyze", "Vector", "Table")]:
        registry.register(ToolContract(name=name, version="1.0.0", input_ports={"input": PortContract(artifact_type=input_type)}, output_ports={"output": PortContract(artifact_type=output_type)}))
    catalog = candidate_catalog(registry, request("处理数据", [inspected()]), limit=3)
    assert set(catalog.operation_ids) == {"io.load_vector", "z.analyze"}
    untrusted = candidate_catalog(registry, request("处理数据", [inspected(source="user")]), limit=3)
    assert "z.analyze" not in untrusted.operation_ids


@pytest.mark.parametrize("goal", [
    "处理上传文件", "仅保留 code 列", "geocode this address",
    "不要编写代码，删除重复点", "无需 Python，删除重复点",
    "只保留地区代码字段", "把 Python 版本这一列更名为版本号",
    "Do not execute code; use a buffer operation.",
    "Without Python, find the bounding boxes.",
    "不要写任何 Python 代码，只做几何去重。",
])
def test_code_is_not_a_fallback_for_non_code_or_negated_requests(goal):
    assert "code.execute" not in candidate_catalog(ToolContractRegistry.with_builtins(), request(goal, [inspected()])).operation_ids


@pytest.mark.parametrize("goal", ["请编写 Python 脚本处理附件", "运行自定义代码", "execute custom code for this file", "用 Python 对文件做处理", "Write a Python script to compute this."])
def test_explicit_code_requests_keep_code_available(goal):
    assert "code.execute" in candidate_catalog(ToolContractRegistry.with_builtins(), request(goal, [inspected()])).operation_ids


def test_calculation_hints_match_actual_expression_languages():
    registry = ToolContractRegistry.with_builtins()
    vector = candidate_catalog(registry, request("字段计算面积", [inspected()]))
    attribute_hint = next(item["planner_hint"] for item in vector if item["op"] == "attribute.calculate")
    assert attribute_hint["example_params"]["expression"].startswith("$area")
    assert "$length" in attribute_hint["note"]
    raster = candidate_catalog(registry, request("给有效像元值翻倍", [inspected("Raster")]))
    assert "io.load_raster" in raster.operation_ids
    raster_hint = next(item["planner_hint"] for item in raster if item["op"] == "raster.calculate")
    assert raster_hint["example_params"]["expression"] == "raster * 2"
    assert "np.where" in raster_hint["note"] and "np.isnan" in raster_hint["note"]
    assert "nodata" in raster_hint["note"].lower()


@pytest.mark.parametrize("source,target", [("分类", "组别"), ("值", "测量值"), ("label", "display_label")])
def test_rename_request_distinguishes_existing_source_from_new_destination(source, target):
    artifact = inspected()
    fields = {source: "object", "record_id": "int64"}
    artifact["schema"]["fields"] = fields
    artifact["schema"]["inspection"]["fields"] = fields
    goal = f"保留全部行，把字段{source}改名为{target}。"
    req = request(goal, [artifact])
    planner = OpenAICompatiblePlanner(api_key="test", base_url="https://provider.invalid", model="test", timeout_seconds=1, max_tokens=256, max_retries=0)
    catalog = candidate_catalog(planner.registry, req)
    messages = planner._messages(req, catalog)
    payload = json.loads(messages[1]["content"])
    assert payload["user_goal"] == goal
    assert payload["input_artifacts"][0]["schema"]["fields"] == fields
    assert target not in fields
    rename = next(item for item in payload["operation_catalog"]["operations"] if item["op"] == "attribute.rename_field")
    assert rename["planner_hint"]["field_roles"] == {
        "old_name": "existing_input_field", "new_name": "user_supplied_output_field",
    }
    note = rename["planner_hint"]["note"]
    assert "exact" in note and "must not already exist" in note
    assert "源字段必须存在" in messages[0]["content"]
    assert "目标字段名不必已存在" in messages[0]["content"]


def test_geometry_hints_distinguish_requested_coverage_from_rectangular_envelopes():
    catalog = candidate_catalog(ToolContractRegistry.with_builtins(), request("计算缓冲覆盖范围和外接矩形", [inspected()]))
    hints = {item["op"]: item.get("planner_hint", {}) for item in catalog}
    assert "already" in hints["vector.buffer"]["note"]
    assert "explicitly" in hints["vector.bounding_boxes"]["note"]
    assert "rectangle" in hints["vector.bounding_boxes"]["note"]


def test_rename_hint_demonstrates_unquoted_literal_field_names_without_using_corpus_values():
    catalog = candidate_catalog(ToolContractRegistry.with_builtins(), request("修改列名", [inspected()]))
    hint = next(item["planner_hint"] for item in catalog if item["op"] == "attribute.rename_field")
    example = hint["literal_name_example"]
    assert example["params"]["old_name"] in example["input_fields"]
    assert example["params"]["new_name"] not in example["input_fields"]
    assert example["params"]["old_name"] in example["request"]
    assert example["params"]["new_name"] in example["request"]
    assert "名称" not in str(example) and "新名" not in str(example)


def test_rename_hint_keeps_missing_ambiguous_and_conflicting_names_as_clarifications():
    catalog = candidate_catalog(ToolContractRegistry.with_builtins(), request("重命名字段", [inspected()]))
    hint = next(item["planner_hint"] for item in catalog if item["op"] == "attribute.rename_field")
    assert set(hint["clarify_if"]) == {
        "source_missing_or_ambiguous", "destination_not_supplied", "destination_already_exists", "source_equals_destination",
    }


_CORPUS_ROOT = Path(__file__).parents[1] / "live"
_CASES = json.loads((_CORPUS_ROOT / "runtime_live_intent_corpus.json").read_text(encoding="utf-8"))
_ARTIFACTS = tuple(json.loads((_CORPUS_ROOT / "runtime_live_fixture_facts.json").read_text(encoding="utf-8"))["artifact_declarations"].values())


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["case_id"])
def test_grounded_business_corpus_catalog_recall(case):
    assert len(_CASES) == 201
    catalog = candidate_catalog(ToolContractRegistry.with_builtins(), request(case["prompt"], _ARTIFACTS))
    assert set(case["expected_ops"]).issubset(catalog.operation_ids)
    assert len(catalog) <= 18
