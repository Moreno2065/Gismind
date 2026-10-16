"""Offline sequence-scope regressions; frozen browser text is read-only input."""
import pytest

from app.runtime.compiler import AwaitingInputDiagnostic, CompileError, Compiler
from app.runtime.contracts import PlannerDraft, PlannerStepDraft, TaskOutputRef, ToolContractRegistry
from app.runtime.goal_requirements import build_goal_requirements
from app.runtime.llm_planner import PlannerRequest
from app.runtime.queue import RuntimeSupervisor
from tests.browser.runtime_chinese_corpus import PHRASES, load_cases
from tests.unit.test_runtime_buffer_ambiguity_grounding import draft_steps, source


CASES = tuple(case for case in load_cases() if case.family == "sequential_buffers")


def replace(step, **updates):
    return step.model_copy(update=updates)


def tasks(first=30, second=95, threshold=12):
    result = draft_steps(kind="File", distance=first)
    result[1] = replace(result[1], params={"field": "数值", "operator": ">", "value": threshold})
    ref = TaskOutputRef(task_key="buffer", port="vector")
    result.insert(-2, PlannerStepDraft(task_key="second", op="vector.buffer", inputs={"vector": ref},
                                     params={"distance_m": second}))
    result[-2] = replace(result[-2], inputs={"vector": TaskOutputRef(task_key="second", port="vector")})
    result[-1] = replace(result[-1], inputs={"input": TaskOutputRef(task_key="second", port="vector")})
    return result


def request(goal, patch=None):
    return PlannerRequest(run_id="sequential-scope", input_revision=1, user_goal=goal,
                          input_artifacts=(source("File"),), revision_patch=patch or {})


def compile_tasks(goal, steps, patch=None):
    artifact = source("File")
    return Compiler(ToolContractRegistry.with_builtins(), artifact_lookup=lambda _: artifact,
                    policy={"trusted_user_goal": goal, "visible_artifacts": [artifact],
                            "revision_patch": patch or {}}).compile(
        PlannerDraft(run_id="sequential-scope", user_goal=goal, tasks=steps))


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.case_id)
def test_all_200_frozen_sequences_bind_each_occurrence_and_compile(case):
    first, second = case.params["first_distance_m"], case.params["second_distance_m"]
    requirements = build_goal_requirements(case.prompt, [source("File")])
    stages = requirements.get("ordered_stages", [])
    assert [stage["requirements"][0]["params"]["distance_m"] for stage in stages] == [first, second]
    assert not requirements["missing_facts"]
    steps = tasks(first, second, case.params["threshold"])
    RuntimeSupervisor._guard_buffer_distance(request(case.prompt), PlannerDraft(tasks=steps))
    plan = compile_tasks(case.prompt, steps)
    assert len([task for task in plan.tasks if task.op == "vector.buffer"]) == 2


@pytest.mark.parametrize("profile", [1, 2, 3])
@pytest.mark.parametrize("mutation", ["omit", "parallel", "wrong_distance", "reverse", "extra"])
def test_fixed_surface_sequence_still_rejects_plan_mutations(profile, mutation):
    case = next(case for case in CASES if case.profile == profile and case.paraphrase == 3)
    first, second = case.params["first_distance_m"], case.params["second_distance_m"]
    steps = tasks(first, second, case.params["threshold"])
    if mutation == "omit":
        steps.pop(3)
        steps[-2] = replace(steps[-2], inputs={"vector": TaskOutputRef(task_key="buffer", port="vector")})
        steps[-1] = replace(steps[-1], inputs={"input": TaskOutputRef(task_key="buffer", port="vector")})
    elif mutation == "parallel":
        steps[3] = replace(steps[3], inputs={"vector": TaskOutputRef(task_key="filter", port="output")})
    elif mutation == "wrong_distance":
        steps[3] = replace(steps[3], params={"distance_m": second + 1})
    elif mutation == "reverse":
        steps[2] = replace(steps[2], params={"distance_m": second})
        steps[3] = replace(steps[3], params={"distance_m": first})
    else:
        steps.insert(-2, PlannerStepDraft(task_key="extra", op="vector.buffer",
            inputs={"vector": TaskOutputRef(task_key="second", port="vector")}, params={"distance_m": second}))
        steps[-2] = replace(steps[-2], inputs={"vector": TaskOutputRef(task_key="extra", port="vector")})
        steps[-1] = replace(steps[-1], inputs={"input": TaskOutputRef(task_key="extra", port="vector")})
    with pytest.raises(CompileError, match="GOAL_LINEAGE_MISMATCH"):
        compile_tasks(case.prompt, steps)


@pytest.mark.parametrize("second", ["适当", "95", "30米或95米"])
def test_surface_sequence_missing_or_ambiguous_units_still_pause(second):
    goal = f"先生成筛选点周边30米的面，再以该面为上一步输入扩展{second}，请分别执行两次缓冲。"
    with pytest.raises(AwaitingInputDiagnostic):
        RuntimeSupervisor._guard_buffer_distance(request(goal), PlannerDraft(tasks=tasks()))
        compile_tasks(goal, tasks())


@pytest.mark.parametrize("goal", [
    "先生成筛选点周边30米的面，再对原始点扩展95米，请分别执行两次缓冲。",
    "先生成筛选点周边30米的面，不要以该面为上一步输入扩展95米。",
    "生成点周边30米或95米的面。",
    "生成筛选点周边30米的面，附近道路距离95米。",
    "先生成筛选点周边30米的面，再以该面为上一步输入向内扩展95米。",
    "先生成筛选点周边30米的面，再以该面为下一步输入扩展95米。",
])
def test_single_or_unbound_operations_do_not_acquire_a_sequence(goal):
    assert not build_goal_requirements(goal).get("ordered_stages")
    with pytest.raises(AwaitingInputDiagnostic):
        RuntimeSupervisor._guard_buffer_distance(request(goal), PlannerDraft(tasks=tasks()))


def test_unscoped_reply_does_not_overwrite_two_surface_steps():
    goal = PHRASES["sequential_buffers"][3].format(first_distance_m=30, second_distance_m=95)
    with pytest.raises(AwaitingInputDiagnostic):
        compile_tasks(goal, tasks(50, 50), {"clarification": "缓冲距离改为50米。"})


@pytest.mark.parametrize("goal", [
    "先生成筛选点周边30米的面，再以该面为上一步输入扩展95米，最后对原始点扩展120米。",
    "先生成筛选点周边30米的面，再以该面为上一步输入扩展95米，最后以第二步结果为输入扩展120米。",
    "先生成点周边15米的面，另对原始点生成周边30米的面，再以该面为上一步输入扩展95米。",
    "先生成筛选点周边30米的面，再以该面为上一步输入不扩展95米。",
    "图层名称为生成筛选点周边30米的面，再以该面为上一步输入扩展95米。",
    "先生成筛选点周边30米的面，再以该面为下一步输入扩展95米。",
    "先生成筛选点周边30米的面，再对原始点扩展95米。",
    "先做30米缓冲，再以上一步结果扩展95米，最后对原始点扩展120米。",
    "先做30米缓冲，再以上一步结果不扩展95米。",
    "图层名称为先做30米缓冲，再以上一步结果扩展95米。",
])
@pytest.mark.parametrize("buffer_count", [0, 1, 2])
def test_ungrounded_surface_sequence_cannot_silently_drop_requested_actions(goal, buffer_count):
    steps = tasks() if buffer_count == 2 else draft_steps(kind="File", distance=30 if buffer_count else None)
    with pytest.raises(AwaitingInputDiagnostic):
        RuntimeSupervisor._guard_buffer_distance(request(goal), PlannerDraft(tasks=steps))
        compile_tasks(goal, steps)
    # The compiler must reject even when there are no buffers for the
    # supervisor's distance guard to inspect.
    with pytest.raises(AwaitingInputDiagnostic, match="GOAL_FACTS_REQUIRED"):
        compile_tasks(goal, steps)


@pytest.mark.parametrize("unbound", ["以该面为下一步输入", "对原始点"])
def test_complete_clarification_resolves_sequence_scope_without_erasing_filter(unbound):
    goal = ("先只保留数值严格大于12的站点。先生成筛选点周边30米的面，"
            f"再{unbound}扩展95米。在地图上展示结果，并导出为地理包文件。")
    patch = {"clarification": "先生成筛选点周边30米的面，再以该面为上一步输入扩展95米。"}
    requirements = build_goal_requirements(goal, [source("File")], patch)
    assert not requirements["missing_facts"]
    assert requirements["conditions"][0]["params"] == {
        "field": "数值", "operator": ">", "value": 12, "negate": False}
    assert {item["op"] for item in requirements["operations"]} >= {"map.build", "file.export"}
    RuntimeSupervisor._guard_buffer_distance(request(goal, patch), PlannerDraft(tasks=tasks()))
    compile_tasks(goal, tasks(), patch)
    with pytest.raises(CompileError, match="GOAL_REQUIREMENT_MISSING"):
        compile_tasks(goal, tasks(threshold=13), patch)


@pytest.mark.parametrize("reply", ["95米。", "再对上一步扩展95米。", "先生成筛选点周边30米的面，再对原始点扩展95米。"])
def test_incomplete_clarification_cannot_clear_sequence_scope(reply):
    goal = "先生成筛选点周边30米的面，再以该面为下一步输入扩展95米。"
    with pytest.raises(AwaitingInputDiagnostic, match="GOAL_FACTS_REQUIRED"):
        compile_tasks(goal, tasks(), {"clarification": reply})


def test_clarification_cannot_omit_an_original_third_buffer_action():
    goal = "先生成筛选点周边30米的面，再以该面为上一步输入扩展95米，最后对原始点扩展120米。"
    reply = "先生成筛选点周边30米的面，再以该面为上一步输入扩展95米。"
    with pytest.raises(AwaitingInputDiagnostic, match="GOAL_FACTS_REQUIRED"):
        compile_tasks(goal, tasks(), {"clarification": reply})


@pytest.mark.parametrize("prefix", ["", "图层名称为先做30米缓冲，"])
@pytest.mark.parametrize("input_text", ["原始站点中筛选出的点", "上传站点的筛选结果", "筛选后的站点"])
def test_orphan_single_step_requires_complete_explicit_input_clarification(prefix, input_text):
    goal = f"先只保留数值严格大于12的站点。{prefix}再以上一步结果扩展95米。"
    patch = {"clarification": f"对{input_text}做95米缓冲。"}
    steps = draft_steps(kind="File", distance=95)
    steps[1] = replace(steps[1], params={"field": "数值", "operator": ">", "value": 12})
    requirements = build_goal_requirements(goal, [source("File")], patch)
    assert not requirements["missing_facts"]
    assert requirements["conditions"][0]["params"]["value"] == 12
    RuntimeSupervisor._guard_buffer_distance(request(goal, patch), PlannerDraft(tasks=steps))
    compile_tasks(goal, steps, patch)
    omitted = draft_steps(kind="File")
    omitted[1] = replace(omitted[1], params=dict(steps[1].params))
    for wrong_steps in [omitted, tasks(95, 95),
                        [replace(step, params={"distance_m": 96}) if step.op == "vector.buffer" else step
                         for step in steps],
                        [replace(step, params={"field": "数值", "operator": ">", "value": 13})
                         if step.op == "attribute.filter" else step for step in steps]]:
        with pytest.raises(CompileError):
            compile_tasks(goal, wrong_steps, patch)


@pytest.mark.parametrize("reply", [
    "95米。", "做95米缓冲。", "对上一步结果做95米缓冲。",
    "对筛选结果做95米或120米缓冲。", "对筛选结果做缓冲。",
    "对筛选结果不扩展95米。", "对筛选结果向内扩展95米。",
])
def test_orphan_single_step_incomplete_or_invalid_answers_remain_paused(reply):
    goal = "再以上一步结果扩展95米。"
    with pytest.raises(AwaitingInputDiagnostic, match="GOAL_FACTS_REQUIRED"):
        compile_tasks(goal, draft_steps(kind="File", distance=95), {"clarification": reply})


@pytest.mark.parametrize("extra", [False, True])
def test_existing_rename_then_single_buffer_keeps_exact_ordered_occurrence(extra):
    goal = "先把名称字段重命名为新名，再做95米缓冲。在地图上展示结果，并导出为地理包文件。"
    steps = tasks(95, 95) if extra else draft_steps(kind="File", distance=95)
    steps.insert(2, PlannerStepDraft(task_key="rename", op="attribute.rename_field",
        inputs={"input": TaskOutputRef(task_key="filter", port="output")},
        params={"old_name": "名称", "new_name": "新名"}))
    steps[3] = replace(steps[3], inputs={"vector": TaskOutputRef(task_key="rename", port="output")})
    requirements = build_goal_requirements(goal, [source("File")])
    assert [item["op"] for stage in requirements["ordered_stages"]
            for item in stage["requirements"]] == ["attribute.rename_field", "vector.buffer"]
    if extra:
        with pytest.raises(CompileError, match="GOAL_LINEAGE_MISMATCH"):
            compile_tasks(goal, steps)
    else:
        compile_tasks(goal, steps)
