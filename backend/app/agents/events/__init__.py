"""EventCollector + emit_event + EVENT_CONTRACTS.

Thread-safe async event collection for SSE streaming. EventCollector is created
at POST /chat time and consumed by GET /api/chat/{session_id}/events.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

_SENTINEL = object()

EventHandler = Callable[[dict[str, Any]], None]

# ---------------------------------------------------------------------------
# EVENT_CONTRACTS:  event_name -> (event_type, display_kind)
# ---------------------------------------------------------------------------

EVENT_CONTRACTS: dict[str, tuple[str, str]] = {
    # Run-level
    "run.session": ("run.session", "progress"),
    "run.thought": ("run.thought", "debug"),
    "run.summary": ("run.summary", "result"),
    "run.completed": ("run.completed", "result"),
    "run.failed": ("run.failed", "result"),
    "run.paused": ("run.paused", "progress"),
    "run.plan": ("run.plan", "workflow_step"),
    # Sub-task lifecycle (dispatcher dispatch_node)
    "run.task.start": ("run.task.start", "progress"),
    "run.task.complete": ("run.task.complete", "workflow_step"),
    # Step-level (one code block)
    "code.generation": ("code.generation", "workflow_step"),
    "code.execution.start": ("code.execution.start", "progress"),
    "code.execution.stdout": ("code.execution.stdout", "debug"),
    "code.execution.stderr": ("code.execution.stderr", "debug"),
    "code.execution.complete": ("code.execution.complete", "workflow_step"),
    "code.execution.error": ("code.execution.error", "warning"),
    # Tool-call level
    "tool.call.start": ("tool.call.start", "progress"),
    "tool.preflight.warning": ("tool.preflight.warning", "warning"),
    "tool.preflight.blocked": ("tool.preflight.blocked", "warning"),
    "tool.call.complete": ("tool.call.complete", "workflow_step"),
    "tool.postcondition.failed": ("tool.postcondition.failed", "warning"),
    "tool.postflight.warning": ("tool.postflight.warning", "warning"),
    "tool.postflight.empty_result": ("tool.postflight.empty_result", "warning"),
    # Risk events
    "tool.risk.detected": ("tool.risk.detected", "warning"),
    "tool.risk.auto_repair": ("tool.risk.auto_repair", "progress"),
    "tool.risk.blocked": ("tool.risk.blocked", "warning"),
    # Judge / pending
    "judge.decision": ("judge.decision", "debug"),
    "judge.awaiting_input": ("judge.awaiting_input", "confirmation"),
}


def _build_event(event: str, message: str, payload: dict) -> dict[str, Any]:
    """Build a standardised event dict with contract fields."""
    event_type, display_kind = EVENT_CONTRACTS.get(event, (event, "debug"))
    item: dict[str, Any] = {
        "event": event,
        "event_type": event_type,
        "display_kind": display_kind,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    item.update(payload)
    return item


class EventCollector:
    """asyncio.Queue-based event collector.

    Thread-safe: async callers use ``put_nowait`` directly; sync callers
    (ThreadPoolExecutor) use ``call_soon_threadsafe``.  Drops events silently
    when the event loop is closed.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop | None = None,
        *,
        max_queue_size: int = 512,
    ) -> None:
        """If *loop* is ``None``, uses ``asyncio.get_running_loop()``.

        The explicit *loop* parameter is mainly for testability; production
        callers always create the collector inside an async context.
        """
        self._loop = loop if loop is not None else asyncio.get_running_loop()
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=max(1, int(max_queue_size)),
        )
        self._has_consumer = False
        self._dropped_count = 0
        # Dedup tracker: (stage, code, tool_name) -> True
        self._dedup_set: set[tuple[str, str, str]] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def emit(self, event: str, message: str, **payload: Any) -> None:
        """Sync/async dual-entry point.

        Async code: ``put_nowait`` directly.
        Sync code (ThreadPoolExecutor): ``call_soon_threadsafe``.
        Closed loop: log warning and discard.
        """
        item = _build_event(event, message, payload)

        # Dedup: only preflight/postflight issues per (stage, code, tool_name)
        dedup_key = self._dedup_key(event, payload)
        if dedup_key is not None:
            if dedup_key in self._dedup_set:
                return
            self._dedup_set.add(dedup_key)

        try:
            try:
                current = asyncio.get_running_loop()
            except RuntimeError:
                current = None

            if current is self._loop:
                self._enqueue(item)
            elif self._loop.is_running():
                self._loop.call_soon_threadsafe(self._enqueue, item)
            else:
                # Loop not running (sync test) → direct put_nowait
                self._enqueue(item)
        except RuntimeError:
            logger.warning(
                "EventCollector: loop closed, event dropped: %s", event,
            )
        except Exception:
            logger.warning("EventCollector.emit failed for %s", event, exc_info=True)

    def stop(self) -> None:
        """Terminate the ``consume()`` loop.  Thread-safe.

        Pushes a sentinel into the queue so that the consumer unblocks
        and exits cleanly instead of hanging on ``await queue.get()``.
        """
        try:
            try:
                current = asyncio.get_running_loop()
            except RuntimeError:
                current = None

            if current is self._loop:
                self._enqueue(_SENTINEL)
            elif self._loop.is_running():
                self._loop.call_soon_threadsafe(self._enqueue, _SENTINEL)
            else:
                self._enqueue(_SENTINEL)
        except RuntimeError:
            pass

    async def get(self, timeout: float) -> dict[str, Any] | None:
        """Return next event or ``None`` on timeout.

        Thread-affinity: must be called from the event loop that owns *self._queue*.
        This replaces the ``asyncio.wait_for(anext(consume()))`` pattern which
        would cancel the async generator on every timeout.
        """
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return None

    async def consume(self) -> AsyncIterator[dict[str, Any]]:
        """Async generator that yields events as they arrive.

        Stops when ``stop()`` is called (sentinel pushed into queue)
        or when the loop is closed.
        """
        self._has_consumer = True
        try:
            while True:
                item = await self._queue.get()
                if item is _SENTINEL:
                    break
                yield item
        finally:
            self._has_consumer = False

    def queue_has_consumer(self) -> bool:
        """Return whether a consumer is currently iterating ``consume()``."""
        return self._has_consumer

    def mark_no_consumer(self) -> None:
        """Mark consumer as gone.  Used by SSE endpoint after disconnect."""
        self._has_consumer = False

    def clear_dedup(self) -> None:
        """Clear the dedup set (e.g. at the start of a new sub-agent run)."""
        self._dedup_set.clear()

    @property
    def dropped_count(self) -> int:
        """Number of oldest events evicted because the consumer fell behind."""
        return self._dropped_count

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _enqueue(self, item: Any) -> None:
        """Append one event while keeping memory usage strictly bounded.

        Preserve the newest lifecycle state (including the stop sentinel) by
        evicting the oldest queued item when the SSE consumer is slower than
        producers.  This method always runs on the collector's event loop in
        production, including calls scheduled from worker threads.
        """
        if self._queue.full():
            try:
                self._queue.get_nowait()
                self._dropped_count += 1
            except asyncio.QueueEmpty:
                pass
        self._queue.put_nowait(item)

    @staticmethod
    def _dedup_key(event: str, payload: dict) -> tuple[str, str, str] | None:
        """Return dedup key ``(stage, code, tool_name)`` or ``None``.

        Only preflight / postflight issues are deduped.  stdout / stderr events
        are never deduped.
        """
        if event in ("tool.preflight.warning", "tool.preflight.blocked",
                      "tool.postflight.warning", "tool.postflight.empty_result"):
            stage = payload.get("stage", "")
            code = payload.get("code", "")
            tool_name = payload.get("tool_name", "")
            if stage and code and tool_name:
                return (stage, code, tool_name)
        return None


def emit_event(
    handler: EventHandler | None,
    event: str,
    message: str,
    **payload: Any,
) -> None:
    """Synchronous convenience wrapper.

    Builds a standardised event dict and passes it to ``handler(item)``.
    Silently discards if handler is None.
    """
    if handler is None:
        return
    try:
        item = _build_event(event, message, payload)
        handler(item)
    except Exception:
        logger.warning("emit_event failed for %s", event, exc_info=True)


__all__ = [
    "EVENT_CONTRACTS",
    "EventCollector",
    "EventHandler",
    "emit_event",
]
