"""Hook pipeline system for agent lifecycle extensibility.

Provides a unified hook mechanism for BEFORE_PROMPT_BUILD,
BEFORE_TOOL_CALL, AFTER_TOOL_CALL, and AFTER_RUN lifecycle points.
"""

from app.agents.hooks.pipeline import (
    HookPipeline,
    HookExecutionError,
    get_pipeline,
    register_hook,
)
from app.agents.hooks.contexts import (
    HookPoint,
    BeforeRunContext,
    BeforePromptContext,
    BeforeToolContext,
    AfterToolContext,
    AfterRunContext,
)

__all__ = [
    "HookPipeline",
    "HookExecutionError",
    "HookPoint",
    "get_pipeline",
    "register_hook",
    "BeforeRunContext",
    "BeforePromptContext",
    "BeforeToolContext",
    "AfterToolContext",
    "AfterRunContext",
]
