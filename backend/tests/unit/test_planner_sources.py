"""Planner-source contracts for the root dispatcher.

These tests deliberately inject only the root LLM transport.  They exercise
the production parser and DAG validator, and make the planning provenance part
of the stable dispatcher contract.
"""

from __future__ import annotations

import json
from collections import deque
from typing import Any

from langchain_core.messages import AIMessage

from app.agents.dispatcher import planner_router_node


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


def test_closed_attribute_filter_never_invokes_invalid_root_tool_names():
    """A closed filter contract bypasses an LLM that could invent bad tools."""
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

    assert llm.calls == 0
    assert result["planner_source"] == "guardrail"
    assert [task["tool_name"] for task in result["task_plan"]["tasks"]] == [
        "data_io_read", "extract_by_attribute",
    ]
