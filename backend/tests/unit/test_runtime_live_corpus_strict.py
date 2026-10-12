"""Deterministic contracts for the opt-in live planner evidence corpus."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.runtime import Compiler, ToolContractRegistry
from app.runtime.contracts import ArtifactRef, PlannerDraft, PlannerStepDraft, TaskOutputRef


_LIVE_PATH = Path(__file__).parents[1] / "live" / "test_runtime_live_llm_actions.py"


def _live_module():
    spec = importlib.util.spec_from_file_location("runtime_live_actions_under_test", _LIVE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _call(draft):
    return SimpleNamespace(draft=draft, latency_ms=0)


def _metadata(module):
    return {
        item["artifact_id"]: {
            **item,
            "validation_status": "valid",
            "size_bytes": 1,
            "storage_uri": f"artifacts/sha256/{item['content_hash'][:2]}/{item['content_hash']}",
        }
        for item in module._input_artifacts()
    }


def test_static_facts_are_independent_of_registry(monkeypatch):
    module = _live_module()

    def fail_registry(*args, **kwargs):
        raise AssertionError("fixture facts must not consult the Runtime registry")

    monkeypatch.setattr(ToolContractRegistry, "with_builtins", fail_registry)
    artifacts = module._input_artifacts()
    assert {item["artifact_id"] for item in artifacts} == {
        "live-vector", "live-vector-mask", "live-vector-other", "live-vector-polygons",
        "live-vector-gcj02", "live-raster", "live-raster-other", "live-table",
        "live-file", "live-coordinate",
    }
    assert module._static_fixture_facts()["operation_witnesses"]


def test_positive_evaluation_rejects_unexpected_operation():
    module = _live_module()
    draft = PlannerDraft(
        user_goal="静态",
        tasks=[
            PlannerStepDraft(task_key="geocode", op="geo.geocode", params={"address": "南京市玄武区"}),
            PlannerStepDraft(
                task_key="publish", op="publish_file",
                inputs={"input": ArtifactRef(artifact_id="live-file", type="File", crs={"kind": "EPSG", "code": 4326})},
                params={"path": "live-output.bin"},
            ),
        ],
        origin="llm",
    )
    planner = SimpleNamespace(create_draft=lambda request: _call(draft), repair_draft=lambda *args, **kwargs: _call(draft))
    case = module.LiveCase("strict-extra", "positive", "请求坐标", ("geo.geocode",), "llm", {"requires_compile": True})
    compiler = Compiler(ToolContractRegistry.with_builtins(), artifact_lookup=_metadata(module).get, policy={"allowed_path_root": str(Path.cwd())})

    result = module._evaluate(case, planner, compiler, module._input_artifacts())

    assert result["ok"] is False
    assert result["failure"] == "UNEXPECTED_OPERATION"


def test_multistep_evaluation_requires_compiled_task_output_dependency():
    module = _live_module()
    draft = PlannerDraft(
        user_goal="静态",
        tasks=[
            PlannerStepDraft(task_key="load", op="io.load_vector", inputs={"file": ArtifactRef(artifact_id="live-file", type="File", crs={"kind": "EPSG", "code": 4326})}),
            PlannerStepDraft(task_key="buffer", op="vector.buffer", inputs={"vector": ArtifactRef(artifact_id="live-vector", type="Vector", crs={"kind": "EPSG", "code": 4326})}, params={"distance_m": 500}),
        ],
        origin="llm",
    )
    planner = SimpleNamespace(create_draft=lambda request: _call(draft), repair_draft=lambda *args, **kwargs: _call(draft))
    case = module.LiveCase("strict-dependency", "multi_step", "加载后缓冲", ("io.load_vector", "vector.buffer"), "llm", {"requires_compile": True, "must_use_task_output_refs": True})
    compiler = Compiler(ToolContractRegistry.with_builtins(), artifact_lookup=_metadata(module).get, policy={"allowed_path_root": str(Path.cwd())})

    result = module._evaluate(case, planner, compiler, module._input_artifacts())

    assert result["ok"] is False
    assert result["failure"] == "DEPENDENCY_REF_REQUIRED"


def test_multistep_evaluation_checks_compiled_ir_dependency_and_port():
    module = _live_module()
    draft = PlannerDraft(
        user_goal="静态",
        tasks=[
            PlannerStepDraft(task_key="load", op="io.load_vector", inputs={"file": ArtifactRef(artifact_id="live-file", type="File", crs={"kind": "EPSG", "code": 4326})}),
            PlannerStepDraft(task_key="buffer", op="vector.buffer", inputs={"vector": TaskOutputRef(task_key="load", port="vector", ordinal=0)}, params={"distance_m": 500}),
        ],
        origin="llm",
    )
    planner = SimpleNamespace(create_draft=lambda request: _call(draft), repair_draft=lambda *args, **kwargs: _call(draft))
    case = module.LiveCase("strict-dependency-valid", "multi_step", "加载后缓冲", ("io.load_vector", "vector.buffer"), "llm", {"requires_compile": True, "must_use_task_output_refs": True})
    compiler = Compiler(ToolContractRegistry.with_builtins(), artifact_lookup=_metadata(module).get, policy={"allowed_path_root": str(Path.cwd())})

    result = module._evaluate(case, planner, compiler, module._input_artifacts())

    assert result["ok"] is True
    assert result["compiled_task_order"] == ["io.load_vector", "vector.buffer"]
    assert result["dependency_ports"] == {"buffer.vector": "load.vector"}


def test_release_verdict_rejects_one_unsafe_clarification_among_200_cases():
    module = _live_module()
    results = [
        {"case_id": f"case-{index}", "kind": "positive", "ok": True, "repairs": 0}
        for index in range(199)
    ]
    results.append({"case_id": "unsafe-negative", "kind": "negative", "ok": False, "repairs": 0})

    verdict = module._gate_verdict(results)

    assert verdict["final_compile_rate"] == 1.0
    assert verdict["safety_failures"] == ["unsafe-negative"]
    assert verdict["passed"] is False


def test_unsupported_safety_is_distinct_from_task_fulfillment():
    module = _live_module()
    results = [
        {"case_id": "supported", "kind": "positive", "ok": True, "repairs": 0, "task_fulfilled": True},
        {"case_id": "unsupported", "kind": "unsupported", "ok": True, "repairs": 0, "task_fulfilled": False},
    ]

    verdict = module._gate_verdict(results)

    assert verdict["supported_total"] == 1
    assert verdict["unsupported_total"] == 1
    assert verdict["unsupported_safe"] == 1
    assert verdict["task_fulfilled"] == 1
    assert verdict["final_compile_rate"] == 1.0


def test_unsupported_contract_expression_is_grounded_in_production_handler():
    from app.tools.spatial_analysis import SpatialAnalyzer

    assert "$area" in SpatialAnalyzer.field_calculator.__doc__
    import inspect
    source = inspect.getsource(SpatialAnalyzer.field_calculator)
    assert 'expr_lower.startswith("$area")' in source
    assert 'expr_lower.startswith("$length")' in source
    assert '"仅支持 $area 和 $length 表达式"' in source


def test_compiled_oracle_rejects_wrong_explicit_business_parameter():
    module = _live_module()
    case = next(case for case in module._cases() if case.case_id == "positive-001")
    draft = PlannerDraft(
        user_goal=case.prompt, origin="llm",
        tasks=[PlannerStepDraft(task_key="geocode", op="geo.geocode", params={"address": "北京"})],
    )
    plan = Compiler(ToolContractRegistry.with_builtins()).compile(draft)

    ok, failure, _ = module._compiled_semantics(case, plan)

    assert ok is False
    assert failure == "BUSINESS_PARAMETER_MISMATCH"


def test_compiled_oracle_rejects_wrong_visible_artifact_id():
    module = _live_module()
    case = next(case for case in module._cases() if case.case_id == "positive-011")
    metadata = _metadata(module)
    metadata["other-vector"] = {**metadata["live-vector"], "artifact_id": "other-vector"}
    draft = PlannerDraft(
        user_goal=case.prompt, origin="llm",
        tasks=[PlannerStepDraft(
            task_key="buffer", op="vector.buffer",
            inputs={"vector": ArtifactRef(artifact_id="other-vector", type="Vector", crs={"kind": "EPSG", "code": 4326})},
            params={"distance_m": 500},
        )],
    )
    plan = Compiler(ToolContractRegistry.with_builtins(), artifact_lookup=metadata.get).compile(draft)

    ok, failure, _ = module._compiled_semantics(case, plan)

    assert ok is False
    assert failure == "ARTIFACT_BINDING_MISMATCH"


def test_compiled_oracle_checks_side_effect_confirmation_annotation():
    module = _live_module()
    case = module.LiveCase(
        "must-confirm", "positive", "请求坐标", ("geo.geocode",), "llm",
        {"requires_compile": True, "requires_confirmation": True},
    )
    draft = PlannerDraft(
        user_goal=case.prompt, origin="llm",
        tasks=[PlannerStepDraft(
            task_key="geocode", op="geo.geocode", params={"address": "南京市玄武区"},
        )],
    )
    plan = Compiler(ToolContractRegistry.with_builtins()).compile(draft)

    ok, failure, _ = module._compiled_semantics(case, plan)

    assert ok is False
    assert failure == "CONFIRMATION_POLICY_MISSING"


def test_explicit_user_facts_override_generic_operation_witnesses():
    module = _live_module()
    witnesses = module._static_fixture_facts()["operation_witnesses"]
    assert witnesses["vector.buffer"]["required_params"]["distance_m"] == 500
    assert witnesses["vector.simplify"]["required_params"]["tolerance"] == 10
    assert witnesses["poi.search"]["required_params"]["radius"] == 2000
    assert witnesses["attribute.keep_fields"]["required_params"]["fields"] == ["名称", "人口"]
    assert witnesses["attribute.filter"]["required_params"] == {
        "field": "人口", "operator": ">", "value": 10000,
    }
    assert witnesses["attribute.rename_field"]["required_params"] == {
        "old_name": "old_name", "new_name": "new_name",
    }
    assert witnesses["vector.convert_datum"]["required_params"] == {
        "source_crs": {"kind": "GCJ02"}, "target_crs": {"kind": "WGS84"},
    }


def test_binary_operation_witnesses_distinguish_input_roles():
    module = _live_module()
    witnesses = module._static_fixture_facts()["operation_witnesses"]
    assert witnesses["vector.clip"]["required_inputs"]["input"] != witnesses["vector.clip"]["required_inputs"]["mask"]
    assert witnesses["vector.join_nearest"]["required_inputs"]["input"] != witnesses["vector.join_nearest"]["required_inputs"]["other"]
    assert witnesses["vector.count_points"]["required_inputs"]["points"] != witnesses["vector.count_points"]["required_inputs"]["polygons"]


def test_many_port_accepts_expected_direct_artifact_after_an_upstream_output():
    module = _live_module()
    metadata = _metadata(module)
    case = module.LiveCase(
        "merge-chain", "multi_step", "先读取文件，再与另一图层合并",
        ("io.load_vector", "vector.merge"), "llm",
        {"requires_compile": True, "must_use_task_output_refs": True},
    )
    draft = PlannerDraft(
        user_goal=case.prompt, origin="llm",
        tasks=[
            PlannerStepDraft(task_key="load", op="io.load_vector", inputs={
                "file": ArtifactRef(artifact_id="live-file", type="File", crs={"kind": "EPSG", "code": 4326}),
            }),
            PlannerStepDraft(task_key="merge", op="vector.merge", inputs={"input": [
                TaskOutputRef(task_key="load", port="vector"),
                ArtifactRef(artifact_id="live-vector-other", type="Vector", crs={"kind": "EPSG", "code": 4326}),
            ]}),
        ],
    )
    plan = Compiler(ToolContractRegistry.with_builtins(), artifact_lookup=metadata.get).compile(draft)

    ok, failure, _ = module._compiled_semantics(case, plan)

    assert ok is True, failure


def test_unsupported_calculations_remain_clarifications_with_original_prompt():
    module = _live_module()
    cases = {case.case_id: case for case in module._cases()}
    for case_id in ("positive-038", "positive-043", "multi-015", "multi-037"):
        case = cases[case_id]
        assert case.kind == "unsupported"
        assert case.expected_origin == "needs_clarification"
        assert case.expected_ops == ()
        assert module._request_prompt(case) == case.prompt
    assert cases["positive-060"].expected_ops == ("attribute.calculate",)
    assert cases["positive-060"].semantic["expected_params"]["attribute.calculate"] == {
        "field_name": "score", "expression": "$area * 2",
    }


def test_case_specific_fact_overrides_witness_for_exact_keep_fields():
    module = _live_module()
    case = next(case for case in module._cases() if case.case_id == "multi-007")
    expected = case.semantic["expected_params"]["attribute.keep_fields"]
    assert expected == {"fields": ["名称"]}
    assert {"fields": ["名称"]} in [block["required_params"] for block in module._case_witness(case)]
