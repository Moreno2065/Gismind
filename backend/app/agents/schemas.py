"""Pydantic data models for sub-agent orchestration (Phase 1 / Task 1.3).

These models define the schemas used by SubAgentState, AgentRootState, and the
consensus / verification / refinement sub-system.

PendingTask is the dataclass persisted to Redis when a sub-agent pauses awaiting
user input (e.g. preflight ``ask_user`` or ``confirm_overwrite`` repair kinds).
The primary identifier is ``sub_agent_run_id`` (NOT ``task_id``) so that the
resume flow can target the exact sub-agent run that emitted the pause.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# PendingTask — dataclass for awaiting-input flow (段 4 / Task 5)
# ---------------------------------------------------------------------------

@dataclass
class PendingTask:
    """一次挂起的用户输入请求。

    主键使用 ``sub_agent_run_id``（而非 ``task_id``），因为在多 sub-agent
    并行模式下，同一 ``task_id`` 可能对应多个 run_id；resume
    只能精确恢复其中之一。Redis 序列化为 JSON，TTL 24h。

    Attributes:
        sub_agent_run_id: sub-agent 唯一 run_id，frontend resume 时回带。
        original_request: 原始用户输入（用于上下文回填）。
        missing_slots: 需要用户填入的 slot 名列表（如 ``["distance", "output_path"]``）。
        candidates: 多候选让用户选择（如 disambiguation）。
        slot_patch_schema: 用户补全 slot 的 JSON Schema。
        choices: verifier 提供的可选输入。
        correction_history: 用户输入修正历史。
        message: 给用户的中文提示。
        issues: 触发的 ValidationIssue 列表（to_dict 形式）。
        created_at: ISO8601 时间戳（UTC）。
    """

    sub_agent_run_id: str
    original_request: str
    missing_slots: list[str] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    slot_patch_schema: dict[str, Any] = field(default_factory=dict)
    choices: list[dict[str, Any]] = field(default_factory=list)
    correction_history: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""
    issues: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "sub_agent_run_id": self.sub_agent_run_id,
            "original_request": self.original_request,
            "missing_slots": list(self.missing_slots),
            "candidates": list(self.candidates),
            "slot_patch_schema": dict(self.slot_patch_schema),
            "choices": list(self.choices),
            "correction_history": list(self.correction_history),
            "message": self.message,
            "issues": list(self.issues),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PendingTask:
        return cls(
            **{k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        )


# ---------------------------------------------------------------------------
# Pydantic models (existing)
# ---------------------------------------------------------------------------


class PlanInstruction(BaseModel):
    """One atomic user instruction covered by the workflow DAG."""

    id: str
    text: str


class SubTask(BaseModel):
    """A single task within a TaskPlan, assigned to one sub-agent role."""

    id: str
    agent_role: str
    goal: str
    depends_on: list[str] = Field(default_factory=list)
    expected_artifacts: list[str] = Field(default_factory=list)
    instruction_id: Optional[str] = None
    tool_name: Optional[str] = None
    # Root-owned exact values (for example a user-specified distance limit)
    # are applied after the native planner fills schema references. This keeps
    # non-negotiable constraints deterministic instead of trusting model recall.
    tool_args: dict[str, Any] = Field(default_factory=dict)


class TaskPlan(BaseModel):
    """Validated workflow DAG produced by the root planner.

    ``instructions`` is optional only for backward-compatible checkpoint/test
    replay. New planner output always supplies it so every atomic instruction
    has an explicit owner in the DAG.
    """

    instructions: list[PlanInstruction] = Field(default_factory=list)
    tasks: list[SubTask]

    @model_validator(mode="after")
    def validate_dag_and_instruction_coverage(self) -> "TaskPlan":
        task_ids = [task.id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("workflow DAG contains duplicate task ids")

        known_tasks = set(task_ids)
        indegree = {task.id: 0 for task in self.tasks}
        outgoing: dict[str, list[str]] = {task.id: [] for task in self.tasks}
        for task in self.tasks:
            if task.id in task.depends_on:
                raise ValueError(f"workflow DAG cycle: task {task.id!r} depends on itself")
            for dep in task.depends_on:
                if dep not in known_tasks:
                    raise ValueError(
                        f"workflow DAG task {task.id!r} depends on unknown task {dep!r}"
                    )
                indegree[task.id] += 1
                outgoing[dep].append(task.id)

        ready = [task_id for task_id, degree in indegree.items() if degree == 0]
        visited = 0
        while ready:
            current = ready.pop()
            visited += 1
            for child in outgoing[current]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
        if visited != len(self.tasks):
            raise ValueError("workflow DAG contains a dependency cycle")

        if self.instructions:
            instruction_ids = [item.id for item in self.instructions]
            if len(instruction_ids) != len(set(instruction_ids)):
                raise ValueError("workflow plan contains duplicate instruction ids")
            known_instructions = set(instruction_ids)
            covered: set[str] = set()
            for task in self.tasks:
                if not task.instruction_id:
                    raise ValueError(
                        f"workflow DAG task {task.id!r} has no instruction_id"
                    )
                if task.instruction_id not in known_instructions:
                    raise ValueError(
                        f"workflow DAG task {task.id!r} references unknown instruction "
                        f"{task.instruction_id!r}"
                    )
                covered.add(task.instruction_id)
            missing = known_instructions - covered
            if missing:
                raise ValueError(
                    f"atomic instructions not covered by workflow DAG: {sorted(missing)}"
                )
        return self


class VerifierOutput(BaseModel):
    """Result of a verifier check on a sub-agent's output."""

    approved: bool
    reason: str
    refinement_hints: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    needs_input: bool = False
    missing_slots: list[str] = Field(default_factory=list)
    choices: list[dict[str, Any]] = Field(default_factory=list)
    input_reason: Optional[str] = None
    verifier_unavailable: bool = False
    invalid_rejection: bool = False


class RefineNote(BaseModel):
    """Record of one refinement iteration applied to a sub-agent run."""

    iteration: int
    verifier_reason: str
    refinement_hints: list[str]
    applied: bool


class SubAgentOutcome(BaseModel):
    """The final outcome of a single sub-agent run (one SubTask executed)."""

    task_id: str
    run_id: str
    agent_role: str
    status: Literal["success", "refined", "failed", "awaiting_input"]
    artifacts: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int = 0
    iteration_used: int = 0
    verifier_output: Optional[VerifierOutput] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    pending_task: Optional[dict[str, Any]] = None
