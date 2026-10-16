"""Deterministic coverage of explicitly requested parallel deliveries."""
import pytest

from app.runtime.compiler import AwaitingInputDiagnostic, CompileError, Compiler
from app.runtime.contracts import ArtifactRef, PlannerDraft, PlannerStepDraft


def compile_formats(goal, formats, patch=None):
    return Compiler(policy={"trusted_user_goal": goal, "revision_patch": patch or {}}).compile(
        PlannerDraft(user_goal=goal, tasks=[PlannerStepDraft(task_key="export_" + str(index), op="file.export",
            inputs={"input": ArtifactRef(artifact_id="vector", type="Vector", crs={"kind": "EPSG", "code": 4326})},
            params={"format": format}) for index, format in enumerate(formats)]))


def test_two_explicit_formats_are_two_required_deliveries():
    plan = compile_formats("请将结果分别导出为GeoJSON和GPKG，两种都需要。", ["geojson", "gpkg"])
    assert {t.params["format"] for t in plan.tasks} == {"geojson", "gpkg"}
    with pytest.raises(CompileError, match="GOAL_REQUIREMENT_MISSING"):
        compile_formats("请将结果分别导出为GeoJSON和GPKG，两种都需要。", ["geojson"])


def test_ambiguous_format_choice_is_not_silently_both_or_one():
    with pytest.raises(AwaitingInputDiagnostic, match="EXPORT_FORMAT_CHOICE_REQUIRED"):
        compile_formats("导出为GeoJSON或GPKG。", ["geojson", "gpkg"])


@pytest.mark.parametrize("goal", ["导出结果为GeoJSON，不要导出GPKG。", "导出结果为GeoJSON但不要导出GPKG。"])
def test_negated_format_is_not_a_required_or_allowed_additional_delivery(goal):
    assert len(compile_formats(goal, ["geojson"]).tasks) == 1
    with pytest.raises(CompileError, match="export format contradicts"):
        compile_formats(goal, ["geojson", "gpkg"])


def test_later_explicit_export_format_choice_can_resolve_unsupported_original():
    plan = compile_formats("导出为CSV。", ["gpkg"], {"clarification": "那就导出为GPKG。"})
    assert plan.tasks[0].params["format"] == "gpkg"


@pytest.mark.parametrize("goal", [
    "分别独立完成：1、对原始地块做100米缓冲并在地图上显示。2、对原始地块做500米缓冲并导出GPKG。",
    "请按顺序完成：1、对原始地块做100米缓冲。2、对上一步结果再做500米缓冲并导出GPKG。",
])
def test_explicit_buffer_distances_defer_to_compiler_composition_check(goal):
    from app.runtime.llm_planner import PlannerRequest
    from app.runtime.queue import RuntimeSupervisor
    request = PlannerRequest(run_id="run", input_revision=1, user_goal=goal, input_artifacts=(), revision_patch={})
    draft = PlannerDraft(tasks=[PlannerStepDraft(task_key="b" + str(value), op="vector.buffer",
        inputs={"vector": ArtifactRef(artifact_id="vector", type="Vector", crs={"kind": "EPSG", "code": 4326})},
        params={"distance_m": value}) for value in [100, 500]])
    RuntimeSupervisor._guard_buffer_distance(request, draft)
