"""Tests for the Runtime-owned restricted code execution boundary.

The predecessor namespace tests exercised the removed LangGraph host RPC
bridge. Runtime code tasks now run
in a restricted child process and return only the explicit ``__result__``
value; these tests keep the security and result-contract coverage at that
boundary.
"""
from __future__ import annotations

import pytest

from app.runtime.code_mode.ast_guard import ASTBannedNodeError, inspect
from app.runtime.code_mode.executor import execute_source


def test_runtime_code_returns_explicit_result():
    result = execute_source("value = 2\n__result__ = {'value': value + 1}")

    assert result["status"] == "success"
    assert result["source"] == "sandbox"
    assert result["data"] == {"value": 3}


def test_runtime_code_without_result_returns_process_streams():
    result = execute_source("print('runtime-ok')")

    assert result["status"] == "success"
    assert "runtime-ok" in result["data"]["stdout"]


@pytest.mark.parametrize("source", [
    "import os",
    "from pathlib import Path",
    "x = __import__('os')",
    "eval('1 + 1')",
])
def test_runtime_ast_guard_rejects_unsafe_code(source):
    with pytest.raises(ASTBannedNodeError):
        inspect(source)


def test_runtime_ast_guard_always_selects_sandbox():
    inspection = inspect("value = 1\n__result__ = value")

    assert inspection.required_executor == "sandbox"


def test_runtime_code_rejects_unsafe_process_source():
    result = execute_source("import os")

    assert result["status"] == "error"
    assert result["error_code"] == "SANDBOX_FORBIDDEN_IMPORT"


def test_runtime_code_does_not_expose_host_runtime_tools():
    result = execute_source(
        "__result__ = {'has_tool_fns': 'tool_fns' in globals(), "
        "'has_rpc': 'rpc' in globals()}"
    )

    assert result["status"] == "success"
    assert result["data"] == {"has_tool_fns": False, "has_rpc": False}


def test_runtime_code_receives_only_declared_artifact_bytes_and_metadata():
    result = execute_source(
        "__result__ = {'text': inputs['source'].decode('utf-8'), "
        "'artifact_id': artifact_metadata['source']['artifact_id'], "
        "'has_path': 'path' in artifact_metadata['source']}",
        inputs={
            "source": {
                "data": b"runtime artifact", "artifact_id": "art_input",
                "content_hash": "a" * 64, "media_type": "text/plain", "type": "File",
            }
        },
    )

    assert result["status"] == "success"
    assert result["data"] == {"text": "runtime artifact", "artifact_id": "art_input", "has_path": False}
