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


def test_native_executor_converts_semantically_wrong_success_to_error(monkeypatch) -> None:
    """A handler cannot publish an out-of-radius POI merely by returning success."""
    from app.agents import tool_execution

    def wrong_poi(ctx):
        return ToolResult(
            tool_call_id=ctx.tool_call_id,
            tool_name="query_poi",
            status="success",
            data={
                "pois": [{
                    "name": "far", "location": [118.806, 32.0429], "distance": 1.0,
                    "crs": "GCJ02", "source": "Amap",
                }],
                "query": "奶茶", "center": [118.7845, 32.0429],
                "radius_m": 500, "radius_tolerance_m": 5, "crs": "GCJ02",
            },
        )

    monkeypatch.setitem(tool_execution._TOOL_REGISTRY, "query_poi", wrong_poi)
    state = {
        "agent_role": "poi",
        "planner_output": SimpleNamespace(tool_calls=[SimpleNamespace(
            name="query_poi", id="bad-radius", args={
                "query": "奶茶", "location": [118.7845, 32.0429], "radius": 500,
            },
        )]),
        "tool_results": [], "session_vars": {}, "iteration": 0,
    }

    result = tool_execution.native_tool_executor_node(state)
    tool_result = result["tool_results"][-1]
    assert tool_result.status == "error"
    assert tool_result.error_code == "SEMANTIC_POSTCONDITION_FAILED"
    assert "POI_OUTSIDE_RADIUS" in (tool_result.message or "")


def test_poi_sse_semantic_summary_is_bounded_and_result_derived() -> None:
    from app.agents.tool_execution import _tool_semantic_summary

    summary = _tool_semantic_summary("query_poi", {
        "query": "咖啡", "pois": [
            {"location": [118.7845, 32.0429], "distance": 0.0},
            {"location": [118.7850, 32.0429], "distance": 47.0},
        ],
        "center": [118.7845, 32.0429], "radius_m": 300,
        "radius_tolerance_m": 5, "crs": "GCJ02",
    })

    assert summary == {
        "kind": "poi_radius", "query": "咖啡", "poi_count": 2,
        "center": [118.7845, 32.0429], "radius_m": 300.0,
        "radius_tolerance_m": 5.0, "crs": "GCJ02", "max_distance_m": 47.0,
    }


def test_code_mode_returns_structured_error_for_semantically_wrong_success(monkeypatch) -> None:
    """The sandbox proxy must enforce the same postcondition contract."""
    from app.agents import tool_execution

    def wrong_poi(ctx):
        return ToolResult(
            tool_call_id=ctx.tool_call_id,
            tool_name="query_poi",
            status="success",
            data={
                "pois": [{
                    "name": "far", "location": [118.806, 32.0429], "distance": 1.0,
                    "crs": "GCJ02", "source": "Amap",
                }],
                "query": "奶茶", "center": [118.7845, 32.0429],
                "radius_m": 500, "radius_tolerance_m": 5, "crs": "GCJ02",
            },
        )

    monkeypatch.setitem(tool_execution._TOOL_REGISTRY, "query_poi", wrong_poi)
    query_poi = tool_execution._build_code_mode_tool_fns(get_spec("poi"), session_vars={})["query_poi"]

    result = query_poi(query="奶茶", location=[118.7845, 32.0429], radius=500)

    assert result["status"] == "error"
    assert result["error_code"] == "SEMANTIC_POSTCONDITION_FAILED"
    assert "POI_OUTSIDE_RADIUS" in result["message"]


def test_native_buffer_accepts_a_real_wgs84_input_after_gcj02_output_conversion() -> None:
    """Postconditions compare geometries in one CRS, not raw display coordinates."""
    from app.agents import tool_execution

    source = {
        "type": "FeatureCollection",
        "_crs_label": "WGS84",
        "features": [{
            "type": "Feature", "properties": {"name": "origin"},
            "geometry": {"type": "Point", "coordinates": [118.778, 32.038]},
        }],
    }
    state = {
        "agent_role": "geometer",
        "planner_output": SimpleNamespace(tool_calls=[SimpleNamespace(
            name="buffer", id="real-buffer", args={"geometry_from": 0, "radius_m": 100},
        )]),
        "tool_results": [], "iteration": 0,
        "session_vars": {"dep_source": {"result": source}},
    }

    result = tool_execution.native_tool_executor_node(state)
    tool_result = result["tool_results"][-1]

    assert tool_result.status == "success"
    assert tool_result.params == {"geometry_from": 0, "radius_m": 100}
    assert tool_result.data["_crs_label"] == "GCJ02"


def test_native_overlay_and_clip_preserve_real_topology_and_properties() -> None:
    """The real handlers must satisfy the blocking topology postconditions."""
    from app.agents import tool_execution

    def collection(ring, **properties):
        return {
            "type": "FeatureCollection", "_crs_label": "WGS84",
            "features": [{
                "type": "Feature", "properties": properties,
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }],
        }

    left = collection(
        [[118.776, 32.036], [118.780, 32.036], [118.780, 32.040], [118.776, 32.040], [118.776, 32.036]],
        city="Nanjing",
    )
    right = collection(
        [[118.778, 32.038], [118.782, 32.038], [118.782, 32.042], [118.778, 32.042], [118.778, 32.038]],
        land="park",
    )

    def run(tool_name, args):
        return tool_execution.native_tool_executor_node({
            "agent_role": "geometer",
            "planner_output": SimpleNamespace(tool_calls=[SimpleNamespace(name=tool_name, id=tool_name, args=args)]),
            "tool_results": [], "iteration": 0,
            "session_vars": {"dep_left": {"result": left}, "dep_right": {"result": right}},
        })["tool_results"][-1]

    overlay = run("overlay", {"geometry_a_from": 0, "geometry_b_from": 1, "how": "intersection"})
    clipped = run("clip_layer", {"input_ref": 0, "overlay_ref": 1})

    assert overlay.status == "success", overlay.message
    assert clipped.status == "success", clipped.message
    assert overlay.data["features"][0]["properties"]["city"] == "Nanjing"
    assert overlay.data["features"][0]["properties"]["land"] == "park"
    assert clipped.data["features"][0]["properties"]["city"] == "Nanjing"


def test_native_export_rereads_the_real_geojson_before_success(tmp_path, monkeypatch) -> None:
    """Export success reaches Dispatcher only after a real file round-trip check."""
    from app.agents import tool_execution
    from app.config import settings

    monkeypatch.setattr(settings, "APP_WORKSPACE_DIR", str(tmp_path))
    source = {
        "type": "FeatureCollection", "_crs_label": "WGS84",
        "features": [
            {"type": "Feature", "properties": {"name": "a"}, "geometry": {"type": "Point", "coordinates": [118.77, 32.03]}},
            {"type": "Feature", "properties": {"name": "b"}, "geometry": {"type": "Point", "coordinates": [118.78, 32.04]}},
        ],
    }
    result = tool_execution.native_tool_executor_node({
        "agent_role": "geometer",
        "planner_output": SimpleNamespace(tool_calls=[SimpleNamespace(
            name="export_result", id="export", args={"data_from": 0, "format": "geojson", "output_path": "checked.geojson"},
        )]),
        "tool_results": [], "iteration": 0,
        "session_vars": {"dep_source": {"result": source}},
    })["tool_results"][-1]

    assert result.status == "success", result.message
    assert result.data["feature_count"] == 2
    assert (tmp_path / "exports" / "checked.geojson").is_file()


def test_native_reclassify_excludes_nodata_before_postcondition_success(tmp_path) -> None:
    """The native result includes a verifiable valid/nodata pixel partition."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from app.agents import tool_execution

    source_path = tmp_path / "source.tif"
    output_path = tmp_path / "classified.tif"
    with rasterio.open(
        source_path, "w", driver="GTiff", width=2, height=1, count=1,
        dtype="float32", crs="EPSG:4326", nodata=-9999.0,
        transform=from_origin(118.77, 32.04, 0.001, 0.001),
    ) as dataset:
        dataset.write(np.array([[1.0, -9999.0]], dtype="float32"), 1)

    result = tool_execution.native_tool_executor_node({
        "agent_role": "geometer",
        "planner_output": SimpleNamespace(tool_calls=[SimpleNamespace(
            name="reclassify_raster", id="reclassify", args={
                "src_path": str(source_path), "bins": [5.0], "values": [1.0, 2.0], "dst_path": str(output_path),
            },
        )]),
        "tool_results": [], "iteration": 0, "session_vars": {},
    })["tool_results"][-1]

    assert result.status == "success", result.message
    assert result.data["class_counts"] == {"1": 1}
    assert result.data["valid_pixel_count"] == 1
    assert result.data["nodata_pixel_count"] == 1
    assert result.data["total_pixel_count"] == 2
    with rasterio.open(output_path) as dataset:
        assert dataset.read(1, masked=True).mask.tolist() == [[False, True]]


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


def test_empty_native_step_is_a_terminal_data_result_not_a_retry() -> None:
    """A valid zero-row query must not repeat the provider call."""
    from app.agents import tool_execution

    empty_payload = {
        "pois": [],
        "query": "不存在的品牌",
        "center": [118.7845, 32.0429],
        "radius_m": 500,
        "radius_tolerance_m": 5,
        "crs": "GCJ02",
    }
    result = tool_execution.native_step_finalize_node({
        "iteration": 1,
        "max_iterations": 3,
        "required_tool_name": "query_poi",
        "tool_results": [ToolResult(
            tool_call_id="call-empty-poi",
            tool_name="query_poi",
            status="empty",
            data=empty_payload,
            message="未找到相关 POI",
            source="Amap",
        )],
    })

    assert result["should_stop"] is True
    assert result["decision"] == "FINISH"
    assert result["final_output"]["status"] == "empty"
    assert result["final_output"]["results"][0]["data"] == empty_payload


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
