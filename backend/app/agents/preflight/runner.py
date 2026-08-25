"""run_with_preflight：preflight → handler → postflight 编排器。

在 _build_code_mode_tool_fns 的 proxy 外层包一层：
1. 先执行 preflight 检查（匹配 semantic_action 的所有规则）。
2. blocking issue (severity="error") 抛 PreflightError，复用 EXECUTION_ERROR 路径。
3. 通过后执行 handler。
4. 对结果做 postflight 检查（空结果 / feature_count 异常），warning 注入 result.data。
"""
from __future__ import annotations

from typing import Any, Callable

from app.agents.preflight.registry import preflight_for
from app.agents.preflight.validation import PreflightError, ValidationIssue


def run_with_preflight(
    tool_name: str,
    semantic_action: str,
    fn: Callable,
    args: tuple,
    kwargs: dict[str, Any],
    workspace: Any = None,
) -> Any:
    """preflight → handler → postflight wrapper。

    Args:
        tool_name: 工具名称。
        semantic_action: 当前工具的 semantic action（用于匹配 preflight 规则）。
        fn: 实际处理函数（工具 handler）。
        args: 传给 handler 的 positional args。
        kwargs: 传给 handler 的 keyword args。
        workspace: WorkspaceState 实例，供 preflight 规则只读访问。

    Returns:
        handler 的返回值。

    Raises:
        PreflightError: 有 blocking issue 时抛出，被 executor 的 except 路径捕获。
    """
    ctx: dict[str, Any] = {
        "tool_name": tool_name,
        "kwargs": kwargs,
        "workspace": workspace,
    }

    # ---- Preflight ----
    issues = preflight_for(semantic_action, ctx)
    blocking = [i for i in issues if i.severity == "error"]

    # Convert issues to structured GISRisks for unified decision chain
    try:
        from app.agents.risks.taxonomy import validation_issue_to_risk
        from app.agents.risks.policy import RiskPolicy

        risks = [validation_issue_to_risk(i) for i in issues]
        if risks:
            policy = RiskPolicy()
            decision = policy.decide(risks)
            import logging
            logging.getLogger(__name__).info(
                "RiskPolicy preflight decision: %s (tool=%s, %d risks)",
                decision.kind, tool_name, len(risks),
            )
            # Emit risk events for observability
            if decision.kind == "fail":
                msg = "; ".join(i.message for i in blocking)
                raise PreflightError(msg, issues=blocking)
    except ImportError:
        # Risks module not available — fall back to original logic
        if blocking:
            msg = "; ".join(i.message for i in blocking)
            raise PreflightError(msg, issues=blocking)

    # ---- Execute handler ----
    # Handlers 签名为 (ctx) 单参，kwargs 仅供 preflight 规则读取上下文，
    # 不透传给 handler。
    result = fn(*args)

    # ---- Postflight ----
    from app.agents.preflight.postflight import run_postflight

    post_issues = run_postflight(tool_name, result, ctx)
    if post_issues and hasattr(result, "data") and isinstance(result.data, dict):
        warnings = [i.message for i in post_issues if i.severity == "warning"]
        if warnings:
            existing = result.data.get("postflight_warnings", [])
            if isinstance(existing, list):
                result.data["postflight_warnings"] = existing + warnings
            else:
                result.data["postflight_warnings"] = warnings

    return result
