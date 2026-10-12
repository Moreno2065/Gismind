"""Contract-derived task shapes; no provider, database, or compiler dependency."""
from __future__ import annotations

import importlib
import importlib.util
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from app.runtime.contracts import PortContract, ToolContract, ToolContractRegistry


def api():
    spec = importlib.util.find_spec("app.runtime.task_schema")
    assert spec is not None, "contract-derived task schema module is not implemented"
    return importlib.import_module("app.runtime.task_schema")


def ref(kind="Vector"):
    return {"kind": "artifact", "artifact_id": "uploaded", "type": kind,
            "selection": "single", "ordinal": 0}


def task(op="vector.dissolve", inputs=None, params=None, key="consumer"):
    return {"task_key": key, "op": op, "inputs": inputs or {}, "params": params or {}}


def contracts():
    return list(ToolContractRegistry.with_builtins().values())


def contract(name):
    return ToolContractRegistry.with_builtins().get(name)


def test_dissolve_shape_uses_contract_port_not_artifact_type_name():
    schema = api().contract_task_schema(contract("vector.dissolve"))
    assert schema["properties"]["op"] == {"const": "vector.dissolve"}
    inputs = schema["properties"]["inputs"]
    assert set(inputs["properties"]) == {"input"}
    assert inputs["required"] == ["input"]
    assert inputs["additionalProperties"] is False
    assert schema["properties"]["params"] == contract("vector.dissolve").model_dump()["parameter_schema"]


@pytest.mark.parametrize("operation", sorted(c.name for c in contracts()))
def test_every_builtin_task_schema_is_self_contained_and_contract_derived(operation):
    item = contract(operation)
    schema = api().contract_task_schema(item)
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]["inputs"]["properties"]) == set(item.input_ports)
    assert set(schema["properties"]["inputs"]["required"]) == {
        name for name, port in item.input_ports.items() if port.required
    }
    validator = Draft202012Validator
    validator.check_schema(schema)
    sample = task(operation, {name: ref(port.artifact_type) for name, port in item.input_ports.items()})
    # Required business parameters are intentionally absent. Resolving all input
    # references must still succeed; a dangling $ref would raise here.
    errors = list(validator(schema).iter_errors(sample))
    assert all(list(error.path)[:1] == ["params"] for error in errors)


def test_many_port_schema_retains_scalar_and_list_acceptance_and_bounds():
    validator = Draft202012Validator
    item = ToolContract(name="extension.collect", version="1.0.0", input_ports={
        "sources": PortContract(artifact_type="Vector", cardinality="many", min_items=1, max_items=2)
    }, parameter_schema={"type": "object", "additionalProperties": False, "properties": {}})
    schema = api().contract_task_schema(item)
    check = validator(schema)
    assert check.is_valid(task(item.name, {"sources": ref()}))
    assert check.is_valid(task(item.name, {"sources": [ref(), ref()]}))
    assert not check.is_valid(task(item.name, {"sources": []}))
    assert not check.is_valid(task(item.name, {"sources": [ref(), ref(), ref()]}))
    one = validator(api().contract_task_schema(contract("vector.dissolve")))
    assert not one.is_valid(task(inputs={"input": [ref()]}))


def test_deep_parameter_refs_remain_valid_in_contract_and_catalog_schemas():
    validator = Draft202012Validator
    item = ToolContract(name="extension.deep", version="1.0.0", parameter_schema={
        "type": "object", "additionalProperties": False, "required": ["settings"],
        "properties": {"settings": {"$ref": "#/$defs/Settings"}},
        "$defs": {"Settings": {"type": "object", "additionalProperties": False,
            "required": ["name"], "properties": {"name": {"type": "string"}}}},
    })
    valid = task(item.name, params={"settings": {"name": "actual"}})
    invalid = task(item.name, params={"settings": {"name": 7}})
    for schema in [api().contract_task_schema(item), api().catalog_task_schema([item, contract("vector.dissolve")])]:
        validator.check_schema(schema)
        assert validator(schema).is_valid(valid)
        assert not validator(schema).is_valid(invalid)


def test_schema_generation_does_not_mutate_contract_and_keeps_open_params_open():
    item = ToolContract(name="extension.open", version="1.0.0", parameter_schema={
        "type": "object", "additionalProperties": True,
        "properties": {"optional": {"type": "string", "default": "kept"}},
    })
    before = item.model_dump()
    schema = api().contract_task_schema(item)
    assert schema["properties"]["params"]["additionalProperties"] is True
    assert "required" not in schema["properties"]["params"]
    schema["properties"]["params"]["properties"]["optional"]["default"] = "changed"
    assert item.model_dump() == before


def test_template_identifies_slots_without_inventing_business_values_or_output_port():
    template = api().contract_task_template(contract("vector.buffer"))
    assert template["template_only"] is True
    assert set(template["task"]["inputs"]) == {"vector"}
    assert template["task"]["params"] == {}
    assert template["required_parameter_slots"]["distance_m"]["type"] == "number"
    assert template["task"]["inputs"]["vector"]["port"] == "<declared-upstream-output-port>"
    assert set(template["output_ports"]) == {"vector"}


def test_preflight_diagnoses_same_bad_port_from_real_draft_without_rewriting():
    steps = [task(inputs={"vector": ref()}, params={"by": "分类码"})]
    before = deepcopy(steps)
    errors = api().validate_catalog_tasks(steps, [contract("vector.dissolve")])
    assert len(errors) == 1
    assert errors[0]["code"] == "PORT_MISMATCH"
    assert errors[0]["path"] == "tasks.consumer.inputs.vector"
    details = errors[0]["details"]
    assert details["op"] == "vector.dissolve"
    assert details["allowed_ports"] == details["required_ports"] == ["input"]
    assert details["unexpected_ports"] == ["vector"]
    assert details["missing_ports"] == ["input"]
    assert steps == before


def test_preflight_reports_required_parameters_and_unknown_names():
    errors = api().validate_catalog_tasks([task("vector.buffer", {"vector": ref()}, {"distance": 9})], [contract("vector.buffer")])
    assert errors[0]["code"] == "INVALID_PARAMETER"
    assert errors[0]["details"]["required_parameters"] == ["distance_m"]
    assert errors[0]["details"]["missing_parameters"] == ["distance_m"]
    assert errors[0]["details"]["unexpected_parameters"] == ["distance"]


def test_preflight_leaves_numeric_ranges_and_business_values_to_compiler():
    steps = [task("vector.buffer", {"vector": ref()}, {"distance_m": -1})]
    assert api().validate_catalog_tasks(steps, [contract("vector.buffer")]) == []


@pytest.mark.parametrize("inputs,code", [({"input": [ref()]}, "PORT_MISMATCH"),
    ({"input": ref("Raster")}, "TYPE_MISMATCH"),
    ({"input": {"kind": "task_output", "task_key": "missing", "port": "output"}}, "INVALID_REF"),
    ({"input": {"kind": "artifact", "artifact_id": "uploaded", "ordinal": "0"}}, "INVALID_REF")])
def test_preflight_rejects_invalid_shape_type_and_reference_without_coercion(inputs, code):
    assert api().validate_catalog_tasks([task(inputs=inputs)], [contract("vector.dissolve")])[0]["code"] == code


def test_preflight_uses_producer_contract_for_output_port_diagnostic():
    steps = [task("io.load_vector", {"file": ref("File")}, key="load"),
             task(inputs={"input": {"kind": "task_output", "task_key": "load", "port": "output"}})]
    errors = api().validate_catalog_tasks(steps, [contract("io.load_vector"), contract("vector.dissolve")])
    assert errors[0]["code"] == "PORT_MISMATCH"
    assert errors[0]["details"]["source_op"] == "io.load_vector"
    assert errors[0]["details"]["allowed_output_ports"] == ["vector"]
    steps[1]["inputs"]["input"]["port"] = "vector"
    assert api().validate_catalog_tasks(steps, [contract("io.load_vector"), contract("vector.dissolve")]) == []


def test_preflight_is_bounded_to_request_catalog_not_global_registry():
    errors = api().validate_catalog_tasks([task("vector.buffer", {"vector": ref()}, {"distance_m": 5})], [contract("vector.dissolve")])
    assert errors[0]["code"] == "OPERATION_NOT_IN_CATALOG"
    assert errors[0]["details"]["allowed_operation_ids"] == ["vector.dissolve"]


def test_preflight_accepts_empty_clarification_tasks_and_empty_catalog_rejects_tasks():
    assert api().validate_catalog_tasks([], []) == []
    assert api().validate_catalog_tasks([task()], [])[0]["code"] == "OPERATION_NOT_IN_CATALOG"
    assert api().catalog_task_schema([]) == {"not": {}}


def test_preflight_keeps_compiler_defaults_open_params_and_many_scalar_semantics():
    item = ToolContract(name="extension.defaults", version="1.0.0", input_ports={
        "sources": PortContract(artifact_type="Vector", cardinality="many", min_items=1, max_items=2)
    }, parameter_schema={"type": "object", "additionalProperties": True,
        "required": ["mode"], "properties": {"mode": {"type": "string", "default": "safe"}}})
    steps = [task(item.name, {"sources": ref()}, {"extension_value": 8})]
    before = deepcopy(steps)
    assert api().validate_catalog_tasks(steps, [item]) == []
    assert steps == before
    steps[0]["inputs"]["sources"] = []
    assert api().validate_catalog_tasks(steps, [item])[0]["code"] == "PORT_MISMATCH"


def test_many_minimum_two_does_not_accept_scalar_in_schema_or_preflight():
    validator = Draft202012Validator
    item = ToolContract(name="extension.pairs", version="1.0.0", input_ports={
        "pair": PortContract(artifact_type="Vector", cardinality="many", min_items=2, max_items=2)
    })
    scalar = task(item.name, {"pair": ref()})
    assert not validator(api().contract_task_schema(item)).is_valid(scalar)
    assert api().validate_catalog_tasks([scalar], [item])[0]["code"] == "PORT_MISMATCH"


def test_preflight_accepts_valid_pydantic_steps_without_requiring_plain_dicts():
    from app.runtime.contracts import PlannerStepDraft

    step = PlannerStepDraft.model_validate(task(inputs={"input": ref()}, params={"by": "class"}))
    assert api().validate_catalog_tasks([step], [contract("vector.dissolve")]) == []


def test_planner_draft_schema_scopes_catalog_refs_in_real_envelope():
    module = api()
    assert hasattr(module, "planner_draft_schema"), "request-scoped PlannerDraft schema is not implemented"
    validator = Draft202012Validator
    schema = module.planner_draft_schema([contract("io.load_vector"), contract("vector.dissolve")])
    validator.check_schema(schema)
    assert schema["properties"]["tasks"]["items"] == {"$ref": "#/$defs/CatalogTask"}
    valid = {"tasks": [task("io.load_vector", {"file": ref("File")}, key="load"),
        task(inputs={"input": {"kind": "task_output", "task_key": "load", "port": "vector"}})]}
    assert validator(schema).is_valid(valid)
    invalid = deepcopy(valid)
    invalid["tasks"][1]["inputs"] = {"vector": ref()}
    assert not validator(schema).is_valid(invalid)
    invalid = {"tasks": [task("vector.buffer", {"vector": ref()}, {"distance_m": 2})]}
    assert not validator(schema).is_valid(invalid)
    assert validator(module.planner_draft_schema([])).is_valid({"tasks": [], "origin": "needs_clarification", "clarification": "需要哪一份数据？"})


def test_planner_envelope_preserves_deep_parameter_refs():
    module = api()
    assert hasattr(module, "planner_draft_schema"), "request-scoped PlannerDraft schema is not implemented"
    validator = Draft202012Validator
    item = ToolContract(name="extension.deep", version="1.0.0", parameter_schema={
        "type": "object", "properties": {"config": {"$ref": "#/$defs/Config"}},
        "$defs": {"Config": {"type": "object", "properties": {"label": {"type": "string"}}}},
    })
    schema = module.planner_draft_schema([item])
    assert validator(schema).is_valid({"tasks": [task(item.name, params={"config": {"label": "name"}})]})
    assert not validator(schema).is_valid({"tasks": [task(item.name, params={"config": {"label": 4}})]})
