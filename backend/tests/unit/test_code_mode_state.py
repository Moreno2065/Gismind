"""Tests for session_vars serialization + __result__ dict handling + error codes.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.agents.code_mode.types import ExecutionResult


# ============================================================
# ExecutionResult 序列化
# ============================================================

def test_execution_result_to_dict():
    """to_dict() 返回 JSON-可序列化 dict。"""
    er = ExecutionResult(success=True, result={"pois": [1, 2]})
    d = er.to_dict()
    assert isinstance(d, dict)
    assert d["success"] is True
    assert d["result"] == {"pois": [1, 2]}


def test_execution_result_round_trip():
    """ExecutionResult → to_dict() → dict 含所有字段。"""
    er = ExecutionResult(
        success=True,
        stdout="hello",
        result={"id": 42},
        duration_ms=100,
        error_code=None,
        required_executor="inline",
        executor_type="inline",
        code="x = 1",
    )
    d = er.to_dict()
    assert d["success"] is True
    assert d["stdout"] == "hello"
    assert d["result"] == {"id": 42}
    assert d["duration_ms"] == 100
    assert d["executor_type"] == "inline"


def test_execution_result_unserializable_result():
    """unseralizable result 走 repr() 兜底（不崩溃）。"""
    class Unserializable:
        pass

    er = ExecutionResult(success=True, result=Unserializable(), duration_ms=0)
    d = er.to_dict()
    # result 字段以 repr 形式存在
    assert "result" in d
    assert isinstance(d["result"], str)


def test_execution_result_with_error():
    """错误态序列化不丢 traceback。"""
    er = ExecutionResult(
        success=False,
        error_code="SANDBOX_TIMEOUT",
        traceback="Timeout: code took too long",
    )
    d = er.to_dict()
    assert d["success"] is False
    assert d["error_code"] == "SANDBOX_TIMEOUT"
    assert "Timeout" in d["traceback"]


def test_execution_result_defaults():
    """默认值可序列化。"""
    er = ExecutionResult(code="")
    d = er.to_dict()
    assert d["success"] is False
    assert d["error_code"] is None
    assert d["result"] is None


# ============================================================
# Error codes
# ============================================================

@pytest.mark.parametrize(
    "error_code",
    [
        "AST_BANNED_NODE",
        "INLINE_TIMEOUT",
        "SANDBOX_TIMEOUT",
        "SANDBOX_OOM",
        "EXECUTION_ERROR",
        "SOFT_TIMEOUT",
    ],
)
def test_error_codes_stored_correctly(error_code):
    er = ExecutionResult(success=False, error_code=error_code, code="x")
    assert er.error_code == error_code


def test_success_without_error_code():
    er = ExecutionResult(success=True, code="x")
    assert er.error_code is None