"""code_executor tool wrapper — 包装 run_in_sandbox 为 loop.py 可注册的工具函数。

工具签名: fn(ctx: _ToolContext) -> ToolResult，匹配 _TOOL_REGISTRY 契约。
"""
from __future__ import annotations
import logging

from app.agents.context import _ToolContext
from app.models.schemas import ToolResult
from app.agents.errors import ErrorCode
from app.config import settings
from app.sandbox.runner import run_in_sandbox

logger = logging.getLogger(__name__)


def code_executor(ctx: _ToolContext) -> ToolResult:
    """code_executor 工具处理器: 在受限沙箱中执行 Python 代码。

    从 params 读取:
        code (str, 必需): Python 代码字符串
        memory_mb (int, optional): 内存上限 MB, 默认 512
        timeout_s (int, optional): 墙钟超时秒数, 默认 60

    返回:
        ToolResult: status="success" 或 "error"
            data = {"stdout": str, "stderr": str, "returncode": int, "duration_ms": int}
    """
    code = ctx.params.get("code", "")
    if not code:
        return ToolResult(
            tool_call_id=ctx.tool_call_id,
            tool_name="code_executor",
            status="empty",
            message="code_executor 需要 code 参数",
        )

    if not settings.APP_SANDBOX_ENABLED:
        return ToolResult(
            tool_call_id=ctx.tool_call_id,
            tool_name="code_executor",
            status="empty",
            message="code_executor 沙箱已禁用 (APP_SANDBOX_ENABLED=false)",
        )

    memory_mb = int(ctx.params.get("memory_mb", settings.APP_SANDBOX_MEMORY_MB))
    timeout_s = int(ctx.params.get("timeout_s", settings.APP_SANDBOX_TIMEOUT_S))

    res = run_in_sandbox(code, memory_mb=memory_mb, timeout_s=timeout_s)

    data = {
        "stdout": res.stdout,
        "stderr": res.stderr,
        "returncode": res.returncode,
        "duration_ms": res.duration_ms,
    }

    if res.error_code:
        return ToolResult(
            tool_call_id=ctx.tool_call_id,
            tool_name="code_executor",
            status="error",
            error_code=ErrorCode(res.error_code).value,
            message=res.stderr or res.stdout,
            data=data,
        )

    return ToolResult(
        tool_call_id=ctx.tool_call_id,
        tool_name="code_executor",
        status="success",
        data=data,
        source="sandbox",
    )
