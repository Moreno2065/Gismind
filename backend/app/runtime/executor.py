"""Small runtime-only executor for compiled tasks.

This is deliberately narrower than the production dispatcher: it resolves a
contract's declared callable, validates parameters, serializes its result as
an artifact, and records the output/event through the Runtime stores.  It does
not schedule workers or consult Redis.
"""

from __future__ import annotations

import importlib
import inspect
import json
import multiprocessing
import os
import pickle
import uuid
from datetime import datetime, timezone
from typing import Any

from .contracts import ArtifactRef, CompiledTask, TaskOutputRef, ToolContract, ToolContractRegistry
from .db import utc_now
from .errors import TaskExecutionError, bounded_error_message, execution_error_code
from .output_protocol import ArtifactBatch, ArtifactPayload
from .process import kill_process_tree


def _resolve_callable(reference: str, field: str):
    if not isinstance(reference, str) or reference.count(":") != 1:
        raise ValueError(f"{field} must be an importable module:callable reference")
    module_name, callable_name = reference.split(":", 1)
    try:
        value = getattr(importlib.import_module(module_name), callable_name)
    except (ImportError, AttributeError) as exc:
        raise ValueError(f"{field} does not resolve to a callable") from exc
    if not callable(value):
        raise ValueError(f"{field} does not resolve to a callable")
    return value


def _subprocess_invoke(reference: str, kwargs: dict[str, Any], result_path: str) -> None:
    """Invoke an external handler in a killable child process.

    Runtime adapters are intentionally kept in-process (they bridge the
    existing synchronous GIS service and are covered by the runtime E2E
    tests); custom/external handlers use this boundary so timeout is a hard
    process boundary rather than a best-effort future cancellation.  The
    result is written to a staging file instead of a ``multiprocessing.Queue``:
    a GIS handler may return a large GeoDataFrame or byte payload and joining a
    child before draining a Queue can deadlock on its feeder thread.
    """
    temporary = f"{result_path}.tmp"
    try:
        value = _resolve_callable(reference, "handler_ref")(**kwargs)
        with open(temporary, "wb") as stream:
            pickle.dump(("ok", value), stream, protocol=pickle.HIGHEST_PROTOCOL)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, result_path)
    except BaseException as exc:  # child must always report a picklable value
        try:
            with open(temporary, "wb") as stream:
                pickle.dump(("error", {"message": bounded_error_message(f"{type(exc).__name__}: {exc}"), "error_code": execution_error_code(exc)}), stream, protocol=pickle.HIGHEST_PROTOCOL)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, result_path)
        except BaseException:
            # The parent reports a missing result file as a worker failure.
            pass
        finally:
            try:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            except OSError:
                pass


class RuntimeExecutor:
    """Execute one already compiled task through its declared contract."""

    def __init__(self, runtime, artifacts, registry: ToolContractRegistry | None = None):
        self.runtime = runtime
        self.artifacts = artifacts
        self.registry = registry or ToolContractRegistry.with_builtins()

    def _task_status(self, task_id: str) -> str:
        row = self.runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone())
        if row is None:
            raise ValueError(f"unknown task: {task_id}")
        return row[0]

    def assert_claim_current(self, claim, *, allow_terminal: bool = False) -> None:
        """Validate the persisted lease and attempt before any worker write."""
        if claim is None:
            return
        get = claim.get if isinstance(claim, dict) else lambda name, default=None: getattr(claim, name, default)
        task_id = get("task_id")
        owner = get("owner")
        token = get("token")
        generation = get("lease_generation")
        attempt_id = get("attempt_id", "")
        row = self.runtime._read(
            lambda c: c.execute(
                """
                SELECT t.lease_owner,t.lease_token,t.lease_generation,t.lease_expires_at,
                       t.status,a.attempt_id,a.lease_generation,a.status,a.executor_identity
                FROM tasks t LEFT JOIN task_attempts a
                  ON a.attempt_id=? AND a.task_id=t.task_id
                WHERE t.task_id=?
                """,
                (attempt_id, task_id),
            ).fetchone()
        )
        allowed_statuses = {"leased", "running", "validating"}
        if allow_terminal:
            allowed_statuses.update({"succeeded", "failed", "cancelled"})
        try:
            expiry = datetime.fromisoformat(str(row[3]).replace("Z", "+00:00")) if row is not None else None
        except (TypeError, ValueError):
            expiry = None
        if row is None or row[0] != owner or row[1] != token or int(row[2]) != int(generation or -1) or expiry is None or expiry <= datetime.now(timezone.utc) or row[4] not in allowed_statuses:
            raise RuntimeError("STALE_LEASE")
        attempt_statuses = {"leased", "running", "validating", "retry_wait"}
        if allow_terminal:
            attempt_statuses.update({"succeeded", "failed", "cancelled"})
        if attempt_id and (row[5] != attempt_id or int(row[6]) != int(generation or -1) or row[7] not in attempt_statuses or not row[8] or not str(row[8]).strip()):
            raise RuntimeError("STALE_LEASE")

    def _validate_task_scope(self, task_id: str, run_id: str, task_key: str) -> str:
        row = self.runtime._read(
            lambda c: c.execute(
                """
                SELECT t.task_key, p.run_id, p.plan_id
                FROM tasks t JOIN plans p ON p.plan_id=t.plan_id
                WHERE t.task_id=?
                """,
                (task_id,),
            ).fetchone()
        )
        if row is None:
            raise ValueError(f"unknown task: {task_id}")
        if row[1] != run_id:
            raise ValueError("task_id does not belong to run")
        if row[0] != task_key:
            raise ValueError("compiled task key does not match persisted task")
        return row[2]

    def _validate_persisted_compiled(self, task_id: str, compiled: CompiledTask) -> None:
        """Make the durable PlanIR authoritative at the execution boundary."""
        row = self.runtime._read(
            lambda c: c.execute(
                "SELECT t.task_json, p.ir_json FROM tasks t JOIN plans p ON p.plan_id=t.plan_id WHERE t.task_id=?",
                (task_id,),
            ).fetchone()
        )
        if row is None:
            raise ValueError(f"unknown task: {task_id}")
        persisted = None
        try:
            if row[1]:
                # ``plans.ir_json`` stores the compiler's canonical payload
                # (the digest and execution identity live in sibling columns),
                # rather than the expanded PlanIR envelope.
                canonical = json.loads(row[1])
                if not isinstance(canonical, dict) or not isinstance(canonical.get("tasks"), list):
                    raise ValueError("canonical PlanIR payload is malformed")
                persisted = next(
                    (CompiledTask.model_validate(candidate) for candidate in canonical["tasks"]
                     if isinstance(candidate, dict) and candidate.get("task_key") == compiled.task_key),
                    None,
                )
            elif row[0] and row[0] != "{}":
                persisted = CompiledTask.model_validate_json(row[0])
        except Exception as exc:
            raise ValueError("persisted compiled task is invalid") from exc
        if persisted is None:
            raise ValueError("task has no persisted compiled PlanIR")
        if persisted.model_dump(mode="json") != compiled.model_dump(mode="json"):
            raise ValueError("compiled task does not match persisted PlanIR")

    def _ensure_run_running(self, run_id: str, *, claim=None) -> None:
        run = self.runtime.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run: {run_id}")
        transitions = {
            "queued": ("planning", "ready", "running"),
            "planning": ("ready", "running"),
            "ready": ("running",),
            "running": (),
        }
        if run["status"] not in transitions:
            raise ValueError(f"run is not executable: {run['status']}")
        for status in transitions[run["status"]]:
            self.runtime.update_run_status(run_id, status, fence=claim)

    def _ensure_task_running(self, task_id: str, *, claim=None) -> None:
        if claim is not None:
            self.runtime.executor_start_task(claim)
            return
        status = self._task_status(task_id)
        if status == "ready":
            self.runtime.update_task_status(task_id, "leased")
            status = "leased"
        if status == "leased":
            self.runtime.update_task_status(task_id, "running")
            status = "running"
        if status != "running":
            raise ValueError(f"task is not executable: {status}")

    def _artifact_payload(self, artifact_id: str) -> dict[str, Any]:
        metadata = self.runtime.get_artifact(artifact_id)
        if metadata is None:
            raise ValueError(f"input artifact does not exist: {artifact_id}")
        path = self.artifacts.path_for(artifact_id)
        raw = path.read_bytes()
        try:
            data: Any = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = raw
        return {**metadata, "path": str(path), "data": data}

    def _resolve_input(self, value: Any, run_id: str | None = None, plan_id: str | None = None) -> Any:
        if isinstance(value, ArtifactRef):
            return self._artifact_payload(value.artifact_id)
        if isinstance(value, TaskOutputRef):
            if value.selection == "all":
                rows = self.runtime._read(
                    lambda c: c.execute(
                        """
                        SELECT o.artifact_id
                        FROM task_outputs o JOIN tasks t ON t.task_id=o.task_id
                        JOIN plans p ON p.plan_id=t.plan_id
                        WHERE t.task_key=? AND o.output_name=?
                          AND (? IS NULL OR p.run_id=?)
                          AND (? IS NULL OR p.plan_id=?)
                          AND t.status='succeeded'
                        ORDER BY o.ordinal
                        """,
                        (value.task_key, value.port, run_id, run_id, plan_id, plan_id),
                    ).fetchall()
                )
                if not rows:
                    raise ValueError(f"unresolved task output reference: {value.task_key}.{value.port}")
                return [self._artifact_payload(row[0]) for row in rows]
            row = self.runtime._read(
                lambda c: c.execute(
                    """
                    SELECT o.artifact_id
                    FROM task_outputs o JOIN tasks t ON t.task_id=o.task_id
                    JOIN plans p ON p.plan_id=t.plan_id
                    WHERE t.task_key=? AND o.output_name=?
                      AND (? IS NULL OR p.run_id=?)
                      AND (? IS NULL OR p.plan_id=?)
                      AND t.status='succeeded' AND o.ordinal=?
                    ORDER BY o.created_at DESC LIMIT 1
                    """,
                    (value.task_key, value.port, run_id, run_id, plan_id, plan_id, value.ordinal if value.ordinal is not None else 0),
                ).fetchone()
            )
            if row is None:
                raise ValueError(f"unresolved task output reference: {value.task_key}.{value.port}")
            return self._artifact_payload(row[0])
        if isinstance(value, list):
            return [self._resolve_input(item, run_id, plan_id) for item in value]
        return value

    def _check_side_effect_confirmation(self, task_id: str, run_id: str, compiled: CompiledTask, contract: ToolContract) -> None:
        if not contract.side_effect:
            return
        if not compiled.action_hash or contract.confirmation_policy.mode != "required":
            raise ValueError("CONFIRMATION_REQUIRED")
        confirmed = self.runtime._read(
            lambda c: c.execute(
                """
                SELECT 1
                FROM task_confirmations tc
                JOIN tasks t ON t.task_id=tc.task_id
                JOIN plans p ON p.plan_id=t.plan_id
                WHERE tc.task_id=? AND tc.status='confirmed' AND tc.action_hash=?
                  AND p.run_id=?
                LIMIT 1
                """,
                (task_id, compiled.action_hash, run_id),
            ).fetchone()
        )
        if confirmed is None:
            raise ValueError("CONFIRMATION_REQUIRED")

    @staticmethod
    def _status(result: Any) -> str:
        if result is None:
            return "error"
        if isinstance(result, dict):
            value = result.get("status")
        else:
            value = getattr(result, "status", None)
        if value is None:
            return "error"
        return str(value).lower()

    @staticmethod
    def _result_data(result: Any) -> Any:
        if isinstance(result, dict) and "data" in result:
            return result["data"]
        return getattr(result, "data", result)

    @staticmethod
    def _is_empty_payload(value: Any, output) -> bool:
        """Classify structural zero-result payloads without inspecting values.

        An empty list is a valid Table payload, while it is also the explicit
        zero-item representation for a many-cardinality output.  The declared
        Artifact type is therefore required to avoid treating non-empty
        business dictionaries as empty merely because they contain no generic
        ``items`` key.
        """
        if value is None:
            return True
        if isinstance(value, ArtifactBatch):
            return len(value) == 0
        if output is None:
            return False
        artifact_type = output.artifact_type
        if output.cardinality == "many" and isinstance(value, list):
            return len(value) == 0
        if artifact_type == "Vector" and isinstance(value, dict):
            features = value.get("features")
            return isinstance(features, list) and len(features) == 0
        if artifact_type == "Table" and isinstance(value, list):
            return len(value) == 0
        if artifact_type == "POISet" and isinstance(value, dict):
            rows = value.get("pois", value.get("results"))
            return isinstance(rows, list) and len(rows) == 0
        if artifact_type == "MapLayer" and isinstance(value, dict):
            layers = value.get("layers")
            return isinstance(layers, list) and len(layers) == 0
        if artifact_type == "File" and isinstance(value, (bytes, bytearray)):
            return len(value) == 0
        return False

    @classmethod
    def _result_is_empty(cls, data: Any, outputs) -> bool:
        if outputs:
            for output in outputs:
                value = data
                if isinstance(data, dict) and output.port in data:
                    value = data[output.port]
                if cls._is_empty_payload(value, output):
                    return True
            return False
        return cls._is_empty_payload(data, None)

    def _mark_failed(self, task_id: str, run_id: str, *, event_type: str, message: str, error_code: str = "TOOL_EXECUTION_FAILED", claim=None) -> None:
        try:
            self.assert_claim_current(claim)
            if self._task_status(task_id) in {"running", "validating"}:
                self.runtime.update_task_status(task_id, "failed", blocked_reason=message, fence=claim)
        except RuntimeError as exc:
            if str(exc) == "STALE_LEASE":
                return
            raise
        self.runtime.append_event(run_id, event_type, {"task_id": task_id, "message": message, "error_code": error_code})

    def _invoke_handler(self, handler_ref: str, handler, kwargs: dict[str, Any], timeout: float):
        if handler_ref.startswith(("app.runtime.contracts:", "app.runtime.tool_handlers:")):
            return handler(**kwargs)
        context = multiprocessing.get_context("spawn")
        result_path = self.artifacts.create_staging_path("handler-result.pkl")
        try:
            process = context.Process(target=_subprocess_invoke, args=(handler_ref, kwargs, str(result_path)), daemon=True)
            process.start()
            process.join(max(0.001, timeout))
            if process.is_alive():
                kill_process_tree(process)
                process.join(2)
                if process.is_alive():
                    process.kill()
                    process.join(2)
                raise TimeoutError(f"runtime handler timed out after {timeout}s")
            if not result_path.is_file():
                raise TaskExecutionError("WORKER_LOST", f"runtime handler exited without a result (exitcode={process.exitcode})")
            try:
                with result_path.open("rb") as stream:
                    kind, payload = pickle.load(stream)
            except (OSError, EOFError, pickle.PickleError, ValueError, TypeError) as exc:
                raise TaskExecutionError("WORKER_PROTOCOL_ERROR", "runtime handler result is unreadable") from exc
            if kind == "error":
                if not isinstance(payload, dict) or not isinstance(payload.get("message"), str):
                    raise TaskExecutionError("WORKER_PROTOCOL_ERROR", "runtime handler error result is unreadable")
                raise TaskExecutionError(execution_error_code(payload), payload["message"])
            if kind != "ok":
                raise TaskExecutionError("WORKER_PROTOCOL_ERROR", "runtime handler returned an unknown result marker")
            return payload
        finally:
            result_path.unlink(missing_ok=True)

    @staticmethod
    def _output_items(output, value: Any) -> list[Any]:
        """Normalize output ports using explicit payload/batch wrappers.

        A Table's natural representation is a list of row objects.  Therefore
        ordinary lists always remain one payload; only ``ArtifactBatch`` may
        expand into multiple Artifacts.
        """
        if isinstance(value, ArtifactPayload):
            if output is not None and output.cardinality == "many":
                raise ValueError("many output requires ArtifactBatch")
            return [value.value]
        if isinstance(value, ArtifactBatch):
            if output is None or output.cardinality == "one":
                raise ValueError("one output cannot return ArtifactBatch")
            items = list(value)
            if len(items) < output.min_items or (output.max_items is not None and len(items) > output.max_items):
                raise ValueError("output item count is outside the declared cardinality")
            return items
        if output is None or output.cardinality == "one":
            return [value]
        raise ValueError("many output requires ArtifactBatch")

    @staticmethod
    def _validate_vector_payload_crs(output, item: Any) -> None:
        """Reject a Vector payload whose physical CRS disagrees with metadata."""
        if output is None or output.artifact_type != "Vector" or not isinstance(item, dict):
            return
        declared = output.crs or {}
        kind = str(declared.get("kind", "")).upper() if isinstance(declared, dict) else ""
        code = declared.get("code") if isinstance(declared, dict) else None
        expected = "EPSG:4326" if kind == "WGS84" or (kind == "EPSG" and code == 4326) else f"EPSG:{code}" if kind == "EPSG" and isinstance(code, int) else None
        physical = item.get("_physical_crs")
        label = item.get("_crs_label")
        if kind == "GCJ02":
            if label != "GCJ02" or physical not in {None, "EPSG:4326", "WGS 84"}:
                raise ValueError("Vector payload CRS does not match declared GCJ02 metadata")
            return
        if label == "GCJ02":
            raise ValueError("Vector payload declares GCJ02 coordinates under a physical CRS metadata")
        if expected and physical and str(physical).upper().replace(" ", "") not in {expected.upper().replace(" ", ""), expected.upper().replace("EPSG:", "EPSG:")}:
            raise ValueError("Vector payload physical CRS does not match declared CRS metadata")

    @staticmethod
    def _input_artifact_ids(inputs: dict[str, Any]) -> list[str]:
        """Extract the immutable Artifact IDs that actually reached a Task."""
        found: list[str] = []

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                artifact_id = value.get("artifact_id")
                if isinstance(artifact_id, str) and artifact_id and artifact_id not in found:
                    found.append(artifact_id)
                for nested in value.values():
                    if nested is not artifact_id:
                        collect(nested)
            elif isinstance(value, list):
                for nested in value:
                    collect(nested)

        for value in inputs.values():
            collect(value)
        return found

    def execute(self, task: CompiledTask | dict[str, Any], *, run_id: str, task_id: str | None = None, claim=None,
                started_by_supervisor: bool = False) -> dict[str, Any]:
        compiled = task if isinstance(task, CompiledTask) else CompiledTask.model_validate(task)
        task_id = task_id or compiled.task_key
        self.assert_claim_current(claim)
        plan_id = self._validate_task_scope(task_id, run_id, compiled.task_key)
        self._validate_persisted_compiled(task_id, compiled)
        contract = self.registry.get(compiled.op, compiled.contract_version)
        self._check_side_effect_confirmation(task_id, run_id, compiled, contract)
        handler = _resolve_callable(contract.handler_ref, "handler_ref")
        validators = list(dict.fromkeys([*compiled.validators, contract.validator] if contract.validator else compiled.validators))

        self._ensure_run_running(run_id, claim=claim)
        if started_by_supervisor:
            # The parent reserved this attempt before launching a child. The
            # child validates that reservation instead of starting it again;
            # a second supervisor must not treat the same running attempt as
            # permission to invoke the handler twice.
            if claim is None or self._task_status(task_id) != "running":
                raise RuntimeError("STALE_LEASE")
        else:
            self._ensure_task_running(task_id, claim=claim)
        try:
            inputs = {name: self._resolve_input(value, run_id, plan_id) for name, value in compiled.inputs.items()}
        except Exception as exc:
            code = execution_error_code(exc, "VALIDATION_FAILED")
            self._mark_failed(task_id, run_id, event_type="task.failed", message=str(exc), error_code=code, claim=claim)
            raise TaskExecutionError(code, str(exc)) from exc
        parent_artifact_ids = self._input_artifact_ids(inputs)
        kwargs = dict(compiled.params)
        kwargs.update(inputs)
        # Validators must see the resolved artifact payloads as well as the
        # declared parameters.  Running them earlier allowed an adapter to
        # validate a parameter-only shape and then receive a different input.
        try:
            for reference in validators:
                validator = _resolve_callable(reference, "validator_ref")
                if validator(**kwargs) is not True:
                    raise ValueError(f"runtime parameter validation failed for {compiled.op}")
        except Exception as exc:
            code = execution_error_code(exc, "VALIDATION_FAILED")
            self._mark_failed(task_id, run_id, event_type="task.failed", message=str(exc), error_code=code, claim=claim)
            raise TaskExecutionError(code, str(exc)) from exc
        claim_attempt = claim is not None and (claim.get("attempt_id") if isinstance(claim, dict) else getattr(claim, "attempt_id", None))
        if isinstance(claim, dict):
            attempt_id = claim.get("attempt_id")
        else:
            attempt_id = getattr(claim, "attempt_id", None) if claim is not None else None
        if not attempt_id:
            attempt_id = f"{task_id}:{uuid.uuid4().hex}"
            try:
                self.runtime.create_task_attempt(attempt_id, task_id, 0, status="running")
            except Exception:
                attempt_id = None
        # Handler failures are deliberately returned to the Supervisor.  The
        # durable queue owns retry policy, backoff, and the persisted attempt
        # count; keeping a local loop here would reset the count on every
        # claim and permit more executions than the frozen CompiledTask policy.
        self.assert_claim_current(claim)
        result = self._invoke_handler(contract.handler_ref, handler, kwargs, float(compiled.resource_budget.timeout_seconds))
        status = self._status(result)
        empty_allowed = status == "empty" and contract.empty_policy == "allow"
        if status not in {"success", "ok"} and not empty_allowed:
            message = str((result.get("message") if isinstance(result, dict) else getattr(result, "message", None)) or status)
            code = execution_error_code(result, "TASK_EMPTY" if status == "empty" else "TOOL_EXECUTION_FAILED")
            self._mark_failed(task_id, run_id, event_type="task.empty" if status == "empty" else "task.failed", message=message, error_code=code, claim=claim)
            raise TaskExecutionError(code, f"task result is {status}: {message}")
        data = self._result_data(result)
        if self._result_is_empty(data, compiled.outputs) and contract.empty_policy != "allow":
            self._mark_failed(task_id, run_id, event_type="task.empty", message="handler returned an empty payload", error_code="TASK_EMPTY", claim=claim)
            raise TaskExecutionError("TASK_EMPTY", "task result is empty: handler returned an empty payload")
        self.assert_claim_current(claim)
        self.runtime.update_task_status(task_id, "validating", fence=claim)
        for reference in compiled.postconditions:
            try:
                check = _resolve_callable(reference, "postcondition")
                check_kwargs = {"result": result, "inputs": inputs, "params": compiled.params}
                # Runtime-owned semantic validators may use empty_policy to
                # distinguish a valid zero-row/provider response from a
                # malformed or silent result. Keep third-party extension
                # postconditions compatible with the original three args.
                try:
                    signature = inspect.signature(check)
                    if "empty_policy" in signature.parameters or any(
                        parameter.kind == inspect.Parameter.VAR_KEYWORD
                        for parameter in signature.parameters.values()
                    ):
                        check_kwargs["empty_policy"] = contract.empty_policy
                except (TypeError, ValueError):
                    pass
                valid = check(**check_kwargs)
            except Exception as exc:
                code = execution_error_code(exc, "VALIDATION_FAILED")
                self._mark_failed(
                    task_id,
                    run_id,
                    event_type="task.failed",
                    message=f"postcondition raised: {reference}: {exc}",
                    error_code=code,
                    claim=claim,
                )
                raise TaskExecutionError(code, f"postcondition raised: {reference}") from exc
            if valid is not True:
                self._mark_failed(task_id, run_id, event_type="task.failed", message=f"postcondition failed: {reference}", error_code="VALIDATION_FAILED", claim=claim)
                raise TaskExecutionError("VALIDATION_FAILED", f"postcondition failed: {reference}")
        payloads: list[tuple[str, int, bytes, Any]] = []
        try:
            for output in compiled.outputs or [None]:
                name = output.port if output else "output"
                value = data
                if output is not None and isinstance(data, dict) and output.port in data:
                    value = data[output.port]
                items = self._output_items(output, value)
                for ordinal, item in enumerate(items):
                    self._validate_vector_payload_crs(output, item)
                    payload = item if isinstance(item, bytes) else json.dumps(item, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False).encode("utf-8")
                    payloads.append((name, ordinal, payload, output))
        except Exception as exc:
            code = execution_error_code(exc, "VALIDATION_FAILED")
            self._mark_failed(task_id, run_id, event_type="task.failed", message=str(exc), error_code=code, claim=claim)
            if attempt_id and not claim_attempt:
                try:
                    self.runtime.update_task_attempt_status(attempt_id, "failed")
                except Exception:
                    pass
            raise TaskExecutionError(code, str(exc)) from exc
        if not payloads:
            if compiled.empty_policy != "allow":
                self._mark_failed(task_id, run_id, event_type="task.empty", message="handler returned no output", error_code="TASK_EMPTY", claim=claim)
                if attempt_id and not claim_attempt:
                    try:
                        self.runtime.update_task_attempt_status(attempt_id, "failed")
                    except Exception:
                        pass
                raise TaskExecutionError("TASK_EMPTY", "task result is empty: handler returned no output")
            with self.runtime.transaction():
                self.assert_claim_current(claim)
                if attempt_id:
                    self.runtime.update_task_attempt_status(attempt_id, "succeeded", fence=claim)
                self.runtime.update_task_status(task_id, "succeeded", validated=True, fence=claim)
                self.runtime.append_event(run_id, "task.empty", {"task_id": task_id, "allowed": True})
            return {"artifacts": [], "artifact": None, "result": result, "content_hash": None}
        artifacts = []
        try:
            # CAS files are immutable and can survive a failed transaction as
            # verified orphans. Metadata, lineage, output ports, task attempt,
            # terminal task state and all emitted events must instead become
            # visible together. ArtifactStore observes this active transaction
            # and never opens a partial nested commit.
            with self.runtime.transaction():
                for output_name, ordinal, payload, output in payloads:
                    self.assert_claim_current(claim)
                    # Opaque Files and Rasterio-backed Raster outputs must retain
                    # their binary physical representation. Other structured
                    # Runtime outputs are canonical JSON today.
                    file_metadata = output.schema.get("file", {}) if output is not None and output.artifact_type == "File" else {}
                    filename = (
                        file_metadata.get("filename") or f"{compiled.task_key}-{output_name}.bin"
                        if output is not None and output.artifact_type == "File"
                        else f"{compiled.task_key}-{output_name}.tif"
                        if output is not None and output.artifact_type == "Raster"
                        else f"{compiled.task_key}-{output_name}.json"
                    )
                    artifacts.append(self.artifacts.publish_staged(
                        self.artifacts.stage_bytes(payload, filename=filename),
                        origin="task", producer_task_id=task_id,
                        artifact_type=output.artifact_type if output else "File",
                        crs=output.crs if output else None,
                        schema=output.schema if output else {},
                        media_type=file_metadata.get("media_type"),
                        parents=parent_artifact_ids,
                        output_name=output_name,
                        output_ordinal=ordinal,
                        fence=claim,
                        event=(run_id, "artifact.published", {"task_id": task_id, "task_key": compiled.task_key, "op": compiled.op, "output": output_name, "artifact_type": output.artifact_type if output else "File"}),
                    ))
                if attempt_id:
                    self.runtime.update_task_attempt_status(attempt_id, "succeeded", fence=claim)
                self.runtime.update_task_status(task_id, "succeeded", validated=True, fence=claim)
        except Exception as exc:
            self._mark_failed(task_id, run_id, event_type="task.failed", message=str(exc), claim=claim)
            raise
        return {"artifacts": artifacts, "artifact": artifacts[0], "result": result, "content_hash": artifacts[0]["content_hash"]}

__all__ = ["RuntimeExecutor"]
