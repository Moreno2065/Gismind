"""Deterministic PlannerDraft -> PlanIR compiler.

This module performs validation and freezes execution policy only. It has no
database, LLM, dispatcher, or tool execution dependency.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
from collections.abc import Callable
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .contracts import (
    ArtifactRef,
    CompiledTask,
    InputRef,
    OutputSpec,
    PlanIR,
    PlannerDraft,
    PortContract,
    TaskOutputRef,
    ToolContract,
    ToolContractRegistry,
    FILE_EXPORT_FORMATS,
    validate_crs_value,
)
from .planner import PlanDraftInvalid, parse_planner_draft


class CompileError(ValueError):
    def __init__(self, code: str, path: str, message: str, details: dict[str, Any] | None = None):
        self.code = code
        self.path = path
        self.message = message
        self.details = details or {}
        super().__init__(f"{code} at {path}: {message}")


class AwaitingInputDiagnostic(CompileError):
    """A deterministic missing/ambiguous input that requires user input."""


_EXPORT_FORMAT_TOKEN = re.compile(
    r"(?<![a-z0-9])(?:geo[ -]?package|geo[ -]?json|gpkg|csv|shapefile|shp|kml|gml|geoparquet|parquet|xlsx|xls|json)(?![a-z0-9])",
    re.IGNORECASE,
)
_EXPORT_VERB = re.compile(r"导出|另存(?:为)?|保存(?:为)?|输出|下载|转(?:换)?(?:为|成)|export|save|write|download", re.IGNORECASE)


def _requested_export_formats(goal: str) -> set[str]:
    """Read explicit export format words from the trusted request text."""
    aliases = {"geopackage": "gpkg", "geo package": "gpkg", "geo-json": "geojson", "geo json": "geojson"}
    found: set[str] = set()
    for clause in re.split(r"[，,。；;!！?？\n]", goal.lower()):
        for verb in _EXPORT_VERB.finditer(clause):
            following = clause[verb.end():verb.end() + 40]
            next_verb = _EXPORT_VERB.search(following)
            if next_verb:
                following = following[:next_verb.start()]
            for match in _EXPORT_FORMAT_TOKEN.finditer(following):
                found.add(aliases.get(match.group().lower(), match.group().lower().replace(" ", "").replace("-", "")))
            explicit_name = re.match(r"\s*(?:为|成|至|到)\s*\.?\s*([a-z][a-z0-9+_-]{1,15})(?=\s|$|格式|文件|图片|图像)", following)
            if explicit_name:
                found.add(aliases.get(explicit_name.group(1), explicit_name.group(1)))
            explicit_english = re.match(r"\s*(?:as|to)\s+\.?([a-z][a-z0-9+_-]{1,15})(?:\b|$)", following)
            if explicit_english:
                found.add(aliases.get(explicit_english.group(1), explicit_english.group(1)))
        for match in _EXPORT_FORMAT_TOKEN.finditer(clause):
            suffix = clause[match.end():match.end() + 8]
            if re.match(r"\s*(?:格式|文件)\s*(?:导出|保存|输出)", suffix):
                found.add(aliases.get(match.group().lower(), match.group().lower().replace(" ", "").replace("-", "")))
    return found


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CompileError("INVALID_PARAMETER", "params", "numbers must be finite")
        if value == 0:
            return 0
        return int(value) if value.is_integer() else value
    return value


def _hash(value: Any) -> str:
    payload = json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_crs_metadata(value: Any) -> dict[str, Any] | None:
    """Convert accepted string CRS spellings to canonical Runtime metadata."""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip().upper().replace(" ", "")
    if text == "WGS84":
        return {"kind": "WGS84"}
    if text in {"EPSG:4326", "4326"}:
        return {"kind": "EPSG", "code": 4326}
    if text == "GCJ02":
        return {"kind": "GCJ02"}
    if text.startswith("EPSG:") and text[5:].isdigit() and int(text[5:]) > 0:
        return {"kind": "EPSG", "code": int(text[5:])}
    return None


class Compiler:
    def __init__(
        self,
        registry: ToolContractRegistry | None = None,
        *,
        artifact_lookup: Callable[[str], dict[str, Any] | None] | None = None,
        auto_reproject: bool = False,
        target_crs: dict[str, Any] | None = None,
        policy: dict[str, Any] | None = None,
        compiler_version: str = "1.0.0",
    ):
        self.registry = registry or ToolContractRegistry.with_builtins()
        self.artifact_lookup = artifact_lookup
        self.auto_reproject = auto_reproject
        self.target_crs = target_crs
        self.policy = policy or {}
        self.compiler_version = compiler_version

    def compile(
        self,
        draft: PlannerDraft | dict[str, Any] | str,
        *,
        auto_reproject: bool | None = None,
        target_crs: dict[str, Any] | None = None,
    ) -> PlanIR:
        try:
            parsed = parse_planner_draft(draft)
        except PlanDraftInvalid as exc:
            raise CompileError("PLAN_DRAFT_INVALID", "draft", exc.message, exc.details) from exc
        except Exception as exc:
            raise CompileError("PLAN_DRAFT_INVALID", "draft", "planner draft does not satisfy the Draft schema", {"error": str(exc)}) from exc
        steps = list(parsed.tasks)
        trusted_goal = self.policy.get("trusted_user_goal")
        requested_export_formats = _requested_export_formats(trusted_goal) if isinstance(trusted_goal, str) else set()
        if requested_export_formats:
            if len(requested_export_formats) != 1 or not requested_export_formats.issubset(FILE_EXPORT_FORMATS):
                raise CompileError("INVALID_PARAMETER", "tasks.file.export.params.format", "requested export format is unsupported or ambiguous", {"requested_formats": sorted(requested_export_formats), "supported_formats": sorted(FILE_EXPORT_FORMATS)})
            if not any(self.registry.canonical_name(step.op) == "file.export" for step in steps):
                raise CompileError("INVALID_PARAMETER", "tasks", "explicit export request requires a file.export task", {"requested_format": next(iter(requested_export_formats))})
        if not steps:
            raise CompileError("EMPTY_PLAN", "tasks", "empty plan is not executable")
        allowed_operation_ids = list(parsed.allowed_operation_ids)
        if allowed_operation_ids or parsed.catalog_hash is not None:
            if not allowed_operation_ids or not parsed.catalog_hash:
                raise CompileError("CATALOG_METADATA_INVALID", "draft", "catalog operation IDs and hash must be provided together")
            try:
                # Keep catalog hashing in the planner boundary module so the
                # hash covers the exact prompt metadata (including hints),
                # while avoiding an import cycle at module initialization.
                from .llm_planner import canonical_operation_ids, catalog_hash_for_operation_ids
                allowed_operation_ids = list(canonical_operation_ids(self.registry, allowed_operation_ids))
                expected_catalog_hash = catalog_hash_for_operation_ids(self.registry, allowed_operation_ids)
            except (KeyError, ValueError) as exc:
                raise CompileError("CATALOG_METADATA_INVALID", "draft.catalog_hash", "catalog contains an unknown operation") from exc
            if parsed.catalog_hash != expected_catalog_hash:
                raise CompileError("CATALOG_HASH_MISMATCH", "draft.catalog_hash", "catalog hash does not match operation IDs")
            allowed_set = set(allowed_operation_ids)
            for step in steps:
                canonical_step_op = self.registry.canonical_name(step.op)
                if canonical_step_op not in allowed_set:
                    raise CompileError(
                        "OPERATION_NOT_IN_CATALOG", f"tasks.{step.task_key}.op",
                        f"operation is outside the planner catalog: {step.op}",
                        {"operation": canonical_step_op, "allowed_operation_ids": sorted(allowed_set), "catalog_hash": parsed.catalog_hash},
                    )
        for index, ref in enumerate(parsed.external_inputs):
            self._validate_artifact_ref(ref, f"external_inputs[{index}]")
            self._validate_crs(ref.crs, f"external_inputs[{index}]")
        if len({step.task_key for step in steps}) != len(steps):
            raise CompileError("DUPLICATE_TASK_KEY", "tasks", "task_key must be unique")
        auto_reproject = self.auto_reproject if auto_reproject is None else auto_reproject
        target_crs = self.target_crs if target_crs is None else target_crs
        selector_version = self.policy.get("metric_crs_selector_version", "1.0.0")
        if auto_reproject and target_crs is None and self.policy.get("metric_bbox") is not None:
            target_crs = metric_crs_selector(self.policy["metric_bbox"], self.policy.get("metric_crs_candidates", []), version=selector_version)
        contracts: dict[str, ToolContract] = {}
        for step in steps:
            try:
                contracts[step.task_key] = self.registry.get(step.op)
            except KeyError:
                raise CompileError("UNKNOWN_TOOL", f"tasks.{step.task_key}.op", f"unknown tool: {step.op}") from None

        normalized_inputs: dict[str, dict[str, InputRef | list[InputRef]]] = {}
        for step in steps:
            contract = contracts[step.task_key]
            normalized_inputs[step.task_key] = {}
            raw_inputs = dict(step.inputs)
            # Explicit compatibility spelling for pre-contract drafts; the IR
            # remains canonical ``map.build.vector``.
            if step.op == "map.build" and "layer" in raw_inputs and "vector" not in raw_inputs:
                raw_inputs["vector"] = raw_inputs.pop("layer")
            port_details = {
                "operation": contract.name,
                "allowed_ports": sorted(contract.input_ports),
                "required_ports": sorted(name for name, port in contract.input_ports.items() if port.required),
                "provided_ports": sorted(raw_inputs),
                "missing_ports": sorted(name for name, port in contract.input_ports.items() if port.required and name not in raw_inputs),
                "unexpected_ports": sorted(set(raw_inputs) - set(contract.input_ports)),
            }
            for port_name, raw in raw_inputs.items():
                if port_name not in contract.input_ports:
                    raise CompileError("PORT_MISMATCH", f"tasks.{step.task_key}.inputs.{port_name}", "input port is not declared by the contract", port_details)
                normalized_inputs[step.task_key][port_name] = self._normalize_input(raw, f"tasks.{step.task_key}.inputs.{port_name}")
                self._validate_input_shape(normalized_inputs[step.task_key][port_name], contract.input_ports[port_name], f"tasks.{step.task_key}.inputs.{port_name}")
            missing_ports = sorted(name for name, port in contract.input_ports.items() if port.required and name not in normalized_inputs[step.task_key])
            if missing_ports:
                raise CompileError("PORT_MISMATCH", f"tasks.{step.task_key}.inputs", f"required input port is missing: {missing_ports[0]}", port_details)

        self._validate_graph(steps, normalized_inputs)
        ordered_steps = self._topological_step_order(steps, normalized_inputs)
        compiled: list[CompiledTask] = []
        # Resolve declared output ports for every task before validating any
        # reference. Draft order is not semantic, so a consumer may appear
        # before its producer in the input JSON.
        task_outputs: dict[str, dict[str, OutputSpec]] = {
            step.task_key: {
                name: OutputSpec(
                    port=name,
                    artifact_type=port.artifact_type,
                    cardinality=port.cardinality,
                    min_items=port.min_items,
                    max_items=port.max_items,
                    crs=None,
                    schema=port.schema,
                )
                for name, port in contract.output_ports.items()
            }
            for step, contract in ((step, contracts[step.task_key]) for step in ordered_steps)
        }
        for step in ordered_steps:
            contract = contracts[step.task_key]
            inputs = normalized_inputs[step.task_key]
            for port_name, value in inputs.items():
                port = contract.input_ports[port_name]
                values = value if isinstance(value, list) else [value]
                for index, ref in enumerate(values):
                    self._validate_ref(ref, port, f"tasks.{step.task_key}.inputs.{port_name}[{index}]", task_outputs)
                    if isinstance(ref, ArtifactRef):
                        self._validate_artifact_ref(ref, f"tasks.{step.task_key}.inputs.{port_name}[{index}]", port)
                        self._validate_crs(ref.crs, f"tasks.{step.task_key}.inputs.{port_name}[{index}]")
                        self._validate_port_crs(ref.crs, port, f"tasks.{step.task_key}.inputs.{port_name}[{index}]")
                    else:
                        source_crs = self._task_output_crs(ref, task_outputs)
                        self._validate_port_crs(source_crs, port, f"tasks.{step.task_key}.inputs.{port_name}[{index}]")
            for port_name, port in contract.input_ports.items():
                same_as = (port.crs_policy or {}).get("same_as")
                if not same_as or port_name not in inputs or same_as not in inputs:
                    continue
                left = inputs[port_name]
                right = inputs[same_as]
                left_crs = self._input_crs_sequence(left, task_outputs)
                right_crs = self._input_crs_sequence(right, task_outputs)
                if len(left_crs) == 1 and len(right_crs) > 1:
                    left_crs = left_crs * len(right_crs)
                elif len(right_crs) == 1 and len(left_crs) > 1:
                    right_crs = right_crs * len(left_crs)
                if len(left_crs) != len(right_crs) or any(left != right for left, right in zip(left_crs, right_crs) if left is not None and right is not None):
                    raise CompileError("CRS_CONFLICT", f"tasks.{step.task_key}.inputs.{port_name}", "inputs requiring the same CRS disagree")
            params = self._normalize_params(step.params, contract, f"tasks.{step.task_key}.params")
            if contract.name == "file.export" and requested_export_formats:
                requested_format = next(iter(requested_export_formats))
                if params["format"] != requested_format:
                    raise CompileError("INVALID_PARAMETER", f"tasks.{step.task_key}.params.format", "export format contradicts the user request", {"requested_format": requested_format, "planned_format": params["format"]})
            if contract.name == "code.execute":
                self._validate_code_input_declarations(params, inputs, f"tasks.{step.task_key}")
            self._validate_side_effect_params(contract, params, f"tasks.{step.task_key}.params")
            for parameter_name in ("source_crs", "target_crs"):
                parameter_crs = params.get(parameter_name)
                if isinstance(parameter_crs, dict):
                    self._validate_crs(parameter_crs, f"tasks.{step.task_key}.params.{parameter_name}")
            outputs = self._output_specs(contract, params, inputs, step.task_key, task_outputs)
            task_outputs[step.task_key] = {output.port: output for output in outputs}
            compiled.append(self._compiled_task(step, contract, inputs, params, outputs, op=self.registry.canonical_name(step.op)))

        if auto_reproject and target_crs is not None:
            compiled = self._insert_reprojection(compiled, target_crs)
        compiled = [self._refresh_execution_identity(task) for task in compiled]
        # Conversion tasks are real DAG nodes.  Preserve a deterministic
        # producer-before-consumer order in the serialized IR even when the
        # draft (or the compiler-generated node) was encountered in another
        # order.
        compiled = self._topological_order(compiled)
        self._validate_budget(compiled)
        input_ids = sorted({ref.artifact_id for task in compiled for ref in self._iter_refs(task.inputs) if isinstance(ref, ArtifactRef)})
        input_ids = sorted(set(input_ids) | {ref.artifact_id for ref in parsed.external_inputs})
        try:
            payload = {
                "ir_version": "1",
                "compiler_version": self.compiler_version,
                "metric_crs_selector_version": selector_version,
                "input_revision": parsed.input_revision,
                "user_goal": parsed.user_goal,
                "input_artifact_ids": input_ids,
                "allowed_operation_ids": sorted(allowed_operation_ids),
                "catalog_hash": parsed.catalog_hash,
                "tasks": [task.model_dump(mode="json") for task in sorted(compiled, key=lambda item: item.task_key)],
                "bindings": sorted((_canonical(binding) for binding in parsed.bindings), key=lambda binding: json.dumps(binding, sort_keys=True, separators=(",", ":"))),
            }
            canonical_json = json.dumps(_canonical(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except CompileError:
            raise
        except Exception as exc:
            raise CompileError("INVALID_PARAMETER", "plan", "plan contains a value that cannot be serialized") from exc
        execution_fingerprint = _hash({
            "compiler_version": self.compiler_version,
            "input_revision": parsed.input_revision,
            "external_inputs": [self._ref_fingerprint(ref) for ref in parsed.external_inputs],
            "tasks": [{"task_key": task.task_key, "execution_fingerprint": task.execution_fingerprint} for task in compiled],
        })
        return PlanIR(
            compiler_version=self.compiler_version,
            run_id=parsed.run_id,
            input_revision=parsed.input_revision,
            user_goal=parsed.user_goal,
            input_artifact_ids=input_ids,
            # Execution-facing order is topological; the canonical payload
            # above remains key-sorted so the hash is independent of draft
            # ordering.
            tasks=compiled,
            bindings=payload["bindings"],
            allowed_operation_ids=sorted(allowed_operation_ids),
            catalog_hash=parsed.catalog_hash,
            plan_hash=hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
            canonical_json=canonical_json,
            execution_fingerprint=execution_fingerprint,
        )

    @staticmethod
    def _normalize_input(raw: Any, path: str) -> InputRef | list[InputRef]:
        values = raw if isinstance(raw, list) else [raw]
        result = []
        for index, item in enumerate(values):
            try:
                if isinstance(item, (ArtifactRef, TaskOutputRef)):
                    result.append(item)
                elif isinstance(item, dict) and item.get("kind") == "task_output" or isinstance(item, dict) and "task_key" in item:
                    result.append(TaskOutputRef.model_validate(item))
                else:
                    result.append(ArtifactRef.model_validate(item))
            except Exception as exc:
                raise CompileError("INVALID_REF", f"{path}[{index}]", "input reference is invalid") from exc
        return result if isinstance(raw, list) else result[0]

    @staticmethod
    def _validate_input_shape(value: InputRef | list[InputRef], port: PortContract, path: str) -> None:
        if isinstance(value, list) and port.cardinality == "one":
            raise CompileError("PORT_MISMATCH", path, "one port cannot receive a list")
        count = len(value) if isinstance(value, list) else 1
        if count < port.min_items or (port.max_items is not None and count > port.max_items):
            raise CompileError("PORT_MISMATCH", path, "input cardinality is outside the contract bounds")

    @staticmethod
    def _validate_graph(steps, inputs) -> None:
        keys = {step.task_key for step in steps}
        edges = {key: set() for key in keys}
        for task_key, ports in inputs.items():
            for value in ports.values():
                for ref in value if isinstance(value, list) else [value]:
                    if isinstance(ref, TaskOutputRef):
                        if ref.task_key not in keys:
                            raise CompileError("INVALID_REF", f"tasks.{task_key}.inputs", f"unknown task reference: {ref.task_key}")
                        if ref.task_key == task_key:
                            raise CompileError("DAG_CYCLE", f"tasks.{task_key}.inputs", "task cannot reference itself")
                        edges[ref.task_key].add(task_key)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                raise CompileError("DAG_CYCLE", f"tasks.{key}", "task graph contains a cycle")
            if key in visited:
                return
            visiting.add(key)
            for child in sorted(edges[key]):
                visit(child)
            visiting.remove(key)
            visited.add(key)

        for key in sorted(keys):
            visit(key)

    @staticmethod
    def _topological_step_order(steps, inputs) -> list:
        by_key = {step.task_key: step for step in steps}
        indegree = {key: 0 for key in by_key}
        children: dict[str, set[str]] = {key: set() for key in by_key}
        for task_key, ports in inputs.items():
            for value in ports.values():
                for ref in value if isinstance(value, list) else [value]:
                    if isinstance(ref, TaskOutputRef) and task_key not in children[ref.task_key]:
                        children[ref.task_key].add(task_key)
                        indegree[task_key] += 1
        ready = sorted(key for key, degree in indegree.items() if degree == 0)
        ordered = []
        while ready:
            key = ready.pop(0)
            ordered.append(by_key[key])
            for child in sorted(children[key]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
            ready.sort()
        if len(ordered) != len(steps):
            raise CompileError("DAG_CYCLE", "tasks", "task graph contains a cycle")
        return ordered

    def _validate_ref(self, ref, port: PortContract, path: str, task_outputs) -> None:
        if isinstance(ref, ArtifactRef):
            if ref.selection == "all" and port.cardinality != "many":
                raise CompileError("INVALID_REF", path, "selection=all requires a many input port")
            if not port.accepts_type(ref.type):
                raise CompileError("TYPE_MISMATCH", path, f"expected {port.allowed_types or port.artifact_type}, got {ref.type}")
            return
        if ref.selection == "all" and port.cardinality != "many":
            raise CompileError("INVALID_REF", path, "selection=all requires a many input port")
        outputs = task_outputs.get(ref.task_key)
        if outputs is not None and ref.port not in outputs:
            raise CompileError("PORT_MISMATCH", path, f"unknown output port: {ref.port}")
        if outputs is not None and ref.selection == "single" and ref.ordinal is None:
            raise CompileError("INVALID_REF", path, "single selection requires an ordinal")
        if outputs is not None and ref.port in outputs:
            output = outputs[ref.port]
            if output.cardinality == "many" and ref.selection != "all":
                raise CompileError("INVALID_REF", path, "many outputs require selection=all")
            if ref.selection == "all" and output.cardinality != "many":
                raise CompileError("INVALID_REF", path, "selection=all requires a many output port")
            if not port.accepts_type(output.artifact_type):
                raise CompileError("TYPE_MISMATCH", path, f"expected {port.artifact_type}, got {output.artifact_type}")
            if ref.selection == "single" and output.max_items is not None and ref.ordinal is not None and ref.ordinal >= output.max_items:
                raise CompileError("INVALID_REF", path, "output ordinal is outside the declared maximum")
            required = set((port.schema or {}).get("required", []))
            source_schema = output.schema or {}
            available = source_schema.get("fields")
            if isinstance(available, dict):
                available_fields = set(available)
            else:
                available_fields = set(source_schema.get("required", [])) if isinstance(source_schema, dict) else set()
            if required and not required.issubset(available_fields):
                raise CompileError("TYPE_MISMATCH", path, "source output schema does not satisfy the input port")

    @staticmethod
    def _task_output_crs(ref: TaskOutputRef, task_outputs: dict[str, dict[str, OutputSpec]]) -> dict[str, Any] | None:
        outputs = task_outputs.get(ref.task_key, {})
        output = outputs.get(ref.port)
        return output.crs if output is not None else None

    @staticmethod
    def _input_crs_sequence(value, task_outputs: dict[str, dict[str, OutputSpec]]) -> list[dict[str, Any] | None]:
        refs = value if isinstance(value, list) else [value]
        result = []
        for ref in refs:
            result.append(ref.crs if isinstance(ref, ArtifactRef) else Compiler._task_output_crs(ref, task_outputs))
        return result

    @staticmethod
    def _validate_port_crs(crs: dict[str, Any] | None, port: PortContract, path: str) -> None:
        policy = port.crs_policy or {}
        if policy.get("required") and crs is None:
            raise AwaitingInputDiagnostic("CRS_REQUIRED", path, "input CRS is required")
        target = policy.get("target")
        if target is not None:
            try:
                validate_crs_value(target)
            except ValueError as exc:
                raise CompileError("INVALID_PARAMETER", f"{path}.target", str(exc)) from exc
            if crs is not None and crs != target:
                raise CompileError("CRS_CONFLICT", path, "input CRS does not satisfy the port target CRS")

    def _validate_artifact_ref(self, ref: ArtifactRef, path: str, port: PortContract | None = None) -> None:
        if self.artifact_lookup is None:
            return
        try:
            artifact = self.artifact_lookup(ref.artifact_id)
        except Exception as exc:
            raise CompileError("INVALID_REF", path, "artifact lookup failed") from exc
        if not isinstance(artifact, dict) or artifact.get("validation_status") != "valid":
            raise CompileError("INVALID_REF", path, "artifact reference is not a valid published artifact")
        required_metadata = ("type", "crs", "schema", "content_hash", "storage_uri", "size_bytes", "validation_status")
        if any(key not in artifact for key in required_metadata):
            raise CompileError("INVALID_REF", path, "artifact lookup metadata is incomplete")
        artifact_type = artifact.get("type", artifact.get("artifact_type"))
        if artifact_type is not None and ref.type != "Artifact" and artifact_type != ref.type:
            raise CompileError("TYPE_MISMATCH", path, f"artifact metadata type does not match reference: expected {ref.type}, got {artifact_type}")
        if port is not None and artifact_type is not None and not port.accepts_type(artifact_type):
            raise CompileError("TYPE_MISMATCH", path, f"artifact metadata type does not satisfy port: expected {port.artifact_type}, got {artifact_type}")
        artifact_crs = artifact.get("crs")
        schema_metadata = artifact.get("schema") if isinstance(artifact.get("schema"), dict) else {}
        declared = schema_metadata.get("file", {}).get("declared_crs") if isinstance(schema_metadata.get("file"), dict) else None
        if declared is not None:
            if not isinstance(declared, dict) or set(declared) != {"value", "source"} or declared.get("source") != "user":
                raise CompileError("INVALID_REF", path, "File declared_crs must be wrapped with value and source=user")
            declared = declared["value"]
        effective_crs = artifact_crs or declared
        if ref.crs is not None and artifact_crs is not None and artifact_crs != ref.crs:
            raise CompileError("CRS_CONFLICT", path, "artifact metadata CRS does not match reference")
        if effective_crs is not None:
            try:
                validate_crs_value(effective_crs)
            except (TypeError, ValueError) as exc:
                raise CompileError("INVALID_REF", path, "artifact metadata CRS is invalid") from exc
        if "schema" in artifact and artifact["schema"] is not None and not isinstance(artifact["schema"], dict):
            raise CompileError("INVALID_REF", path, "artifact metadata schema is invalid")
        if ref.content_hash is not None and str(artifact.get("content_hash", "")).lower() != ref.content_hash:
            raise CompileError("INVALID_REF", path, "artifact content hash does not match")
        if not artifact.get("storage_uri", "").startswith("artifacts/sha256/"):
            raise CompileError("INVALID_REF", path, "artifact reference is not canonical CAS")
        content_hash = artifact.get("content_hash")
        if not isinstance(content_hash, str) or len(content_hash) != 64 or any(char not in "0123456789abcdefABCDEF" for char in content_hash):
            raise CompileError("INVALID_REF", path, "artifact metadata has no valid SHA-256 content hash")
        canonical_uri = f"artifacts/sha256/{content_hash[:2].lower()}/{content_hash.lower()}"
        if artifact.get("storage_uri") != canonical_uri:
            raise CompileError("INVALID_REF", path, "artifact metadata does not point to its canonical CAS object")
        for size_key in ("size", "size_bytes"):
            if size_key in artifact and (isinstance(artifact[size_key], bool) or not isinstance(artifact[size_key], int) or artifact[size_key] < 0):
                raise CompileError("INVALID_REF", path, "artifact metadata has an invalid size")
        if "size" in artifact and "size_bytes" in artifact and artifact["size"] != artifact["size_bytes"]:
            raise CompileError("INVALID_REF", path, "artifact metadata size fields disagree")
        if port is not None:
            required = set((port.schema or {}).get("required", []))
            available = artifact.get("schema") or {}
            if isinstance(available, dict) and isinstance(available.get("fields"), dict):
                available = available["fields"]
            if required and not required.issubset(available if isinstance(available, dict) else set()):
                raise CompileError("TYPE_MISMATCH", path, "artifact schema does not satisfy the input port")

    @staticmethod
    def _validate_crs(crs: dict[str, Any] | None, path: str) -> None:
        if crs is None:
            return
        kind = crs.get("kind")
        if kind not in {"EPSG", "WGS84", "GCJ02"}:
            raise CompileError("INVALID_PARAMETER", path, "CRS kind is invalid")
        if kind == "EPSG":
            code = crs.get("code")
            if isinstance(code, str) and code.upper() == "GCJ02":
                raise CompileError("GCJ02_NOT_EPSG", path, "GCJ02 must not be represented as an EPSG code")
            if isinstance(code, bool) or not isinstance(code, int) or code <= 0:
                raise CompileError("INVALID_PARAMETER", path, "EPSG code must be a positive integer")
            if crs.get("axis_order") is not None:
                raise CompileError("INVALID_PARAMETER", path, "EPSG CRS has no axis_order field")
        elif kind == "WGS84":
            if set(crs) - {"kind", "axis_order"}:
                raise CompileError("INVALID_PARAMETER", path, "WGS84 CRS contains unknown fields")
        elif kind == "GCJ02":
            if set(crs) != {"kind"}:
                raise CompileError("INVALID_PARAMETER", path, "GCJ02 CRS contains unknown fields")
        try:
            validate_crs_value(crs)
        except ValueError as exc:
            raise CompileError("INVALID_PARAMETER", path, str(exc)) from exc

    @staticmethod
    def _normalize_params(params: dict[str, Any], contract: ToolContract, path: str) -> dict[str, Any]:
        schema = contract.parameter_schema or {}
        normalized = dict(params)
        properties = schema.get("properties", {})
        parameter_details = {
            "operation": contract.name,
            "parameter_schema": schema,
            "provided_parameters": sorted(params),
            "missing_parameters": sorted(name for name in schema.get("required", []) if name not in params and "default" not in properties.get(name, {})),
            "unexpected_parameters": sorted(set(params) - set(properties)) if schema.get("additionalProperties", False) is False else [],
        }
        if schema.get("additionalProperties", False) is False:
            unknown = sorted(set(normalized) - set(properties))
            if unknown:
                raise CompileError("INVALID_PARAMETER", f"{path}.{unknown[0]}", "parameter is not declared by the contract", parameter_details)
        for name, rule in properties.items():
            if name not in normalized and "default" in rule:
                normalized[name] = rule["default"]
        required = schema.get("required", [])
        missing = [name for name in required if name not in normalized]
        if missing:
            raise CompileError("INVALID_PARAMETER", path, f"missing required parameter: {missing[0]}", parameter_details)
        normalized = _canonical(normalized)
        for name, rule in properties.items():
            if name not in normalized:
                continue
            value = normalized[name]
            if "unit" in rule and isinstance(value, dict):
                if set(value) != {"value", "unit"} or isinstance(value["value"], bool) or not isinstance(value["value"], (int, float)) or not math.isfinite(float(value["value"])):
                    raise CompileError("INVALID_PARAMETER", f"{path}.{name}", "unit value must contain finite numeric value and unit")
                factors = {("m", "m"): 1, ("km", "m"): 1000, ("s", "s"): 1, ("min", "s"): 60}
                source_unit = value["unit"]
                target_unit = rule["unit"]
                if (source_unit, target_unit) not in factors:
                    raise CompileError("INVALID_PARAMETER", f"{path}.{name}", "unsupported unit conversion")
                normalized[name] = value["value"] * factors[(source_unit, target_unit)]
                value = normalized[name]
            if rule.get("type") == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise CompileError("INVALID_PARAMETER", f"{path}.{name}", "parameter must be numeric")
            if rule.get("type") == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
                raise CompileError("INVALID_PARAMETER", f"{path}.{name}", "parameter must be an integer")
            if rule.get("type") == "boolean" and not isinstance(value, bool):
                raise CompileError("INVALID_PARAMETER", f"{path}.{name}", "parameter must be boolean")
            if rule.get("type") == "string" and not isinstance(value, str):
                raise CompileError("INVALID_PARAMETER", f"{path}.{name}", "parameter must be a string")
            if rule.get("type") == "object" and not isinstance(value, dict):
                raise CompileError("INVALID_PARAMETER", f"{path}.{name}", "parameter must be an object")
            if rule.get("type") == "array" and not isinstance(value, list):
                raise CompileError("INVALID_PARAMETER", f"{path}.{name}", "parameter must be an array")
            # ``bool`` is an ``int`` subclass in Python, but it is a valid
            # JSON boolean when the contract says ``type=boolean``.  Do not
            # run the finite-number guard on it after the explicit type check.
            if isinstance(value, (int, float)) and not isinstance(value, bool) and not math.isfinite(float(value)):
                raise CompileError("INVALID_PARAMETER", f"{path}.{name}", "parameter must be finite")
            if "enum" in rule and value not in rule["enum"]:
                raise CompileError("INVALID_PARAMETER", f"{path}.{name}", "parameter is not an allowed value")
            if rule.get("type") == "string" and "minLength" in rule and len(value) < rule["minLength"]:
                raise CompileError("INVALID_PARAMETER", f"{path}.{name}", "parameter is shorter than the minimum length")
            if rule.get("type") == "string" and "maxLength" in rule and len(value) > rule["maxLength"]:
                raise CompileError("INVALID_PARAMETER", f"{path}.{name}", "parameter exceeds the maximum length")
            if "minimum" in rule and value < rule["minimum"]:
                raise CompileError("INVALID_PARAMETER", f"{path}.{name}", "parameter is below minimum")
            if "maximum" in rule and value > rule["maximum"]:
                raise CompileError("INVALID_PARAMETER", f"{path}.{name}", "parameter exceeds maximum")
        # Apply the same strict checks recursively to object/array schemas.
        for name, rule in properties.items():
            rule_type = rule.get("type") if isinstance(rule, dict) else None
            rule_types = set(rule_type) if isinstance(rule_type, (list, tuple)) else {rule_type}
            if name in normalized and isinstance(rule, dict) and rule_types & {"object", "array"}:
                normalized[name] = Compiler._validate_parameter_schema_value(normalized[name], rule, f"{path}.{name}")
        if contract.name == "code.execute" and "source_code" in normalized:
            source = normalized["source_code"]
            if not isinstance(source, str) or not source:
                raise CompileError("INVALID_PARAMETER", f"{path}.source_code", "source_code must be non-empty text")
            Compiler._validate_code_source(source, f"{path}.source_code")
            source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
            supplied_hash = normalized.get("source_code_hash")
            if supplied_hash is not None and supplied_hash != source_hash:
                raise CompileError("INVALID_PARAMETER", f"{path}.source_code_hash", "source_code_hash does not match source_code")
            normalized["source_code_hash"] = source_hash
        return normalized

    @staticmethod
    def _validate_code_source(source: str, path: str) -> None:
        try:
            from app.runtime.code_mode.ast_guard import inspect as inspect_code
            inspect_code(source)
        except Exception as exc:
            if exc.__class__.__name__ == "ASTBannedNodeError":
                raise CompileError("INVALID_PARAMETER", path, "source_code violates the sandbox AST policy") from exc
            if isinstance(exc, (SyntaxError, ValueError, TypeError)):
                raise CompileError("INVALID_PARAMETER", path, "source_code is not valid Python") from exc
        try:
            tree = ast.parse(source, mode="exec")
        except (SyntaxError, ValueError, TypeError) as exc:
            raise CompileError("INVALID_PARAMETER", path, "source_code is not valid Python") from exc
        banned_names = {"socket", "tool_fns", "callable", "rpc", "eval", "exec", "compile", "__import__"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                raise CompileError("INVALID_PARAMETER", path, "source_code imports are not allowed")
            # ``__result__`` is the explicit, sandbox-owned output channel;
            # every other dunder remains forbidden to prevent hidden runtime
            # and interpreter access.
            if isinstance(node, ast.Name) and (node.id in banned_names or (node.id.startswith("__") and node.id != "__result__")):
                raise CompileError("INVALID_PARAMETER", path, "source_code contains a forbidden name")
            if isinstance(node, ast.Attribute) and (node.attr.startswith("__") or node.attr in {"call", "request", "post", "get", "execute"}):
                raise CompileError("INVALID_PARAMETER", path, "source_code contains a forbidden operation")

    @staticmethod
    def _validate_code_input_declarations(params: dict[str, Any], inputs: dict[str, Any], path: str) -> None:
        """Require a stable name for every File Artifact exposed to code."""
        bound = inputs.get("input")
        refs = [] if bound is None else (bound if isinstance(bound, list) else [bound])
        declarations = params.get("declared_inputs")
        if not refs:
            if declarations not in (None, []):
                raise CompileError("INVALID_PARAMETER", f"{path}.params.declared_inputs", "code task has no Artifact inputs to declare")
            return
        if not isinstance(declarations, list) or len(declarations) != len(refs):
            raise CompileError("INVALID_PARAMETER", f"{path}.params.declared_inputs", "code task must declare each Artifact input exactly once")
        names = list(declarations)
        if any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) for name in names):
            raise CompileError("INVALID_PARAMETER", f"{path}.params.declared_inputs", "declared input names must be identifiers")
        if len(set(names)) != len(names):
            raise CompileError("INVALID_PARAMETER", f"{path}.params.declared_inputs", "declared input names must be unique")

    @staticmethod
    def _validate_parameter_schema_value(value: Any, rule: dict[str, Any], path: str) -> Any:
        if not isinstance(rule, dict):
            raise CompileError("INVALID_PARAMETER", path, "parameter schema rule must be an object")
        if "unit" in rule and isinstance(value, dict) and set(value) == {"value", "unit"}:
            raw = value["value"]
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
                raise CompileError("INVALID_PARAMETER", path, "unit value must be finite numeric")
            factors = {("m", "m"): 1, ("km", "m"): 1000, ("s", "s"): 1, ("min", "s"): 60}
            try:
                value = raw * factors[(value["unit"], rule["unit"])]
            except KeyError as exc:
                raise CompileError("INVALID_PARAMETER", path, "unsupported unit conversion") from exc
        typ = rule.get("type")
        if isinstance(typ, (list, tuple)):
            candidates = tuple(item for item in typ if isinstance(item, str))
            if "object" in candidates and isinstance(value, dict):
                typ = "object"
            elif "array" in candidates and isinstance(value, list):
                typ = "array"
            elif "string" in candidates and isinstance(value, str):
                typ = "string"
            elif "number" in candidates and isinstance(value, (int, float)) and not isinstance(value, bool):
                typ = "number"
            elif "integer" in candidates and isinstance(value, int) and not isinstance(value, bool):
                typ = "integer"
            elif "boolean" in candidates and isinstance(value, bool):
                typ = "boolean"
            else:
                raise CompileError("INVALID_PARAMETER", path, f"parameter must match one of {list(candidates)}")
            rule = dict(rule)
            rule["type"] = typ
        if typ == "object":
            if not isinstance(value, dict):
                raise CompileError("INVALID_PARAMETER", path, "parameter must be an object")
            properties = rule.get("properties", {})
            if not isinstance(properties, dict):
                raise CompileError("INVALID_PARAMETER", path, "object properties schema is invalid")
            result = dict(value)
            for name, child in properties.items():
                if name not in result and isinstance(child, dict) and "default" in child:
                    result[name] = child["default"]
            required = rule.get("required", [])
            if not isinstance(required, list):
                raise CompileError("INVALID_PARAMETER", path, "required schema is invalid")
            missing = [name for name in required if name not in result]
            if missing:
                raise CompileError("INVALID_PARAMETER", path, f"missing required parameter: {missing[0]}")
            if rule.get("additionalProperties", False) is False:
                unknown = sorted(set(result) - set(properties))
                if unknown:
                    raise CompileError("INVALID_PARAMETER", f"{path}.{unknown[0]}", "parameter is not declared by the contract")
            return {name: Compiler._validate_parameter_schema_value(result[name], properties[name], f"{path}.{name}") if name in properties else result[name] for name in sorted(result)}
        if typ == "array":
            if not isinstance(value, list):
                raise CompileError("INVALID_PARAMETER", path, "parameter must be an array")
            if "minItems" in rule and len(value) < rule["minItems"]:
                raise CompileError("INVALID_PARAMETER", path, "array is shorter than minimum length")
            if "maxItems" in rule and len(value) > rule["maxItems"]:
                raise CompileError("INVALID_PARAMETER", path, "array exceeds maximum length")
            child = rule.get("items")
            return [Compiler._validate_parameter_schema_value(item, child, f"{path}[{index}]") for index, item in enumerate(value)] if child is not None else value
        if typ == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise CompileError("INVALID_PARAMETER", path, "parameter must be finite numeric")
        elif typ == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise CompileError("INVALID_PARAMETER", path, "parameter must be an integer")
        elif typ == "boolean" and not isinstance(value, bool):
            raise CompileError("INVALID_PARAMETER", path, "parameter must be boolean")
        elif typ == "string":
            if not isinstance(value, str):
                raise CompileError("INVALID_PARAMETER", path, "parameter must be a string")
            if "minLength" in rule and len(value) < rule["minLength"]:
                raise CompileError("INVALID_PARAMETER", path, "parameter is shorter than minimum length")
            if "maxLength" in rule and len(value) > rule["maxLength"]:
                raise CompileError("INVALID_PARAMETER", path, "parameter exceeds maximum length")
        if "enum" in rule and value not in rule["enum"]:
            raise CompileError("INVALID_PARAMETER", path, "parameter is not an allowed value")
        try:
            if "minimum" in rule and value < rule["minimum"]:
                raise CompileError("INVALID_PARAMETER", path, "parameter is below minimum")
            if "maximum" in rule and value > rule["maximum"]:
                raise CompileError("INVALID_PARAMETER", path, "parameter exceeds maximum")
        except TypeError as exc:
            raise CompileError("INVALID_PARAMETER", path, "parameter constraint comparison is invalid") from exc
        return value

    def _output_specs(self, contract: ToolContract, params: dict[str, Any], inputs, task_key: str, task_outputs: dict[str, dict[str, OutputSpec]] | None = None) -> list[OutputSpec]:
        input_crs = None
        input_schema: dict[str, Any] | None = None
        for value in inputs.values():
            refs = value if isinstance(value, list) else [value]
            for ref in refs:
                if isinstance(ref, ArtifactRef):
                    if ref.crs is not None:
                        input_crs = ref.crs
                    if self.artifact_lookup is not None:
                        try:
                            metadata = self.artifact_lookup(ref.artifact_id)
                        except Exception as exc:
                            raise CompileError("INVALID_REF", f"tasks.{task_key}.inputs", "artifact lookup failed") from exc
                        if isinstance(metadata, dict):
                            candidate_schema = metadata.get("schema")
                            input_crs = metadata.get("crs", input_crs)
                            if input_crs is None and isinstance(candidate_schema, dict):
                                file_meta = candidate_schema.get("file")
                                if isinstance(file_meta, dict):
                                    declared = file_meta.get("declared_crs")
                                    if declared is not None:
                                        if not isinstance(declared, dict) or set(declared) != {"value", "source"} or declared.get("source") != "user":
                                            raise CompileError("INVALID_REF", f"tasks.{task_key}.inputs", "File declared_crs must be wrapped with value and source=user")
                                        input_crs = declared["value"]
                            if isinstance(candidate_schema, dict):
                                input_schema = candidate_schema
                    if input_crs is not None or input_schema is not None:
                        break
                elif isinstance(ref, TaskOutputRef) and task_outputs is not None:
                    source = task_outputs.get(ref.task_key, {}).get(ref.port)
                    if source is not None:
                        if source.crs is not None:
                            input_crs = source.crs
                        if source.schema:
                            input_schema = source.schema
                    break
        if "target_crs" in params:
            input_crs = _normalize_crs_metadata(params["target_crs"])
        if contract.name in {"io.csv_to_points", "raster.vectorize"} and "crs" in params:
            input_crs = _normalize_crs_metadata(params["crs"])
        if contract.name == "geo.geocode":
            input_crs = {"kind": "GCJ02"}
        if input_crs is None and contract.name in {"io.load_vector", "io.load_raster"} and any((port.crs_policy or {}).get("required") for port in contract.output_ports.values()):
            raise AwaitingInputDiagnostic("CRS_REQUIRED", f"tasks.{task_key}.outputs", "spatial File input requires schema.file.declared_crs", {"patch_schema": {"type": "object", "properties": {"file": {"properties": {"declared_crs": {"type": "object"}}}}}})
        if contract.name == "file.export":
            if input_crs is not None and input_crs.get("kind") == "GCJ02":
                raise CompileError("CRS_CONFLICT", f"tasks.{task_key}.inputs.input", "file.export requires a physical EPSG CRS; convert GCJ02 explicitly")
            export_format = params["format"]
            extension, media_type = FILE_EXPORT_FORMATS[export_format]
            safe_task_key = re.sub(r"[^A-Za-z0-9_-]", "_", task_key)[:64] or "export"
            file_schema = {"file": {
                "format": export_format,
                "filename": f"{safe_task_key}-file{extension}",
                "extension": extension,
                "media_type": media_type,
                "download_disposition": "attachment",
            }}
            output_crs = {"kind": "EPSG", "code": 4326} if export_format == "geojson" else input_crs
            return [OutputSpec(port="file", artifact_type="File", crs=output_crs, schema=file_schema)]
        return [OutputSpec(port=name, artifact_type=port.artifact_type, cardinality=port.cardinality, min_items=port.min_items, max_items=port.max_items, crs=input_crs, schema=input_schema if contract.name == "io.load_vector" and input_schema is not None else port.schema) for name, port in sorted(contract.output_ports.items())]

    def _compiled_task(self, step, contract, inputs, params, outputs, *, op: str | None = None) -> CompiledTask:
        canonical_op = op or step.op
        try:
            input_fingerprints = [self._ref_fingerprint(ref) for ref in self._iter_refs(inputs)]
            execution_payload = {
                "compiler_version": self.compiler_version,
                "contract_version": contract.version,
                "op": canonical_op,
                "handler_ref": contract.handler_ref,
                "validator": contract.validator,
                "manifest": contract.manifest,
                "runtime_version": self.policy.get("runtime_version", "1"),
                "environment": self.policy.get("environment", {}),
                "tool_implementation": self.policy.get("tool_implementation", contract.handler_ref),
                "environment_fingerprint": self.policy.get("environment_fingerprint", {}),
                "interpreter": self.policy.get("interpreter"),
                "package_fingerprint": self.policy.get("package_fingerprint"),
                "sandbox_policy": self.policy.get("sandbox_policy"),
                "params": params,
                "inputs": input_fingerprints,
                "cache_policy": contract.cache_policy.model_dump(mode="json"),
                "side_effect": contract.side_effect,
            }
            execution_fingerprint = _hash(execution_payload)
        except CompileError:
            raise
        except Exception as exc:
            raise CompileError("INVALID_PARAMETER", f"tasks.{step.task_key}.params", "task execution fingerprint is not serializable") from exc
        action_hash = None
        if contract.side_effect:
            action = {"compiler_version": self.compiler_version, "op": canonical_op, "version": contract.version, "params": params, "inputs": input_fingerprints, "execution_fingerprint": execution_fingerprint}
            if contract.confirmation_policy.action_fields:
                action["action_fields"] = {field: params.get(field) for field in contract.confirmation_policy.action_fields}
            action_hash = _hash(action)
        cache_key = None if contract.side_effect or contract.cache_policy.mode == "disabled" else _hash({"execution_fingerprint": execution_fingerprint, "cache_policy": contract.cache_policy.model_dump(mode="json")})
        validators = list(contract.validators)
        if contract.validator:
            validators.append(contract.validator)
        return CompiledTask(task_key=step.task_key, op=canonical_op, contract_version=contract.version, origin="planner", inputs=inputs, params=params, outputs=outputs, empty_policy=contract.empty_policy, fallback_policy=contract.fallback_policy, idempotent=contract.idempotency, retry_policy=contract.retry_policy, cache_policy=contract.cache_policy, resource_budget=contract.resource_budget, validators=validators, postconditions=list(contract.postconditions), side_effect=contract.side_effect, confirmation_policy=contract.confirmation_policy, action_hash=action_hash, executor_type=contract.executor_type, cache_key=cache_key, execution_fingerprint=execution_fingerprint)

    @staticmethod
    def _iter_refs(inputs):
        for value in inputs.values() if isinstance(inputs, dict) else inputs:
            for ref in value if isinstance(value, list) else [value]:
                yield ref

    def _ref_fingerprint(self, ref: InputRef) -> dict[str, Any]:
        try:
            value = ref.model_dump(mode="json")
        except Exception as exc:
            raise CompileError("INVALID_REF", "artifact", "artifact reference cannot be serialized") from exc
        if isinstance(ref, ArtifactRef) and self.artifact_lookup is not None:
            try:
                artifact = self.artifact_lookup(ref.artifact_id)
            except Exception as exc:
                raise CompileError("INVALID_REF", f"artifact.{ref.artifact_id}", "artifact lookup failed") from exc
            if artifact is not None:
                if artifact.get("type", artifact.get("artifact_type")) is not None:
                    value["type"] = artifact.get("type", artifact.get("artifact_type"))
                value["content_hash"] = artifact.get("content_hash")
                value["schema"] = artifact.get("schema")
                value["crs"] = artifact.get("crs", ref.crs)
                value["size_bytes"] = artifact.get("size_bytes", artifact.get("size"))
        return _canonical(value)

    def _validate_side_effect_params(self, contract: ToolContract, params: dict[str, Any], path: str) -> None:
        if not contract.side_effect:
            return
        for field in contract.confirmation_policy.action_fields:
            if field not in params:
                raise CompileError("CONFIRMATION_REQUIRED", f"{path}.{field}", "side-effect action field is required")
        for field in ("path", "destination", "output_path"):
            if field not in params:
                continue
            raw_path = params[field]
            if not isinstance(raw_path, str) or not raw_path:
                raise CompileError("INVALID_PARAMETER", f"{path}.{field}", "side-effect path must be a confined relative path")
            # Planner paths must obey the same confinement contract on every
            # host. POSIX Path treats Windows drives and backslashes as ordinary
            # filename characters, so inspect both grammars before resolution.
            windows_path = PureWindowsPath(raw_path)
            posix_path = PurePosixPath(raw_path)
            if (windows_path.drive or windows_path.root or posix_path.root
                    or ".." in windows_path.parts or ".." in posix_path.parts):
                raise CompileError("INVALID_PARAMETER", f"{path}.{field}", "side-effect path must be a confined relative path")
            allowed_root = self.policy.get("allowed_path_root")
            if not allowed_root:
                raise CompileError("INVALID_PARAMETER", f"{path}.{field}", "side-effect paths require an allowed root")
            root = Path(allowed_root).resolve()
            destination = (root / raw_path).resolve()
            try:
                destination.relative_to(root)
            except ValueError as exc:
                raise CompileError("INVALID_PARAMETER", f"{path}.{field}", "side-effect path escapes the allowed root") from exc
            # Persist the canonical resolved destination so the action hash
            # binds the exact path that the executor will hand to the writer.
            params[field] = str(destination)

    def _insert_reprojection(self, tasks: list[CompiledTask], target_crs: dict[str, Any]) -> list[CompiledTask]:
        self._validate_crs(target_crs, "target_crs")
        result: list[CompiledTask] = []
        existing_keys = {task.task_key for task in tasks}
        output_crs = {
            task.task_key: {output.port: output.crs for output in task.outputs}
            for task in tasks
        }
        for task in tasks:
            rewritten = dict(task.inputs)
            inserted_for_task = False
            propagated_candidates: list[dict[str, Any]] = []
            for port_name, value in list(rewritten.items()):
                refs = value if isinstance(value, list) else [value]
                crs_values = [ref.crs if isinstance(ref, ArtifactRef) else output_crs.get(ref.task_key, {}).get(ref.port) if isinstance(ref, TaskOutputRef) else None for ref in refs]
                known_crs = [crs for crs in crs_values if crs is not None]
                propagated_candidates.extend(known_crs)
                if known_crs and (len(known_crs) != len(crs_values) or any(crs != known_crs[0] for crs in known_crs[1:])):
                    raise CompileError("CRS_CONFLICT", f"tasks.{task.task_key}.inputs.{port_name}", "a many input has inconsistent source CRS")
                source_crs = known_crs[0] if known_crs else None
                if source_crs is None or source_crs == target_crs or task.op in {"vector.reproject", "vector.reproject_many", "raster.reproject", "raster.reproject_many"}:
                    continue
                source_meta = self._compiled_ref_metadata(refs[0], tasks, output_crs)
                many = isinstance(value, list) or any(ref.selection == "all" for ref in refs)
                artifact_type = source_meta.artifact_type
                if artifact_type not in {"Vector", "Raster"}:
                    raise CompileError("CRS_CONFLICT", f"tasks.{task.task_key}.inputs.{port_name}", f"no reprojection contract exists for {artifact_type}")
                if source_crs.get("kind") == "GCJ02" or target_crs.get("kind") == "GCJ02":
                    if artifact_type != "Vector":
                        raise CompileError("CRS_CONFLICT", f"tasks.{task.task_key}.inputs.{port_name}", "GCJ02 datum conversion is only defined for vector data")
                    conversion_op = "vector.convert_datum"
                else:
                    conversion_op = ("raster.reproject_many" if artifact_type == "Raster" else "vector.reproject_many") if many else ("raster.reproject" if artifact_type == "Raster" else "vector.reproject")
                key = f"{task.task_key}__reproject__{port_name}"
                if key in existing_keys:
                    raise CompileError("DUPLICATE_TASK_KEY", f"tasks.{task.task_key}", f"compiler conversion key already exists: {key}")
                existing_keys.add(key)
                inserted_for_task = True
                conversion = self.registry.get(conversion_op)
                conversion_port = "raster" if conversion_op.startswith("raster") else "vector"
                conversion_inputs = {conversion_port: value}
                conversion_params = {"target_crs": target_crs, "source_crs": source_crs} if conversion_op == "vector.convert_datum" else {"target_crs": target_crs}
                conversion_output = OutputSpec(
                    port=conversion_port,
                    artifact_type=source_meta.artifact_type,
                    cardinality="many" if many else source_meta.cardinality,
                    min_items=(len(refs) if isinstance(value, list) else source_meta.min_items if isinstance(refs[0], TaskOutputRef) else 0) if many else source_meta.min_items,
                    max_items=(len(refs) if isinstance(value, list) else source_meta.max_items if isinstance(refs[0], TaskOutputRef) else None) if many else source_meta.max_items,
                    crs=target_crs,
                    schema=source_meta.schema,
                )
                conversion_fingerprint = _hash({
                    "compiler_version": self.compiler_version,
                    "contract_version": conversion.version,
                    "op": conversion_op,
                    "handler_ref": conversion.handler_ref,
                    "runtime_version": self.policy.get("runtime_version", "1"),
                    "environment": self.policy.get("environment", {}),
                    "environment_fingerprint": self.policy.get("environment_fingerprint", {}),
                    "interpreter": self.policy.get("interpreter"),
                    "package_fingerprint": self.policy.get("package_fingerprint"),
                    "sandbox_policy": self.policy.get("sandbox_policy"),
                    "params": conversion_params,
                    "inputs": [self._ref_fingerprint(ref) for ref in self._iter_refs(conversion_inputs)],
                    "output": conversion_output.model_dump(mode="json"),
                })
                conversion_task = CompiledTask(task_key=key, op=conversion_op, contract_version=conversion.version, origin="compiler", inputs=conversion_inputs, params=conversion_params, outputs=[conversion_output], empty_policy=conversion.empty_policy, fallback_policy=conversion.fallback_policy, idempotent=conversion.idempotency, retry_policy=conversion.retry_policy, cache_policy=conversion.cache_policy, resource_budget=conversion.resource_budget, validators=[conversion.validator] if conversion.validator else [], postconditions=conversion.postconditions, side_effect=False, confirmation_policy=conversion.confirmation_policy, executor_type=conversion.executor_type, execution_fingerprint=conversion_fingerprint)
                result.append(conversion_task)
                rewritten[port_name] = TaskOutputRef(task_key=key, port=conversion_port, selection="all" if many else "single", ordinal=None if many else 0)
            updated_outputs = task.outputs
            if inserted_for_task:
                updated_outputs = [output.model_copy(update={"crs": target_crs}) for output in task.outputs]
                output_crs[task.task_key] = {output.port: output.crs for output in updated_outputs}
            else:
                # A producer may have been rewritten earlier in this pass.
                # Propagate that effective CRS through ordinary middle tasks
                # so the next TaskOutputRef sees target_crs rather than the
                # stale pre-rewrite metadata.
                propagated_crs = propagated_candidates[0] if propagated_candidates else None
                if propagated_crs is not None and any(output.crs != propagated_crs for output in task.outputs):
                    updated_outputs = [output.model_copy(update={"crs": propagated_crs}) for output in task.outputs]
                    output_crs[task.task_key] = {output.port: output.crs for output in updated_outputs}
            result.append(task.model_copy(update={"inputs": rewritten, "outputs": updated_outputs}))
        return result

    def _refresh_execution_identity(self, task: CompiledTask) -> CompiledTask:
        """Recompute identity after compiler rewrites inputs or output metadata."""
        previous = task.execution_fingerprint
        payload = task.model_dump(mode="json")
        payload.pop("execution_fingerprint", None)
        payload.pop("cache_key", None)
        payload.pop("action_hash", None)
        # Keep the original contract/environment fingerprint as an input so
        # a rewrite cannot erase interpreter/package/sandbox identity.
        fingerprint = _hash({"base_execution_fingerprint": previous, "task": payload})
        cache_key = None if task.side_effect or task.cache_policy.mode == "disabled" else _hash({"execution_fingerprint": fingerprint, "cache_policy": task.cache_policy.model_dump(mode="json")})
        action_hash = _hash({"task_key": task.task_key, "op": task.op, "params": task.params, "inputs": payload["inputs"], "outputs": payload["outputs"], "execution_fingerprint": fingerprint}) if task.side_effect else None
        return task.model_copy(update={"execution_fingerprint": fingerprint, "cache_key": cache_key, "action_hash": action_hash})

    @staticmethod
    def _compiled_ref_artifact_type(ref: InputRef, tasks: list[CompiledTask]) -> str:
        if isinstance(ref, ArtifactRef):
            return ref.type
        for task in tasks:
            if task.task_key == ref.task_key:
                for output in task.outputs:
                    if output.port == ref.port:
                        return output.artifact_type
        return "Artifact"

    def _compiled_ref_metadata(self, ref: InputRef, tasks: list[CompiledTask], output_crs: dict[str, dict[str, Any]]) -> OutputSpec:
        if isinstance(ref, ArtifactRef):
            artifact_type = ref.type
            crs = ref.crs
            schema = {}
            if self.artifact_lookup is not None:
                try:
                    metadata = self.artifact_lookup(ref.artifact_id)
                except Exception as exc:
                    raise CompileError("INVALID_REF", f"artifact:{ref.artifact_id}", "artifact lookup failed") from exc
                if isinstance(metadata, dict):
                    artifact_type = metadata.get("type", artifact_type)
                    crs = metadata.get("crs") or crs
                    schema = metadata.get("schema") if isinstance(metadata.get("schema"), dict) else {}
                    if crs is None and isinstance(schema.get("file"), dict):
                        declared = schema["file"].get("declared_crs")
                        if declared is not None:
                            if not isinstance(declared, dict) or declared.get("source") != "user" or "value" not in declared:
                                raise CompileError("INVALID_REF", f"artifact:{ref.artifact_id}", "File declared_crs must be wrapped with value and source=user")
                            crs = declared["value"]
            return OutputSpec(port="source", artifact_type=artifact_type, cardinality="many" if ref.selection == "all" else "one", min_items=0 if ref.selection == "all" else 1, max_items=None if ref.selection == "all" else 1, crs=crs, schema=schema)
        for task in tasks:
            if task.task_key == ref.task_key:
                for output in task.outputs:
                    if output.port == ref.port:
                        return output.model_copy(update={"crs": output_crs.get(task.task_key, {}).get(output.port, output.crs)})
        return OutputSpec(port=ref.port, artifact_type="File", crs=None)

    @staticmethod
    def _topological_order(tasks: list[CompiledTask]) -> list[CompiledTask]:
        """Return stable producer-before-consumer order for an immutable IR."""
        by_key = {task.task_key: task for task in tasks}
        indegree = {key: 0 for key in by_key}
        children: dict[str, set[str]] = {key: set() for key in by_key}
        for task in tasks:
            for ref in Compiler._iter_refs(task.inputs):
                if not isinstance(ref, TaskOutputRef) or ref.task_key not in by_key:
                    continue
                if task.task_key not in children[ref.task_key]:
                    children[ref.task_key].add(task.task_key)
                    indegree[task.task_key] += 1
        ready = sorted(key for key, degree in indegree.items() if degree == 0)
        ordered: list[CompiledTask] = []
        while ready:
            key = ready.pop(0)
            ordered.append(by_key[key])
            for child in sorted(children[key]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
            ready.sort()
        if len(ordered) != len(tasks):
            raise CompileError("DAG_CYCLE", "tasks", "task graph contains a cycle")
        return ordered

    def _validate_budget(self, tasks: list[CompiledTask]) -> None:
        max_tasks = self.policy.get("max_tasks")
        max_memory = self.policy.get("max_memory_mb")
        max_timeout = self.policy.get("max_timeout_seconds")
        if max_tasks is not None and len(tasks) > max_tasks:
            raise CompileError("RESOURCE_BUDGET_EXCEEDED", "tasks", "task count exceeds policy budget")
        if max_memory is not None and sum(task.resource_budget.memory_mb for task in tasks) > max_memory:
            raise CompileError("RESOURCE_BUDGET_EXCEEDED", "tasks", "memory budget exceeds policy")
        if max_timeout is not None and sum(task.resource_budget.timeout_seconds for task in tasks) > max_timeout:
            raise CompileError("RESOURCE_BUDGET_EXCEEDED", "tasks", "timeout budget exceeds policy")


def metric_crs_selector(bbox: dict[str, float], candidates: list[dict[str, Any]], *, version: str = "1.0.0") -> dict[str, Any]:
    """Select exactly one metric CRS whose applicability bbox contains bbox."""
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version) or version not in {"1.0.0", "2.0.0"}:
        raise CompileError("INVALID_PARAMETER", "metric_crs_selector.version", "unsupported selector version")
    if not isinstance(bbox, dict) or not isinstance(candidates, list):
        raise CompileError("INVALID_PARAMETER", "metric_crs_selector", "bbox and candidates must be structured values")
    try:
        raw_bbox = [bbox["xmin"], bbox["ymin"], bbox["xmax"], bbox["ymax"]]
        if any(isinstance(value, bool) for value in raw_bbox):
            raise ValueError("bbox coordinates must be finite numbers")
        requested = tuple(float(value) for value in raw_bbox)
        if any(not math.isfinite(value) for value in requested) or requested[0] >= requested[2] or requested[1] >= requested[3]:
            raise ValueError("bbox must be finite and ordered")
    except (KeyError, TypeError, ValueError) as exc:
        raise AwaitingInputDiagnostic("CRS_REQUIRED", "metric_crs_selector.bbox", "a valid bbox is required", {"patch_schema": {"type": "object", "required": ["xmin", "ymin", "xmax", "ymax"]}}) from exc
    matches = []
    invalid_metric_candidate = False
    for candidate in candidates:
        try:
            if not isinstance(candidate, dict) or not isinstance(candidate.get("bbox"), (list, tuple)) or len(candidate["bbox"]) != 4:
                continue
            cb = tuple(float(value) for value in candidate["bbox"])
            if any(not math.isfinite(value) for value in cb) or cb[0] > cb[2] or cb[1] > cb[3]:
                continue
            if cb[0] <= requested[0] and cb[1] <= requested[1] and cb[2] >= requested[2] and cb[3] >= requested[3]:
                crs = candidate.get("crs")
                if not isinstance(crs, dict) or crs.get("kind") != "EPSG":
                    invalid_metric_candidate = True
                    continue
                validate_crs_value(crs)
                code = crs.get("code")
                try:
                    from pyproj import CRS
                    crs_obj = CRS.from_epsg(int(code))
                    units = {str(axis.unit_name).lower() for axis in crs_obj.axis_info}
                    metric_units = {"metre", "meter", "metres", "meters", "foot", "feet"}
                    if not crs_obj.is_projected or not units or not units.issubset(metric_units):
                        invalid_metric_candidate = True
                        continue
                except (ImportError, TypeError, ValueError, RuntimeError):
                    invalid_metric_candidate = True
                    continue
                declared_unit = candidate.get("unit", candidate.get("units"))
                if declared_unit is not None and str(declared_unit).lower() not in {"m", "meter", "meters", "metre", "metres", "ft", "foot", "feet"}:
                    invalid_metric_candidate = True
                    continue
                matches.append(crs)
        except (KeyError, TypeError, ValueError):
            continue
    if len(matches) != 1:
        if not matches and invalid_metric_candidate:
            raise CompileError("CRS_CONFLICT", "metric_crs_selector", "candidate CRS is not a supported projected metric CRS")
        code = "CRS_REQUIRED" if not matches else "CRS_CONFLICT"
        raise AwaitingInputDiagnostic(code, "metric_crs_selector", "metric CRS applicability is not unique", {"selector_version": version, "patch_schema": {"type": "object", "required": ["metric_crs"]}})
    return matches[0]


def compile_plan(draft, *, registry: ToolContractRegistry | None = None, **kwargs) -> PlanIR:
    allowed = {"artifact_lookup", "auto_reproject", "target_crs", "policy", "compiler_version"}
    unknown = sorted(set(kwargs) - allowed)
    if unknown:
        raise CompileError("INVALID_PARAMETER", "compile_plan", f"unknown compiler option: {unknown[0]}")
    return Compiler(registry, **kwargs).compile(draft)

__all__ = ["AwaitingInputDiagnostic", "CompileError", "Compiler", "compile_plan", "metric_crs_selector"]
