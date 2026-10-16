"""One worker-owned deadline and HTTP-call budget for a planning generation."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import math
import threading
import time


class PlanningControlError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        self.diagnostics = {"code": code}
        super().__init__(code)


class PlanningBudget:
    def __init__(self, timeout_seconds: float, max_http_calls: int, *, stop_event=None):
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("planning timeout must be finite and positive")
        if isinstance(max_http_calls, bool) or not isinstance(max_http_calls, int) or max_http_calls < 1:
            raise ValueError("planning HTTP-call budget must be a positive integer")
        self.deadline = time.monotonic() + timeout_seconds
        self.max_http_calls = max_http_calls
        self.http_calls = 0
        self.cancelled = threading.Event()
        self.cancel_code = "STALE_PLANNER_LEASE"
        self.stop_event = stop_event

    def cancel(self, code: str) -> None:
        if not self.cancelled.is_set():
            self.cancel_code = code
            self.cancelled.set()

    def check(self) -> None:
        if self.cancelled.is_set():
            raise PlanningControlError(self.cancel_code)
        if self.stop_event is not None and self.stop_event.is_set():
            raise PlanningControlError("WORKER_SHUTDOWN")
        if time.monotonic() >= self.deadline:
            raise PlanningControlError("LLM_PLANNING_DEADLINE")

    def reserve_call(self) -> None:
        self.check()
        if self.http_calls >= self.max_http_calls:
            raise PlanningControlError("LLM_CALL_BUDGET_EXCEEDED")
        self.http_calls += 1

    async def wait(self, awaitable):
        """Cancel and join owned async I/O before releasing the worker."""
        task = asyncio.ensure_future(awaitable)
        try:
            while not task.done():
                self.check()
                await asyncio.wait({task}, timeout=min(0.05, max(0.001, self.deadline - time.monotonic())))
            self.check()
            return task.result()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


current_planning_budget: ContextVar[PlanningBudget | None] = ContextVar("gismind_planning_budget", default=None)
