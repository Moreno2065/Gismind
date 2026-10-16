"""Offline buffer-intent regressions; these do not claim real-provider success."""
import pytest

from app.runtime.compiler import AwaitingInputDiagnostic, CompileError, Compiler
from app.runtime.contracts import ArtifactRef, PlannerDraft, PlannerStepDraft, TaskOutputRef, ToolContractRegistry
from app.runtime.goal_requirements import build_goal_requirements
from tests.browser.runtime_chinese_corpus import PHRASES


FIELDS = {"编号": "int64", "名称": "object", "数值": "float64", "类别": "object"}
PREFIX = "先只保留数值严格大于24的站点，等于阈值的不要。"
DELIVERY = "在地图上展示最终结果，并将同一结果导出为地理包空间数据库文件供下载。"
REPLY = ("缓冲半径确定为275米。对之前数值严格大于24的站点逐个缓冲，"
         "不同站点的面不要合并。" + DELIVERY + "保留未要求修改的原始属性。")


def source(kind="Vector"):
    digest = "b" * 64
    crs = {"kind": "EPSG", "code": 3857}
    schema = {"fields": FIELDS, "geometry_types": ["Point"]}
    if kind == "File":
        schema["inspection"] = {"source": "physical_file", "format": "gpkg",
                                "artifact_type": "Vector", "crs": crs,
                                "fields": FIELDS, "geometry_types": ["Point"]}
    return {"artifact_id": "sites", "type": kind, "crs": crs, "schema": schema,
            "content_hash": digest, "storage_uri": "artifacts/sha256/bb/" + digest,
            "size_bytes": 100, "validation_status": "valid"}


def draft_steps(kind="Vector", distance=None):
    tasks = []
    input_ref = ArtifactRef(artifact_id="sites", type=kind, crs={"kind": "EPSG", "code": 3857})
    if kind == "File":
        tasks.append(PlannerStepDraft(task_key="load", op="io.load_vector", inputs={"file": input_ref}))
        input_ref = TaskOutputRef(task_key="load", port="vector")
    tasks.append(PlannerStepDraft(task_key="filter", op="attribute.filter", inputs={"input": input_ref},
                                  params={"field": "数值", "operator": ">", "value": 24}))
    result = TaskOutputRef(task_key="filter", port="output")
    if distance is not None:
        tasks.append(PlannerStepDraft(task_key="buffer", op="vector.buffer", inputs={"vector": result},
                                      params={"distance_m": distance}))
        result = TaskOutputRef(task_key="buffer", port="vector")
    tasks.extend([
        PlannerStepDraft(task_key="map", op="map.build", inputs={"vector": result}),
        PlannerStepDraft(task_key="export", op="file.export", inputs={"input": result},
                         params={"format": "gpkg"}),
    ])
    return tasks


def compile_goal(goal, *, kind="Vector", distance=None, reply=None):
    artifact = source(kind)
    return Compiler(ToolContractRegistry.with_builtins(), artifact_lookup=lambda _: artifact,
                    policy={"trusted_user_goal": goal, "visible_artifacts": [artifact],
                            "revision_patch": {"clarification": reply} if reply else {}}).compile(
        PlannerDraft(run_id="buffer-ambiguity", user_goal=goal, tasks=draft_steps(kind, distance)))


@pytest.mark.parametrize("phrase", PHRASES["buffer_ambiguous"])
@pytest.mark.parametrize("distance", [None, 100])
@pytest.mark.parametrize("kind", ["Vector", "File"])
def test_ambiguous_buffer_cannot_be_omitted_or_given_an_invented_distance(phrase, distance, kind):
    with pytest.raises(AwaitingInputDiagnostic, match="GOAL_FACTS_REQUIRED") as failure:
        compile_goal(PREFIX + phrase + DELIVERY, kind=kind, distance=distance)
    assert any("缓冲" in fact and "距离" in fact for fact in failure.value.details["missing_facts"])


@pytest.mark.parametrize("phrase", PHRASES["buffer_ambiguous"])
def test_concrete_reply_binds_distance_and_retains_filter_map_and_export(phrase):
    plan = compile_goal(PREFIX + phrase + DELIVERY, distance=275, reply=REPLY)
    assert not plan.goal_requirements["missing_facts"]
    buffer = next(item for item in plan.goal_requirements["operations"] if item["op"] == "vector.buffer")
    assert buffer["params"] == {"distance_m": 275}
    assert {task.op for task in plan.tasks} >= {"attribute.filter", "vector.buffer", "map.build", "file.export"}


@pytest.mark.parametrize("phrase", PHRASES["buffer"] + PHRASES["rename_buffer"])
def test_explicit_buffer_synonyms_retain_the_buffer_obligation(phrase):
    requirements = build_goal_requirements(PREFIX + phrase.format(distance_m=275) + DELIVERY, [source()])
    buffer = next((item for item in requirements["operations"] if item["op"] == "vector.buffer"), None)
    assert buffer is not None
    assert buffer["params"] == {"distance_m": 275}
    assert not requirements["missing_facts"]


@pytest.mark.parametrize("phrase", [
    "请把符合数值条件的站点扩成影响区面，大小适中。",
    "请生成筛选后站点的服务范围面，范围适中。",
    "将筛选后的点向外扩展成多边形。",
])
def test_clarified_buffer_still_cannot_be_omitted_or_replaced_with_another_radius(phrase):
    for distance in (None, 100):
        with pytest.raises(CompileError, match="GOAL_REQUIREMENT_MISSING"):
            compile_goal(PREFIX + phrase + DELIVERY, distance=distance, reply=REPLY)


@pytest.mark.parametrize("text", [
    "不要给站点做缓冲区。",
    "请不要把站点扩成影响区面。",
    "不需要为站点生成服务范围面。",
    "不用将站点向外扩展成多边形。",
    "图层名称为缓冲区，属性包括服务范围和影响区面。",
    "附件包含已有的站点服务范围面和影响区面。",
    "将图层名称改为生成缓冲区。",
    "将图层重命名为“生成缓冲区”。",
    "请生成已有缓冲区的统计表。",
    "保留名称字段。缓冲区只是图层说明。",
])
def test_negation_and_layer_descriptions_do_not_require_a_new_buffer(text):
    requirements = build_goal_requirements(text + "在地图上展示附件。", [source()])
    assert not any(item["op"] == "vector.buffer" for item in requirements["operations"])
    assert not any("缓冲" in item for item in requirements["missing_facts"])


@pytest.mark.parametrize("other_distance", ["附件中的站点离道路100米。", "站点平均海拔为100米。"])
def test_an_unrelated_measurement_cannot_supply_an_unspecified_radius(other_distance):
    goal = other_distance + PREFIX + PHRASES["buffer_ambiguous"][8] + DELIVERY
    with pytest.raises(AwaitingInputDiagnostic, match="GOAL_FACTS_REQUIRED"):
        compile_goal(goal, distance=100)


def test_exact_batch04_gpkg_request_pauses_before_compiling_the_observed_omission():
    goal = ("第一个附件为地理包文件。附件位于东经118.77度、北纬32.035度附近。"
            "站点有编号、名称、数值、类别属性。原始坐标采用世界大地坐标系一九八四"
            "（标准代码4326）的经纬度。" + PREFIX + PHRASES["buffer_ambiguous"][8]
            + DELIVERY + "保留未要求修改的原始属性。")
    with pytest.raises(AwaitingInputDiagnostic, match="GOAL_FACTS_REQUIRED") as failure:
        compile_goal(goal, kind="File")
    assert failure.value.details["patch_schema"]["required"] == ["clarification"]


@pytest.mark.parametrize("phrase", [
    "请把离道路100米的站点扩成影响区面，大小适中。",
    "给海拔100米的站点做缓冲，半径暂未确定。",
    "请生成离道路100米的站点的缓冲区，大小适中。",
])
def test_same_clause_other_measurements_are_not_buffer_distances(phrase):
    with pytest.raises(AwaitingInputDiagnostic, match="GOAL_FACTS_REQUIRED"):
        compile_goal(PREFIX + phrase + DELIVERY, distance=100)


@pytest.mark.parametrize("number", ["1，000", "1,000"])
def test_thousands_separators_do_not_split_buffer_intent(number):
    requirements = build_goal_requirements(f"做{number}米缓冲，并在地图显示。", [source()])
    buffer = next((item for item in requirements["operations"] if item["op"] == "vector.buffer"), None)
    assert buffer is not None
    assert buffer["params"] == {"distance_m": 1000}
    assert not requirements["missing_facts"]


@pytest.mark.parametrize("phrase", [
    "给海拔100米的站点做275米缓冲。",
    "请把离道路100米的站点扩成半径275米的影响区面。",
])
def test_explicit_radius_after_an_unrelated_measurement_is_preserved(phrase):
    plan = compile_goal(PREFIX + phrase + DELIVERY, distance=275)
    buffer = next(item for item in plan.goal_requirements["operations"] if item["op"] == "vector.buffer")
    assert buffer["params"] == {"distance_m": 275}
    assert not plan.goal_requirements["missing_facts"]


def test_distinct_alternative_radii_remain_ambiguous():
    with pytest.raises(AwaitingInputDiagnostic, match="GOAL_FACTS_REQUIRED"):
        compile_goal(PREFIX + "做缓冲100米或275米。" + DELIVERY, distance=100)
