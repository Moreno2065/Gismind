"""Planner-source contracts for the root dispatcher.

These tests deliberately inject only the root LLM transport.  They exercise
the production parser and DAG validator, and make the planning provenance part
of the stable dispatcher contract.
"""

from __future__ import annotations

import json
from collections import deque
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from app.agents.dispatcher import planner_router_node
from app.agents.schemas import TaskPlan


class ScriptedRootLLM:
    """Small deterministic transport; it is not a dispatcher or HTTP mock."""

    def __init__(self, responses: list[dict[str, Any] | str]) -> None:
        self._responses = deque(responses)
        self.calls = 0

    def invoke(self, _messages: list[Any]) -> AIMessage:
        self.calls += 1
        response = self._responses.popleft()
        if isinstance(response, dict):
            response = json.dumps(response, ensure_ascii=False)
        return AIMessage(content=response)


def _plan(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "task_plan": {
            "instructions": [{"id": "i1", "text": "执行用户的空间分析请求"}],
            "tasks": [{**task, "instruction_id": "i1"} for task in tasks],
        }
    }


def test_unseen_poi_rewrite_uses_root_llm_and_preserves_executable_dag():
    """A normal paraphrase must not be hidden behind the keyword catalog."""
    llm = ScriptedRootLLM([
        _plan([
            {
                "id": "locate",
                "agent_role": "geo",
                "tool_name": "geo_code",
                "goal": "解析南京鼓楼医院的位置",
                "depends_on": [],
            },
            {
                "id": "search",
                "agent_role": "poi",
                "tool_name": "query_poi",
                "goal": "查询鼓楼医院步行三分钟范围内的咖啡店",
                "depends_on": ["locate"],
            },
            {
                "id": "render",
                "agent_role": "viz",
                "tool_name": "map_layer_build",
                "goal": "在地图上展示咖啡店结果",
                "depends_on": ["search"],
            },
        ])
    ])

    result = planner_router_node(
        {
            "user_input": "给我找出鼓楼医院周边步行三分钟能到的咖啡店，并在地图上标出来",
            "upload_file_ids": [],
            "messages": [],
        },
        llm=llm,
    )

    tasks = result["task_plan"]["tasks"]
    assert result["planner_source"] == "root_llm"
    assert llm.calls == 1
    assert [(task["agent_role"], task["tool_name"]) for task in tasks] == [
        ("geo", "geo_code"),
        ("poi", "query_poi"),
        ("viz", "map_layer_build"),
    ]
    assert tasks[1]["depends_on"] == ["locate"]
    assert tasks[2]["depends_on"] == ["search"]


def test_documented_catalog_is_used_only_after_root_llm_fails():
    """A normal catalog-shaped POI request must not bypass Root planning."""
    llm = ScriptedRootLLM(["not json", "still not json"])

    result = planner_router_node(
        {
            "user_input": "南京新街口500米内有多少蜜雪冰城",
            "upload_file_ids": [],
            "messages": [],
        },
        llm=llm,
    )

    tools = {task["tool_name"] for task in result["task_plan"]["tasks"]}
    assert result["planner_source"] == "fallback"
    assert llm.calls == 2
    assert {"geo_code", "query_poi"}.issubset(tools)


def test_explicit_coordinate_conversion_remains_a_strong_constraint_guardrail():
    """Exact coordinate-system conversion is safe to plan without a root LLM."""
    llm = ScriptedRootLLM([])

    result = planner_router_node(
        {
            "user_input": "将 116.397128,39.916527 从 WGS84 转换成 GCJ02",
            "upload_file_ids": [],
            "messages": [],
        },
        llm=llm,
    )

    assert result["planner_source"] == "guardrail"
    assert llm.calls == 0
    assert [task["tool_name"] for task in result["task_plan"]["tasks"]] == ["geo_transform"]
    assert result["task_plan"]["tasks"][0]["tool_args"] == {
        "operation": "wgs84_to_gcj02",
        "lng": 116.397128,
        "lat": 39.916527,
    }


def test_attribute_filter_uses_fallback_only_after_root_llm_invents_bad_tools():
    """An invalid Root DAG is repaired by the catalog and labeled fallback."""
    invalid = _plan([{
        "id": "read",
        "agent_role": "geometer",
        "tool_name": "data_io_read",
        "goal": "读取上传点图层",
        "depends_on": [],
    }, {
        "id": "filter",
        "agent_role": "geometer",
        "tool_name": "attribute_filter",
        "goal": "筛选 class 等于 station",
        "depends_on": ["read"],
    }])
    llm = ScriptedRootLLM([invalid, invalid])

    result = planner_router_node(
        {
            "user_input": "从我刚传的点图层里挑出 class 等于 station 的记录，并作为新图层显示。",
            "upload_file_ids": ["file_points"],
            "messages": [],
        },
        llm=llm,
    )

    assert llm.calls == 2
    assert result["planner_source"] == "fallback"
    assert [task["tool_name"] for task in result["task_plan"]["tasks"]] == [
        "data_io_read", "extract_by_attribute",
    ]


def test_attribute_filter_accepts_a_valid_root_llm_dag():
    """A valid unseen filter plan remains attributable to the Root LLM."""
    llm = ScriptedRootLLM([_plan([{
        "id": "read",
        "agent_role": "geometer",
        "tool_name": "data_io_read",
        "goal": "读取上传点图层",
        "depends_on": [],
    }, {
        "id": "filter",
        "agent_role": "geometer",
        "tool_name": "extract_by_attribute",
        "goal": "筛选 class 等于 station 的记录",
        "depends_on": ["read"],
        "tool_args": {"field": "class", "operator": "==", "value": "station"},
    }])])

    result = planner_router_node(
        {
            "user_input": "从我刚传的点图层里挑出 class 等于 station 的记录，并作为新图层显示。",
            "upload_file_ids": ["file_points"],
            "messages": [],
        },
        llm=llm,
    )

    assert llm.calls == 1
    assert result["planner_source"] == "root_llm"
    assert [task["tool_name"] for task in result["task_plan"]["tasks"]] == [
        "data_io_read", "extract_by_attribute",
    ]


def test_same_radius_count_comparison_prunes_redundant_coder_task():
    """The deterministic assembler compares counts; Code Mode adds latency and risk."""
    llm = ScriptedRootLLM([_plan([
        {
            "id": "locate",
            "agent_role": "geo",
            "tool_name": "geo_code",
            "goal": "沿用南京新街口位置",
            "depends_on": [],
        },
        {
            "id": "search",
            "agent_role": "poi",
            "tool_name": "query_poi",
            "goal": "查询五百米内茶百道",
            "depends_on": ["locate"],
        },
        {
            "id": "compare",
            "agent_role": "coder",
            "tool_name": "code_executor",
            "goal": "和上一轮数量比较",
            "depends_on": ["search"],
        },
    ])])

    result = planner_router_node(
        {
            "user_input": "沿用刚才的位置，改查茶百道并和上一轮数量比较。",
            "upload_file_ids": [],
            "messages": [HumanMessage(content="南京新街口五百米内蜜雪冰城")],
        },
        llm=llm,
    )

    assert result["planner_source"] == "root_llm"
    assert [task["tool_name"] for task in result["task_plan"]["tasks"]] == [
        "geo_code", "query_poi",
    ]


def test_count_comparison_pruning_keeps_task_plan_instruction_coverage_valid():
    """Pruning a comparison-only task must also remove its orphan instruction."""
    llm = ScriptedRootLLM([{
        "task_plan": {
            "instructions": [
                {"id": "i1", "text": "查询南京新街口五百米内茶百道"},
                {"id": "i2", "text": "与上一轮蜜雪冰城数量比较"},
            ],
            "tasks": [
                {
                    "id": "locate", "agent_role": "geo", "tool_name": "geo_code",
                    "goal": "沿用南京新街口位置", "depends_on": [], "instruction_id": "i1",
                },
                {
                    "id": "search", "agent_role": "poi", "tool_name": "query_poi",
                    "goal": "查询五百米内茶百道", "depends_on": ["locate"],
                    "instruction_id": "i1",
                },
                {
                    "id": "compare", "agent_role": "coder", "tool_name": "code_executor",
                    "goal": "和上一轮数量比较", "depends_on": ["search"],
                    "instruction_id": "i2",
                },
            ],
        },
    }])

    result = planner_router_node(
        {
            "user_input": "沿用刚才的位置，改查茶百道并和上一轮数量比较。",
            "upload_file_ids": [],
            "messages": [HumanMessage(content="南京新街口五百米内蜜雪冰城")],
        },
        llm=llm,
    )

    validated = TaskPlan.model_validate(result["task_plan"])
    assert [instruction.id for instruction in validated.instructions] == ["i1"]
    assert [task.tool_name for task in validated.tasks] == ["geo_code", "query_poi"]


def test_count_comparison_prunes_redundant_previous_brand_query() -> None:
    """The prior count comes from session facts, not a second Mixue lookup."""
    llm = ScriptedRootLLM([{
        "task_plan": {
            "instructions": [
                {"id": "i1", "text": "查询茶百道"},
                {"id": "i2", "text": "复查上一轮蜜雪冰城并比较"},
            ],
            "tasks": [
                {
                    "id": "locate", "agent_role": "geo", "tool_name": "geo_code",
                    "goal": "沿用南京新街口位置", "depends_on": [], "instruction_id": "i1",
                },
                {
                    "id": "tea", "agent_role": "poi", "tool_name": "query_poi",
                    "goal": "查询五百米内茶百道", "depends_on": ["locate"],
                    "instruction_id": "i1",
                },
                {
                    "id": "mixue", "agent_role": "poi", "tool_name": "query_poi",
                    "goal": "复查上一轮五百米内蜜雪冰城", "depends_on": ["locate"],
                    "instruction_id": "i2",
                },
            ],
        },
    }])

    result = planner_router_node(
        {
            "user_input": "沿用刚才的位置，改查茶百道并和上一轮数量比较。",
            "upload_file_ids": [],
            "messages": [HumanMessage(content="南京新街口五百米内蜜雪冰城")],
        },
        llm=llm,
    )

    validated = TaskPlan.model_validate(result["task_plan"])
    assert [instruction.id for instruction in validated.instructions] == ["i1"]
    assert [(task.id, task.tool_name) for task in validated.tasks] == [
        ("locate", "geo_code"), ("tea", "query_poi"),
    ]


def test_root_planner_repairs_invalid_scalar_tool_args_before_dispatch():
    """The Root contract must reject bad scalar args before a sub-agent sees them."""
    invalid = _plan([
        {
            "id": "locate", "agent_role": "geo", "tool_name": "geo_code",
            "goal": "解析南京鼓楼医院的位置", "depends_on": [],
        },
        {
            "id": "search", "agent_role": "poi", "tool_name": "query_poi",
            "goal": "查询附近咖啡店", "depends_on": ["locate"],
            "tool_args": {"radius": "500"},
        },
    ])
    valid = _plan([
        {
            "id": "locate", "agent_role": "geo", "tool_name": "geo_code",
            "goal": "解析南京鼓楼医院的位置", "depends_on": [],
        },
        {
            "id": "search", "agent_role": "poi", "tool_name": "query_poi",
            "goal": "查询附近咖啡店", "depends_on": ["locate"],
            "tool_args": {"radius": 500},
        },
    ])
    llm = ScriptedRootLLM([invalid, valid])

    result = planner_router_node(
        {"user_input": "找鼓楼医院周边 500 米咖啡店", "messages": []},
        llm=llm,
    )

    assert llm.calls == 2
    assert result["planner_source"] == "root_llm"
    assert result["task_plan"]["tasks"][1]["tool_args"]["radius"] == 500


def test_dispatcher_owns_upload_order_and_dependency_reference_args():
    """LLM plans express the DAG, while Dispatcher binds file and artifact identities."""
    llm = ScriptedRootLLM([_plan([
        {
            "id": "left", "agent_role": "geometer", "tool_name": "data_io_read",
            "goal": "读取第一个图层", "depends_on": [],
        },
        {
            "id": "right", "agent_role": "geometer", "tool_name": "data_io_read",
            "goal": "读取第二个图层", "depends_on": [],
        },
        {
            "id": "intersect", "agent_role": "geometer", "tool_name": "overlay",
            "goal": "计算两个图层的交集", "depends_on": ["left", "right"],
            "tool_args": {"how": "intersection"},
        },
    ])])

    result = planner_router_node(
        {
            "user_input": "计算两个上传面图层的交集",
            "upload_file_ids": ["file_left", "file_right"],
            "messages": [],
        },
        llm=llm,
    )

    assert llm.calls == 1
    assert result["planner_source"] == "root_llm"
    tasks = {task["id"]: task for task in result["task_plan"]["tasks"]}
    assert tasks["left"]["tool_args"] == {"file_id": "file_left"}
    assert tasks["right"]["tool_args"] == {"file_id": "file_right"}
    assert tasks["intersect"]["tool_args"] == {
        "how": "intersection",
        "geometry_a_from": 0,
        "geometry_b_from": 1,
    }


def test_run_plan_trace_records_bound_upload_order_and_dependency_arguments():
    """The public trace must expose the exact data-plane identity actually dispatched."""
    from app.agents.events.current import reset_current_handler, set_current_handler

    llm = ScriptedRootLLM([_plan([
        {
            "id": "left", "agent_role": "geometer", "tool_name": "data_io_read",
            "goal": "读取第一个图层", "depends_on": [],
        },
        {
            "id": "right", "agent_role": "geometer", "tool_name": "data_io_read",
            "goal": "读取第二个图层", "depends_on": [],
        },
        {
            "id": "intersect", "agent_role": "geometer", "tool_name": "overlay",
            "goal": "计算交集", "depends_on": ["left", "right"],
            "tool_args": {"how": "intersection"},
        },
    ])])
    events: list[dict[str, Any]] = []
    token = set_current_handler(events.append)
    try:
        planner_router_node({
            "user_input": "计算两个上传面图层的交集",
            "upload_file_ids": ["file_first", "file_second"],
            "messages": [],
            "session_id": "trace-session",
            "run_id": "run-trace",
        }, llm=llm)
    finally:
        reset_current_handler(token)

    plan_event = next(event for event in events if event["event"] == "run.plan")
    assert plan_event["upload_file_ids"] == ["file_first", "file_second"]
    tasks = {task["id"]: task for task in plan_event["tasks"]}
    assert tasks["left"]["tool_args"] == {"file_id": "file_first"}
    assert tasks["right"]["tool_args"] == {"file_id": "file_second"}
    assert tasks["intersect"]["tool_args"] == {
        "how": "intersection", "geometry_a_from": 0, "geometry_b_from": 1,
    }


def test_root_planner_rejects_model_supplied_upload_identity_then_binds_the_real_one():
    """A Root LLM may never substitute a guessed file_id for a browser upload."""
    invalid = _plan([{
        "id": "read", "agent_role": "geometer", "tool_name": "data_io_read",
        "goal": "读取上传图层", "depends_on": [],
        "tool_args": {"file_id": "file_guessed"},
    }])
    valid = _plan([{
        "id": "read", "agent_role": "geometer", "tool_name": "data_io_read",
        "goal": "读取上传图层", "depends_on": [],
    }])
    llm = ScriptedRootLLM([invalid, valid])

    result = planner_router_node(
        {
            "user_input": "读取我上传的图层",
            "upload_file_ids": ["file_browser_authoritative"],
            "messages": [],
        },
        llm=llm,
    )

    assert llm.calls == 2
    assert result["planner_source"] == "root_llm"
    assert result["task_plan"]["tasks"][0]["tool_args"] == {
        "file_id": "file_browser_authoritative",
    }
