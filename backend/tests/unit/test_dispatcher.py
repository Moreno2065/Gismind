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


def test_documented_attribute_equality_routes_to_structured_attribute_filter():
    """A class equality request must use the registered structured filter tool."""
    from app.agents.dispatcher import _strong_constraint_guardrail_plan

    plan = _strong_constraint_guardrail_plan(
        "从我刚传的点图层里挑出 class 等于 station 的记录，并作为新图层显示。",
        ["file_points"],
    )

    assert plan is not None
    assert [task.tool_name for task in plan.tasks] == ["data_io_read", "extract_by_attribute"]
    filter_task = plan.tasks[-1]
    assert filter_task.depends_on == ["t0"]
    assert filter_task.tool_args == {"field": "class", "operator": "==", "value": "station"}


def test_documented_elevation_reclass_routes_to_slope_then_exact_bins():
    """The common elevation synonym has a closed three-step raster contract."""
    from app.agents.dispatcher import _strong_constraint_guardrail_plan

    plan = _strong_constraint_guardrail_plan(
        "对上传的高程栅格先求坡度，再按小于15度、15到30度和大于30度分成三个等级后显示。",
        ["file_dem"],
    )

    assert plan is not None
    assert [task.tool_name for task in plan.tasks] == ["data_io_read", "slope", "reclassify_raster"]
    assert plan.tasks[1].depends_on == ["t0"]
    assert plan.tasks[2].depends_on == ["t1"]
    assert plan.tasks[2].tool_args == {"bins": [15, 30], "values": [1, 2, 3]}


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
