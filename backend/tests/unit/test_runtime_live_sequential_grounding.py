"""Offline grounding regressions from real browser failures, not live evidence."""
import json

import pytest

from app.runtime.compiler import AwaitingInputDiagnostic, CompileError
from app.runtime.contracts import PlannerDraft, TaskOutputRef, ToolContractRegistry
from app.runtime.goal_requirements import build_goal_requirements
from app.runtime.llm_planner import (
    OpenAICompatiblePlanner, PlannerRequest, _verified_buffer_distance_m, candidate_catalog,
)
from app.runtime.queue import RuntimeSupervisor
from tests.unit.test_runtime_composition_refinements import buffer_chain
from tests.unit.test_runtime_harness_goal_coverage import compile_goal, ref, source, step


NATURAL = (
    "筛选之后逐点外扩{first}米，随后把上一步缓冲区继续外扩{second}米，保留这两个处理步骤。"
    "在地图上展示最终结果。"
)


def request(goal, patch=None):
    return PlannerRequest(run_id="sequential-grounding", input_revision=1,
        user_goal=goal, input_artifacts=(source(),), revision_patch=patch or {})


@pytest.mark.parametrize(("first", "second"), [(30, 95), (35, 115), (30, 30)])
def test_real_browser_natural_sequence_has_separate_distance_and_lineage_proofs(first, second):
    goal = NATURAL.format(first=first, second=second)
    draft = PlannerDraft(tasks=buffer_chain(first, second))
    RuntimeSupervisor._guard_buffer_distance(request(goal), draft)
    plan = compile_goal(goal, draft.tasks)
    stages = plan.goal_requirements["ordered_stages"]
    assert [s["requirements"][0]["params"]["distance_m"] for s in stages] == [first, second]


@pytest.mark.parametrize("goal", [
    "先对点做30米缓冲，再对上一步的面做95米缓冲，在地图显示结果。",
    "第一步为站点建立30米缓冲面，第二步基于第一步结果再缓冲95米，在地图显示结果。",
    "对站点先缓冲30米，然后对上一步产生的多边形缓冲95米，在地图显示结果。",
    "选中的点先外扩30米，上一步的面再外扩95米，在地图显示结果。",
    "先以选中点为中心扩展30米，然后以第一步输出的面为输入继续扩展95米，在地图显示结果。",
    "先对点做零点零三公里缓冲，再对前一步的面做九十五米缓冲，在地图显示结果。",
])
def test_explicit_previous_result_synonyms_are_grounded_independently_of_plan(goal):
    RuntimeSupervisor._guard_buffer_distance(request(goal), PlannerDraft(tasks=buffer_chain(30, 95)))
    assert len(compile_goal(goal, buffer_chain(30, 95)).goal_requirements["ordered_stages"]) == 2


@pytest.mark.parametrize("mutation", ["reverse", "original", "skip_second", "collapse", "extra"])
def test_natural_sequence_does_not_waive_compiler_order_and_occurrence_validation(mutation):
    goal = NATURAL.format(first=30, second=95)
    tasks = buffer_chain(30, 95)
    if mutation == "reverse":
        tasks = buffer_chain(95, 30)
    elif mutation == "original":
        tasks[1] = step("second", "vector.buffer", ref(), {"distance_m": 95})
    elif mutation == "skip_second":
        tasks[-1] = step("map", "map.build", TaskOutputRef(task_key="first", port="vector"))
    elif mutation == "collapse":
        tasks = [tasks[0], step("map", "map.build", TaskOutputRef(task_key="first", port="vector"))]
    else:
        tasks.insert(-1, step("extra", "vector.buffer", TaskOutputRef(task_key="second", port="vector"), {"distance_m": 95}))
        tasks[-1] = step("map", "map.build", TaskOutputRef(task_key="extra", port="vector"))
    with pytest.raises(CompileError, match="GOAL_LINEAGE_MISMATCH"):
        compile_goal(goal, tasks)


@pytest.mark.parametrize("goal", [
    "对站点做30米或95米缓冲，在地图显示。",
    "先对站点做30米缓冲，再对原始站点做95米缓冲，在地图显示。",
    "先对站点做30米缓冲，附近道路距离95米，在地图显示。",
    "先对站点做30米缓冲，不要对上一步做95米缓冲，在地图显示。",
])
def test_two_distances_without_explicit_positive_previous_result_binding_still_clarify(goal):
    assert not build_goal_requirements(goal).get("ordered_stages")
    with pytest.raises(AwaitingInputDiagnostic):
        RuntimeSupervisor._guard_buffer_distance(request(goal), PlannerDraft(tasks=buffer_chain(30, 95)))


@pytest.mark.parametrize("second", ["适当", "95", "30米或95米"])
def test_ambiguous_or_unitless_stage_still_requires_user_fact(second):
    goal = f"先对点做30米缓冲，再对上一步结果做{second}缓冲，在地图显示。"
    with pytest.raises(AwaitingInputDiagnostic):
        RuntimeSupervisor._guard_buffer_distance(request(goal), PlannerDraft(tasks=buffer_chain(30, 95)))
        compile_goal(goal, buffer_chain(30, 95))


def test_unscoped_clarification_does_not_broadcast_one_distance_to_natural_stages():
    goal = NATURAL.format(first=30, second=95)
    patch = {"clarification": "缓冲改成50米。"}
    with pytest.raises(AwaitingInputDiagnostic):
        compile_goal(goal, buffer_chain(50, 50), patch=patch)


def test_explicit_complete_sequence_answer_can_resolve_a_missing_stage_distance():
    goal = "先对站点做30米缓冲，再对上一步结果做适当缓冲，在地图显示结果。"
    patch = {"clarification": "先对站点做30米缓冲，再对上一步结果做95米缓冲。"}
    plan = compile_goal(goal, buffer_chain(30, 95), patch=patch)
    assert [s["requirements"][0]["params"]["distance_m"]
            for s in plan.goal_requirements["ordered_stages"]] == [30, 95]


def test_natural_stage_prompt_does_not_advertise_an_op_wide_scalar():
    goal = NATURAL.format(first=30, second=95)
    req = request(goal)
    assert _verified_buffer_distance_m(req) is None
    registry = ToolContractRegistry.with_builtins()
    planner = OpenAICompatiblePlanner(api_key="unused", base_url="https://unused.invalid", model="unused",
        registry=registry, timeout_seconds=1, max_tokens=1000, max_retries=0)
    message = json.loads(planner._messages(req, candidate_catalog(registry, req))[-1]["content"])
    assert "vector.buffer" not in message.get("verified_request_facts", {})
    assert len(message["goal_requirements"]["ordered_stages"]) == 2
