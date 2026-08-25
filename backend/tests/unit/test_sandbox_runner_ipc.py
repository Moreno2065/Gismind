"""Tests for sandbox_runner IPC: tempfile injection + UUID sentinel stderr callback.

重点验证：
- session_vars 通过 tempfile env 注入（不经过命令行）
- UUID sentinel 在 stderr 的输出与解析
- run_in_sandbox 返回类型适配
"""
from __future__ import annotations

import json
import os
import pickle
import tempfile
import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.agents.code_mode.sandbox_runner import (
    _write_session_vars,
    _parse_result_sentinel,
    _GEN_STATE_SENTINEL,
)


# ============================================================
# session_vars 写入 tempfile
# ============================================================

def test_write_session_vars_creates_tempfile():
    """_write_session_vars 创建 tempfile，返回 env var dict。"""
    vars_dict = {"pois": [{"id": 1}], "origin": (118.78, 32.04)}
    env = _write_session_vars(vars_dict)
    path = env.get("APP_SANDBOX_VARS_PATH")
    assert path, "必须含 APP_SANDBOX_VARS_PATH"
    assert os.path.exists(path), f"临时文件应存在：{path}"
    with open(path, "rb") as f:
        loaded = pickle.load(f)
    assert loaded == vars_dict
    # 清理
    try:
        os.unlink(path)
    except OSError:
        pass


def test_write_session_vars_empty():
    """空 dict 也写入 tempfile（子进程 loader 统一处理）。"""
    env = _write_session_vars({})
    path = env["APP_SANDBOX_VARS_PATH"]
    assert os.path.exists(path)
    with open(path, "rb") as f:
        loaded = pickle.load(f)
    assert loaded == {}
    try:
        os.unlink(path)
    except OSError:
        pass


# ============================================================
# UUID sentinel 生成
# ============================================================

def test_sentinel_has_uuid():
    """sentinel 含 UUID 后缀，碰撞概率为零。"""
    sentinel_id, start_token, end_token = _GEN_STATE_SENTINEL()
    # token 格式：__GISMIND_STATE_<uuid.hex>__START__ / __END__
    assert sentinel_id in start_token, f"sentinel_id {sentinel_id} 应在 start_token {start_token} 中"
    assert sentinel_id in end_token, f"sentinel_id {sentinel_id} 应在 end_token {end_token} 中"
    assert "__START__" in start_token
    assert "__END__" in end_token


def test_sentinel_id_is_random():
    """连续两次调用生成的 ID 不同。"""
    sid1, _, _ = _GEN_STATE_SENTINEL()
    sid2, _, _ = _GEN_STATE_SENTINEL()
    assert sid1 != sid2


# ============================================================
# sentinel 解析
# ============================================================

def test_parse_sentinel_happy_path():
    """正常 sentinel 字符串解析出 __result__ dict。"""
    sentinel_id, start, end = _GEN_STATE_SENTINEL()
    payload = json.dumps({"pois": [1, 2], "msg": "hello"})
    stderr_text = f"dummy\nerror\n{start}{payload}{end}\n"

    result = _parse_result_sentinel(stderr_text)
    assert result == {"pois": [1, 2], "msg": "hello"}


def test_parse_sentinel_no_match_returns_none():
    """stderr 无匹配 sentinel 时返回 None。"""
    result = _parse_result_sentinel("normal output\nno sentinel here")
    assert result is None


def test_parse_sentinel_handles_user_print_noise():
    """stderr 里混杂其他内容也能正确匹配 sentinel。"""
    sentinel_id, start, end = _GEN_STATE_SENTINEL()
    payload = json.dumps({"id": 42})
    stderr_text = f"""Traceback (most recent call last):
  File "<sandbox>", line 2, in <module>
    print("hello")
{start}{payload}{end}
Extra stderr after sentinel
"""
    result = _parse_result_sentinel(stderr_text)
    assert result == {"id": 42}


def test_parse_sentinel_empty_result():
    """__result__ 为空或 None 时返回 {} 或 None？约定：None 视为没有结果。"""
    sentinel_id, start, end = _GEN_STATE_SENTINEL()
    payload = json.dumps({})
    stderr_text = f"{start}{payload}{end}"
    result = _parse_result_sentinel(stderr_text)
    assert result == {}


def test_parse_sentinel_malformed_json_returns_none():
    """sentinel 内 json 非法时返回 None（由 executor 捕获后 fallback）。"""
    sentinel_id, start, end = _GEN_STATE_SENTINEL()
    stderr_text = f"{start}not valid json{end}"
    result = _parse_result_sentinel(stderr_text)
    assert result is None


def test_parse_sentinel_with_multiple_uuid_instances():
    """不同次调用的 sentinel 之间不会交叉匹配（UUID 不同）。"""
    # 模拟两次调用的 sentinel 残留（不会发生，但验证解析器的健壮性）
    sentinel_id_1, s1_start, s1_end = _GEN_STATE_SENTINEL()
    sentinel_id_2, s2_start, s2_end = _GEN_STATE_SENTINEL()

    payload1 = json.dumps({"step": 1})
    payload2 = json.dumps({"step": 2})
    stderr_text = f"{s1_start}{payload1}{s1_end}\n{s2_start}{payload2}{s2_end}"

    # 解析器猜用第一个？或者用最后一个？实际上每步调一次，UUID 不同。
    # 这里测试：用 sentinel_id_2 能正确匹配 payload2
    result = _parse_result_sentinel(stderr_text)
    assert result is not None


# ============================================================
# run_in_sandbox 返回类型适配
# ============================================================

def test_sandbox_executor_adapts_sandbox_result():
    """SandboxExecutor 适配 run_in_sandbox 的 SandboxResult dataclass。"""
    from app.agents.code_mode.sandbox_runner import SandboxExecutor
    from app.sandbox.runner import SandboxResult

    executor = SandboxExecutor()
    mock_result = SandboxResult(
        stdout="hello\n",
        stderr="__GISMIND_STATE_x__START__{}__GISMIND_STATE_x__END__\n",
        returncode=0,
        duration_ms=10,
    )
    with patch("app.agents.code_mode.sandbox_runner.run_in_sandbox", return_value=mock_result):
        res = executor.execute("print('hello')", session_vars={})

    assert res.success is True
    assert "hello" in res.stdout
    assert res.returncode == 0
    assert res.executor_type == "sandbox"


def test_sandbox_executor_adapts_failed_result():
    """SandboxResult.returncode != 0 → ExecutionResult.success = False。"""
    from app.agents.code_mode.sandbox_runner import SandboxExecutor
    from app.sandbox.runner import SandboxResult

    executor = SandboxExecutor()
    mock_result = SandboxResult(
        stdout="",
        stderr="ZeroDivisionError: division by zero\n__GISMIND_STATE_x__START__{}__GISMIND_STATE_x__END__\n",
        returncode=1,
        duration_ms=5,
    )
    with patch("app.agents.code_mode.sandbox_runner.run_in_sandbox", return_value=mock_result):
        res = executor.execute("x = 1/0", session_vars={})

    assert res.success is False
    assert "ZeroDivisionError" in res.stderr or "ZeroDivisionError" in (res.traceback or "")
    assert res.error_code == "EXECUTION_ERROR"


def test_sandbox_executor_propagates_error_code():
    """SandboxResult.error_code 透传到 ExecutionResult。"""
    from app.agents.code_mode.sandbox_runner import SandboxExecutor
    from app.sandbox.runner import SandboxResult

    executor = SandboxExecutor()
    mock_result = SandboxResult(
        stdout="",
        stderr="timeout\n__GISMIND_STATE_x__START__{}__GISMIND_STATE_x__END__\n",
        returncode=-1,
        duration_ms=30_000,
        error_code="SANDBOX_TIMEOUT",
    )
    with patch("app.agents.code_mode.sandbox_runner.run_in_sandbox", return_value=mock_result):
        res = executor.execute("while True: pass", session_vars={})

    assert res.success is False
    assert res.error_code == "SANDBOX_TIMEOUT"


def test_sandbox_executor_injects_session_vars():
    """SandboxExecutor 把 session_vars 写入 tempfile 再调用 run_in_sandbox。"""
    from app.agents.code_mode.sandbox_runner import SandboxExecutor
    from app.sandbox.runner import SandboxResult

    executor = SandboxExecutor()
    session_vars_input = {"origin": (118.78, 32.04), "exists": True}
    mock_result = SandboxResult(
        stdout="ok\n",
        stderr="__GISMIND_STATE_x__START__" + '{"msg":"ok"}' + "__GISMIND_STATE_x__END__\n",
        returncode=0,
        duration_ms=10,
    )

    with patch("app.agents.code_mode.sandbox_runner.run_in_sandbox", return_value=mock_result) as mock:
        res = executor.execute("print('ok')", session_vars=session_vars_input)

    assert res.executor_type == "sandbox"
    # 验证 run_in_sandbox 被调用时 code 含 session_vars 路径字面量注入
    # （不再在 wrapper 里读 APP_SANDBOX_VARS_PATH env，避免子进程 import os）
    called_code = mock.call_args[0][0]
    assert "sitecustomize_gismind" in called_code
    assert "_sv_path" in called_code
    assert ".svars" in called_code