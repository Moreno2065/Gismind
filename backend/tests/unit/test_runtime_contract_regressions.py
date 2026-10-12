"""Regression coverage for post-cutover Runtime contracts."""

import pytest

from app.runtime import ArtifactRef, CompileError, Compiler, PlannerDraft, PlannerStepDraft, ToolContractRegistry


def test_runtime_registry_excludes_kernel_semantics():
    kernel = {"select_toolkit", "inspect_workspace", "suggest_skill", "load_skill", "proactive_clarification"}
    assert {c.name for c in ToolContractRegistry.with_builtins().values()}.isdisjoint(kernel)


def test_compiler_preserves_operation_and_source_hash():
    source = "__result__ = {'ok': 1}"
    plan = Compiler(ToolContractRegistry.with_builtins()).compile(
        PlannerDraft(user_goal="sandbox", tasks=[PlannerStepDraft(task_key="run", op="code.execute", params={"source_code": source})])
    )
    assert plan.tasks[0].op == "code.execute"
    assert plan.tasks[0].params["source_code_hash"]


def test_code_task_requires_a_stable_name_for_each_bound_artifact():
    step = PlannerStepDraft(
        task_key="run", op="code.execute", params={"source_code": "__result__ = 1"},
        inputs={"input": [ArtifactRef(artifact_id="art_file", type="File")]},
    )
    with pytest.raises(CompileError, match="declare each Artifact"):
        Compiler(ToolContractRegistry.with_builtins()).compile(PlannerDraft(user_goal="inspect", tasks=[step]))

    named = step.model_copy(update={"params": {"source_code": "__result__ = inputs['document']", "declared_inputs": ["document"]}})
    plan = Compiler(ToolContractRegistry.with_builtins()).compile(PlannerDraft(user_goal="inspect", tasks=[named]))
    assert plan.tasks[0].params["declared_inputs"] == ["document"]


def test_runtime_contract_handlers_are_importable():
    for contract in ToolContractRegistry.with_builtins().values():
        module, name = contract.handler_ref.split(":", 1)
        imported = __import__(module, fromlist=[name])
        assert callable(getattr(imported, name))


def test_isochrone_contract_uses_coordinate_input_and_closed_business_params():
    contract = ToolContractRegistry.with_builtins().get("vector.isochrone")

    assert set(contract.input_ports) == {"origin"}
    assert contract.input_ports["origin"].artifact_type == "Coordinate"
    assert contract.output_ports["vector"].artifact_type == "Vector"
    assert contract.parameter_schema["additionalProperties"] is False
    assert set(contract.parameter_schema["required"]) == {"mode", "time_min"}


def test_isochrone_adapter_passes_coordinate_and_params_not_a_geodataframe(monkeypatch):
    from app.runtime.tool_handlers import handle_vector_isochrone
    from app.tools.spatial_analysis import SpatialAnalyzer

    captured = {}

    def fake(self, origin, mode, time_min):
        captured.update(origin=origin, mode=mode, time_min=time_min)
        from shapely.geometry import Polygon
        return {"status": "success", "data": {"geometry": Polygon([(0, 0), (1, 0), (1, 1), (0, 0)])}}

    monkeypatch.setattr(SpatialAnalyzer, "isochrone", fake)
    result = handle_vector_isochrone(
        origin={"data": {"location": [118.78, 32.04]}, "crs": {"kind": "GCJ02"}},
        mode="walking", time_min=15,
    )

    assert result["status"] == "success"
    assert result["data"]["type"] == "FeatureCollection"
    assert result["data"]["features"][0]["geometry"]["type"] == "Polygon"
    assert captured == {"origin": (118.78, 32.04), "mode": "walking", "time_min": 15}


def test_all_public_parameter_schemas_reject_unknown_model_fields():
    open_contracts = [
        contract.name for contract in ToolContractRegistry.with_builtins().values()
        if contract.parameter_schema.get("additionalProperties") is not False
    ]

    assert open_contracts == []


def test_public_contracts_do_not_use_generic_handler_validator_placeholders():
    registry = ToolContractRegistry.with_builtins()

    assert all(contract.validator and "tool_handlers:validate_" not in contract.validator for contract in registry.values())
    assert all(contract.validator.startswith("app.runtime.contracts:_validate_public_") for contract in registry.values())


def test_coordinate_crs_parameters_become_canonical_output_artifact_metadata():
    plan = Compiler(ToolContractRegistry.with_builtins()).compile(
        PlannerDraft(
            user_goal="表格转点",
            tasks=[PlannerStepDraft(
                task_key="points", op="io.csv_to_points",
                inputs={"input": ArtifactRef(artifact_id="table-1", type="Table")},
                params={"crs": "EPSG:3857"},
            )],
        )
    )

    assert plan.tasks[0].outputs[0].crs == {"kind": "EPSG", "code": 3857}
