"""Tests for HybridExecutor routing — fence stripping + AST routing + ThreadPoolExecutor inline.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from app.agents.code_mode.executor import (
    _strip_fence_and_think,
    execution_to_tool_result,
    HybridExecutor,
)
from app.agents.code_mode.types import ExecutionResult
from app.models.schemas import PlannerOutput


# ============================================================
# fence 预处理
# ============================================================

def test_strip_python_fence():
    code = "```python\nx = 1\n```"
    assert _strip_fence_and_think(code) == "x = 1"


def test_strip_py_fence():
    code = "```py\nx = 2\n```"
    assert _strip_fence_and_think(code) == "x = 2"


def test_strip_no_language_label():
    code = "```\nx = 3\n```"
    assert _strip_fence_and_think(code) == "x = 3"


def test_strip_think_tag():
    code = "<thinking>some reasoning</thinking>\nx = 4\n"
    assert _strip_fence_and_think(code) == "x = 4"


def test_strip_anthropic_think_tag():
    code = "<think>I'll use geo_code</think>\nx = 5"
    assert _strip_fence_and_think(code) == "x = 5"


def test_multi_fence_merge():
    """多个 fence 按出现顺序拼接（ast.parse 校验合法时才拼）。"""
    code = '```python\nx = 1\n```\nSome text\n```python\ny = 2\n```\n'
    # 拼接后是 "x = 1\n\ny = 2" — 合法的 Python（多行表达式）
    result = _strip_fence_and_think(code)
    assert "x = 1" in result
    assert "y = 2" in result


def test_multi_fence_fallback_if_parse_fails():
    """多个 fence 拼接后语法不合法 → fallback 到只取最后一个。"""
    code = (
        '```python\nthis is not valid python\n```\n'
        '```python\nz = 3\n```\n'
    )
    result = _strip_fence_and_think(code)
    assert "this is not" not in result  # 第一个被丢弃
    assert "z = 3" in result  # 最后一个保留


def test_no_fence_just_plain_code():
    code = "x = 1\ny = 2"
    assert _strip_fence_and_think(code) == code


def test_empty_return_empty():
    assert _strip_fence_and_think("") == ""
    assert _strip_fence_and_think("  \n  ") == ""


# ============================================================
# ToolResult 映射（execution_to_tool_result）
# ============================================================

def test_execution_to_tool_result_happy():
    """正常执行 → mode="code", tool_name="__code_block__", status="success"。"""
    er = ExecutionResult(success=True, result={"pois": [1]}, code="x = 1")
    tr = execution_to_tool_result(er, iteration=3)
    assert tr.mode == "code"
    assert tr.tool_name == "__code_block__"
    assert tr.status == "success"
    assert tr.data["result"] == {"pois": [1]}
    assert tr.data["executor_type"] == "sandbox"


def test_execution_to_tool_result_failed():
    """失败态 → status="error", data 含 traceback（截断）。"""
    er = ExecutionResult(
        success=False,
        traceback="Line 1 error\ndetail line 2",
        error_code="EXECUTION_ERROR",
    )
    tr = execution_to_tool_result(er, iteration=1)
    assert tr.status == "error"
    assert tr.error_code == "EXECUTION_ERROR"
    assert "Line 1 error" in tr.data["traceback"]


def test_execution_to_tool_result_traceback_truncated():
    """traceback 超过 3000 字符 → 截断 + "truncated" 标记。"""
    long_tb = "x\n" * 2000  # ~4000 chars
    er = ExecutionResult(success=False, traceback=long_tb, error_code="SANDBOX_TIMEOUT")
    tr = execution_to_tool_result(er, iteration=1)
    assert len(tr.data["traceback"]) < 3500
    assert "truncated" in tr.data["traceback"].lower()


def test_execution_to_tool_result_carries_executor_type():
    er = ExecutionResult(success=True, executor_type="sandbox", code="")
    tr = execution_to_tool_result(er, iteration=1)
    assert tr.data["executor_type"] == "sandbox"
    assert tr.source == "sandbox"


def test_code_executor_emits_task_scoped_inspectable_events():
    from app.agents.events.current import reset_current_handler, set_current_handler
    from app.agents.tool_execution import code_executor_node

    class FakeExecutor:
        def execute(self, **_kwargs):
            return ExecutionResult(
                success=True,
                stdout="hello",
                stderr="warning",
                result={"count": 2},
                duration_ms=12,
                executor_type="sandbox",
            )

    events: list[dict] = []
    token = set_current_handler(events.append)
    try:
        with patch("app.agents.tool_execution._get_shared_executor", return_value=FakeExecutor()):
            code_executor_node({
                "planner_output": PlannerOutput(thinking="execute", code="__result__ = {'count': 2}"),
                "iteration": 1,
                "agent_role": "coder",
                "parent_task_id": "t-code",
                "session_vars": {},
            })
    finally:
        reset_current_handler(token)

    assert [event["event"] for event in events] == [
        "code.execution.start",
        "code.execution.stdout",
        "code.execution.stderr",
        "code.execution.complete",
    ]
    assert all(event["task_id"] == "t-code" for event in events)
    assert events[-1]["duration_ms"] == 12
    assert events[-1]["result"] == {"count": 2}


# ============================================================
# HybridExecutor — AST 路由分流 (inline 已废弃，全走 sandbox)
# ============================================================


def test_hybrid_executor_routes_while_to_sandbox():
    """含有 while 的代码 → AST 说 sandbox。"""
    executor = HybridExecutor()
    result = executor.execute(
        code="x = 0\nwhile x < 10:\n    x += 1\n__result__ = {'x': x}",
        session_vars={},
        known_tools={},
    )
    assert result.required_executor == "sandbox"


def test_hybrid_executor_routes_sandbox_tool_call():
    """调了 sandbox 工具 → AST 说 sandbox。"""
    executor = HybridExecutor()
    result = executor.execute(
        code="data = parse_zip(b'test')",
        session_vars={},
        known_tools={"parse_zip": "sandbox"},
    )
    assert result.required_executor == "sandbox"


def test_hybrid_executor_ast_banned_returns_execution_error():
    """AST outright banned → ExecutionResult error_code=AST_BANNED_NODE（不抛异常）。"""
    executor = HybridExecutor()
    result = executor.execute(
        code="import os",
        session_vars={},
        known_tools={},
    )
    assert result.success is False
    assert result.error_code == "AST_BANNED_NODE"


def test_hybrid_executor_session_vars_injection():
    """exec 后 session_vars 可以通过 __result__ dict 更新（真实 sandbox，不 patch）。"""
    executor = HybridExecutor()
    result = executor.execute(
        code="pois = [1, 2, 3]; __result__ = {'pois': pois}",
        session_vars={},
        known_tools={},
    )
    assert result.success is True, (result.error_code, result.stderr, result.traceback)
    assert result.result == {"pois": [1, 2, 3]}


def test_hybrid_executor_session_vars_naming_conflict():
    """🔴 关键：__result__['buffer'] 与工具名冲突 → 跳过 + warning。"""
    executor = HybridExecutor()
    result = executor.execute(
        code='__result__ = {"buffer": "this is a string"}',
        session_vars={},
        known_tools={"buffer": "inline"},
    )
    assert result.success is True
    # session_vars update 在 executor 外部做 — 这里验证 executor 内部不报错
    # 实际冲突检测在 namespace.py 中，executor 负责传递 __result__


def test_hybrid_executor_fence_stripping():
    """executor 在入口剥 fence（真实 sandbox）。"""
    executor = HybridExecutor()
    result = executor.execute(
        code="```python\n__result__ = {'msg': 'ok'}\n```",
        session_vars={},
        known_tools={},
    )
    assert result.success is True, (result.error_code, result.stderr, result.traceback)
    assert result.result == {"msg": "ok"}


def test_hybrid_executor_think_tag_stripping():
    """executor 在入口剥 think tag（真实 sandbox）。"""
    executor = HybridExecutor()
    result = executor.execute(
        code="<thinking>some reasoning</thinking>\n__result__ = {'msg': 'ok'}",
        session_vars={},
        known_tools={},
    )
    assert result.success is True, (result.error_code, result.stderr, result.traceback)
    assert result.result == {"msg": "ok"}


def test_hybrid_executor_no_result_is_ok():
    """没有 __result__ 的代码也能正常跑（真实 sandbox）。"""
    executor = HybridExecutor()
    result = executor.execute(
        code="x = 1 + 2",
        session_vars={},
        known_tools={},
    )
    assert result.success is True, (result.error_code, result.stderr, result.traceback)
    assert result.result == {} or result.result is None  # 没有 result 也不崩溃


def test_sandbox_executor_real_e2e_no_patch():
    """GAP3 wiring: SandboxExecutor → run_in_sandbox without mocking.

    Proves session_vars injection + __result__ sentinel path end-to-end.
    """
    from app.agents.code_mode.sandbox_runner import SandboxExecutor

    executor = SandboxExecutor(timeout_s=15, memory_mb=128)
    result = executor.execute(
        code="__result__ = {'sum': a + b, 'tag': tag}",
        session_vars={"a": 10, "b": 32, "tag": "e2e"},
    )
    assert result.success is True, (result.error_code, result.stderr, result.traceback)
    assert result.executor_type == "sandbox"
    assert result.result == {"sum": 42, "tag": "e2e"}


def test_hybrid_executor_real_e2e_no_patch():
    """GAP3 wiring: HybridExecutor → SandboxExecutor → real subprocess.

    Does not patch run_in_sandbox; validates full code-mode path.
    """
    executor = HybridExecutor()
    result = executor.execute(
        code=(
            "```python\n"
            "total = base + 2\n"
            "__result__ = {'total': total}\n"
            "```"
        ),
        session_vars={"base": 40},
        known_tools={},
    )
    assert result.success is True, (result.error_code, result.stderr, result.traceback)
    assert result.required_executor == "sandbox"
    assert result.executor_type == "sandbox"
    assert result.result == {"total": 42}
