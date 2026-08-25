"""DuplicateActionGuard: detect and prevent LLM from repeating identical tool calls.

Inspired by PineFlow's DuplicateActionGuard. When the agent calls the same
tool with the same parameters multiple times (within a sliding window), the
guard flags it and suggests the planner try an alternative approach instead
of wasting tokens on redundant execution.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class DuplicateActionGuard:
    """Track recent actions and detect duplicates within a sliding window."""

    def __init__(self) -> None:
        self._history: list[tuple[str, str]] = []  # (tool_name, fingerprint)

    def record(self, tool_name: str, params: Any) -> None:
        """Record an action execution."""
        fp = self._fingerprint(params)
        self._history.append((tool_name, fp))

    def is_duplicate(self, tool_name: str, params: Any, window: int = 3) -> bool:
        """Check if the same action was executed within the last *window* calls.

        Returns True if duplicate detected, False otherwise.
        """
        if not self._history:
            return False
        fp = self._fingerprint(params)
        recent = self._history[-window:]
        for prev_name, prev_fp in recent:
            if prev_name == tool_name and prev_fp == fp:
                logger.info(
                    "DuplicateActionGuard: duplicate detected tool=%s (window=%d)",
                    tool_name, window,
                )
                return True
        return False

    def suggestion(self, tool_name: str = "") -> str:
        """Generate a suggestion message for the LLM when a duplicate is detected."""
        recent_tools = [name for name, _ in self._history[-5:]]
        hint = f"你已经重复调用了 {tool_name}，请尝试不同的方案。" if tool_name else "检测到重复动作。"
        if recent_tools:
            hint += f"\n最近调用的工具：{', '.join(recent_tools)}"
        hint += "\n建议：1) 更换数据源 2) 调整参数（如半径、关键词）3) 如果结果已足够，请输出最终答案。"
        return hint

    def _fingerprint(self, params: Any) -> str:
        """Create a stable fingerprint from params for comparison.

        Excludes volatile fields (timestamps, random IDs) and normalises
        dict key ordering.
        """
        _EXCLUDE_KEYS = {"_timestamp", "_run_id", "tool_call_id"}

        def _clean(obj: Any) -> Any:
            if isinstance(obj, dict):
                return {
                    k: _clean(v)
                    for k, v in sorted(obj.items())
                    if k not in _EXCLUDE_KEYS
                }
            if isinstance(obj, (list, tuple)):
                return [_clean(v) for v in obj]
            if isinstance(obj, float):
                return round(obj, 6)
            return obj

        cleaned = _clean(params)
        raw = json.dumps(cleaned, ensure_ascii=False, default=str, sort_keys=True)
        return hashlib.md5(raw.encode()).hexdigest()

    @property
    def history_count(self) -> int:
        return len(self._history)


# ---------------------------------------------------------------------------
# Code-level duplicate detection helpers
# ---------------------------------------------------------------------------

def extract_tool_calls_from_code(code: str) -> list[tuple[str, dict]]:
    """Best-effort extraction of tool calls from Python code string.

    Parses the code looking for patterns like:
        tool_name(param1=val1, param2=val2)

    This is a heuristic, not a full AST analysis. Used only for duplicate
    detection, not for security (AST Guard handles that).
    """
    import re

    calls: list[tuple[str, dict]] = []
    # Match function_name(keyword=value, ...) patterns
    # Skip common built-ins and control structures
    _SKIP = {"print", "len", "range", "str", "int", "float", "list", "dict",
             "tuple", "set", "type", "isinstance", "hasattr", "getattr",
             "sorted", "reversed", "enumerate", "zip", "map", "filter",
             "sum", "min", "max", "abs", "round", "open", "json", "math"}

    pattern = re.compile(r"(\w+)\s*\(([^)]*)\)")
    for match in pattern.finditer(code):
        name = match.group(1)
        if name in _SKIP or name.startswith("_"):
            continue
        args_str = match.group(2).strip()
        if not args_str:
            calls.append((name, {}))
            continue
        # Parse keyword arguments
        params: dict = {}
        for part in args_str.split(","):
            part = part.strip()
            if "=" in part:
                k, _, v = part.partition("=")
                params[k.strip()] = v.strip()
        calls.append((name, params))

    return calls
