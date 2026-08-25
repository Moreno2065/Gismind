"""Light-weight global metrics aggregator for agent orchestration.

NOTE: This is a process-level singleton — all concurrent sessions within the
same process share the same counter. For per-session metrics, create a
separate AgentMetrics instance instead of using the global singleton.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class AgentMetrics:
    verifier_calls: int = 0
    verifier_approvals: int = 0
    sub_task_durations_ms: list[int] = field(default_factory=list)

    @property
    def verify_hit_rate(self) -> float:
        if self.verifier_calls == 0:
            return 0.0
        return self.verifier_approvals / self.verifier_calls

    @property
    def avg_sub_task_duration_ms(self) -> float:
        if not self.sub_task_durations_ms:
            return 0.0
        return sum(self.sub_task_durations_ms) / len(self.sub_task_durations_ms)


_singleton_metrics = AgentMetrics()
_singleton_lock = threading.Lock()


def get_metrics() -> AgentMetrics:
    """Return the process-level metrics singleton (thread-safe)."""
    return _singleton_metrics


def increment_verifier_calls() -> None:
    with _singleton_lock:
        _singleton_metrics.verifier_calls += 1


def increment_verifier_approvals() -> None:
    with _singleton_lock:
        _singleton_metrics.verifier_approvals += 1


def add_sub_task_duration(duration_ms: int) -> None:
    with _singleton_lock:
        _singleton_metrics.sub_task_durations_ms.append(duration_ms)
