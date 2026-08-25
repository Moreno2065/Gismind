"""HybridExecutor — code-mode 主引擎：AST 检查、sandbox 执行、registry RPC。

D2 决策：LLM 代码始终在子进程 sandbox 执行，绝不在主进程 exec 任意 LLM 代码。
已注册工具通过 sandbox→host TCP JSON-lines RPC 回调主进程 registry handler。
AST 禁用项和 sandbox 资源限制（timeout / memory / import blacklist）继续作为边界。
"""
from __future__ import annotations

import ast
import re
import time
from typing import Any, Callable, Optional

from app.agents.code_mode.ast_guard import inspect as ast_inspect
from app.agents.code_mode.ast_guard import ASTBannedNodeError
from app.agents.code_mode.sandbox_runner import SandboxExecutor
from app.agents.code_mode.types import ExecutionResult
from app.models.schemas import ToolResult


# ============================================================
# Fence / think tag 预处理
# ============================================================

def _strip_fence_and_think(code: str) -> str:
    """Executor 入口预处理：多 fence 拼接 → ast.parse 校验 → 单 fence → 剥 think tag → 原样。

    鲁棒性阶梯（从尝试多 fence 到退化为原始代码）：
    1. 检测所有 ```python ... ``` 块，按出现顺序用 \\n\\n 拼接
    2. ast.parse 校验拼接结果：合法 → 返回拼接结果
    3. 拼接失败 → 回退到只取最后一个 fence（最可能是"主代码块"）
    4. 无 fence → 剥 <thinking>...</thinking> / <think>...</think>
    5. 无 fence 且无 think tag → 返回原始代码
    """
    if not code or not code.strip():
        return ""

    # 模式 1: 检测所有 fence
    fences = re.findall(r"```(?:python|py)?\s*\n(.*?)\n?```", code, re.DOTALL)
    if len(fences) > 1:
        # 尝试拼接
        merged = "\n\n".join(f.strip() for f in fences)
        try:
            ast.parse(merged)
            return merged.strip()
        except SyntaxError:
            pass  # 拼接失败，fallthrough 到单 fence

    if fences:
        return fences[-1].strip()

    # 模式 2: 剥 think tag
    cleaned = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", code, flags=re.DOTALL)
    cleaned = cleaned.strip()
    return cleaned


# ============================================================
# ToolResult 映射
# ============================================================

def execution_to_tool_result(
    exec_result: ExecutionResult,
    iteration: int,
) -> ToolResult:
    """把 ExecutionResult 映射为 ToolResult（mode="code"，tool_name="__code_block__"）。

    Args:
        exec_result: HybridExecutor 的执行结果。
        iteration: 当前迭代轮次（用于生成 tool_call_id）。

    Returns:
        ToolResult: mode="code" 的 ToolResult，下游 verifier/voter 按 mode 分支处理。
    """
    # traceback 截断到 3000 字符
    tb = exec_result.traceback or ""
    if len(tb) > 3000:
        tb = tb[:3000] + f"\n... [truncated, {len(exec_result.traceback)} chars total]"

    return ToolResult(
        tool_call_id=f"call_{iteration}_code",
        tool_name="__code_block__",
        mode="code",
        status="success" if exec_result.success else "error",
        data={
            "code": exec_result.code,
            "stdout": exec_result.stdout,
            "stderr": exec_result.stderr,
            "result": exec_result.result,
            "executor_type": exec_result.executor_type,
            "required_executor": exec_result.required_executor,
            "duration_ms": exec_result.duration_ms,
            "traceback": tb,
            "error_code": exec_result.error_code,
        },
        source=exec_result.executor_type,
        error_code=exec_result.error_code,
        message=exec_result.stderr if not exec_result.success else None,
    )


# ============================================================
# HybridExecutor
# ============================================================

class HybridExecutor:
    """code-mode 主执行器。

    用法：
        executor = HybridExecutor()
        result = executor.execute(
            code="pois = query_poi(bbox=...); __result__ = {'pois': pois}",
            session_vars={"origin": (118.78, 32.04)},
            known_tools={"query_poi": "async", "buffer_geometry": "inline"},
            tool_fns={"query_poi": real_fn, ...},
        )
        tool_result = execution_to_tool_result(result, iteration=3)
        # session_vars 更新
        if isinstance(result.result, dict):
            session_vars.update(result.result)
    """

    def __init__(self):
        # 工具函数映射（name → callable）：由外部注入，或内部根据 known_tools 自动配置
        self._tool_fns: dict[str, Callable] = {}

    def register_tool_fns(self, tool_fns: dict[str, Callable]) -> None:
        """注册工具函数映射（供接线后由 tool_registry 注入真实实现）。"""
        self._tool_fns.update(tool_fns)

    def execute(
        self,
        code: str,
        session_vars: Optional[dict] = None,
        known_tools: Optional[dict[str, str]] = None,
        tool_fns: Optional[dict[str, Callable]] = None,
        on_event: Optional[Callable[[str, str], Any]] = None,
    ) -> ExecutionResult:
        """主入口：fence 预处理 → AST 检查 → 始终 sandbox（工具经 RPC 回主进程）。

        Args:
            code: LLM 写的 Python 代码（可能含 fence / think tag）。
            session_vars: 跨 step 持久化的变量。
            known_tools: 工具名 → executor_type 的映射（用于 AST 分析和 namespace 构造）。
            tool_fns: 工具名 → 真实函数的映射（可选；若未传由 self._tool_fns 兜底）。
            on_event: 可选事件回调（event_name, message, **payload）。
                     v1（Task 4）只透传不实际 emit，留给 Task 6 接线。

        Returns:
            ExecutionResult: 包含执行结果 / traceback / required_executor。
        """
        start = time.time()
        sv = session_vars or {}
        tools = known_tools or {}
        fns = tool_fns or self._tool_fns

        # 1. 预处理
        cleaned = _strip_fence_and_think(code)
        if not cleaned:
            return ExecutionResult(
                success=True,
                result={},
                duration_ms=int((time.time() - start) * 1000),
                code=code,
            )

        # 2. AST 检查
        try:
            inspection = ast_inspect(cleaned, known_tools=tools)
        except ASTBannedNodeError as e:
            duration = int((time.time() - start) * 1000)
            return ExecutionResult(
                success=False,
                error_code="AST_BANNED_NODE",
                traceback=f"AST banned node: {e.node_type} at line {e.lineno}: {e.snippet}",
                duration_ms=duration,
                code=code,
            )

        # 3. D2: 所有 LLM 代码走 sandbox；非本地工具经 host RPC 回调 registry。
        #    不再因 "非 sandbox 工具" 在主进程 exec 任意 LLM 代码。
        _ = on_event  # reserved
        return self._exec_in_sandbox(
            cleaned,
            sv,
            inspection.call_graph,
            code,
            tool_fns=fns,
            known_tools=tools,
        )

    def _exec_in_sandbox(
        self,
        code: str,
        session_vars: dict,
        call_graph: dict[str, str],
        original_code: str,
        tool_fns: Optional[dict[str, Callable]] = None,
        known_tools: Optional[dict[str, str]] = None,
    ) -> ExecutionResult:
        """子进程 sandbox 执行（复用 SandboxExecutor + host tool RPC）。"""
        sandbox_executor = SandboxExecutor()
        result = sandbox_executor.execute(
            code,
            session_vars=session_vars,
            call_graph=call_graph,
            tool_fns=tool_fns,
            known_tools=known_tools,
        )
        result.code = original_code
        return result
