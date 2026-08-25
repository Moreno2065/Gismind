"""Legacy observer/judge/tool-handler unit-style tests (misfiled under integration/).

Historical React Loop wiring cases were removed with ``build_app`` / ``planner_node``.
Remaining tests exercise ``observer.observe`` / ``judge.judge`` / tool handlers with
``unittest.mock`` LLM/API doubles — they are **not** production graph wiring tests.

Real wiring lives in:
- ``test_sub_agent_loop.py``
- ``test_checkpoint_replay.py``
- ``test_awaiting_input_e2e.py``
- ``test_resume_api.py``
"""

import json
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from app.models.schemas import (
    ToolResult,
    JudgeDecision,
)
from app.agents import tool_execution as loop
from app.agents import observer as observer_mod
from app.agents import judge as judge_mod


# ============================================================
# 辅助：构造 PlannerOutput / ToolResult / Mock LLM 响应
# ============================================================

def _success_tool_result(tool_name="query_poi", data=None, source="Amap"):
    """构造成功 ToolResult。"""
    return ToolResult(
        tool_call_id="call-1",
        tool_name=tool_name,
        status="success",
        data=data or {"count": 12, "pois": [{"name": "蜜雪冰城(新街口店)"}]},
        source=source,
        truncated=False,
    )


def _error_tool_result(tool_name="query_poi", error_code="AMAP_RATE_LIMITED"):
    """构造失败 ToolResult。"""
    return ToolResult(
        tool_call_id="call-1",
        tool_name=tool_name,
        status="error",
        message="高德限流",
        error_code=error_code,
    )


# ============================================================
# 1-5: 旧 React Loop 集成测试已移除（对应 build_app / planner_node 已删除）
# ============================================================







# ============================================================
# 6. 截断：已移除（_truncate_result_str 已删除，code-mode 走 executor 截断）
# ============================================================


# ============================================================
# 7. observer.observe 单元测试
# ============================================================

class TestObserverObserve:
    """observer.observe：把 ToolResult 摘要成 ≤200 字自然语言。"""

    @patch("app.agents.observer.create_llm")
    def test_observe_returns_string(self, mock_create_llm):
        mock_llm = MagicMock()
        resp = MagicMock()
        resp.content = "找到 12 个蜜雪冰城，来源高德。"
        mock_llm.invoke.return_value = resp
        mock_create_llm.return_value = mock_llm

        summary = observer_mod.observe(_success_tool_result())
        assert isinstance(summary, str)
        assert len(summary) <= 200

    @patch("app.agents.observer.create_llm")
    def test_observe_truncated_result_mentioned(self, mock_create_llm):
        mock_llm = MagicMock()
        resp = MagicMock()
        resp.content = "找到 1500+ 个 POI（已截断），来源高德。"
        mock_llm.invoke.return_value = resp
        mock_create_llm.return_value = mock_llm

        tr = ToolResult(
            tool_call_id="c1", tool_name="query_poi",
            status="success", data={"count": 1500},
            source="Amap", truncated=True,
        )
        summary = observer_mod.observe(tr)
        assert "截断" in summary

    @patch("app.agents.observer.create_llm")
    def test_observe_empty_result(self, mock_create_llm):
        mock_llm = MagicMock()
        resp = MagicMock()
        resp.content = "未找到相关 POI。"
        mock_llm.invoke.return_value = resp
        mock_create_llm.return_value = mock_llm

        tr = ToolResult(
            tool_call_id="c1", tool_name="query_poi",
            status="empty", message="未找到相关 POI",
        )
        summary = observer_mod.observe(tr)
        assert "未找到" in summary or "无" in summary

    def test_observer_system_prompt_content(self):
        """OBSERVER_SYSTEM_PROMPT 含关键约束。"""
        from app.agents.observer import OBSERVER_SYSTEM_PROMPT
        assert "Observer" in OBSERVER_SYSTEM_PROMPT
        assert "200" in OBSERVER_SYSTEM_PROMPT  # ≤200 字
        assert "摘要" in OBSERVER_SYSTEM_PROMPT
        assert "Amap" in OBSERVER_SYSTEM_PROMPT or "高德" in OBSERVER_SYSTEM_PROMPT


# ============================================================
# 8. judge.judge 单元测试
# ============================================================

class TestJudgeJudge:
    """judge.judge：判断 CONTINUE/RETRY/FINISH。"""

    @patch("app.agents.judge.create_llm")
    def test_judge_finish(self, mock_create_llm):
        mock_llm = MagicMock()
        resp = MagicMock()
        resp.content = json.dumps(
            {"decision": "FINISH", "reason": "任务完成"}, ensure_ascii=False
        )
        mock_llm.invoke.return_value = resp
        mock_create_llm.return_value = mock_llm

        state = {"iteration": 1, "messages": [], "tool_results": []}
        result = judge_mod.judge(state)
        assert result["should_stop"] is True
        assert result["decision"] == "FINISH"

    @patch("app.agents.judge.create_llm")
    def test_judge_continue(self, mock_create_llm):
        mock_llm = MagicMock()
        resp = MagicMock()
        resp.content = json.dumps(
            {"decision": "CONTINUE", "reason": "未完成"}, ensure_ascii=False
        )
        mock_llm.invoke.return_value = resp
        mock_create_llm.return_value = mock_llm

        state = {"iteration": 1, "messages": [], "tool_results": []}
        result = judge_mod.judge(state)
        assert result["should_stop"] is False
        assert result["decision"] == "CONTINUE"

    @patch("app.agents.judge.create_llm")
    def test_judge_retry(self, mock_create_llm):
        mock_llm = MagicMock()
        resp = MagicMock()
        resp.content = json.dumps(
            {"decision": "RETRY", "reason": "高德限流"}, ensure_ascii=False
        )
        mock_llm.invoke.return_value = resp
        mock_create_llm.return_value = mock_llm

        state = {"iteration": 1, "messages": [], "tool_results": []}
        result = judge_mod.judge(state)
        assert result["should_stop"] is False
        assert result["decision"] == "RETRY"
        # RETRY 应附加失败上下文消息
        assert "messages" in result

    @patch("app.agents.judge.create_llm")
    def test_judge_force_finish_at_max_iterations(self, mock_create_llm):
        """iteration >= APP_MAX_ITERATIONS 时强制 FINISH，不调 LLM。"""
        from app.config import settings
        mock_llm = MagicMock()
        mock_create_llm.return_value = mock_llm

        state = {
            "iteration": settings.APP_MAX_ITERATIONS,
            "messages": [],
            "tool_results": [],
        }
        result = judge_mod.judge(state)
        assert result["should_stop"] is True
        assert result["decision"] == "FINISH"
        # 应强制 FINISH，不调用 LLM
        mock_llm.invoke.assert_not_called()

    @patch("app.agents.judge.create_llm")
    def test_judge_retry_message_contains_error_info(self, mock_create_llm):
        """RETRY 时附加的 messages 含失败上下文。"""
        mock_llm = MagicMock()
        resp = MagicMock()
        resp.content = json.dumps(
            {"decision": "RETRY", "reason": "高德限流"}, ensure_ascii=False
        )
        mock_llm.invoke.return_value = resp
        mock_create_llm.return_value = mock_llm

        # 构造一个含 error ToolResult 的 state
        error_tr = _error_tool_result(error_code="AMAP_RATE_LIMITED")
        state = {
            "iteration": 1,
            "messages": [],
            "tool_results": [error_tr],
        }
        result = judge_mod.judge(state)
        # 附加的 message 应含 error_code 或 失败 字样
        added_msgs = result.get("messages", [])
        added_text = "".join(
            getattr(m, "content", str(m)) for m in added_msgs
        )
        assert "AMAP_RATE_LIMITED" in added_text or "失败" in added_text

    def test_judge_system_prompt_content(self):
        """JUDGE_SYSTEM_PROMPT 含关键决策规则。"""
        from app.agents.judge import JUDGE_SYSTEM_PROMPT
        assert "Judge" in JUDGE_SYSTEM_PROMPT or "裁判" in JUDGE_SYSTEM_PROMPT
        assert "CONTINUE" in JUDGE_SYSTEM_PROMPT
        assert "RETRY" in JUDGE_SYSTEM_PROMPT
        assert "FINISH" in JUDGE_SYSTEM_PROMPT
        assert "10" in JUDGE_SYSTEM_PROMPT  # 最大迭代


# ============================================================
# 10. tool_executor 已移除（code-mode 走 code_executor_node）
# ============================================================


# ============================================================
# 11. Sprint 1 增量：LOCATION_DRIFT 校验 + candidates 透传
#     已改写为直接调 _TOOL_REGISTRY handler（code-mode 路径）
# ============================================================


def _geo_code_with_location(lng: float, lat: float) -> ToolResult:
    """构造一个成功的 geo_code ToolResult（用于在前置 iteration 喂入 results_data）。"""
    return ToolResult(
        tool_call_id="geo_seed",
        tool_name="geo_code",
        status="success",
        data={
            "status": "success",
            "location": [lng, lat],
            "formatted_address": "seed",
            "source": "Amap",
            "candidates": [],
            "confidence": 1.0,
            "disambiguated": False,
            "principal_rank": 0,
            "cached": False,
        },
        source="Amap",
    )


class TestLocationDriftIntegration:
    """Sprint 1 增量：LOCATION_DRIFT 校验（直接调 _TOOL_REGISTRY handler）。"""

    def test_query_poi_free_tuple_rejected_when_anchor_exists(self, fake_redis):
        """geo_code 给 (118.78, 32.04)，query_poi 偏离 ~12km → LOCATION_DRIFT。"""
        from app.agents.tool_execution import _TOOL_REGISTRY
        from app.agents.context import _ToolContext

        geo_payload = _geo_code_with_location(118.78, 32.04).data

        with patch("app.agents.tool_execution.POIQuery") as mock_poi_class, \
             patch("app.agents.tool_execution.GeoCoder") as mock_geo_class:
            mock_geo_instance = MagicMock()
            mock_geo_instance.geocode = AsyncMock(return_value=geo_payload)
            mock_geo_class.return_value = mock_geo_instance

            mock_poi_instance = mock_poi_class.return_value
            mock_poi_instance.search_poi_tool.return_value = {
                "status": "success",
                "data": {"count": 0, "pois": []},
                "source": "Amap",
            }

            instances = {
                "poi": mock_poi_instance,
                "geo_coder": mock_geo_instance,
            }

            # 先调 geo_code handler
            geo_ctx = _ToolContext(
                tool_call_id="call_1_0", tool_name="geo_code",
                iteration=1, params={"location": "南京新街口"},
                results_data={}, instances=instances,
            )
            geo_handler = _TOOL_REGISTRY["geo_code"]
            geo_tr = geo_handler(geo_ctx)

            # 把 geo_code 结果放入 results_data
            results_data = {0: geo_tr.data}

            # 再调 query_poi handler
            poi_ctx = _ToolContext(
                tool_call_id="call_1_1", tool_name="query_poi",
                iteration=1,
                params={"query": "蜜雪冰城", "location": [118.90, 32.10], "radius": 500},
                results_data=results_data, instances=instances,
            )
            poi_handler = _TOOL_REGISTRY["query_poi"]
            poi_tr = poi_handler(poi_ctx)

        assert geo_tr.status == "success", f"geo_code 自身应成功: {geo_tr}"
        assert poi_tr.status == "error", f"query_poi 期望 error，实际: {poi_tr}"
        assert poi_tr.error_code == "LOCATION_DRIFT", f"期望 LOCATION_DRIFT，实际: {poi_tr.error_code}"

    def test_query_poi_drift_within_tolerance_passes(self, fake_redis):
        """geo_code 给 (118.78, 32.04)，query_poi 偏移 ~14m → 不应 LOCATION_DRIFT。"""
        from app.agents.tool_execution import _TOOL_REGISTRY
        from app.agents.context import _ToolContext

        geo_payload = _geo_code_with_location(118.78, 32.04).data

        with patch("app.agents.tool_execution.POIQuery") as mock_poi_class, \
             patch("app.agents.tool_execution.GeoCoder") as mock_geo_class:
            mock_geo_instance = MagicMock()
            mock_geo_instance.geocode = AsyncMock(return_value=geo_payload)
            mock_geo_class.return_value = mock_geo_instance

            mock_poi_instance = mock_poi_class.return_value
            mock_poi_instance.search_poi_tool.return_value = {
                "status": "success",
                "data": {"count": 5, "pois": []},
                "source": "Amap",
            }

            instances = {
                "poi": mock_poi_instance,
                "geo_coder": mock_geo_instance,
            }

            geo_ctx = _ToolContext(
                tool_call_id="call_1_0", tool_name="geo_code",
                iteration=1, params={"location": "南京新街口"},
                results_data={}, instances=instances,
            )
            geo_handler = _TOOL_REGISTRY["geo_code"]
            geo_tr = geo_handler(geo_ctx)
            results_data = {0: geo_tr.data}

            poi_ctx = _ToolContext(
                tool_call_id="call_1_1", tool_name="query_poi",
                iteration=1,
                params={"query": "蜜雪冰城", "location": [118.7801, 32.0401], "radius": 500},
                results_data=results_data, instances=instances,
            )
            poi_handler = _TOOL_REGISTRY["query_poi"]
            poi_tr = poi_handler(poi_ctx)

        assert poi_tr.status == "success", f"query_poi 应成功，实际: {poi_tr}"
        assert poi_tr.error_code != "LOCATION_DRIFT"


class TestGeoCodeCandidatesObservable:
    """Sprint 1 增量：geo_code 返回的 candidates 应透传到 ToolResult.data。"""

    def test_geo_code_candidates_observable_in_tool_result(self, fake_redis):
        """geo_code 返回的 candidates 必须出现在 ToolResult.data['candidates']。"""
        from app.agents.tool_execution import _TOOL_REGISTRY
        from app.agents.context import _ToolContext

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "1",
            "geocodes": [
                {"location": "118.7845,32.0429", "formatted_address": "新街口 A", "location_type": "POI"},
                {"location": "118.7910,32.0450", "formatted_address": "新街口 B", "location_type": "地铁站"},
                {"location": "118.7950,32.0800", "formatted_address": "新街口 C", "location_type": "地名"},
            ],
        }
        mock_resp.raise_for_status.return_value = None

        async_client_instance = AsyncMock()
        async_client_instance.get = AsyncMock(return_value=mock_resp)
        async_client_instance.__aenter__ = AsyncMock(return_value=async_client_instance)
        async_client_instance.__aexit__ = AsyncMock(return_value=None)

        async_client_cls = MagicMock()
        async_client_cls.return_value = async_client_instance

        with patch("app.tools.geo_code.httpx.AsyncClient", async_client_cls):
            ctx = _ToolContext(
                tool_call_id="call_1_0", tool_name="geo_code",
                iteration=1, params={"location": "新街口"},
                results_data={},
                instances={"geo_coder": loop.GeoCoder()},
            )
            handler = _TOOL_REGISTRY["geo_code"]
            tr = handler(ctx)

        assert tr.status == "success", f"geo_code 应成功: {tr}"
        assert isinstance(tr.data, dict)
        assert "candidates" in tr.data, f"candidates missing from ToolResult.data: {tr.data}"
        cands = tr.data["candidates"]
        assert isinstance(cands, list)
        assert len(cands) == 3, f"期望 3 个 candidates，实际: {cands}"
        assert {c["rank"] for c in cands} == {0, 1, 2}
