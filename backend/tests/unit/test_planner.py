"""Planner 单元测试 — build_code_mode_prompt / create_llm / create_code_mode_llm。

plan() 已随旧 React Loop 移除；Planner 现在是 prompt 工厂，不再直接调用 LLM。
"""

from unittest.mock import patch, MagicMock

import pytest

from app.agents.planner_factory import (
    build_code_mode_prompt,
    create_llm,
    create_code_mode_llm,
)


# ============================================================
# 1. build_code_mode_prompt 结构测试
# ============================================================

class TestBuildCodeModePrompt:
    """验证 code-mode system prompt 生成。"""

    def test_geo_role_contains_geo_tools(self):
        prompt = build_code_mode_prompt("geo")
        assert "# 可用函数" in prompt
        assert "geo_code" in prompt
        assert "geo_transform" in prompt

    def test_poi_role_contains_poi_tools(self):
        prompt = build_code_mode_prompt("poi")
        assert "geo_code" in prompt
        assert "query_poi" in prompt

    def test_geometer_role_contains_analysis_tools(self):
        prompt = build_code_mode_prompt("geometer")
        assert "buffer" in prompt
        assert "overlay" in prompt
        assert "voronoi" in prompt
        assert "isochrone" in prompt

    def test_viz_role_contains_map_tools(self):
        prompt = build_code_mode_prompt("viz")
        assert "map_layer_build" in prompt

    def test_coder_role_contains_code_tools(self):
        prompt = build_code_mode_prompt("coder")
        assert "code_executor" in prompt

    def test_prompt_contains_keyword_rules(self):
        prompt = build_code_mode_prompt("geo")
        assert "关键字参数" in prompt
        assert "__result__" in prompt
        assert "await" in prompt

    def test_prompt_describes_unified_code_mode_runtime(self):
        prompt = build_code_mode_prompt("poi")
        assert "code mode" in prompt
        assert "无需 import" in prompt

    def test_unknown_role_raises(self):
        with pytest.raises(KeyError):
            build_code_mode_prompt("unknown_role")


# ============================================================
# 2. create_llm 配置测试
# ============================================================

class TestCreateLLM:
    """验证 create_llm 使用 settings 配置。"""

    @patch("app.agents.planner_factory.ChatOpenAI")
    def test_create_llm_uses_settings(self, mock_chat_openai):
        from app.config import settings
        create_llm()
        mock_chat_openai.assert_called_once()
        kwargs = mock_chat_openai.call_args.kwargs
        assert kwargs["model"] == settings.LLM_MODEL
        assert kwargs["api_key"] == settings.LLM_API_KEY
        assert kwargs["base_url"] == settings.LLM_BASE_URL
        assert kwargs["temperature"] == settings.LLM_TEMPERATURE

    @patch("app.agents.planner_factory.ChatOpenAI")
    def test_create_llm_enforces_json_mode(self, mock_chat_openai):
        create_llm()
        kwargs = mock_chat_openai.call_args.kwargs
        assert kwargs["model_kwargs"]["response_format"] == {"type": "json_object"}


# ============================================================
# 3. create_code_mode_llm 配置测试
# ============================================================

class TestCreateCodeModeLLM:
    """create_code_mode_llm 无 response_format 约束。"""

    @patch("app.agents.planner_factory.ChatOpenAI")
    def test_create_code_mode_llm_no_json_constraint(self, mock_chat_openai):
        create_code_mode_llm()
        kwargs = mock_chat_openai.call_args.kwargs
        assert "response_format" not in kwargs.get("model_kwargs", {})
