"""Regression tests for schema-first sub-agent execution."""

from __future__ import annotations

import json
from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from app.agents.build_sub_agent import _native_planner_node, run_sub_agent
from app.agents.dispatcher import _enrich_goal_from_deps
from app.agents.native_tool_mode import (
    ToolArgumentValidationError,
    build_native_tool_schema,
    validate_tool_arguments,
)
from app.agents.registry import get_spec
from app.models.schemas import ToolResult


class _NativeToolLLM:
    """Small deterministic transport that supports LangChain ``bind_tools``."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.bound_tools: list[dict[str, Any]] = []
        self._verifier = deque([
            {
                "approved": True,
                "reason": "结果正确",
                "refinement_hints": [],
                "confidence": 0.95,
            }
        ])
        self._judge = deque([{"decision": "FINISH", "reason": "任务完成"}])

    def bind_tools(self, tools: list[dict[str, Any]], **kwargs: Any) -> "_NativeToolLLM":
        del kwargs
        self.bound_tools = list(tools)
        return self

    def invoke(self, messages: list[Any], *args: Any, **kwargs: Any) -> AIMessage:
        del args, kwargs
        system = str(getattr(messages[0], "content", "")) if messages else ""
        if "Verifier" in system:
            self.calls.append("verifier")
            return AIMessage(content=json.dumps(self._verifier.popleft(), ensure_ascii=False))
        if "Judge" in system:
            self.calls.append("judge")
            return AIMessage(content=json.dumps(self._judge.popleft(), ensure_ascii=False))
        if "Observer" in system:
            self.calls.append("observer")
            return AIMessage(content="坐标判断工具执行成功。")
        self.calls.append("planner")
        return AIMessage(
            content="调用坐标判断工具。",
            tool_calls=[
                {
                    "name": "geo_transform",
                    "args": {"operation": "out_of_china", "lng": 0, "lat": 0},
                    "id": "call_geo_transform_1",
                    "type": "tool_call",
                }
            ],
        )


def test_non_coder_roles_default_to_native_tool_calls() -> None:
    assert get_spec("geo").execution_mode == "tool_call"
    assert get_spec("poi").execution_mode == "tool_call"
    assert get_spec("geometer").execution_mode == "tool_call"
    assert get_spec("viz").execution_mode == "tool_call"
    assert get_spec("coder").execution_mode == "code"


def test_raster_analysis_tools_are_schema_first_for_geometer() -> None:
    geometer = get_spec("geometer")
    for tool_name in ("slope", "aspect", "hillshade", "zonal_statistics", "reclassify_raster"):
        assert tool_name in geometer.tool_names
        schema = build_native_tool_schema(tool_name)["function"]["parameters"]
        assert schema["additionalProperties"] is False


def test_native_schema_is_closed_and_locally_rejects_unknown_arguments() -> None:
    schema = build_native_tool_schema("geo_transform")
    parameters = schema["function"]["parameters"]

    assert parameters["additionalProperties"] is False
    assert {"operation", "lng", "lat"} <= set(parameters["properties"])
    assert set(parameters["properties"]["operation"]["enum"]) == {
        "wgs84_to_gcj02",
        "gcj02_to_wgs84",
        "gcj02_to_bd09",
        "bd09_to_gcj02",
        "wgs84_to_bd09",
        "bd09_to_wgs84",
        "haversine",
        "out_of_china",
        "auto_detect_crs",
    }

    with pytest.raises(ToolArgumentValidationError, match="unexpected"):
        validate_tool_arguments(
            "geo_transform",
            {"operation": "out_of_china", "lng": 0, "lat": 0, "unexpected": 1},
        )


def test_attribute_filter_schema_accepts_structured_filter_arguments() -> None:
    """The executor supports both expression and field/operator/value forms."""
    schema = build_native_tool_schema("extract_by_attribute")["function"]["parameters"]

    assert {"input_ref", "expression", "field", "operator", "value"} <= set(schema["properties"])
    assert "expression" not in schema["required"]
    assert validate_tool_arguments(
        "extract_by_attribute",
        {"input_ref": 0, "field": "class", "operator": "==", "value": "station"},
    ) == {"input_ref": 0, "field": "class", "operator": "==", "value": "station"}


def test_native_dependency_goal_does_not_tell_model_to_use_python_variables() -> None:
    task = SimpleNamespace(
        goal="渲染上游空间要素",
        agent_role="viz",
        depends_on=["source"],
    )
    results = {
        "source": [{
            "status": "success",
            "agent_role": "coder",
            "artifacts": {"features": [{"type": "Feature"}]},
        }]
    }

    enriched = _enrich_goal_from_deps(task, results)

    assert "Python 变量" not in enriched
    assert "runtime reference catalog" in enriched


def test_native_planner_applies_root_owned_exact_tool_arguments() -> None:
    """A documented boundary must not be weakened when the model omits it."""

    class MissingDistanceLLM:
        def bind_tools(self, _tools, **_kwargs):
            return self

        def invoke(self, _messages):
            return AIMessage(
                content="关联最近公交站。",
                tool_calls=[{
                    "name": "join_by_nearest",
                    "args": {"input_ref": 0, "other_ref": 1},
                    "id": "nearest-1",
                    "type": "tool_call",
                }],
            )

    result = _native_planner_node({
        "agent_role": "geometer",
        "required_tool_name": "join_by_nearest",
        "required_tool_args": {"max_distance": 0},
        "session_vars": {"dep_left": {"result": {"type": "FeatureCollection", "features": []}}, "dep_right": {"result": {"type": "FeatureCollection", "features": []}}},
        "messages": [],
        "user_input": "为每个 POI 关联最近公交站，最大距离为 0 米",
        "iteration": 0,
    }, llm=MissingDistanceLLM())

    assert result["planner_output"].tool_calls[0].args["max_distance"] == 0


def test_native_tool_call_runs_through_real_sub_agent_graph() -> None:
    llm = _NativeToolLLM()

    result = run_sub_agent(
        "geo",
        "判断零度经纬度是否位于中国境外",
        run_id="native-tool-real-graph",
        llm=llm,
    )

    assert result["should_stop"] is True
    assert result["tool_results"][-1].tool_name == "geo_transform"
    assert result["tool_results"][-1].mode == "json"
    assert result["tool_results"][-1].status == "success"
    assert result["tool_results"][-1].data["out_of_china"] is True
    assert llm.calls == ["planner"]
    assert result["final_output"]["status"] == "success"
    assert result["max_iterations"] == 2
    assert [tool["function"]["name"] for tool in llm.bound_tools] == [
        "geo_code",
        "geo_transform",
    ]


def test_failed_native_step_revises_only_current_step_without_finishing() -> None:
    from app.agents import tool_execution

    assert hasattr(tool_execution, "native_step_finalize_node")
    result = tool_execution.native_step_finalize_node({
        "iteration": 1,
        "max_iterations": 3,
        "required_tool_name": "buffer",
        "tool_results": [ToolResult(
            tool_call_id="call-buffer",
            tool_name="buffer",
            status="error",
            error_code="INVALID_TOOL_ARGUMENTS",
            message="radius_m is required",
        )],
    })

    assert result["should_stop"] is False
    assert "buffer" in result["messages"][0].content
    assert "radius_m is required" in result["messages"][0].content


def test_failed_native_tool_result_always_reaches_deterministic_finalize() -> None:
    from app.agents.build_sub_agent import _route_after_verifier_native

    route = _route_after_verifier_native({
        "verifier_output": {"approved": False, "reason": "retry"},
        "tool_results": [ToolResult(
            tool_call_id="call-buffer",
            tool_name="buffer",
            status="empty",
            message="no geometry",
        )],
    })

    assert route == "finalize"


def test_native_finalize_persists_pending_without_legacy_judge(monkeypatch) -> None:
    """Normal-role graphs must keep /resume working after Judge removal."""
    from app.agents.tool_execution import native_step_finalize_node

    saved: dict[str, Any] = {}

    class FakePendingStore:
        def save_sync(self, session_id, pending_task):
            saved["session_id"] = session_id
            saved["pending_task"] = pending_task

    monkeypatch.setattr("app.agents.pending.PendingStore", FakePendingStore)
    pending = {
        "sub_agent_run_id": "run_1",
        "original_request": "做缓冲区",
        "missing_slots": ["distance"],
        "slot_patch_schema": {"distance": {"type": "number", "unit": "m"}},
        "message": "请提供缓冲距离",
    }

    result = native_step_finalize_node({
        "pending_task": pending,
        "session_id": "session_1",
        "run_id": "run_1",
        "user_input": "做缓冲区",
    })

    assert result["decision"] == "AWAITING_INPUT"
    assert saved["session_id"] == "session_1"
    assert saved["pending_task"].sub_agent_run_id == "run_1"


def test_native_preflight_ask_user_becomes_resumable_pending_task() -> None:
    """Schema-first paths must not silently downgrade ask_user to a tool error."""
    from app.agents.preflight.validation import (
        PreflightError,
        RepairProposal,
        ValidationIssue,
    )
    from app.agents.tool_execution import _pending_from_preflight_error

    error = PreflightError(
        "图层 parcels 不存在",
        issues=[ValidationIssue(
            code="layer_not_found",
            stage="preflight",
            severity="error",
            message="图层 parcels 不存在，请选择正确图层。",
            repair=RepairProposal(kind="ask_user"),
        )],
    )

    pending = _pending_from_preflight_error(error, {
        "run_id": "subagent-run-7",
        "user_input": "按字段筛选地块",
    })

    assert pending == {
        "sub_agent_run_id": "subagent-run-7",
        "original_request": "按字段筛选地块",
        "missing_slots": [],
        "slot_patch_schema": {},
        "message": "图层 parcels 不存在，请选择正确图层。",
        "issues": [{
            "code": "layer_not_found",
            "stage": "preflight",
            "severity": "error",
            "message": "图层 parcels 不存在，请选择正确图层。",
            "repair": {"kind": "ask_user", "action": None, "patch": None},
        }],
    }
