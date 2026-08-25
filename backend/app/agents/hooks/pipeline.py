"""Hook pipeline registry for the agent lifecycle.

Inspired by PineFlow's HookPipeline. Provides an ordered pipeline of
lifecycle hooks that can be registered, prioritised, and emitted at each
HookPoint.

Usage::

    from app.agents.hooks import get_pipeline, HookPoint, register_hook

    @register_hook(HookPoint.BEFORE_PROMPT_BUILD, priority=50)
    def inject_skill(ctx):
        ctx.system_prompt += "\\n" + ctx.loaded_skills.get("meter_buffer", "")
        return ctx

    # or imperatively:
    pipeline = get_pipeline()
    pipeline.emit(HookPoint.BEFORE_PROMPT_BUILD, ctx)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from app.agents.hooks.contexts import HookPoint

_log = logging.getLogger(__name__)


class HookExecutionError(RuntimeError):
    """Raised when a critical hook fails and execution must stop."""


class _RegisteredHook:
    __slots__ = ("name", "point", "fn", "priority", "critical")

    def __init__(
        self,
        name: str,
        point: HookPoint,
        fn: Callable[..., Any],
        priority: int = 100,
        critical: bool = False,
    ) -> None:
        self.name = name
        self.point = point
        self.fn = fn
        self.priority = priority
        self.critical = bool(critical)


class HookPipeline:
    """Ordered pipeline of lifecycle hooks for the agent runtime."""

    def __init__(self) -> None:
        self._hooks: dict[HookPoint, list[_RegisteredHook]] = {p: [] for p in HookPoint}

    def register(
        self,
        point: HookPoint,
        fn: Callable[..., Any],
        *,
        name: str = "",
        priority: int = 100,
        replace: bool = False,
        critical: bool = False,
    ) -> None:
        """Register a hook function at the given lifecycle point.

        Args:
            point: The lifecycle point to attach to.
            fn: Hook callable. Receives a context object and may return
                a modified context (or None to pass-through).
            name: Unique name for this hook. Defaults to fn.__name__.
            priority: Lower numbers run first. Default 100.
            replace: If True, silently replace an existing hook with
                the same name at the same point.
            critical: If True, failures in this hook raise
                HookExecutionError instead of being logged and skipped.
        """
        point = _coerce_hook_point(point)
        hook_name = name or getattr(fn, "__name__", "unnamed")
        registered = _RegisteredHook(
            name=hook_name,
            point=point,
            fn=fn,
            priority=priority,
            critical=critical,
        )
        hooks = self._hooks.setdefault(point, [])
        for index, existing in enumerate(hooks):
            if existing.name != hook_name:
                continue
            if existing.fn is fn:
                return  # idempotent re-register
            if replace:
                hooks[index] = registered
                hooks.sort(key=lambda h: h.priority)
                return
            raise ValueError(
                f"Hook {hook_name!r} is already registered for {point.value}. "
                f"Use replace=True to overwrite."
            )
        hooks.append(registered)
        hooks.sort(key=lambda h: h.priority)

    def emit(self, point: HookPoint, ctx: Any) -> Any:
        """Run all hooks registered for *point*, passing *ctx* through the chain.

        Each hook may return a modified context or None (pass-through).
        If a critical hook raises, HookExecutionError propagates.
        Non-critical hook failures are logged and skipped.
        """
        point = _coerce_hook_point(point)
        for registered in self._hooks.get(point, []):
            try:
                result = registered.fn(ctx)
                if result is not None:
                    ctx = result
            except Exception as exc:
                _log.warning(
                    "Hook %r at point %s raised: %s",
                    registered.name, point.value, exc,
                    exc_info=True,
                )
                if registered.critical:
                    raise HookExecutionError(
                        f"Critical hook {registered.name!r} at {point.value} failed."
                    ) from exc
        return ctx

    def hook_names(self, point: HookPoint) -> list[str]:
        """Return names of hooks registered at *point*."""
        point = _coerce_hook_point(point)
        return [h.name for h in self._hooks.get(point, [])]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_GLOBAL_PIPELINE: HookPipeline | None = None


def get_pipeline() -> HookPipeline:
    """Return the global HookPipeline singleton, lazily initialised."""
    global _GLOBAL_PIPELINE
    if _GLOBAL_PIPELINE is None:
        from app.agents.hooks.builtins import _register_builtin_hooks
        _GLOBAL_PIPELINE = HookPipeline()
        _register_builtin_hooks(_GLOBAL_PIPELINE)
    return _GLOBAL_PIPELINE


def register_hook(
    point: HookPoint | str,
    *,
    name: str = "",
    priority: int = 100,
    replace: bool = False,
    critical: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to register a function as a hook at the given point."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        get_pipeline().register(
            _coerce_hook_point(point),
            fn,
            name=name,
            priority=priority,
            replace=replace,
            critical=critical,
        )
        return fn

    return decorator


def _coerce_hook_point(point: HookPoint | str) -> HookPoint:
    if isinstance(point, HookPoint):
        return point
    return HookPoint(str(point or ""))
