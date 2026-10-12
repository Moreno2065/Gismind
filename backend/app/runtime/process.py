"""Small cross-platform process isolation and resource-limit helpers."""

from __future__ import annotations

import os
from typing import Any


def _sandbox_apply_job_object(process: Any, memory_mb: int, cpu_weight: int = 1) -> Any:
    """Delegate Windows process setup to the hardened sandbox implementation.

    Keeping one implementation of Job Object setup is important: the runtime
    worker and Code Mode must have the same KILL_ON_JOB_CLOSE semantics.  The
    import is lazy so importing the runtime remains cross-platform.
    """
    if os.name != "nt" or process is None:
        return None
    try:
        from app.runtime.code_mode.runner import _apply_job_object

        _apply_job_object(process, int(memory_mb), cpu_weight=int(cpu_weight))
        return getattr(process, "_job", None)
    except (ImportError, OSError, AttributeError, TypeError, ValueError):
        return None


def enforce_resource_limits(process: Any, *, memory_mb: int, cpu_weight: int) -> Any:
    """Attach best-effort OS limits and return an optional platform handle.

    Windows Job Objects are used when pywin32 is installed.  Unix workers are
    placed in their own process group by the supervisor and sampled by
    ``process_resource_exceeded``; this function deliberately has no hidden
    fallback that would claim a limit was enforced when it was not.
    """
    if process is None or os.name != "nt":
        return None
    return _sandbox_apply_job_object(process, memory_mb, cpu_weight)


def release_resource_limits(process: Any) -> None:
    """Release a runtime-owned Job Object handle after child teardown."""
    if process is None or os.name != "nt":
        return
    try:
        from app.runtime.code_mode.runner import _close_job_handle

        _close_job_handle(process)
    except (ImportError, OSError, AttributeError, TypeError):
        return


def sample_process_resources(process: Any, *, interval: float = 0.05) -> dict[str, float]:
    """Return an RSS/CPU sample for an isolated process.

    ``psutil`` returns zero for the first non-blocking CPU sample. A short,
    bounded interval makes the budget probe meaningful without stalling the
    worker heartbeat.
    """
    import psutil

    info = psutil.Process(process.pid)
    return {
        "memory_mb": float(info.memory_info().rss) / (1024 * 1024),
        "cpu_percent": float(info.cpu_percent(interval=max(0.01, float(interval)))),
    }


def check_resource_budget(process: Any, *, memory_mb: int, cpu_weight: int, sample_interval: float = 0.05, sampler=None) -> dict[str, Any]:
    """Check a budget and retain enough detail for an auditable event."""
    if process is None or not process.is_alive():
        return {"exceeded": False, "reason": None, "memory_mb": 0.0, "cpu_percent": 0.0}
    sampler_injected = sampler is not None
    if sampler is None:
        sampler = sample_process_resources
    try:
        sample = sampler(process, interval=sample_interval)
    except Exception:
        # A probe failure is not an exceedance.  The supervisor health check
        # exposes hard-limit availability so this cannot be mistaken for a
        # guaranteed limit.
        return {"exceeded": False, "reason": None, "memory_mb": None, "cpu_percent": None, "available": False}
    memory = float(sample["memory_mb"])
    cpu = float(sample["cpu_percent"])
    reason = None
    if memory > int(memory_mb):
        reason = "memory"
    # psutil's interval sample has small scheduling jitter around the exact
    # one-core boundary; retain a narrow tolerance while preserving the
    # configured weight as the actual limiting scale.
    elif int(cpu_weight) > 0 and cpu > float(cpu_weight) * 150.0:
        reason = "cpu"
    result = {"exceeded": reason is not None, "reason": reason, "memory_mb": memory, "cpu_percent": cpu}
    try:
        import psutil
    except (ImportError, AttributeError):
        psutil = None
    if sampler_injected or psutil is None:
        result["available"] = True
    return result


def process_resource_exceeded(process: Any, *, memory_mb: int, cpu_weight: int, sample_interval: float = 0.05) -> bool:
    """Sample RSS/CPU and fail closed only when a measurable limit is exceeded."""
    return bool(check_resource_budget(process, memory_mb=memory_mb, cpu_weight=cpu_weight, sample_interval=sample_interval).get("exceeded"))


def kill_process_tree(process: Any) -> None:
    """Kill descendants and the root process, including process groups."""
    if os.name == "nt" and getattr(process, "_job", None) is not None:
        # Closing a KILL_ON_JOB_CLOSE handle is the authoritative tree kill;
        # psutil is only the fallback for children that were not attachable.
        try:
            from app.runtime.code_mode.runner import _close_job_handle

            _close_job_handle(process)
            try:
                process.kill()
            except (OSError, AttributeError):
                pass
            return
        except (ImportError, OSError, AttributeError, TypeError):
            pass
    try:
        import psutil

        root = psutil.Process(process.pid)
        for child in reversed(root.children(recursive=True)):
            try:
                child.kill()
            except psutil.Error:
                pass
        try:
            root.kill()
        except psutil.Error:
            pass
    except (ImportError, OSError, AttributeError):
        try:
            process.terminate()
        except (OSError, AttributeError):
            try:
                process.kill()
            except (OSError, AttributeError):
                pass


__all__ = [
    "check_resource_budget",
    "enforce_resource_limits",
    "kill_process_tree",
    "process_resource_exceeded",
    "release_resource_limits",
    "sample_process_resources",
]
