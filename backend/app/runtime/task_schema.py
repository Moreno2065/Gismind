"""Contract-derived planner task shapes and bounded structural diagnostics.

These are ordinary JSON Schemas, not a promise of compatibility with a
provider's restricted ``strict`` schema dialect. Parameters retain their
contract's optional/open shape. The preflight is deliberately not another
Compiler: numeric, CRS, artifact identity and business validation stay there.
"""
from __future__ import annotations

from copy import deepcopy
from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import ValidationError

from .contracts import ArtifactRef, PlannerDraft, PlannerStepDraft, PortContract, TaskOutputRef, ToolContract


def _scope_refs(value: Any, scope: str) -> Any:
    """Relocate document-local JSON pointers when embedding a schema."""
    if isinstance(value, dict):
        return {
            key: scope + child[1:] if key == "$ref" and isinstance(child, str)
            and (child == "#" or child.startswith("#/")) else _scope_refs(child, scope)
            for key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scope_refs(child, scope) for child in value]
    return value


def _port_shape(port: PortContract) -> dict[str, Any]:
    return {"types": list(port.allowed_types or (port.artifact_type,)),
            "cardinality": port.cardinality, "min_items": port.min_items,
            "max_items": port.max_items, "required": port.required}


def _input_schema(port: PortContract) -> dict[str, Any]:
    artifact_rules: dict[str, Any] = {"type": {"enum": list(port.allowed_types or (port.artifact_type,))}}
    output_rules: dict[str, Any] = {}
    if port.cardinality == "one":
        artifact_rules["selection"] = output_rules["selection"] = {"const": "single"}
    reference = {"anyOf": [
        {"allOf": [{"$ref": "#/$defs/ArtifactRef"}, {"properties": artifact_rules}]},
        {"allOf": [{"$ref": "#/$defs/TaskOutputRef"}, {"properties": output_rules}]},
    ]}
    if port.cardinality == "one":
        return reference
    array: dict[str, Any] = {"type": "array", "items": reference, "minItems": port.min_items}
    if port.max_items is not None:
        array["maxItems"] = port.max_items
    # Compiler permits a scalar on a many port when one item fits its bounds.
    if port.min_items <= 1 and (port.max_items is None or port.max_items >= 1):
        return {"anyOf": [reference, array]}
    return array


def contract_task_schema(contract: ToolContract) -> dict[str, Any]:
    """Generate an independent task schema from the same execution contract."""
    base = PlannerStepDraft.model_json_schema()
    return {
        "type": "object", "additionalProperties": False,
        "required": ["task_key", "op", "inputs", "params"],
        "properties": {
            "task_key": deepcopy(base["properties"]["task_key"]),
            "op": {"const": contract.name},
            "inputs": {"type": "object", "additionalProperties": False,
                "required": sorted(name for name, port in contract.input_ports.items() if port.required),
                "properties": {name: _input_schema(port) for name, port in contract.input_ports.items()}},
            "params": _scope_refs(contract.model_dump()["parameter_schema"], "#/properties/params"),
        },
        "$defs": deepcopy(base["$defs"]),
    }


def catalog_task_schema(contracts: Iterable[ToolContract]) -> dict[str, Any]:
    """Schema for one task in an exact candidate catalog (not a Draft envelope)."""
    schemas = [contract_task_schema(item) for item in contracts]
    return {"anyOf": [_scope_refs(schema, f"#/anyOf/{index}") for index, schema in enumerate(schemas)]} if schemas else {"not": {}}


def planner_draft_schema(contracts: Iterable[ToolContract]) -> dict[str, Any]:
    """Specialize the Draft envelope to this catalog, preserving local refs."""
    schema = PlannerDraft.model_json_schema()
    schema["$defs"]["CatalogTask"] = _scope_refs(catalog_task_schema(contracts), "#/$defs/CatalogTask")
    schema["properties"]["tasks"]["items"] = {"$ref": "#/$defs/CatalogTask"}
    schema["$defs"].pop("PlannerStepDraft", None)
    return schema


def contract_task_template(contract: ToolContract) -> dict[str, Any]:
    """A labelled, non-executable structure; never invent parameter values."""
    inputs = {}
    for name, port in contract.input_ports.items():
        if not port.required:
            continue
        slot = {"kind": "task_output", "task_key": "<upstream-task-key>",
                "port": "<declared-upstream-output-port>", "selection": "single", "ordinal": 0}
        inputs[name] = [slot] if port.cardinality == "many" else slot
    schema = contract.model_dump()["parameter_schema"]
    return {
        "template_only": True,
        "task": {"task_key": "<unique-task-key>", "op": contract.name, "inputs": inputs, "params": {}},
        "required_parameter_slots": {name: deepcopy(schema.get("properties", {}).get(name, {}))
                                     for name in schema.get("required", [])},
        "output_ports": {name: _port_shape(port) for name, port in contract.output_ports.items()},
    }


def validate_catalog_tasks(tasks: Sequence[PlannerStepDraft | dict[str, Any]],
                           contracts: Iterable[ToolContract]) -> list[dict[str, Any]]:
    """Return structural diagnostics without mutating/coercing caller data.

    Only the supplied request catalog is visible. No aliases are guessed and
    no values are synthesized. Empty tasks are allowed for clarification;
    the caller/Compiler remains responsible for executable-plan policy.
    """
    catalog = {item.name: item for item in contracts}
    raw_tasks = [item.model_dump(mode="json") if isinstance(item, PlannerStepDraft) else item for item in tasks]
    by_key = {item.get("task_key"): item for item in raw_tasks if isinstance(item, dict) and isinstance(item.get("task_key"), str)}
    diagnostics: list[dict[str, Any]] = []

    def report(code: str, path: str, message: str, details: dict[str, Any]) -> None:
        diagnostics.append({"code": code, "path": path, "message": message, "details": details})

    for index, step in enumerate(raw_tasks):
        if not isinstance(step, dict):
            report("PLAN_DRAFT_INVALID", f"tasks[{index}]", "task must be an object", {})
            continue
        task_key = step.get("task_key")
        path = f"tasks.{task_key}" if isinstance(task_key, str) else f"tasks[{index}]"
        op = step.get("op")
        contract = catalog.get(op) if isinstance(op, str) else None
        if contract is None:
            report("OPERATION_NOT_IN_CATALOG", path + ".op", "operation is outside the request catalog",
                   {"op": op, "allowed_operation_ids": sorted(catalog)})
            continue
        inputs, params = step.get("inputs", {}), step.get("params", {})
        if not isinstance(inputs, dict) or not isinstance(params, dict):
            report("PLAN_DRAFT_INVALID", path, "inputs and params must be objects", {"op": op})
            continue
        allowed = sorted(contract.input_ports)
        required = sorted(name for name, port in contract.input_ports.items() if port.required)
        missing = sorted(set(required) - inputs.keys())
        unexpected = sorted(inputs.keys() - contract.input_ports.keys(), key=str)
        port_details = {"op": op, "allowed_ports": allowed, "required_ports": required,
                        "provided_ports": sorted(inputs, key=str), "missing_ports": missing,
                        "unexpected_ports": unexpected,
                        "ports": {name: _port_shape(port) for name, port in contract.input_ports.items()}}
        if missing or unexpected:
            bad_path = path + ".inputs" + (f".{unexpected[0]}" if unexpected else "")
            report("PORT_MISMATCH", bad_path, "input ports do not match the operation contract", port_details)
        for name in sorted(inputs.keys() & contract.input_ports.keys()):
            port, value = contract.input_ports[name], inputs[name]
            input_path = f"{path}.inputs.{name}"
            refs = value if isinstance(value, list) else [value]
            if (isinstance(value, list) and port.cardinality == "one") or len(refs) < port.min_items or (port.max_items is not None and len(refs) > port.max_items):
                report("PORT_MISMATCH", input_path, "input cardinality does not match the contract",
                       {**port_details, "port": name, "provided_count": len(refs)})
                continue
            for ordinal, raw_ref in enumerate(refs):
                ref_path = f"{input_path}[{ordinal}]" if isinstance(value, list) else input_path
                try:
                    if isinstance(raw_ref, (ArtifactRef, TaskOutputRef)):
                        reference = raw_ref
                    elif isinstance(raw_ref, dict) and (raw_ref.get("kind") == "task_output" or "task_key" in raw_ref):
                        reference = TaskOutputRef.model_validate(raw_ref)
                    else:
                        reference = ArtifactRef.model_validate(raw_ref)
                except (ValidationError, ValueError, TypeError) as exc:
                    report("INVALID_REF", ref_path, "input reference is invalid", {"op": op, "port": name, "error": str(exc)})
                    continue
                if reference.selection == "all" and port.cardinality != "many":
                    report("INVALID_REF", ref_path, "selection=all requires a many input port", {"op": op, "port": name})
                    continue
                if isinstance(reference, ArtifactRef):
                    if not port.accepts_type(reference.type):
                        report("TYPE_MISMATCH", ref_path, "artifact type does not match input port",
                               {"op": op, "port": name, "allowed_types": _port_shape(port)["types"], "provided_type": reference.type})
                    continue
                source = by_key.get(reference.task_key)
                source_contract = catalog.get(source.get("op")) if source and isinstance(source.get("op"), str) else None
                if source is None:
                    report("INVALID_REF", ref_path, "unknown task reference", {"source_task_key": reference.task_key})
                elif source_contract is not None and reference.port not in source_contract.output_ports:
                    report("PORT_MISMATCH", ref_path, "unknown source output port",
                           {"op": op, "port": name, "source_task_key": reference.task_key,
                            "source_op": source_contract.name, "provided_output_port": reference.port,
                            "allowed_output_ports": sorted(source_contract.output_ports)})
        schema = contract.parameter_schema
        properties = schema.get("properties", {})
        required_params = sorted(schema.get("required", []))
        missing_params = [name for name in required_params if name not in params and "default" not in properties.get(name, {})]
        unknown_params = sorted(params.keys() - properties.keys(), key=str) if schema.get("additionalProperties", False) is False else []
        if missing_params or unknown_params:
            report("INVALID_PARAMETER", path + ".params", "parameter names do not match the contract",
                   {"op": op, "required_parameters": required_params, "allowed_parameters": sorted(properties),
                    "missing_parameters": missing_params, "unexpected_parameters": unknown_params,
                    "params_schema": contract.model_dump()["parameter_schema"]})
    return diagnostics


__all__ = ["contract_task_schema", "catalog_task_schema", "planner_draft_schema", "contract_task_template", "validate_catalog_tasks"]
