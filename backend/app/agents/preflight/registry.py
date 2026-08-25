"""RuleRegistry 装饰器 + preflight_for 查询。

用法::

    @register_preflight_rule("buffer_requires_projected_crs", "buffer_layer")
    def _check_buffer_crs(ctx: dict) -> list[ValidationIssue]:
        ...

    issues = preflight_for("buffer_layer", ctx)
"""
from __future__ import annotations

from typing import Any, Callable

from app.agents.preflight.validation import ValidationIssue

_RuleFn = Callable[[dict[str, Any]], list[ValidationIssue]]
_RULES: dict[str, tuple[tuple[str, ...], _RuleFn]] = {}


def register_preflight_rule(name: str, *semantic_actions: str):
    """装饰器：把 rule 函数注册到 RuleRegistry。

    Args:
        name: 规则唯一名称。
        *semantic_actions: 该规则适用的 semantic action 名称列表。
                           空列表表示匹配所有 semantic action（慎用）。
    """
    def decorator(fn: _RuleFn) -> _RuleFn:
        _RULES[name] = (tuple(semantic_actions), fn)
        return fn
    return decorator


def preflight_for(semantic_action: str, ctx: dict[str, Any]) -> list[ValidationIssue]:
    """对给定 semantic_action 执行所有匹配的 preflight 规则。

    Args:
        semantic_action: 当前工具的 semantic action 名称。
        ctx: 包含 "workspace"、"kwargs"、"tool_name" 等键的上下文 dict。

    Returns:
        所有匹配规则产出的 ValidationIssue 列表（可能为空）。
    """
    issues: list[ValidationIssue] = []
    for _, (actions, fn) in _RULES.items():
        if semantic_action in actions or not actions:
            issues.extend(fn(ctx))
    return issues
