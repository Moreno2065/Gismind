"""Tests for the code_executor tool wrapper.

These are integration-level tests that exercise code_executor through the
_ToolContext / _TOOL_REGISTRY contract.  The simple test spawns a real
subprocess sandbox; the timeout test uses a tight wall-clock limit.
"""
from unittest.mock import MagicMock

from app.agents.context import _ToolContext
from app.agents.tool_execution import _TOOL_REGISTRY
from app.sandbox.tools import code_executor


def _make_ctx(
    tool_call_id: str = "call_3_0",
    tool_name: str = "code_executor",
    iteration: int = 3,
    params: dict = None,
) -> _ToolContext:
    """Helper: build a _ToolContext with minimal boilerplate."""
    instances = {
        "poi": MagicMock(),
        "geo_coder": MagicMock(),
        "analyzer": MagicMock(),
        "data_io": MagicMock(),
        "layer_builder": MagicMock(),
    }
    return _ToolContext(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        iteration=iteration,
        params=params or {},
        results_data={},
        instances=instances,
    )


# ---- registration -------------------------------------------------------

def test_code_executor_registered():
    assert "code_executor" in _TOOL_REGISTRY, "code_executor not in _TOOL_REGISTRY"
    assert callable(_TOOL_REGISTRY["code_executor"]), "code_executor handler not callable"


# ---- happy path ---------------------------------------------------------

def test_code_executor_simple():
    """print(1+1) should return stdout='2'."""
    ctx = _make_ctx(params={"code": "print(1+1)"})
    result = code_executor(ctx)
    assert result.status == "success", f"expected success, got {result.status}: {result.message}"
    assert result.data.get("stdout") == "2", f"stdout={result.data.get('stdout')!r}"
    assert result.source == "sandbox"
    # returncode may be 0 or 1 depending on subprocess; duration should be >0
    assert result.data["duration_ms"] > 0


def test_code_executor_returns_data():
    """Expressions producing output are captured in stdout."""
    ctx = _make_ctx(params={"code": "print(sum(range(10)))"})
    result = code_executor(ctx)
    assert result.status == "success"
    assert result.data.get("stdout") == "45"


# ---- timeout ------------------------------------------------------------

def test_code_executor_timeout():
    """Code that sleeps past timeout_s should produce SANDBOX_TIMEOUT."""
    ctx = _make_ctx(
        tool_call_id="call_3_1",
        params={"code": "import time; time.sleep(999)", "timeout_s": 1},
    )
    result = code_executor(ctx)
    assert result.status == "error", f"expected error, got {result.status}"
    assert result.error_code == "SANDBOX_TIMEOUT", f"error_code={result.error_code}"
    # data should still contain partial output
    assert result.data is not None


# ---- missing-code guard -------------------------------------------------

def test_code_executor_missing_code():
    """Empty params should produce status='empty'."""
    ctx = _make_ctx(tool_call_id="call_3_2", params={})
    result = code_executor(ctx)
    assert result.status == "empty"
    assert "code" in (result.message or "")


# ---- error_code mapping -------------------------------------------------

def test_code_executor_forbidden_import():
    """Importing a blocked module should produce SANDBOX_FORBIDDEN_IMPORT."""
    ctx = _make_ctx(params={"code": "import os"})
    result = code_executor(ctx)
    assert result.status == "error", f"expected error, got {result.status}"
    assert result.error_code == "SANDBOX_FORBIDDEN_IMPORT", (
        f"error_code={result.error_code}"
    )


def test_golden_missing_code_returns_empty():
    """Golden fixture: empty code (no params) should return status='empty'."""
    ctx = _ToolContext(
        tool_call_id="call_golden_0",
        tool_name="code_executor",
        iteration=1,
        params={},
        results_data={},
        instances={},
    )
    result = code_executor(ctx)
    assert result.status == "empty"
