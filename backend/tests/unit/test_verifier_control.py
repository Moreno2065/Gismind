"""Tests for verifier-driven user-input control schemas."""

from app.agents.schemas import PendingTask, VerifierOutput


def test_verifier_output_control_fields_default_to_safe_values():
    output = VerifierOutput(approved=True, reason="verified")

    assert output.needs_input is False
    assert output.missing_slots == []
    assert output.choices == []
    assert output.input_reason is None
    assert output.verifier_unavailable is False
    assert output.invalid_rejection is False


def test_pending_task_round_trips_verifier_control_fields():
    task = PendingTask(
        sub_agent_run_id="run-123",
        original_request="Find nearby parks",
        missing_slots=["location"],
        candidates=[{"id": "candidate-1"}],
        slot_patch_schema={"location": {"type": "string"}},
        choices=[{"label": "Central Park", "value": "central-park"}],
        correction_history=[{"reason": "Specify a location"}],
        message="Location required",
        issues=[{"code": "missing_location"}],
        created_at="2026-07-18T00:00:00+00:00",
    )

    restored = PendingTask.from_dict(task.to_dict())

    assert restored.slot_patch_schema == {"location": {"type": "string"}}
    assert restored.choices == [{"label": "Central Park", "value": "central-park"}]
    assert restored.correction_history == [{"reason": "Specify a location"}]
    assert restored.candidates == [{"id": "candidate-1"}]
    assert restored.to_dict() == task.to_dict()


def test_verifier_llm_failure_fails_open(monkeypatch):
    from app.agents import verifier_node

    def unavailable_llm():
        raise RuntimeError("provider offline")

    monkeypatch.setattr(verifier_node, "create_llm", unavailable_llm)

    delta = verifier_node.verifier_node({"tool_results": []})

    output = VerifierOutput.model_validate(delta["verifier_output"])
    assert output.approved is True
    assert output.confidence == 0.0
    assert output.verifier_unavailable is True
    assert "deterministic quality gates remain active" in output.reason


def test_missing_verifier_output_fails_open_in_router():
    from app.agents.refine_router import refine_router

    delta = refine_router({"verifier_output": None, "iteration": 6, "max_iterations": 6})

    assert delta["should_stop"] is False
    output = VerifierOutput.model_validate(delta["verifier_output"])
    assert output.approved is True
    assert output.verifier_unavailable is True
    assert output.confidence == 0.0
    assert "deterministic quality gates remain active" in output.reason


def test_low_confidence_approval_passes_through_without_refinement():
    from app.agents.refine_router import refine_router

    delta = refine_router(
        {
            "verifier_output": VerifierOutput(
                approved=True,
                reason="verified",
                confidence=0.1,
            ).model_dump(),
            "iteration": 0,
            "max_iterations": 6,
        }
    )

    assert delta == {"should_stop": False}


def test_generic_rejection_is_normalized_without_refinement():
    from app.agents.refine_router import refine_router

    delta = refine_router(
        {
            "verifier_output": VerifierOutput(
                approved=False,
                reason="not actionable",
                refinement_hints=[" 改进 ", "请改进"],
                confidence=0.8,
            ).model_dump(),
            "iteration": 0,
            "max_iterations": 6,
        }
    )

    assert delta["should_stop"] is False
    assert set(delta) == {"verifier_output", "should_stop"}
    output = VerifierOutput.model_validate(delta["verifier_output"])
    assert output.approved is True
    assert output.invalid_rejection is True
    assert output.needs_input is False


def test_actionable_rejection_adds_refinement_message_and_history():
    from app.agents.refine_router import refine_router

    delta = refine_router(
        {
            "verifier_output": VerifierOutput(
                approved=False,
                reason="missing validation",
                refinement_hints=["Validate that all required fields are present."],
                confidence=0.8,
            ).model_dump(),
            "iteration": 2,
            "max_iterations": 6,
        }
    )

    assert delta["should_stop"] is False
    assert len(delta["messages"]) == 1
    assert "Validate that all required fields are present." in delta["messages"][0].content
    assert delta["refine_history"] == [
        {
            "iteration": 2,
            "verifier_reason": "missing validation",
            "refinement_hints": ["Validate that all required fields are present."],
            "applied": True,
        }
    ]
    # refine_router must not increment or return iteration; boundary uses state only
    assert "iteration" not in delta


def test_actionable_reject_at_max_iterations_stops_without_iteration_delta():
    """When iteration == max_iterations, stop with REFINE_LIMIT_EXCEEDED and no iteration field."""
    from app.agents.errors import ErrorCode
    from app.agents.refine_router import refine_router

    delta = refine_router(
        {
            "verifier_output": VerifierOutput(
                approved=False,
                reason="still invalid",
                refinement_hints=["Add missing required geometry fields."],
                confidence=0.7,
            ).model_dump(),
            "iteration": 6,
            "max_iterations": 6,
        }
    )

    assert delta["should_stop"] is True
    assert delta["termination_cause"] == ErrorCode.REFINE_LIMIT_EXCEEDED.value
    assert "iteration" not in delta
    assert "messages" not in delta


def test_actionable_reject_below_max_iterations_refines_without_iteration_delta():
    """When iteration < max_iterations, emit refine messages and never put iteration in delta."""
    from app.agents.refine_router import refine_router

    delta = refine_router(
        {
            "verifier_output": VerifierOutput(
                approved=False,
                reason="needs more detail",
                refinement_hints=["Re-run POI query with a tighter radius."],
                confidence=0.75,
            ).model_dump(),
            "iteration": 3,
            "max_iterations": 6,
        }
    )

    assert delta["should_stop"] is False
    assert "messages" in delta
    assert len(delta["messages"]) == 1
    assert "Re-run POI query with a tighter radius." in delta["messages"][0].content
    assert "iteration" not in delta


def test_normalized_generic_reject_routes_to_judge_after_refine():
    from app.agents.build_sub_agent import _route_after_refine
    from app.agents.refine_router import refine_router

    state = {
        "verifier_output": VerifierOutput(
            approved=False,
            reason="not actionable",
            refinement_hints=["改进", "请改进"],
            confidence=0.8,
        ).model_dump(),
        "iteration": 0,
        "max_iterations": 6,
        "should_stop": False,
    }
    delta = refine_router(state)
    merged = {**state, **delta}

    assert delta["should_stop"] is False
    assert VerifierOutput.model_validate(delta["verifier_output"]).approved is True
    assert _route_after_refine(merged) == "judge"


def test_parser_failure_emits_fail_open_and_routes_to_judge():
    from app.agents.build_sub_agent import _route_after_refine
    from app.agents.refine_router import refine_router

    state = {
        "verifier_output": None,
        "iteration": 6,
        "max_iterations": 6,
        "should_stop": False,
    }
    delta = refine_router(state)
    merged = {**state, **delta}

    output = VerifierOutput.model_validate(delta["verifier_output"])
    assert output.approved is True
    assert output.verifier_unavailable is True
    assert output.confidence == 0.0
    assert "deterministic quality gates remain active" in output.reason
    assert delta["should_stop"] is False
    assert _route_after_refine(merged) == "judge"


def _input_request_state(**overrides):
    verifier = VerifierOutput(
        approved=False,
        reason="Need output details",
        needs_input=True,
        input_reason="请选择输出格式",
        choices=[{"id": "geojson", "label": "GeoJSON"}],
    )
    state = {
        "run_id": "run-input-123",
        "user_input": "导出缓冲区",
        "pending_task": None,
        "verifier_output": verifier.model_dump(),
    }
    state.update(overrides)
    return state


def test_verifier_pending_task_returns_pending_for_valid_choice_request():
    from app.agents.refine_router import verifier_pending_task

    pending = verifier_pending_task(_input_request_state())

    assert pending == {
        "sub_agent_run_id": "run-input-123",
        "original_request": "导出缓冲区",
        "missing_slots": [],
        "choices": [{"id": "geojson", "label": "GeoJSON"}],
        "message": "请选择输出格式",
        "issues": [],
        "slot_patch_schema": {},
        "correction_history": [],
    }


def test_verifier_pending_task_distance_fills_slot_patch_schema():
    from app.agents.refine_router import verifier_pending_task
    from app.agents.schemas import VerifierOutput

    state = _input_request_state(
        verifier_output=VerifierOutput(
            approved=False,
            reason="Need buffer distance",
            needs_input=True,
            input_reason="请指定缓冲距离",
            missing_slots=["distance"],
        ).model_dump(),
    )

    pending = verifier_pending_task(state)

    assert pending is not None
    assert pending["missing_slots"] == ["distance"]
    assert pending["slot_patch_schema"] == {"distance": {"type": "number", "unit": "m"}}


def test_verifier_pending_task_rejects_unknown_missing_slot():
    from app.agents.refine_router import verifier_pending_task

    state = _input_request_state(
        verifier_output=VerifierOutput(
            approved=False,
            reason="Need output details",
            needs_input=True,
            input_reason="请指定位置",
            missing_slots=["location"],
        ).model_dump(),
    )

    assert verifier_pending_task(state) is None


def test_verifier_pending_task_rejects_tool_failure_reason():
    from app.agents.refine_router import verifier_pending_task

    state = _input_request_state(
        verifier_output=VerifierOutput(
            approved=False,
            reason="工具调用失败，请重试",
            needs_input=True,
            input_reason="请指定距离",
            missing_slots=["distance"],
        ).model_dump(),
    )

    assert verifier_pending_task(state) is None


def test_refine_router_returns_pending_delta_and_routes_to_judge():
    from app.agents.build_sub_agent import _route_after_refine
    from app.agents.refine_router import refine_router

    state = _input_request_state()
    delta = refine_router(state)

    assert delta["should_stop"] is False
    assert delta["pending_task"]["sub_agent_run_id"] == "run-input-123"
    assert delta["pending_task"]["choices"] == [{"id": "geojson", "label": "GeoJSON"}]
    assert delta["verifier_output"] == state["verifier_output"]
    assert _route_after_refine({**state, **delta}) == "judge"


def _pending_task_payload(**overrides):
    pending = {
        "sub_agent_run_id": "run-input-123",
        "original_request": "导出缓冲区",
        "missing_slots": [],
        "choices": [{"id": "geojson", "label": "GeoJSON"}],
        "message": "请选择输出格式",
        "issues": [],
        "slot_patch_schema": {"format": {"type": "string"}},
        "correction_history": [{"reason": "format required"}],
        "candidates": [],
    }
    pending.update(overrides)
    return pending


def test_judge_node_preserves_awaiting_input_when_pending_task():
    from app.agents.tool_execution import judge_node

    pending = _pending_task_payload()
    state = {
        "iteration": 0,
        "max_iterations": 6,
        "pending_task": pending,
        "session_id": "sess-await-1",
        "run_id": "run-input-123",
        "user_input": "导出缓冲区",
        "messages": [],
        "tool_results": [],
    }

    delta = judge_node(state)

    assert delta["should_stop"] is True
    assert delta["decision"] == "AWAITING_INPUT"
    assert delta["pending_task"] == pending
    assert "final_output" not in delta


def test_judge_node_preserves_awaiting_input_even_at_max_iteration():
    from app.agents.tool_execution import judge_node

    pending = _pending_task_payload()
    state = {
        "iteration": 6,
        "max_iterations": 6,
        "pending_task": pending,
        "session_id": "sess-await-max",
        "run_id": "run-input-123",
        "user_input": "导出缓冲区",
        "messages": [],
        "tool_results": [],
    }

    delta = judge_node(state)

    assert delta["should_stop"] is True
    assert delta["decision"] == "AWAITING_INPUT"
    assert delta["pending_task"] == pending
    assert "final_output" not in delta


def test_judge_persistence_forwards_expanded_pending_fields(monkeypatch):
    from app.agents import judge as judge_mod

    saved = {}

    class FakeStore:
        def save_sync(self, session_id, pt):
            saved["session_id"] = session_id
            saved["pt"] = pt

    pending = _pending_task_payload()
    state = {
        "iteration": 0,
        "max_iterations": 6,
        "pending_task": pending,
        "session_id": "sess-fields-1",
        "run_id": "run-input-123",
        "user_input": "导出缓冲区",
        "messages": [],
        "tool_results": [],
    }

    # judge imports PendingStore lazily inside the function; patch at source module.
    monkeypatch.setattr("app.agents.pending.PendingStore", FakeStore)

    result = judge_mod.judge(state)

    assert result["decision"] == "AWAITING_INPUT"
    assert saved["session_id"] == "sess-fields-1"
    pt = saved["pt"]
    assert pt.choices == [{"id": "geojson", "label": "GeoJSON"}]
    assert pt.slot_patch_schema == {"format": {"type": "string"}}
    assert pt.correction_history == [{"reason": "format required"}]
