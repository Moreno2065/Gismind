"""A planner may only use field names supported by the request and upload."""

import json

import pytest

from app.runtime import PlannerDraft, PlannerStepDraft, RuntimeSupervisor
from app.runtime.compiler import AwaitingInputDiagnostic, CompileError
from app.runtime.llm_planner import (
    OpenAICompatiblePlanner, PlannerError, PlannerRequest, _ambiguous_rename_source,
    _ambiguous_rename_target,
    _verified_rename_binding, PROMPT_VERSION,
)


def _request(goal, *, fields=("楼栋", "类别")):
    return PlannerRequest(
        run_id="rename-run", input_revision=1, user_goal=goal,
        input_artifacts=({
            "artifact_id": "art-input", "type": "File",
            "schema": {"inspection": {"artifact_type": "Vector", "fields": {name: "object" for name in fields}}},
        },), revision_patch={},
    )


@pytest.mark.parametrize(("goal", "expected"), [
    ("将楼栋列换成用途列。", None),
    ("楼栋列的数据不变，只把列名改为用途。", ("楼栋", "用途")),
    ("保留记录，将字段楼栋更名为用途。", ("楼栋", "用途")),
    ("把‘楼栋’列改名为‘用途列’，这三个字都属于字段名。", ("楼栋", "用途列")),
    ("将名称列换成新名列。", None),
    ("名称列的数据不变，只把列名改为新名列。", None),
    ("字段名称不合适，改叫新名。", None),
    ("保留名称列，投影坐标系改为EPSG:3857。", None),
    ("把名称列的空值改为未知。", None),
    ("把楼栋列的属性值改成用途。", None),
    ("将建筑名称列改为新名。", None),
    ("把旧楼栋列改为用途。", None),
    ("保留名称列，将建筑列重命名为新名。", None),
    ("请把名称列保留，将建筑列重命名为新名。", None),
    ("请把名称列保留并将建筑列重命名为新名。", None),
    ("把名称字段改名为新-名。", None),
    ("把名称列改为新名即可。", None),
    ("把名称列改为新名后再导出。", None),
    ("别把楼栋改成用途，先看数据。", None),
    ("楼栋和类别都要处理，改成用途。", None),
    ("计算楼栋覆盖范围，类别照旧。", None),
])
def test_only_explicit_rename_binding_is_grounded(goal, expected):
    fields = ("名称", "数值", "类别", "编号") if "名称" in goal else ("楼栋", "类别")
    assert _verified_rename_binding(_request(goal, fields=fields)) == expected


def test_rename_binding_requires_verified_physical_field_schema():
    request = _request("将楼栋列改为用途列。")
    uninspected = PlannerRequest(
        run_id=request.run_id, input_revision=1, user_goal=request.user_goal,
        input_artifacts=({"artifact_id": "art-input", "type": "File", "schema": {}},),
        revision_patch={},
    )
    assert _verified_rename_binding(uninspected) is None


@pytest.mark.parametrize(("goal", "expected"), [
    ("需要将属性表中的名称列换成新名列。", ("名称", "新名列", "新名")),
    ("楼栋列的数据不变，只把列名改为用途列。", ("楼栋", "用途列", "用途")),
    ("将名称列换成“新名列”。", None),
    ("名称列的数据不变，只把列名改为新名。", None),
    ("把名称列的属性值改成新名列。", None),
    ("保留名称列，投影坐标系改为EPSG:3857。", None),
])
def test_unquoted_column_suffix_is_classified_only_for_a_schema_grounded_rename(goal, expected):
    fields = ("名称", "数值") if "名称" in goal else ("楼栋", "类别")
    assert _ambiguous_rename_target(_request(goal, fields=fields)) == expected


@pytest.mark.parametrize(("answer", "target", "error"), [
    (None, "新名列", AwaitingInputDiagnostic),
    ("好的", "新名列", AwaitingInputDiagnostic),
    ("新名", "新名列", CompileError),
    ("新名列", "新名列", None),
    ("目标字段名是“新名”", "新名", None),
])
def test_user_choice_not_model_guess_controls_ambiguous_rename(answer, target, error):
    request = _request("需要将属性表中的名称列换成新名列。", fields=("名称", "数值"))
    if answer is not None:
        request = PlannerRequest(
            run_id=request.run_id, input_revision=2, user_goal=request.user_goal,
            input_artifacts=request.input_artifacts, revision_patch={"clarification": answer},
        )
    draft = PlannerDraft(origin="llm", tasks=[PlannerStepDraft(
        task_key="rename", op="attribute.rename_field",
        params={"old_name": "名称", "new_name": target},
    )])
    if error is None:
        RuntimeSupervisor._guard_ambiguous_rename(request, draft)
    else:
        with pytest.raises(error):
            RuntimeSupervisor._guard_ambiguous_rename(request, draft)


@pytest.mark.parametrize(("goal", "expected"), [
    ("字段名称不合适，改叫新名并输出图层。", ("名称", "新名")),
    ("保留所有记录，将字段名称更名为新名。", ("名称", "新名")),
    ("把名称字段改名为新名。", None),
    ("把“名称”字段改名为新名。", None),
    ("请调整字段名称，将数值列改为新数值。", None),
])
def test_generic_field_name_is_not_silently_bound_to_a_column_named_name(goal, expected):
    assert _ambiguous_rename_source(_request(goal, fields=("名称", "数值"))) == expected


@pytest.mark.parametrize(("answer", "model_source", "error"), [
    (None, "名称", AwaitingInputDiagnostic),
    ("好的", "名称", AwaitingInputDiagnostic),
    ("名称", "名称", None),
    ("原字段名是“数值”", "数值", None),
    ("数值", "名称", CompileError),
])
def test_source_field_choice_must_match_user_clarification(answer, model_source, error):
    request = _request("字段名称不合适，改叫新名并输出图层。", fields=("名称", "数值"))
    if answer is not None:
        request = PlannerRequest(
            run_id=request.run_id, input_revision=2, user_goal=request.user_goal,
            input_artifacts=request.input_artifacts, revision_patch={"clarification": answer},
        )
    draft = PlannerDraft(origin="llm", tasks=[PlannerStepDraft(
        task_key="rename", op="attribute.rename_field",
        params={"old_name": model_source, "new_name": "新名"},
    )])
    if error is None:
        RuntimeSupervisor._guard_ambiguous_rename(request, draft)
    else:
        with pytest.raises(error):
            RuntimeSupervisor._guard_ambiguous_rename(request, draft)


def _planner(monkeypatch, answer):
    planner = OpenAICompatiblePlanner(
        api_key="test", base_url="https://api.deepseek.com/v1", model="deepseek-flash",
        timeout_seconds=1, max_tokens=512, max_retries=0,
    )
    response = {"choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)}}]}
    monkeypatch.setattr(planner, "_call", lambda messages: (response, 1))
    return planner


def test_known_rename_fact_rejects_redundant_model_clarification(monkeypatch):
    planner = _planner(monkeypatch, {
        "draft_version": "1", "origin": "needs_clarification", "tasks": [],
        "clarification": "楼栋列要改成哪个新名字？",
    })
    with pytest.raises(PlannerError) as error:
        planner.create_draft(_request("楼栋列的数据不变，只把列名改为用途。"))
    assert error.value.code == "LLM_DRAFT_INVALID"
    assert error.value.diagnostics["verified_rename_binding"] == {"old_name": "楼栋", "new_name": "用途"}


def test_known_rename_fact_rejects_wrong_executable_target(monkeypatch):
    planner = _planner(monkeypatch, {
        "draft_version": "1", "origin": "llm", "tasks": [{
            "task_key": "rename", "op": "attribute.rename_field", "inputs": {},
            "params": {"old_name": "楼栋", "new_name": "用途列"},
        }],
    })
    with pytest.raises(PlannerError) as error:
        planner.create_draft(_request("将楼栋列换成用途。"))
    assert error.value.code == "LLM_DRAFT_INVALID"
    assert error.value.diagnostics["verified_rename_binding"]["new_name"] == "用途"


def test_quoted_literal_target_is_not_stripped(monkeypatch):
    planner = _planner(monkeypatch, {
        "draft_version": "1", "origin": "llm", "tasks": [{
            "task_key": "rename", "op": "attribute.rename_field", "inputs": {},
            "params": {"old_name": "楼栋", "new_name": "用途列"},
        }],
    })
    call = planner.create_draft(_request("把‘楼栋’列改名为‘用途列’，这三个字都属于字段名。"))
    assert call.draft.tasks[0].params["new_name"] == "用途列"


def test_invalid_draft_shape_is_reported_without_coercing_user_intent(monkeypatch):
    planner = _planner(monkeypatch, {
        "draft_version": "1", "origin": "llm", "tasks": [{
            "task_key": "rename", "op": "attribute.rename_field", "inputs": {},
            "params": {"old_name": "楼栋", "new_name": "用途"},
        }],
        "clarification": "还要修改其他字段吗？",
    })
    with pytest.raises(PlannerError) as error:
        planner.create_draft(_request("楼栋列的数据不变，只把列名改为用途。"))
    assert error.value.code == "LLM_DRAFT_INVALID"
    assert error.value.diagnostics["draft_shape"] == {
        "origin": "llm", "task_count": 1, "clarification_present": True,
    }


def test_mixed_clarification_discards_unsafe_task_instead_of_failing_run(monkeypatch):
    """A real provider may include a guessed task beside a concrete question."""
    planner = _planner(monkeypatch, {
        "draft_version": "1", "origin": "needs_clarification",
        "clarification": "请明确要更名的原字段名。",
        "tasks": [{
            "task_key": "rename", "op": "attribute.rename_field", "inputs": {},
            "params": {"old_name": "名称", "new_name": "新名"},
        }],
    })
    call = planner.create_draft(_request(
        "保留所有记录，将字段名称更名为新名。", fields=("名称", "数值"),
    ))
    assert call.draft.origin == "needs_clarification"
    assert call.draft.tasks == []
    assert call.draft.clarification == "请明确要更名的原字段名。"


def test_bounded_repair_receives_verified_facts_and_can_return_valid_draft(monkeypatch):
    request = _request("楼栋列的数据不变，只把列名改为用途。")
    first = {
        "draft_version": "1", "origin": "needs_clarification", "tasks": [],
        "clarification": "楼栋字段要改成什么？",
    }
    second = {
        "draft_version": "1", "origin": "llm", "tasks": [{
            "task_key": "rename", "op": "attribute.rename_field", "inputs": {},
            "params": {"old_name": "楼栋", "new_name": "用途"},
        }],
    }
    planner = _planner(monkeypatch, first)
    seen_messages = []
    answers = iter((first, second))

    def call(messages):
        seen_messages.append(messages)
        answer = next(answers)
        return {"choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)}}]}, 1

    monkeypatch.setattr(planner, "_call", call)
    with pytest.raises(PlannerError) as first_error:
        planner.create_draft(request)
    repaired = planner.repair_draft(
        request, rejected_draft=None, diagnostic=first_error.value.diagnostics,
    )

    assert repaired.draft.origin == "llm"
    assert repaired.draft.tasks[0].params == {"old_name": "楼栋", "new_name": "用途"}
    assert len(seen_messages) == 2
    assert "verified_request_facts" in seen_messages[1][-1]["content"]
    assert "verified_rename_binding" in seen_messages[1][-1]["content"]


def test_explicit_rename_does_not_block_clarification_for_another_missing_fact(monkeypatch):
    planner = _planner(monkeypatch, {
        "draft_version": "1", "origin": "needs_clarification", "tasks": [],
        "clarification": "缓冲距离是多少米？",
    })
    call = planner.create_draft(_request("将楼栋列改名为用途，再给结果做缓冲，但没有说距离。"))
    assert call.draft.origin == "needs_clarification"
    assert call.draft.clarification == "缓冲距离是多少米？"


def _real_followup_request(*, schema_fields=("old_name", "quantity"), answer="将 old_name 字段改为 new_name，其他属性和几何保持不变。"):
    request = _request(
        "把附件里的一个字段改名为 new_name；如果还缺原字段名，请先问我。",
        fields=schema_fields,
    )
    return PlannerRequest(
        run_id=request.run_id, input_revision=2, user_goal=request.user_goal,
        input_artifacts=request.input_artifacts, revision_patch={"clarification": answer},
    )


def test_real_followup_text_binds_rename_without_changing_original_goal():
    request = _real_followup_request()
    planner = OpenAICompatiblePlanner(
        api_key="offline", base_url="https://api.deepseek.com", model="deepseek-flash",
        timeout_seconds=1, max_tokens=512, max_retries=0,
    )
    from app.runtime.llm_planner import candidate_catalog

    payload = json.loads(planner._messages(request, candidate_catalog(planner.registry, request))[1]["content"])

    assert _verified_rename_binding(request) == ("old_name", "new_name")
    assert payload["user_goal"] == request.user_goal
    assert payload["revision_patch"] == request.revision_patch
    assert payload["authoritative_user_request"] == {
        "original_goal": request.user_goal,
        "latest_clarification": request.revision_patch["clarification"],
    }
    assert payload["verified_request_facts"]["rename"] == {"old_name": "old_name", "new_name": "new_name"}
    assert planner.prompt_version == PROMPT_VERSION


def test_real_followup_repeated_source_question_is_invalid_draft(monkeypatch):
    planner = _planner(monkeypatch, {
        "draft_version": "1", "origin": "needs_clarification", "tasks": [],
        "clarification": "请明确要改名的原字段名：附件 points.geojson 现有字段为 old_name 和 quantity，您想把哪一个改名为 new_name？",
    })
    with pytest.raises(PlannerError) as error:
        planner.create_draft(_real_followup_request())
    assert error.value.code == "LLM_DRAFT_INVALID"
    assert error.value.diagnostics["verified_rename_binding"] == {"old_name": "old_name", "new_name": "new_name"}


def test_followup_rename_binding_still_requires_inspected_existing_source():
    assert _verified_rename_binding(_real_followup_request(schema_fields=("quantity",))) is None
    assert _verified_rename_binding(_real_followup_request(answer="将 old_name 字段改为 quantity。")) is None


@pytest.mark.parametrize(("answer", "expected"), [
    ("不要把楼栋列改为用途，先核对数据。", None),
    ("把楼栋列改为类别。", None),
    ("把楼栋列改为新用途。", ("楼栋", "新用途")),
])
def test_later_user_rename_update_never_reuses_stale_original_binding(answer, expected):
    original = _request("楼栋列的数据不变，只把列名改为用途。")
    request = PlannerRequest(
        run_id=original.run_id, input_revision=2, user_goal=original.user_goal,
        input_artifacts=original.input_artifacts, revision_patch={"clarification": answer},
    )
    assert _verified_rename_binding(request) == expected


def test_negated_followup_rejects_stale_model_rename_task(monkeypatch):
    planner = _planner(monkeypatch, {
        "draft_version": "1", "origin": "llm", "tasks": [{
            "task_key": "rename", "op": "attribute.rename_field", "inputs": {},
            "params": {"old_name": "楼栋", "new_name": "用途"},
        }],
    })
    original = _request("楼栋列的数据不变，只把列名改为用途。")
    request = PlannerRequest(
        run_id=original.run_id, input_revision=2, user_goal=original.user_goal,
        input_artifacts=original.input_artifacts,
        revision_patch={"clarification": "不要把楼栋列改为用途，先核对数据。"},
    )
    with pytest.raises(PlannerError) as error:
        planner.create_draft(request)
    assert error.value.code == "LLM_DRAFT_INVALID"
