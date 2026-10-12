"""Compiler failures carry contract facts for one bounded planner repair."""

import pytest

from app.runtime.compiler import CompileError, Compiler
from app.runtime.contracts import ArtifactRef, PlannerDraft, PlannerStepDraft


def _draft(op, inputs, params):
    return PlannerDraft(run_id="repair-contract", input_revision=1, user_goal="按分类融合",
        tasks=[PlannerStepDraft(task_key="operation", op=op, inputs=inputs, params=params)])


@pytest.mark.parametrize("inputs,unexpected,missing", [
    ({"vector": ArtifactRef(artifact_id="input", type="Vector")}, ["vector"], ["input"]),
    ({}, [], ["input"]),
])
def test_port_failure_supplies_exact_frozen_contract(inputs, unexpected, missing):
    with pytest.raises(CompileError) as caught:
        Compiler().compile(_draft("vector.dissolve", inputs, {"by": "类别"}))
    error = caught.value
    assert error.code == "PORT_MISMATCH"
    assert error.details == {
        "operation": "vector.dissolve", "allowed_ports": ["input"],
        "required_ports": ["input"], "provided_ports": sorted(inputs),
        "missing_ports": missing, "unexpected_ports": unexpected,
    }


@pytest.mark.parametrize("params,missing,unexpected", [
    ({"old_name": "地名"}, ["new_name"], []),
    ({"old_name": "地名", "new_name": "名称", "new_field": "名称"}, [], ["new_field"]),
])
def test_parameter_failure_supplies_schema_without_guessing_values(params, missing, unexpected):
    ref = ArtifactRef(artifact_id="input", type="Vector", crs={"kind": "EPSG", "code": 4326})
    with pytest.raises(CompileError) as caught:
        Compiler().compile(_draft("attribute.rename_field", {"input": ref}, params))
    error = caught.value
    assert error.code == "INVALID_PARAMETER"
    assert error.details["operation"] == "attribute.rename_field"
    assert error.details["missing_parameters"] == missing
    assert error.details["unexpected_parameters"] == unexpected
    assert error.details["parameter_schema"]["required"] == ["old_name", "new_name"]
    assert "suggested_values" not in error.details
