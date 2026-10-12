"""Provider compatibility is tested independently from live model accuracy."""
import copy
import json

import pytest

from app.config import Settings
from app.runtime.llm_planner import OpenAICompatiblePlanner, PlannerError


def client_for(monkeypatch, statuses):
    posted = []

    class Response:
        def __init__(self, status):
            self.status_code = status

        def json(self):
            return {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}], "usage": {"total_tokens": 9}}

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, endpoint, *, json, headers):
            posted.append(copy.deepcopy(json))
            return Response(statuses.pop(0))

    monkeypatch.setattr("app.runtime.llm_planner.httpx.Client", Client)
    monkeypatch.setattr("app.runtime.llm_planner.time.sleep", lambda _: None)
    return posted


def planner(**kwargs):
    return OpenAICompatiblePlanner(api_key="test", base_url=kwargs.pop("base_url", "https://api.deepseek.com/v1"),
                                   model="deepseek-flash", timeout_seconds=3, max_tokens=2048,
                                   max_retries=1, **kwargs)


def test_default_model_is_requested_deepseek_flash(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert Settings(_env_file=None).LLM_MODEL == "deepseek-flash"


def test_deepseek_uses_documented_json_mode_without_vendor_mixture(monkeypatch):
    posted = client_for(monkeypatch, [200])
    body, _ = planner()._call([{"role": "user", "content": "JSON 计划"}])
    assert posted[0]["model"] == "deepseek-flash"
    assert posted[0]["response_format"] == {"type": "json_object"}
    assert posted[0]["thinking"] == {"type": "disabled"}
    assert "enable_thinking" not in posted[0]
    assert "reasoning_effort" not in posted[0]
    assert body["usage"]["planner_response_format"] == "json_object"
    assert body["usage"]["planner_http_calls"] == 1


def test_compatibility_profile_is_retained_after_rate_limit(monkeypatch):
    posted = client_for(monkeypatch, [400, 429, 200])
    body, _ = planner(base_url="https://gateway.invalid/v1")._call([{"role": "user", "content": "JSON"}])
    assert len(posted) == 3
    assert posted[1]["response_format"] == posted[2]["response_format"] == {"type": "json_object"}
    assert all("thinking" not in item for item in posted[1:])
    assert body["usage"]["planner_compatibility_retry"] is True
    assert body["usage"]["planner_http_calls"] == 3


def test_truncated_json_is_not_accepted_as_a_complete_plan():
    with pytest.raises(PlannerError) as exc:
        OpenAICompatiblePlanner._extract_content({"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]})
    assert exc.value.code == "LLM_RESPONSE_TRUNCATED"


def test_provider_refusal_is_not_reinterpreted_as_a_plan():
    with pytest.raises(PlannerError) as exc:
        OpenAICompatiblePlanner._extract_content({"choices": [{"finish_reason": "stop", "message": {"content": "{}", "refusal": "refused"}}]})
    assert exc.value.code == "LLM_REFUSAL"


def test_json_mode_prompt_exposes_actual_reference_schema_and_selection_semantics():
    from app.runtime.contracts import ArtifactRef, TaskOutputRef
    from app.runtime.llm_planner import PlannerRequest, candidate_catalog

    instance = planner()
    request = PlannerRequest(run_id="run", input_revision=1, user_goal="绘制附件", input_artifacts=(), revision_patch={})
    messages = instance._messages(request, candidate_catalog(instance.registry, request))
    payload = json.loads(messages[1]["content"])
    assert payload["reference_schemas"] == {
        "ArtifactRef": ArtifactRef.model_json_schema(), "TaskOutputRef": TaskOutputRef.model_json_schema(),
    }
    assert "selection='single', ordinal=0" in messages[0]["content"]
    assert "selection='all', ordinal=null" in messages[0]["content"]


@pytest.mark.parametrize("question", [None, "", "   "])
def test_live_clarification_must_identify_a_missing_user_fact(monkeypatch, question):
    from app.runtime.llm_planner import PlannerRequest

    instance = planner()
    response = {"draft_version": "1", "tasks": [], "origin": "needs_clarification"}
    if question is not None:
        response["clarification"] = question
    raw = {"choices": [{"message": {"content": json.dumps(response)}}]}
    monkeypatch.setattr(instance, "_call", lambda messages: (raw, 7))
    request = PlannerRequest(run_id="run", input_revision=1, user_goal="把点向外扩一圈", input_artifacts=(), revision_patch={})
    with pytest.raises(PlannerError) as error:
        instance.create_draft(request)
    assert error.value.code == "LLM_DRAFT_INVALID"


def test_live_clarification_preserves_a_concrete_answerable_question(monkeypatch):
    from app.runtime.llm_planner import PlannerRequest, candidate_catalog

    instance = planner()
    question = "请提供每个站点的覆盖距离，单位米。"
    response = {"draft_version": "1", "tasks": [], "origin": "needs_clarification", "clarification": question}
    raw = {"choices": [{"message": {"content": json.dumps(response)}}]}
    monkeypatch.setattr(instance, "_call", lambda messages: (raw, 7))
    request = PlannerRequest(run_id="run", input_revision=1, user_goal="生成站点覆盖区", input_artifacts=(), revision_patch={})
    call = instance.create_draft(request)
    assert call.draft.clarification == question
    messages = instance._messages(request, candidate_catalog(instance.registry, request))
    assert "clarification" in messages[0]["content"]
    assert "明确指出缺失的用户事实" in messages[0]["content"]
