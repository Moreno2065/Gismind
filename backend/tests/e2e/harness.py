"""Drive one corpus case through the real production Runtime chain."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from app.runtime import (
    ArtifactRef,
    ArtifactStore,
    PlannerDraft,
    PlannerStepDraft,
    RuntimeDB,
    RuntimeStore,
    RuntimeSupervisor,
    TaskOutputRef,
)
from app.runtime.llm_planner import PlannerRequest

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


@dataclass(frozen=True)
class FixtureSpec:
    """One published input Artifact."""

    name: str
    artifact_type: str
    build: Callable[[], bytes]
    crs: dict[str, Any] | None = None
    schema: dict[str, Any] | None = None
    filename: str = "input.bin"


@dataclass(frozen=True)
class FixtureRef:
    name: str


@dataclass(frozen=True)
class StepRef:
    task_key: str
    port: str


@dataclass(frozen=True)
class StepSpec:
    task_key: str
    op: str
    inputs: dict[str, FixtureRef | StepRef | list[FixtureRef]] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class E2ECase:
    case_id: str
    category: str
    user_goal: str
    fixtures: tuple[FixtureSpec, ...] = ()
    run_inputs: tuple[str, ...] = ()
    steps: tuple[StepSpec, ...] = ()
    expect: str = "success"
    check: Callable[["CaseResult"], None] | None = None
    external: bool = False
    notes: str = ""

    @property
    def operations(self) -> tuple[str, ...]:
        return tuple(step.op for step in self.steps)


@dataclass(frozen=True)
class CaseResult:
    """Everything a semantic assertion is allowed to look at."""

    case: E2ECase
    run_id: str
    run: dict[str, Any]
    tasks: tuple[dict[str, Any], ...]
    events: tuple[dict[str, Any], ...]
    output_artifacts: tuple[dict[str, Any], ...]
    outcome: str
    codes: tuple[str, ...]
    error: str | None
    failure_messages: tuple[str, ...] = ()
    _paths: dict[str, Path] = field(default_factory=dict)

    def payload(self, index: int = 0) -> bytes:
        """Read the CAS bytes of the Nth published output Artifact."""
        artifact = self.output_artifacts[index]
        return self._paths[artifact["artifact_id"]].read_bytes()

    def json(self, index: int = 0) -> Any:
        return json.loads(self.payload(index).decode("utf-8"))

    def artifact_type(self, index: int = 0) -> str:
        return str(self.output_artifacts[index].get("type", ""))

    def by_type(self, artifact_type: str) -> dict[str, Any]:
        for artifact in self.output_artifacts:
            if artifact.get("type") == artifact_type:
                return artifact
        raise AssertionError(f"no published {artifact_type} artifact in {[a.get('type') for a in self.output_artifacts]}")

    def payload_of(self, artifact_type: str) -> bytes:
        return self._paths[self.by_type(artifact_type)["artifact_id"]].read_bytes()

    def json_of(self, artifact_type: str) -> Any:
        return json.loads(self.payload_of(artifact_type).decode("utf-8"))

    def task_statuses(self) -> dict[str, str]:
        return {str(task["task_key"]): str(task["status"]) for task in self.tasks}

    def codes_text(self) -> str:
        return ",".join(self.codes)


class DeterministicPlanner:
    """A contract-shaped planner double for the single LLM boundary.

    It returns the exact ``PlannerDraft`` a case declares and performs no GIS.
    Catalog recall, compilation, execution and validation remain real.
    """

    provider = "e2e-deterministic"
    model = "e2e-corpus-v1"
    prompt_version = "e2e-corpus-v1"

    def __init__(self, draft: PlannerDraft):
        self._draft = draft

    def create_draft(self, request: PlannerRequest) -> PlannerDraft:
        return self._draft


def _resolve_ref(ref: FixtureRef | StepRef, published: dict[str, dict[str, Any]]) -> Any:
    if isinstance(ref, FixtureRef):
        artifact = published[ref.name]
        return ArtifactRef(
            artifact_id=artifact["artifact_id"],
            type=artifact["type"],
            crs=artifact.get("crs"),
        )
    return TaskOutputRef(task_key=ref.task_key, port=ref.port)


def _decode_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return {}
    return payload if isinstance(payload, dict) else {}


def _event_codes(events: Sequence[dict[str, Any]]) -> list[str]:
    codes: list[str] = []
    for event in events:
        payload = _decode_payload(event)
        if not payload:
            continue
        for key in ("code", "error_code", "diagnostic_code", "reason"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                codes.append(value)
        nested = payload.get("diagnostic")
        if isinstance(nested, dict):
            value = nested.get("code")
            if isinstance(value, str) and value:
                codes.append(value)
    return codes


def run_case(case: E2ECase, root: Path) -> CaseResult:
    """Execute one case through Session -> Plan -> Worker -> Artifact -> Run."""
    root.mkdir(parents=True, exist_ok=True)
    runtime = RuntimeStore(RuntimeDB(root / "runtime.db"))
    runtime.initialize()
    session_id = "sess_e2e"
    runtime.create_session(session_id)
    artifacts = ArtifactStore(root / "cas", runtime)

    published: dict[str, dict[str, Any]] = {}
    for spec in case.fixtures:
        staged = artifacts.stage_bytes(spec.build(), filename=spec.filename)
        published[spec.name] = artifacts.publish_staged(
            staged,
            origin="upload",
            artifact_type=spec.artifact_type,
            crs=spec.crs,
            schema=spec.schema or {},
        )

    run_input_ids = [published[name]["artifact_id"] for name in case.run_inputs]
    steps = [
        PlannerStepDraft(
            task_key=step.task_key,
            op=step.op,
            inputs={
                port: (
                    [_resolve_ref(item, published) for item in value]
                    if isinstance(value, list)
                    else _resolve_ref(value, published)
                )
                for port, value in step.inputs.items()
            },
            params=dict(step.params),
        )
        for step in case.steps
    ]
    draft = PlannerDraft(
        run_id=None,
        input_revision=1,
        user_goal=case.user_goal,
        tasks=steps,
        external_inputs=[
            ArtifactRef(artifact_id=published[name]["artifact_id"], type=published[name]["type"], crs=published[name].get("crs"))
            for name in case.run_inputs
        ],
    )

    run_id = f"run_{uuid.uuid4().hex}"
    runtime.create_run_with_input(
        run_id,
        session_id,
        message=case.user_goal,
        input_artifact_ids=run_input_ids,
        command_id=f"cmd_{uuid.uuid4().hex}",
        idempotency_key=f"idem_{case.case_id}",
        command_payload={"request_hash": f"hash_{case.case_id}", "response": {}},
    )

    supervisor = RuntimeSupervisor(
        runtime,
        artifacts,
        planner=DeterministicPlanner(draft),
        isolate=False,
        autostart=False,
    )
    error: str | None = None
    try:
        supervisor.worker_loop(
            "worker-e2e",
            run_id=run_id,
            max_tasks=len(case.steps) + 1,
            poll_interval=0.001,
        )
    except Exception as exc:  # a worker crash is itself a defect worth recording
        error = f"{type(exc).__name__}: {exc}"

    run = runtime.get_run(run_id) or {}
    task_rows = runtime._read(
        lambda c: [
            dict(row)
            for row in c.execute(
                """SELECT t.task_id,t.task_key,t.tool_name,t.status,t.blocked_reason
                   FROM tasks t JOIN plans p ON p.plan_id=t.plan_id
                   WHERE p.run_id=? ORDER BY t.task_key""",
                (run_id,),
            ).fetchall()
        ]
    )
    events = tuple(runtime.list_events(run_id))
    output_rows = runtime._read(
        lambda c: [
            dict(row)
            for row in c.execute(
                """SELECT DISTINCT a.* FROM task_outputs o
                   JOIN tasks t ON t.task_id=o.task_id JOIN plans p ON p.plan_id=t.plan_id
                   JOIN artifacts a ON a.artifact_id=o.artifact_id
                   WHERE p.run_id=? ORDER BY o.created_at,o.artifact_id""",
                (run_id,),
            ).fetchall()
        ]
    )
    paths = {row["artifact_id"]: artifacts.path_for(row["artifact_id"]) for row in output_rows}
    codes = tuple(dict.fromkeys(_event_codes(events)))
    failure_messages = tuple(
        str(_decode_payload(event).get("message"))
        for event in events
        if event["event_type"] in {"task.failed", "run.failed", "task.empty"}
        and _decode_payload(event).get("message")
    )

    status = str(run.get("status", "unknown"))
    if status == "completed":
        outcome = "success"
    elif status == "awaiting_input":
        outcome = "awaiting_input"
    elif status == "failed":
        outcome = "compile_reject" if "OPERATION_NOT_IN_CATALOG" in codes else "failed"
    elif status == "cancelled":
        outcome = "cancelled"
    else:
        outcome = f"stuck:{status}"
    if error is not None:
        outcome = "worker_crash"

    return CaseResult(
        case=case,
        run_id=run_id,
        run=run,
        tasks=tuple(task_rows),
        events=events,
        output_artifacts=tuple(output_rows),
        outcome=outcome,
        codes=codes,
        error=error,
        failure_messages=failure_messages,
        _paths=paths,
    )
