"""Root dispatcher graph tests.

Mock strategy:
- Mock `create_llm` and `dispatch_node` so planner and dispatch are intercepted.
- The test verifies dispatcher graph compilation and topological sorting logic.
"""

from __future__ import annotations

from unittest.mock import patch

from app.agents.dispatcher import build_dispatcher


def test_dispatcher_graph_compiles():
    """Verify the dispatcher graph compiles without errors."""
    app = build_dispatcher()
    assert app is not None


def test_topological_batches():
    from app.agents.dispatcher import _topological_batches
    from app.agents.schemas import SubTask

    t1 = SubTask(id="t1", agent_role="geo", goal="g1")
    t2 = SubTask(id="t2", agent_role="poi", goal="g2", depends_on=["t1"])
    t3 = SubTask(id="t3", agent_role="poi", goal="g3", depends_on=["t1"])
    t4 = SubTask(id="t4", agent_role="viz", goal="g4", depends_on=["t2", "t3"])

    batches = _topological_batches([t1, t2, t3, t4])
    assert len(batches) == 3
    assert {t.id for t in batches[0]} == {"t1"}
    assert {t.id for t in batches[1]} == {"t2", "t3"}
    assert {t.id for t in batches[2]} == {"t4"}


def test_empty_dependency():
    from app.agents.dispatcher import _topological_batches
    from app.agents.schemas import SubTask

    batches = _topological_batches([])
    assert batches == []


def test_single_batch_independent():
    from app.agents.dispatcher import _topological_batches
    from app.agents.schemas import SubTask

    t1 = SubTask(id="t1", agent_role="geo", goal="g1")
    t2 = SubTask(id="t2", agent_role="poi", goal="g2")
    batches = _topological_batches([t1, t2])
    assert len(batches) == 1
    assert len(batches[0]) == 2


def test_topological_complex_dag():
    from app.agents.dispatcher import _topological_batches
    from app.agents.schemas import SubTask

    t1 = SubTask(id="t1", agent_role="geo", goal="g1")
    t2 = SubTask(id="t2", agent_role="poi", goal="g2", depends_on=["t1"])
    t3 = SubTask(id="t3", agent_role="poi", goal="g3", depends_on=["t1"])
    t4 = SubTask(id="t4", agent_role="geo", goal="g4", depends_on=["t2"])
    t5 = SubTask(id="t5", agent_role="viz", goal="g5", depends_on=["t2", "t3"])
    t6 = SubTask(id="t6", agent_role="viz", goal="g6", depends_on=["t4", "t5"])
    batches = _topological_batches([t1, t2, t3, t4, t5, t6])
    assert len(batches) == 4
    assert {t.id for t in batches[0]} == {"t1"}
    assert {t.id for t in batches[1]} == {"t2", "t3"}
    assert {t.id for t in batches[2]} == {"t4", "t5"}
    assert {t.id for t in batches[3]} == {"t6"}


def test_topological_cyclic_raises():
    from app.agents.dispatcher import _topological_batches
    from app.agents.schemas import SubTask
    import pytest

    t1 = SubTask(id="t1", agent_role="geo", goal="g1", depends_on=["t2"])
    t2 = SubTask(id="t2", agent_role="poi", goal="g2", depends_on=["t1"])
    with pytest.raises(ValueError, match="cyclic"):
        _topological_batches([t1, t2])


def test_successful_poi_dispatch_skips_independent_verifier():
    """Successful poi dispatch must not invoke nested independent verifier."""
    import asyncio
    from unittest.mock import MagicMock, patch

    from app.agents.dispatcher import _dispatch_single
    from app.agents.schemas import SubTask

    results, dispatched, events = {}, {}, []
    legacy_verifier = MagicMock()
    raw_state = {
        "agent_role": "poi",
        "iteration": 1,
        "tool_results": [{"status": "success", "data": {"pois": []}}],
        "final_output": {"summary": "找到结果"},
    }
    with patch("app.agents.build_sub_agent.run_sub_agent", return_value=raw_state), \
         patch(
             "app.agents.dispatcher._verify_outcome_independent",
             legacy_verifier,
             create=True,
         ):
        asyncio.run(_dispatch_single(
            {"task_plan": {"tasks": [{"id": "t1"}]}},
            SubTask(id="t1", agent_role="poi", goal="查询咖啡"),
            results, dispatched, events,
        ))
    legacy_verifier.assert_not_called()
    # Nested independent verifier must be fully removed (no reintroduction stub).
    import app.agents.dispatcher as dispatcher_mod
    assert not hasattr(dispatcher_mod, "_verify_outcome_independent")


# ---------------------------------------------------------------------------
# Phase 4.2: EMPTY_RUN detection tests
# ---------------------------------------------------------------------------

def test_subagent_state_to_outcome_empty_run_no_tool_results_no_output():
    """No tool_results and no meaningful final_output → failed with EMPTY_RUN."""
    from app.agents.dispatcher import subagent_state_to_outcome

    state = {
        "agent_role": "poi",
        "final_output": {},
        "tool_results": [],
        "iteration": 0,
    }
    outcome = subagent_state_to_outcome(state, "t1", "r1")
    assert outcome.status == "failed"
    assert outcome.error_code == "EMPTY_RUN"
    assert outcome.error_message is not None


def test_subagent_state_to_outcome_empty_run_empty_final_output():
    """Empty tool_results + final_output with all falsy fields → EMPTY_RUN."""
    from app.agents.dispatcher import subagent_state_to_outcome

    state = {
        "agent_role": "geo",
        "final_output": {"text": "", "summary": "", "results": []},
        "tool_results": [],
        "iteration": 0,
    }
    outcome = subagent_state_to_outcome(state, "t1", "r1")
    assert outcome.status == "failed"
    assert outcome.error_code == "EMPTY_RUN"


def test_subagent_state_to_outcome_with_clarification_not_empty():
    """final_output with clarification should stay refined, not EMPTY_RUN."""
    from app.agents.dispatcher import subagent_state_to_outcome

    state = {
        "agent_role": "poi",
        "final_output": {"clarification": "need more details"},
        "tool_results": [],
        "iteration": 0,
    }
    outcome = subagent_state_to_outcome(state, "t1", "r1")
    assert outcome.status == "refined"
    assert outcome.error_code is None


def test_subagent_state_to_outcome_with_results_preserves_success():
    """Has tool_results with non-error status → normal success."""
    from app.agents.dispatcher import subagent_state_to_outcome

    state = {
        "agent_role": "poi",
        "final_output": {
            "text": "found some",
            "results": [{"tool_name": "query_poi", "data": {"pois": []}}],
        },
        "tool_results": [{"status": "ok", "mode": "json"}],
        "iteration": 0,
    }
    outcome = subagent_state_to_outcome(state, "t1", "r1")
    assert outcome.status == "success"
    assert outcome.error_code is None
    assert outcome.artifacts["pois"] == []


def test_subagent_state_to_outcome_empty_tool_result_is_failed():
    """A deterministic workflow step returning empty must block downstream tasks."""
    from app.agents.dispatcher import subagent_state_to_outcome

    state = {
        "agent_role": "geometer",
        "final_output": {
            "status": "failed",
            "results": [
                {
                    "tool_name": "buffer",
                    "status": "empty",
                    "data": None,
                    "message": "无法解析输入几何",
                }
            ],
        },
        "tool_results": [
            {
                "tool_name": "buffer",
                "status": "empty",
                "data": None,
                "message": "无法解析输入几何",
            }
        ],
        "iteration": 2,
    }

    outcome = subagent_state_to_outcome(state, "buffer_step", "r1")

    assert outcome.status == "failed"
    assert outcome.error_message == "无法解析输入几何"


def test_subagent_state_to_outcome_preserves_legitimate_empty_poi_artifact():
    """An empty query result is terminal data, while remaining unusable as a dependency."""
    from app.agents.dispatcher import subagent_state_to_outcome

    payload = {
        "pois": [], "query": "不存在的品牌", "center": [118.7845, 32.0429],
        "radius_m": 500, "radius_tolerance_m": 5, "crs": "GCJ02",
    }
    state = {
        "agent_role": "poi",
        "final_output": {
            "status": "empty",
            "summary": "未找到相关 POI",
            "results": [{"tool_name": "query_poi", "source": "Amap", "data": payload}],
        },
        "tool_results": [{
            "tool_name": "query_poi", "status": "empty", "source": "Amap",
            "data": payload, "message": "未找到相关 POI",
        }],
        "iteration": 1,
    }

    outcome = subagent_state_to_outcome(state, "poi_step", "r-empty")

    assert outcome.status == "empty"
    assert outcome.error_code is None
    assert outcome.artifacts["pois"] == []
    assert outcome.artifacts["result"] == payload


def test_assemble_legitimate_empty_poi_converges_without_map_or_failure():
    """Zero POIs should produce a factual empty terminal, not EMPTY_RUN/run.failed."""
    from app.agents.dispatcher import assemble_node

    payload = {
        "pois": [], "query": "不存在的品牌", "center": [118.7845, 32.0429],
        "radius_m": 500, "radius_tolerance_m": 5, "crs": "GCJ02",
    }
    final_output = assemble_node({
        "user_input": "查询不存在的品牌",
        "dispatcher_events": [],
        "sub_results": {
            "poi_step": [{
                "agent_role": "poi", "status": "empty",
                "artifacts": {
                    "pois": [], "result": payload, "result_tool_name": "query_poi",
                    "provenance": {
                        "task_id": "poi_step", "tool_name": "query_poi",
                        "query": "不存在的品牌", "input_file_ids": [],
                        "crs": "GCJ02", "upstream_task_ids": [],
                    },
                },
            }],
        },
    })["final_output"]

    assert final_output["status"] == "empty"
    assert final_output["summary"] == "未找到相关 POI"
    assert final_output["results"][0]["data"] == payload
    assert "map" not in final_output


def test_assemble_keeps_a_refined_poi_result_as_the_terminal_task_product():
    """Verifier refinement is still a successful task-owned artifact."""
    from app.agents.dispatcher import assemble_node

    pois = [{"name": "茶百道", "location": [118.7845, 32.0429], "crs": "GCJ02"}]
    payload = {
        "pois": pois, "query": "茶百道", "center": [118.7845, 32.0429],
        "radius_m": 500, "radius_tolerance_m": 5, "crs": "GCJ02",
    }
    final_output = assemble_node({
        "user_input": "查询茶百道",
        "dispatcher_events": [],
        "sub_results": {
            "tea": [{
                "agent_role": "poi", "status": "refined",
                "artifacts": {
                    "pois": pois, "result": payload, "result_tool_name": "query_poi",
                    "provenance": {
                        "task_id": "tea", "tool_name": "query_poi", "query": "茶百道",
                        "input_file_ids": [], "crs": "GCJ02", "upstream_task_ids": [],
                    },
                },
            }],
        },
    })["final_output"]

    assert final_output["status"] == "success"
    assert final_output["results"][0]["data"] == payload
    assert len(final_output["map"]["layers"][0]["features"]) == 1


def test_geometer_step_preserves_generic_result_for_next_dag_step():
    """Every successful geometer tool result remains addressable by its successor."""
    from app.agents.dispatcher import subagent_state_to_outcome

    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [118.78, 32.04]},
                "properties": {},
            }
        ],
    }
    state = {
        "agent_role": "geometer",
        "final_output": {
            "status": "success",
            "results": [
                {
                    "tool_name": "fix_geometries",
                    "status": "success",
                    "data": geojson,
                }
            ],
        },
        "tool_results": [
            {
                "tool_name": "fix_geometries",
                "status": "success",
                "data": geojson,
            }
        ],
        "iteration": 1,
    }

    outcome = subagent_state_to_outcome(state, "fix_step", "r1")

    assert outcome.status == "success"
    assert outcome.artifacts["geojson"] == geojson
    assert outcome.artifacts["result"] == geojson
    assert outcome.artifacts["result_tool_name"] == "fix_geometries"


def test_geometer_export_result_is_not_dropped_from_artifacts():
    """Non-GeoJSON outputs such as export paths must survive Root assembly."""
    from app.agents.dispatcher import subagent_state_to_outcome

    exported = {"path": "workspace/result.geojson", "format": "GeoJSON"}
    state = {
        "agent_role": "geometer",
        "final_output": {
            "status": "success",
            "results": [
                {
                    "tool_name": "export_result",
                    "status": "success",
                    "data": exported,
                }
            ],
        },
        "tool_results": [
            {
                "tool_name": "export_result",
                "status": "success",
                "data": exported,
            }
        ],
        "iteration": 1,
    }

    outcome = subagent_state_to_outcome(state, "export_step", "r1")

    assert outcome.status == "success"
    assert outcome.artifacts["result"] == exported
    assert outcome.artifacts["result_tool_name"] == "export_result"


def test_assemble_surfaces_export_result_in_final_output():
    """The terminal export step must be visible to the API/UI, not only checkpoints."""
    from app.agents.dispatcher import assemble_node

    class FakeLLM:
        def invoke(self, _messages):
            from types import SimpleNamespace

            return SimpleNamespace(content='{"reply":"处理完成"}')

    state = {
        "user_input": "导出结果",
        "dispatcher_events": [],
        "sub_results": {
            "export_step": [{
                "agent_role": "geometer",
                "status": "success",
                "artifacts": {
                    "result_tool_name": "export_result",
                    "result": {
                        "path": "workspace/result.geojson",
                        "format": "GeoJSON",
                    },
                },
                "iteration_used": 1,
            }]
        },
    }

    result = assemble_node(state, llm=FakeLLM())

    final_output = result["final_output"]
    assert final_output.get("status") != "failed"
    assert final_output["results"] == [{
        "tool_name": "export_result",
        "source": "computed",
        "data": {"path": "workspace/result.geojson", "format": "GeoJSON"},
        "truncated": False,
    }]


def test_subagent_state_to_outcome_with_text_no_tool_results():
    """final_output has text but no tool_results → still success (LLM-only reply)."""
    from app.agents.dispatcher import subagent_state_to_outcome

    state = {
        "agent_role": "poi",
        "final_output": {"text": "Hello, I'm Gismind!"},
        "tool_results": [],
        "iteration": 0,
    }
    outcome = subagent_state_to_outcome(state, "t1", "r1")
    assert outcome.status == "success"


def test_subagent_outcome_uses_latest_code_result_after_refinement():
    from app.agents.dispatcher import subagent_state_to_outcome

    state = {
        "agent_role": "geo",
        "iteration": 2,
        "final_output": {"summary": "refined"},
        "tool_results": [
            {"status": "success", "mode": "code", "data": {"result": {"value": "old"}}},
            {"status": "success", "mode": "code", "data": {"result": {"value": "new"}}},
        ],
    }

    outcome = subagent_state_to_outcome(state, "t1", "r1")

    assert outcome.artifacts["value"] == "new"
    assert outcome.error_code is None


def test_assemble_node_empty_results_and_text_parts():
    """assemble_node with no results_for_final and no text_parts → EMPTY_RUN in final_output."""
    from unittest.mock import patch
    from app.agents.dispatcher import assemble_node

    state = {
        "user_input": "计算北京五环内的缓冲区",
        "dispatcher_events": [],
        "sub_results": {},
    }
    result = assemble_node(state)
    assert result["should_stop"] is True
    fo = result["final_output"]
    assert fo.get("status") == "failed"
    assert fo.get("error_code") == "EMPTY_RUN"
    assert "results" in fo
    assert fo["results"] == []


def test_assemble_node_with_results_no_emptyrun():
    """assemble_node with results_for_final populated → no EMPTY_RUN flag."""
    from app.agents.dispatcher import assemble_node

    state = {
        "user_input": "找附近的咖啡馆",
        "dispatcher_events": [],
        "sub_results": {
            "t1": [{
                "agent_role": "poi",
                "status": "success",
                "artifacts": {"pois": [{"name": "星巴克", "location": [116.4, 39.9]}]},
                "iteration_used": 0,
            }],
        },
    }
    result = assemble_node(state)
    fo = result["final_output"]
    # Should NOT have EMPTY_RUN because text_parts is populated
    assert fo.get("error_code") != "EMPTY_RUN"
    # results_for_final should have the POI entry
    assert len(fo.get("results", [])) >= 1


def test_assemble_geo_summary_preserves_exact_tool_coordinates_without_llm_rewrite():
    """Structured coordinates are facts and must never be contradicted by synthesis."""
    from app.agents.dispatcher import assemble_node

    class ContradictingLLM:
        called = False

        def invoke(self, _messages):
            self.called = True
            return type("R", (), {"content": '{"reply":"工具未直接提供经纬度"}'})()

    llm = ContradictingLLM()
    state = {
        "user_input": "南京夫子庙坐标是什么？",
        "dispatcher_events": [],
        "sub_results": {
            "t1": [{
                "agent_role": "geo",
                "status": "success",
                "artifacts": {
                    "locations": [{
                        "formatted_address": "江苏省南京市秦淮区夫子庙",
                        "location": [118.78821, 32.02064],
                        "source": "Amap",
                    }],
                },
                "iteration_used": 1,
            }],
        },
    }

    result = assemble_node(state, llm=llm)
    summary = result["final_output"]["summary"]

    assert "118.78821" in summary
    assert "32.02064" in summary
    assert "未直接提供" not in summary
    assert llm.called is False
    assert result["final_output"]["status"] == "success"


def test_assemble_geo_transform_result_is_a_success_without_a_map_or_fallback_llm():
    """A numeric coordinate transform is useful even though it has no geometry layer."""
    from app.agents.dispatcher import assemble_node

    class UnexpectedLLM:
        called = False

        def invoke(self, _messages):
            self.called = True
            raise AssertionError("coordinate transform must use exact tool facts")

    llm = UnexpectedLLM()
    state = {
        "user_input": "将坐标从 WGS84 转为 GCJ02",
        "dispatcher_events": [],
        "sub_results": {
            "t0": [{
                "agent_role": "geo",
                "status": "success",
                "artifacts": {
                    "result_tool_name": "geo_transform",
                    "result": {
                        "operation": "wgs84_to_gcj02",
                        "input": {"lng": 118.7782, "lat": 32.0417},
                        "output": {"lng": 118.783409, "lat": 32.039652},
                    },
                },
                "iteration_used": 1,
            }],
        },
    }

    result = assemble_node(state, llm=llm)
    final_output = result["final_output"]

    assert final_output["status"] == "success"
    assert final_output["results"][0]["tool_name"] == "geo_transform"
    assert "118.783409" in final_output["summary"]
    assert llm.called is False


def test_documented_zero_distance_nearest_plan_keeps_the_distance_constraint():
    """The Root plan owns exact user constraints instead of relying on model recall."""
    from app.agents.dispatcher import _documented_prompt_plan

    plan = _documented_prompt_plan(
        "Une los POI subidos con paradas de autobús usando una distancia máxima de 0 metros.",
        ["points", "stops"],
    )

    assert plan is not None
    nearest = next(task for task in plan.tasks if task.tool_name == "join_by_nearest")
    assert nearest.tool_args == {"max_distance": 0}


def test_strong_guardrail_does_not_capture_normal_multiturn_natural_language():
    """Catalog-shaped follow-ups belong to Root planning, not a hidden shortcut."""
    from langchain_core.messages import AIMessage, HumanMessage

    from app.agents.dispatcher import _strong_constraint_guardrail_plan

    plan = _strong_constraint_guardrail_plan(
        "Now find Chabaidao within 500 metres of Xinjiekou and compare its density with the previous Mixue result.",
        [],
        [
            HumanMessage(content="南京新街口500米内蜜雪冰城"),
            AIMessage(content="找到 8 个 POI: 蜜雪冰城 A"),
        ],
    )

    assert plan is None


def test_documented_gps_to_amap_request_routes_to_geo_transform():
    """GPS → 高德 means WGS84 → GCJ02, not an invented generic transform tool."""
    from app.agents.dispatcher import _strong_constraint_guardrail_plan

    plan = _strong_constraint_guardrail_plan(
        "请把 GPS 点 116.397、39.908 换成高德地图可直接使用的坐标。",
        [],
    )

    assert plan is not None
    assert len(plan.tasks) == 1
    assert plan.tasks[0].agent_role == "geo"
    assert plan.tasks[0].tool_name == "geo_transform"
    assert "116.397" in plan.tasks[0].goal
    assert "GCJ02" in plan.tasks[0].goal


def test_attribute_equality_is_not_captured_by_strong_guardrail():
    """A normal uploaded-data filter must remain Root-Planner work."""
    from app.agents.dispatcher import _strong_constraint_guardrail_plan

    plan = _strong_constraint_guardrail_plan(
        "从我刚传的点图层里挑出 class 等于 station 的记录，并作为新图层显示。",
        ["file_points"],
    )

    assert plan is None


def test_elevation_reclass_is_not_captured_by_strong_guardrail():
    """A normal raster workflow must remain Root-Planner work."""
    from app.agents.dispatcher import _strong_constraint_guardrail_plan

    plan = _strong_constraint_guardrail_plan(
        "对上传的高程栅格先求坡度，再按小于15度、15到30度和大于30度分成三个等级后显示。",
        ["file_dem"],
    )

    assert plan is None


def test_assemble_compares_multiturn_poi_density_from_persisted_result():
    """A same-radius follow-up must report the prior result and a count-based comparison."""
    from langchain_core.messages import AIMessage, HumanMessage

    from app.agents.dispatcher import assemble_node

    result = assemble_node({
        "user_input": "再查南京新街口 500 米内的茶百道，并与上一轮蜜雪冰城比较密度。",
        "messages": [
            HumanMessage(content="南京新街口500米内蜜雪冰城"),
            AIMessage(content="已定位：南京新街口\n找到 8 个 POI: 蜜雪冰城 A"),
        ],
        "dispatcher_events": [],
        "sub_results": {
            "t1": [{
                "agent_role": "poi",
                "status": "success",
                "artifacts": {
                    "pois": [
                        {"name": f"茶百道 {index}", "location": [118.78 + index / 1000, 32.04]}
                        for index in range(5)
                    ],
                },
                "iteration_used": 1,
            }],
        },
    })

    summary = result["final_output"]["summary"]
    assert "茶百道检索到 5 个" in summary
    assert "蜜雪冰城检索到 8 个" in summary
    assert "较低" in summary


def test_assemble_compares_valid_zero_poi_result_instead_of_empty_run():
    """A successful zero-result POI lookup must remain answerable downstream."""
    from langchain_core.messages import AIMessage, HumanMessage

    from app.agents.dispatcher import assemble_node

    result = assemble_node({
        "user_input": "沿用刚才的位置，改查茶百道并和上一轮数量比较。",
        "messages": [
            HumanMessage(content="南京新街口500米内蜜雪冰城"),
            AIMessage(content="找到 8 个 POI: 蜜雪冰城 A"),
        ],
        "dispatcher_events": [],
        "sub_results": {
            "t1": [{
                "agent_role": "poi",
                "status": "success",
                "artifacts": {
                    "pois": [],
                    "result": {
                        "pois": [],
                        "provider_status": {"Amap": "empty", "OSM": "unavailable"},
                    },
                    "result_tool_name": "query_poi",
                },
                "iteration_used": 1,
            }],
        },
    })

    summary = result["final_output"]["summary"]
    assert result["final_output"]["status"] == "success"
    assert "未找到相关 POI" in summary
    assert "茶百道检索到 0 个" in summary
    assert "蜜雪冰城检索到 8 个" in summary
    assert "较低" in summary


def test_dispatch_records_task_scoped_artifact_provenance():
    """Every dispatched artifact keeps its producer/task/input identity for safe assembly."""
    import asyncio
    from unittest.mock import patch

    from app.agents.dispatcher import _dispatch_single
    from app.agents.schemas import SubTask

    raw_state = {
        "agent_role": "poi",
        "iteration": 1,
        "tool_results": [{"tool_name": "query_poi", "status": "success"}],
        "final_output": {
            "results": [{
                "tool_name": "query_poi",
                "status": "success",
                "data": {
                    "pois": [{"name": "茶百道", "location": [118.7845, 32.0429], "crs": "GCJ02"}],
                    "query": "茶百道",
                    "center": [118.7845, 32.0429],
                    "radius_m": 500,
                    "crs": "GCJ02",
                },
            }],
        },
    }
    task = SubTask(
        id="chabaidao", agent_role="poi", tool_name="query_poi",
        goal="查询茶百道", depends_on=["locate"], tool_args={"file_id": "file_area"},
    )
    results: dict[str, list[dict]] = {}

    with patch("app.agents.build_sub_agent.run_sub_agent", return_value=raw_state):
        asyncio.run(_dispatch_single(
            {"task_plan": {"tasks": [task.model_dump()]}, "upload_file_ids": ["file_area"]},
            task,
            results,
            {},
            [],
        ))

    provenance = results["chabaidao"][0]["artifacts"]["provenance"]
    assert provenance == {
        "task_id": "chabaidao",
        "tool_name": "query_poi",
        "query": "茶百道",
        "input_file_ids": ["file_area"],
        "crs": "GCJ02",
        "upstream_task_ids": ["locate"],
    }


def test_assemble_comparison_selects_only_the_current_query_artifact_for_count_and_map():
    """An accidental same-role POI branch must not turn 20 + 2 into the current count 22."""
    from langchain_core.messages import AIMessage, HumanMessage

    from app.agents.dispatcher import assemble_node

    def poi_artifact(task_id: str, query: str, count: int) -> dict:
        pois = [
            {
                "name": f"{query} {index}",
                "location": [118.7845 + index / 100_000, 32.0429],
                "crs": "GCJ02",
            }
            for index in range(count)
        ]
        return {
            "pois": pois,
            "result": {"pois": pois, "query": query, "center": [118.7845, 32.0429], "radius_m": 500, "crs": "GCJ02"},
            "result_tool_name": "query_poi",
            "provenance": {
                "task_id": task_id, "tool_name": "query_poi", "query": query,
                "input_file_ids": [], "crs": "GCJ02", "upstream_task_ids": ["locate"],
            },
        }

    result = assemble_node({
        "user_input": "沿用刚才的位置，改查茶百道并和上一轮数量比较。",
        "messages": [
            HumanMessage(content="南京新街口五百米内蜜雪冰城"),
            AIMessage(content="找到 20 个 POI: 蜜雪冰城 A"),
        ],
        "dispatcher_events": [],
        "task_plan": {"tasks": [
            {"id": "mixue_retry", "agent_role": "poi", "tool_name": "query_poi", "goal": "查询蜜雪冰城", "depends_on": ["locate"]},
            {"id": "chabaidao", "agent_role": "poi", "tool_name": "query_poi", "goal": "查询茶百道", "depends_on": ["locate"]},
        ]},
        "sub_results": {
            "mixue_retry": [{"agent_role": "poi", "status": "success", "artifacts": poi_artifact("mixue_retry", "蜜雪冰城", 20)}],
            "chabaidao": [{"agent_role": "poi", "status": "success", "artifacts": poi_artifact("chabaidao", "茶百道", 2)}],
        },
    })["final_output"]

    assert "茶百道检索到 2 个" in result["summary"]
    assert "茶百道检索到 22 个" not in result["summary"]
    poi_results = [item for item in result["results"] if item["tool_name"] == "query_poi"]
    assert [len(item["data"]["pois"]) for item in poi_results] == [2]
    assert len(result["map"]["layers"][0]["features"]) == 2


def test_assemble_scopes_non_comparison_poi_results_and_layers_to_the_query_named_by_the_user():
    """A stale same-role branch must not leak into an ordinary POI answer or map."""
    from app.agents.dispatcher import assemble_node

    def poi_artifact(task_id: str, query: str, count: int) -> dict:
        pois = [
            {
                "name": f"{query} {index}",
                "location": [118.7845 + index / 100_000, 32.0429],
                "crs": "GCJ02",
            }
            for index in range(count)
        ]
        return {
            "pois": pois,
            "result": {"pois": pois, "query": query, "center": [118.7845, 32.0429], "radius_m": 500, "crs": "GCJ02"},
            "result_tool_name": "query_poi",
            "provenance": {
                "task_id": task_id, "tool_name": "query_poi", "query": query,
                "input_file_ids": [], "crs": "GCJ02", "upstream_task_ids": ["locate"],
            },
        }

    def layer(query: str, count: int) -> dict:
        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [118.7845 + index / 100_000, 32.0429]},
                    "properties": {"name": f"{query} {index}"},
                }
                for index in range(count)
            ],
        }

    result = assemble_node({
        "user_input": "查询南京新街口五百米内的茶百道并显示",
        "messages": [],
        "dispatcher_events": [],
        "task_plan": {"tasks": [
            {"id": "mixue_retry", "agent_role": "poi", "tool_name": "query_poi", "goal": "错误复查蜜雪冰城", "depends_on": ["locate"]},
            {"id": "tea", "agent_role": "poi", "tool_name": "query_poi", "goal": "查询茶百道", "depends_on": ["locate"]},
            {"id": "mixue_map", "agent_role": "viz", "tool_name": "map_layer_build", "goal": "错误蜜雪冰城图层", "depends_on": ["mixue_retry"]},
            {"id": "tea_map", "agent_role": "viz", "tool_name": "map_layer_build", "goal": "茶百道图层", "depends_on": ["tea"]},
        ]},
        "sub_results": {
            "mixue_retry": [{"agent_role": "poi", "status": "success", "artifacts": poi_artifact("mixue_retry", "蜜雪冰城", 20)}],
            "tea": [{"agent_role": "poi", "status": "success", "artifacts": poi_artifact("tea", "茶百道", 2)}],
            "mixue_map": [{"agent_role": "viz", "status": "success", "artifacts": {"layers": [layer("蜜雪冰城", 20)]}}],
            "tea_map": [{"agent_role": "viz", "status": "success", "artifacts": {"layers": [layer("茶百道", 2)]}}],
        },
    })["final_output"]

    poi_results = [item for item in result["results"] if item["tool_name"] == "query_poi"]
    assert [len(item["data"]["pois"]) for item in poi_results] == [2]
    assert "找到 20 个 POI" not in result["summary"]
    assert len(result["map"]["layers"]) == 1
    assert len(result["map"]["layers"][0]["features"]) == 2


def test_assemble_preserves_task_provenance_on_every_structured_result():
    """Public final results must retain the identity used to prevent cross-task mixing."""
    from app.agents.dispatcher import assemble_node

    provenance = {
        "task_id": "export",
        "tool_name": "export_result",
        "query": None,
        "input_file_ids": ["file_source"],
        "crs": "WGS84",
        "upstream_task_ids": ["buffer"],
    }
    final_output = assemble_node({
        "user_input": "导出结果",
        "dispatcher_events": [],
        "sub_results": {
            "export": [{
                "agent_role": "geometer",
                "status": "success",
                "artifacts": {
                    "result_tool_name": "export_result",
                    "result": {"path": "workspace/result.geojson", "feature_count": 2},
                    "provenance": provenance,
                },
            }],
        },
    })["final_output"]

    assert final_output["results"][0]["provenance"] == provenance


def test_assemble_deduplicates_raster_result_repeated_by_viz_task():
    """A computed raster and its viz wrapper must produce one frontend layer."""
    from app.agents.dispatcher import assemble_node

    raster = {
        "type": "raster",
        "png_b64": "iVBORw0KGgo=",
        "bbox": [118.79, 32.03, 118.82, 32.06],
        "width": 3,
        "height": 3,
        "value_kind": "slope",
    }
    result = assemble_node({
        "user_input": "计算上传高程栅格的坡度并显示",
        "messages": [],
        "dispatcher_events": [],
        "sub_results": {
            "t2": [{
                "agent_role": "geometer",
                "status": "success",
                "artifacts": {"result": raster, "result_tool_name": "slope"},
            }],
            "t3": [{
                "agent_role": "viz",
                "status": "success",
                "artifacts": {"layers": [raster]},
            }],
        },
    })

    assert len(result["final_output"]["map"]["layers"]) == 1


def test_assemble_reports_completed_map_layer_without_a_fallback_reply():
    """A successful map render must not be replaced by an upload request."""
    from app.agents.dispatcher import assemble_node

    class UnexpectedLLM:
        called = False

        def invoke(self, _messages):
            self.called = True
            raise AssertionError("map facts must not fall through to fallback synthesis")

    llm = UnexpectedLLM()
    layer = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [118.78, 32.04]},
            "properties": {"class": "poi"},
        }],
    }
    result = assemble_node({
        "user_input": "按 class 分级设色",
        "dispatcher_events": [],
        "sub_results": {
            "t1": [{
                "agent_role": "viz",
                "status": "success",
                "artifacts": {"layers": [layer], "result": {"layers": [layer]}},
                "iteration_used": 1,
            }],
        },
    }, llm=llm)

    assert "地图图层已生成" in result["final_output"]["summary"]
    assert llm.called is False


def test_assemble_reports_invalid_geometry_issues_without_a_fallback_reply():
    """Validity issues are user-facing factual results, even without a map layer."""
    from app.agents.dispatcher import assemble_node

    class UnexpectedLLM:
        called = False

        def invoke(self, _messages):
            self.called = True
            raise AssertionError("validity facts must not fall through to fallback synthesis")

    llm = UnexpectedLLM()
    result = assemble_node({
        "user_input": "检查上传多边形有效性",
        "dispatcher_events": [],
        "sub_results": {
            "t1": [{
                "agent_role": "geometer",
                "status": "success",
                "artifacts": {
                    "result_tool_name": "check_validity",
                    "result": {"issues": [{"index": 0, "type": "invalid", "reason": "Self-intersection"}]},
                },
                "iteration_used": 1,
            }],
        },
    }, llm=llm)

    assert "发现 1 个几何问题" in result["final_output"]["summary"]
    assert "Self-intersection" in result["final_output"]["summary"]
    assert llm.called is False


def test_assemble_marks_partial_when_a_required_subtask_failed():
    """A successful upstream result must not hide a failed downstream operation."""
    from app.agents.dispatcher import assemble_node

    state = {
        "user_input": "查地铁站并创建缓冲区",
        "dispatcher_events": [],
        "sub_results": {
            "poi": [{
                "agent_role": "poi",
                "status": "success",
                "artifacts": {
                    "pois": [{"name": "新街口站", "location": [118.784, 32.041]}],
                },
                "iteration_used": 1,
            }],
            "buffer": [{
                "agent_role": "geometer",
                "status": "failed",
                "artifacts": {},
                "error_code": "EMPTY_RESULT",
                "error_message": "缓冲区未生成",
                "iteration_used": 1,
            }],
        },
    }

    final_output = assemble_node(state)["final_output"]

    assert final_output["status"] == "partial"
    assert "缓冲区未生成" in final_output["summary"]
    assert "全部完成" not in final_output["summary"]


def test_assemble_emits_run_failed_not_completed_for_partial_terminal_state():
    """A semantic/downstream failure must never publish a successful run event."""
    from app.agents.dispatcher import assemble_node
    from app.agents.events.current import reset_current_handler, set_current_handler

    events: list[dict] = []
    token = set_current_handler(events.append)
    try:
        result = assemble_node({
            "user_input": "查地铁站并创建缓冲区",
            "session_id": "terminal-partial",
            "run_id": "run-terminal-partial",
            "dispatcher_events": [],
            "sub_results": {
                "poi": [{
                    "agent_role": "poi",
                    "status": "success",
                    "artifacts": {"pois": [{"name": "新街口站", "location": [118.784, 32.041]}]},
                }],
                "buffer": [{
                    "agent_role": "geometer",
                    "status": "failed",
                    "artifacts": {},
                    "error_code": "SEMANTIC_POSTCONDITION_FAILED",
                    "error_message": "BUFFER_AREA_MISMATCH",
                }],
            },
        })
    finally:
        reset_current_handler(token)

    assert result["final_output"]["status"] == "partial"
    assert [event["event"] for event in events] == ["run.failed"]
    assert events[0]["terminal_status"] == "partial"
    assert events[0]["error_code"] == "SUBTASK_PARTIAL_FAILURE"


def test_assemble_marks_failed_when_all_terminal_subtasks_failed():
    from app.agents.dispatcher import assemble_node

    state = {
        "user_input": "生成缓冲区",
        "dispatcher_events": [],
        "sub_results": {
            "buffer": [{
                "agent_role": "geometer",
                "status": "failed",
                "artifacts": {},
                "error_code": "INVALID_INPUT",
                "error_message": "输入几何为空",
                "iteration_used": 1,
            }],
        },
    }

    final_output = assemble_node(state)["final_output"]

    assert final_output["status"] == "failed"
    assert final_output["error_code"] == "SUBTASK_FAILED"
    assert "输入几何为空" in final_output["summary"]


def test_assemble_node_preserves_awaiting_input_pending_task():
    """assemble_node must not erase awaiting_input / pending_task as EMPTY_RUN."""
    from unittest.mock import patch

    from app.agents.dispatcher import assemble_node

    pending = {
        "sub_agent_run_id": "run-await-1",
        "original_request": "南京夫子庙缓冲区",
        "missing_slots": ["distance"],
        "message": "请提供缓冲距离（米）",
        "issues": [],
    }
    state = {
        "user_input": "南京夫子庙缓冲区",
        "dispatcher_events": [],
        "pending_task": pending,
        "final_output": {
            "status": "awaiting_input",
            "pending_task": pending,
            "summary": "请提供缓冲距离（米）",
        },
        "sub_results": {
            "t1": [{
                "agent_role": "geometer",
                "status": "awaiting_input",
                "pending_task": pending,
                "artifacts": {},
                "iteration_used": 0,
            }],
        },
    }

    with patch("app.agents.dispatcher.create_llm") as mock_llm:
        result = assemble_node(state)

    mock_llm.assert_not_called()
    assert result["should_stop"] is True
    assert result.get("pending_task") == pending
    fo = result["final_output"]
    assert fo.get("status") == "awaiting_input"
    assert fo.get("pending_task") == pending
    assert fo.get("error_code") != "EMPTY_RUN"
    assert fo.get("status") != "failed"
    assert "请提供缓冲距离" in (fo.get("summary") or fo.get("text") or "")


def test_assemble_node_ignores_stale_sub_awaiting_after_replan_marker():
    """Successful resume replan must not re-arm pause from historical sub_results."""
    from unittest.mock import patch

    from app.agents.dispatcher import assemble_node

    stale_pending = {
        "sub_agent_run_id": "run-old",
        "original_request": "南京夫子庙缓冲区",
        "missing_slots": ["distance"],
        "message": "请提供缓冲距离（米）",
        "issues": [],
    }
    state = {
        "user_input": "南京夫子庙缓冲区\n\n用户补充参数：{\"distance\": 500.0}",
        "dispatcher_events": [],
        "pending_task": None,
        "final_output": {},
        "sub_results": {
            "t_old": [{
                "agent_role": "geometer",
                "status": "awaiting_input",
                "pending_task": stale_pending,
                "artifacts": {},
                "iteration_used": 0,
            }],
            "t_new": [{
                "agent_role": "geometer",
                "status": "success",
                "artifacts": {"geojson": {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [118.78, 32.04]}, "properties": {}}]}},
                "iteration_used": 1,
            }],
        },
    }

    with patch("app.agents.dispatcher.create_llm") as mock_llm:
        mock_llm.return_value.invoke.return_value = type("R", (), {"content": '{"reply": "完成"}'})()
        # llm_invoke_with_retry path may not use .invoke; assemble catches exceptions
        result = assemble_node(state)

    fo = result["final_output"]
    assert fo.get("status") != "awaiting_input"
    assert result.get("pending_task") in (None, {})
    if result.get("pending_task") is not None:
        assert result.get("pending_task") != stale_pending
    assert fo.get("pending_task") != stale_pending
