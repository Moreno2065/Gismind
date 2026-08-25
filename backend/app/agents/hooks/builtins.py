"""Built-in hook registrations for the Gismind agent lifecycle.

These hooks wire together the existing subsystems (preflight, skill
injection, session memory, risk detection) into the unified HookPipeline.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.hooks.contexts import (
    HookPoint,
    BeforePromptContext,
    BeforeToolContext,
    AfterToolContext,
    AfterRunContext,
)

logger = logging.getLogger(__name__)


def _register_builtin_hooks(pipeline) -> None:
    """Register all built-in hooks on the given pipeline."""
    pipeline.register(
        HookPoint.BEFORE_PROMPT_BUILD,
        _inject_loaded_skills,
        name="inject_loaded_skills",
        priority=50,
    )
    pipeline.register(
        HookPoint.BEFORE_PROMPT_BUILD,
        _inject_session_memory,
        name="inject_session_memory",
        priority=60,
    )
    pipeline.register(
        HookPoint.BEFORE_TOOL_CALL,
        _run_preflight_validation,
        name="preflight_validation",
        priority=50,
    )
    pipeline.register(
        HookPoint.AFTER_TOOL_CALL,
        _detect_risks,
        name="detect_risks",
        priority=50,
    )
    pipeline.register(
        HookPoint.AFTER_RUN,
        _persist_session_memory,
        name="persist_session_memory",
        priority=100,
    )


# ---------------------------------------------------------------------------
# BEFORE_PROMPT_BUILD hooks
# ---------------------------------------------------------------------------

def _inject_loaded_skills(ctx: BeforePromptContext) -> BeforePromptContext:
    """Inject loaded skill content into the system prompt."""
    if not ctx.loaded_skills:
        return ctx

    skill_parts = []
    for name, content in ctx.loaded_skills.items():
        if content:
            skill_parts.append(f"### Skill: {name}\n{content}")

    if skill_parts:
        skill_block = "\n\n".join(skill_parts)
        ctx.system_prompt = f"{ctx.system_prompt}\n\n## Loaded Skills\n{skill_block}"
        logger.debug("inject_loaded_skills: added %d skills", len(skill_parts))

    return ctx


def _inject_session_memory(ctx: BeforePromptContext) -> BeforePromptContext:
    """Inject session memory snippet into the system prompt."""
    if ctx.session_memory:
        ctx.system_prompt = (
            f"{ctx.system_prompt}\n\n## Session Memory\n{ctx.session_memory}"
        )
    return ctx


# ---------------------------------------------------------------------------
# BEFORE_TOOL_CALL hooks
# ---------------------------------------------------------------------------

def _run_preflight_validation(ctx: BeforeToolContext) -> BeforeToolContext:
    """Run preflight validation rules for the tool being called."""
    try:
        from app.agents.preflight.registry import preflight_for

        preflight_ctx = {
            "tool_name": ctx.tool_name,
            "kwargs": ctx.params,
            "state": ctx.state,
        }
        issues = preflight_for(ctx.tool_name, preflight_ctx)
        if issues:
            ctx.validation_issues.extend(issues)
            logger.info(
                "preflight hook: %d issues for tool=%s",
                len(issues), ctx.tool_name,
            )
    except ImportError:
        pass  # preflight not available yet
    except Exception:
        logger.warning("preflight hook failed", exc_info=True)
    return ctx


# ---------------------------------------------------------------------------
# AFTER_TOOL_CALL hooks
# ---------------------------------------------------------------------------

def _detect_risks(ctx: AfterToolContext) -> AfterToolContext:
    """Convert validation issues / tool results into GISRisk objects."""
    try:
        from app.agents.risks.taxonomy import result_to_risks

        risks = result_to_risks(ctx.tool_name, ctx.result)
        if risks:
            ctx.risks.extend(risks)
            logger.info(
                "risk detection: %d risks for tool=%s",
                len(risks), ctx.tool_name,
            )
    except ImportError:
        pass  # risks module not available yet
    except Exception:
        logger.warning("risk detection hook failed", exc_info=True)
    return ctx


# ---------------------------------------------------------------------------
# AFTER_RUN hooks
# ---------------------------------------------------------------------------

def _persist_session_memory(ctx: AfterRunContext) -> AfterRunContext:
    """Extract and persist session memory from the run results."""
    try:
        from app.agents.session_memory import SessionMemory

        memory = SessionMemory(ctx.session_id)
        final_output = ctx.result.get("final_output", {})
        results = final_output.get("results", [])
        if results:
            # extract_and_store is async; use asyncio.run in hook context
            import asyncio as _asyncio
            _asyncio.run(memory.extract_and_store(results))
            logger.debug("session memory persisted for session=%s", ctx.session_id)
    except ImportError:
        logger.warning("session_memory module not available for persist hook")
    except Exception:
        logger.warning("persist_session_memory hook failed", exc_info=True)
    return ctx
