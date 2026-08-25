"""Unified token budget controller for sub-agent prompt assembly.

Inspired by PineFlow's ContextBudget: provides a single point of control
for how many tokens each section of the prompt (role knowledge, tool
definitions, history, loaded skills, session vars) is allowed to consume.

CJK characters are estimated at ~1.5 char/token; ASCII at ~4 char/token.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token estimation helpers
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """Rough token count: CJK ~1.5 char/token, ASCII ~4 char/token."""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿" or "　" <= ch <= "〿")
    ascii_chars = len(text) - cjk
    return int(cjk / 1.5 + ascii_chars / 4.0)


def _trim_to_token_estimate(text: str, max_tokens: int) -> str:
    """Trim *text* so its estimated token count stays within *max_tokens*."""
    if not text:
        return text
    chars_per_token = 3.5  # blended average
    max_chars = int(max_tokens * chars_per_token)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n... (truncated, budget exceeded)"


# ---------------------------------------------------------------------------
# ContextBudget
# ---------------------------------------------------------------------------

@dataclass
class ContextBudget:
    """Token budget controller for a single sub-agent invocation."""

    max_tokens: int = 6000
    allocations: dict[str, int] = field(default_factory=lambda: {
        "user_request": 500,
        "role_knowledge": 800,
        "tool_prompt": 800,
        "observer_summary": 400,
        "history_steps": 1500,
        "loaded_skills": 1000,
        "session_vars": 300,
        "tool_results": 800,
        "session_memory": 600,
        "system_overhead": 300,
    })

    # -- public API --

    def allocate(self, section: str, content: str) -> str:
        """Trim *content* so its estimated token count fits the *section* budget."""
        limit = self.allocations.get(section)
        if limit is None or limit <= 0:
            return content
        return _trim_to_token_estimate(content, limit)

    def remaining(self, *, used_sections: dict[str, int] | None = None) -> int:
        """Return remaining token budget after subtracting *used_sections*."""
        used = sum(used_sections.values()) if used_sections else 0
        return max(0, self.max_tokens - used)

    def section_limit(self, section: str) -> int:
        return self.allocations.get(section, 0)


# ---------------------------------------------------------------------------
# Message trimming (for history windows)
# ---------------------------------------------------------------------------

def trim_messages(messages: Sequence, max_tokens: int) -> list:
    """Keep the most recent messages that fit within *max_tokens*.

    Iterates from the tail; stops when cumulative estimate exceeds budget.
    Returns messages in their original order.
    """
    if not messages:
        return []

    total = 0
    kept: list = []
    for msg in reversed(list(messages)):
        content = _extract_content(msg)
        cost = estimate_tokens(content)
        if total + cost > max_tokens:
            break
        kept.insert(0, msg)
        total += cost
    return kept


def _extract_content(msg: Any) -> str:
    """Extract text content from a LangChain message or plain dict."""
    if isinstance(msg, str):
        return msg
    # LangChain BaseMessage
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content
    # dict fallback
    if isinstance(msg, dict):
        return str(msg.get("content", ""))
    return str(msg)


# ---------------------------------------------------------------------------
# Budget report (debugging / observability)
# ---------------------------------------------------------------------------

def build_budget_report(payload: dict[str, Any], *, max_tokens: int = 6000) -> dict[str, Any]:
    """Return a token-usage report for prompt payload sections."""
    sections: dict[str, dict[str, int]] = {}
    total = 0
    for key, value in dict(payload or {}).items():
        text = _section_text(value)
        tokens = estimate_tokens(text)
        total += tokens
        sections[str(key)] = {
            "estimated_tokens": tokens,
            "chars": len(text),
        }
    return {
        "estimated_total_tokens": total,
        "max_tokens": max_tokens,
        "remaining_tokens": max(0, max_tokens - total),
        "over_budget": total > max_tokens,
        "sections": sections,
    }


def _section_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    except Exception:
        return str(value)
