"""RunController: pause / cancel / resume for agent execution.

Inspired by PineFlow's Run abstraction. Each chat request creates a
RunController that is registered in a process-level registry. The
dispatcher checks the controller at batch boundaries to honour cancel
and pause signals.

Thread-safe: uses ``threading.Event`` for cancel/pause signals so they
can be set from any thread (e.g. the FastAPI cancel endpoint handler
running on the async event loop while the dispatcher runs in a thread
pool).
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)


class RunState(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class RunController:
    """Per-run execution controller with cancel and pause support."""

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id
        self._cancel_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # initially running (not paused)
        self._state = RunState.PENDING
        self._created_at = time.time()
        self._updated_at = time.time()
        self._lock = threading.Lock()

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def state(self) -> RunState:
        return self._state

    @property
    def created_at(self) -> float:
        return self._created_at

    @property
    def updated_at(self) -> float:
        return self._updated_at

    def _set_state(self, state: RunState) -> None:
        with self._lock:
            self._state = state
            self._updated_at = time.time()

    # -- Signals --

    def start(self) -> None:
        """Mark the run as running."""
        self._set_state(RunState.RUNNING)

    def request_cancel(self) -> None:
        """Signal the run to cancel (non-blocking)."""
        self._cancel_event.set()
        self._set_state(RunState.CANCELLED)
        logger.info("RunController: cancel requested for run=%s", self._run_id)

    def request_pause(self) -> None:
        """Signal the run to pause (non-blocking)."""
        self._pause_event.clear()
        self._set_state(RunState.PAUSED)
        logger.info("RunController: pause requested for run=%s", self._run_id)

    def request_resume(self) -> None:
        """Signal the run to resume from pause."""
        self._pause_event.set()
        self._set_state(RunState.RUNNING)
        logger.info("RunController: resume requested for run=%s", self._run_id)

    def mark_completed(self) -> None:
        with self._lock:
            if self._cancel_event.is_set():
                return
            self._state = RunState.COMPLETED
            self._updated_at = time.time()

    def mark_failed(self) -> None:
        with self._lock:
            if self._cancel_event.is_set():
                return
            self._state = RunState.FAILED
            self._updated_at = time.time()

    # -- Polling (called by dispatcher at batch boundaries) --

    def should_stop(self) -> bool:
        """Return True if the run has been cancelled."""
        return self._cancel_event.is_set()

    def wait_if_paused(self, timeout: float = 60.0) -> bool:
        """Block if the run is paused. Returns True if resumed, False if cancelled.

        Args:
            timeout: Max seconds to wait before checking cancel signal.
                     If cancelled during wait, returns False immediately.
        """
        while not self._pause_event.is_set():
            if self._cancel_event.is_set():
                return False
            # Wait briefly then re-check (allows cancel during pause)
            self._pause_event.wait(timeout=min(timeout, 1.0))
        return not self._cancel_event.is_set()

    def to_dict(self) -> dict:
        return {
            "run_id": self._run_id,
            "status": self._state.value,
            "created_at": self._created_at,
            "updated_at": self._updated_at,
        }


# ---------------------------------------------------------------------------
# Process-level run registry
# ---------------------------------------------------------------------------

_RUN_REGISTRY: dict[str, RunController] = {}
_REGISTRY_LOCK = threading.Lock()
_REGISTRY_MAX_SIZE = 1000  # prevent unbounded growth
_REGISTRY_MAX_AGE_S = 3600  # auto-cleanup entries older than 1 hour


def create_run_controller(run_id: str) -> RunController:
    """Create and register a new RunController."""
    _cleanup_stale_runs()
    controller = RunController(run_id)
    with _REGISTRY_LOCK:
        _RUN_REGISTRY[run_id] = controller
    return controller


def get_run_controller(run_id: str) -> Optional[RunController]:
    """Retrieve a RunController by run_id, or None if not found."""
    return _RUN_REGISTRY.get(run_id)


def cleanup_run(run_id: str) -> None:
    """Remove a run from the registry."""
    with _REGISTRY_LOCK:
        _RUN_REGISTRY.pop(run_id, None)


def _cleanup_stale_runs() -> None:
    """Remove runs older than _REGISTRY_MAX_AGE_S or if registry is too large."""
    now = time.time()
    with _REGISTRY_LOCK:
        # Remove stale entries
        stale = [
            rid for rid, ctrl in _RUN_REGISTRY.items()
            if now - ctrl.created_at > _REGISTRY_MAX_AGE_S
        ]
        for rid in stale:
            del _RUN_REGISTRY[rid]
        # If still too large, remove oldest
        if len(_RUN_REGISTRY) > _REGISTRY_MAX_SIZE:
            sorted_runs = sorted(
                _RUN_REGISTRY.items(),
                key=lambda item: item[1].created_at,
            )
            for rid, _ in sorted_runs[:len(sorted_runs) - _REGISTRY_MAX_SIZE]:
                del _RUN_REGISTRY[rid]
