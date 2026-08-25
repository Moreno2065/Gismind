"""Stream adapter: links LangGraph runs to EventCollector.

.. deprecated::
    Replaced by ``events/current.py`` contextvar-based handler.
    Kept for backward compatibility only; will be removed in a future version.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], None]

# Process-level registry: session_id -> EventHandler
_handlers: dict[str, EventHandler] = {}


def register_handler(session_id: str, on_event: EventHandler) -> None:
    """Register an event handler for *session_id*."""
    _handlers[session_id] = on_event
    logger.debug("stream_adapter: handler registered for session=%s", session_id)


def unregister_handler(session_id: str) -> None:
    """Unregister the handler for *session_id*."""
    _handlers.pop(session_id, None)
    logger.debug("stream_adapter: handler unregistered for session=%s", session_id)


def get_handler(session_id: str) -> EventHandler | None:
    """Return the registered handler for *session_id*, or ``None``."""
    return _handlers.get(session_id)


def emit_for_session(session_id: str, event: str, message: str, **payload: Any) -> None:
    """Look up the handler for *session_id* and emit an event.

    No-op if no handler is registered. Matches ``emit_event`` contract:
    handlers receive a single event dict, not multi-arg kwargs.
    """
    handler = _handlers.get(session_id)
    if handler is None:
        return
    try:
        from app.agents.events import emit_event

        emit_event(handler, event, message, **payload)
    except Exception:
        logger.warning(
            "emit_for_session failed session=%s event=%s", session_id, event,
            exc_info=True,
        )


__all__ = [
    "EventHandler",
    "emit_for_session",
    "get_handler",
    "register_handler",
    "unregister_handler",
]
