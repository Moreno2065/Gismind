"""Per-run event handler stored in a context variable.

Replaces the old ``_on_event`` field in LangGraph state (which was not
serialisable and would silently fail through SqliteSaver checkpoints).

Usage::

    from app.agents.events.current import get_current_handler, set_current_handler

    token = set_current_handler(my_handler)
    try:
        ...
    finally:
        reset_current_handler(token)
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Callable

EventHandler = Callable[..., None]
"""Signature: ``handler(item: dict) -> None`` (emit_event builds the dict).

Production graph nodes always call ``emit_event(handler, event, message, **payload)``,
which constructs a standardised event dict and invokes ``handler(item)``. Direct
callers of ``EventCollector.emit(event, message, **payload)`` remain multi-arg.
"""

_current_handler: contextvars.ContextVar[EventHandler | None] = contextvars.ContextVar(
    "gismind_event_handler", default=None
)


def set_current_handler(handler: EventHandler | None) -> contextvars.Token[EventHandler | None]:
    """Push *handler* as the active event emitter for the current context."""
    return _current_handler.set(handler)


def reset_current_handler(token: contextvars.Token[EventHandler | None]) -> None:
    """Restore the previous handler from *token*."""
    _current_handler.reset(token)


@contextmanager
def event_handler_context(handler: EventHandler | None) -> Iterator[None]:
    """Scope *handler* to one run and always restore the previous context."""
    token = set_current_handler(handler)
    try:
        yield
    finally:
        reset_current_handler(token)


def get_current_handler() -> EventHandler | None:
    """Return the active event handler, or ``None``."""
    return _current_handler.get()
