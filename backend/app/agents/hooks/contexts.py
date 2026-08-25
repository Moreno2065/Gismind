"""Hook context objects and lifecycle points."""

from __future__ import annotations

import enum
from collections.abc import Callable
from typing import Any


class HookPoint(enum.Enum):
    """Lifecycle points where hooks can be registered."""
    BEFORE_RUN = "before_run"
    BEFORE_PROMPT_BUILD = "before_prompt_build"
    BEFORE_TOOL_CALL = "before_tool_call"
    AFTER_TOOL_CALL = "after_tool_call"
    AFTER_RUN = "after_run"


class BeforeRunContext:
    """Mutable context passed through BEFORE_RUN hooks."""

    __slots__ = ("user_input", "session_id", "session_memory", "state", "data")

    def __init__(
        self,
        user_input: str = "",
        session_id: str = "",
        session_memory: str = "",
        state: dict[str, Any] | None = None,
    ) -> None:
        self.user_input = user_input
        self.session_id = session_id
        self.session_memory = session_memory
        self.state = state or {}
        self.data: dict[str, Any] = {}


class BeforePromptContext:
    """Mutable context passed through BEFORE_PROMPT_BUILD hooks.

    Hooks can modify ``system_prompt``, ``history``, or inject
    ``loaded_skills`` / ``session_memory`` snippets.
    """

    __slots__ = (
        "user_input", "state", "system_prompt", "history",
        "visible_tools", "loaded_skills", "session_memory", "data",
    )

    def __init__(
        self,
        user_input: str = "",
        state: dict[str, Any] | None = None,
        system_prompt: str = "",
        history: list | None = None,
        visible_tools: list[str] | None = None,
        loaded_skills: dict[str, str] | None = None,
        session_memory: str = "",
    ) -> None:
        self.user_input = user_input
        self.state = state or {}
        self.system_prompt = system_prompt
        self.history = history or []
        self.visible_tools = visible_tools or []
        self.loaded_skills = loaded_skills or {}
        self.session_memory = session_memory
        self.data: dict[str, Any] = {}


class BeforeToolContext:
    """Mutable context passed through BEFORE_TOOL_CALL hooks."""

    __slots__ = ("tool_name", "params", "code", "state", "validation_issues", "data")

    def __init__(
        self,
        tool_name: str = "",
        params: dict[str, Any] | None = None,
        code: str = "",
        state: dict[str, Any] | None = None,
    ) -> None:
        self.tool_name = tool_name
        self.params = params or {}
        self.code = code
        self.state = state or {}
        self.validation_issues: list[Any] = []
        self.data: dict[str, Any] = {}


class AfterToolContext:
    """Mutable context passed through AFTER_TOOL_CALL hooks."""

    __slots__ = ("tool_name", "result", "state", "risks", "data")

    def __init__(
        self,
        tool_name: str = "",
        result: Any = None,
        state: dict[str, Any] | None = None,
    ) -> None:
        self.tool_name = tool_name
        self.result = result
        self.state = state or {}
        self.risks: list[Any] = []
        self.data: dict[str, Any] = {}


class AfterRunContext:
    """Mutable context passed through AFTER_RUN hooks."""

    __slots__ = ("result", "session_id", "user_input", "steps", "data")

    def __init__(
        self,
        result: dict[str, Any] | None = None,
        session_id: str = "",
        user_input: str = "",
        steps: list | None = None,
    ) -> None:
        self.result = result or {}
        self.session_id = session_id
        self.user_input = user_input
        self.steps = steps or []
        self.data: dict[str, Any] = {}
