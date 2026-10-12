import pytest

from app.runtime.compiler import AwaitingInputDiagnostic, CompileError, Compiler
from app.runtime.planner import PlanDraftInvalid, PlannerService
from app.runtime.contracts import (
    ArtifactRef,
    CachePolicy,
    ConfirmationPolicy,
    PlannerDraft,
    PlannerStepDraft,
    PortContract,
    ResourceBudget,
    RetryPolicy,
    TaskOutputRef,
    ToolContract,
    ToolContractRegistry,
)


def _registry() -> ToolContractRegistry:
    return ToolContractRegistry.with_builtins()


def _draft(*steps, run_id="run-1", input_revision=1, external_inputs=None):
    return PlannerDraft(
        run_id=run_id,
        input_revision=input_revision,
        user_goal="test plan",
        tasks=list(steps),
        external_inputs=external_inputs or [],
        origin="test",
    )


def _step(task_key, op, inputs=None, params=None):
    return PlannerStepDraft(
        task_key=task_key,
        op=op,
        inputs=inputs or {},
        params=params or {},
    )


def test_contract_models_parse_and_canonical_plan_is_serializable():
    ref = ArtifactRef(artifact_id="a1", type="Vector", crs={"kind": "EPSG", "code": 4326})
    assert ref.selection == "single"
    assert ref.ordinal == 0
    contract = _registry().get("vector.buffer")
    assert contract.input_ports["vector"].artifact_type == "Vector"
    assert contract.retry_policy.max_attempts >= 1
    plan = Compiler(_registry()).compile(
        _draft(_step("buffer", "vector.buffer", {"vector": ref}, {"distance_m": 5}))
    )
    assert plan.plan_hash == plan.plan_hash
    assert plan.model_dump(mode="json")["tasks"][0]["op"] == "vector.buffer"


def test_planner_service_allows_one_repair_and_has_stable_error():
    calls = []
    service = PlannerService(repair_callback=lambda value, error: calls.append(value) or {"run_id": "r", "input_revision": 1, "user_goal": "ok", "tasks": []})
    assert service.parse_and_repair("not-json").run_id == "r"
    assert calls == ["not-json"]
    with pytest.raises(PlanDraftInvalid) as exc:
        PlannerService(repair_callback=lambda value, error: {"tasks": [{}]}).parse_and_repair("not-json")
    assert exc.value.code == "PLAN_DRAFT_INVALID"


def test_map_and_datum_contracts_are_documented_shapes():
    registry = _registry()
    map_contract = registry.get("map.build")
    assert tuple(map_contract.input_ports) == ("vector",)
    assert map_contract.output_ports["layer"].artifact_type == "MapLayer"
    assert registry.get("vector.convert_datum").input_ports["vector"].artifact_type == "Vector"


def test_coordinate_and_reprojection_contracts_freeze_normalized_crs_shapes():
    registry = _registry()
    transform = registry.get("geo.transform")
    assert transform.version == "1.1.0"
    assert set(transform.parameter_schema["required"]) == {"lng", "lat", "source_crs", "target_crs"}
    plan = Compiler(registry).compile(_draft(_step(
        "transform", "geo.transform",
        params={
            "lng": 118.78, "lat": 32.04,
            "source_crs": {"kind": "WGS84"}, "target_crs": {"kind": "GCJ02"},
        },
    )))
    assert plan.tasks[0].outputs[0].crs == {"kind": "GCJ02"}

    for operation in ("vector.reproject", "vector.reproject_many"):
        assert registry.get(operation).parameter_schema["properties"]["target_crs"]["type"] == "object"


def test_compile_plan_rejects_unknown_options():
    from app.runtime.compiler import compile_plan
    with pytest.raises(CompileError) as exc:
        compile_plan(_draft(), not_a_compiler_option=True)
    assert exc.value.code == "INVALID_PARAMETER"


def test_metric_crs_selector_is_unique_or_awaits_input():
    from app.runtime.compiler import metric_crs_selector
    assert metric_crs_selector({"xmin": 110, "ymin": 30, "xmax": 111, "ymax": 31}, [{"crs": {"kind": "EPSG", "code": 4547}, "bbox": [109, 29, 112, 32], "unit": "m"}])["code"] == 4547
    with pytest.raises(AwaitingInputDiagnostic):
        metric_crs_selector({"xmin": 0, "ymin": 0, "xmax": 1, "ymax": 1}, [])


def test_compiler_can_select_metric_crs_and_freezes_selector_version_in_plan():
    ref = ArtifactRef(artifact_id="a", type="Vector", crs={"kind": "EPSG", "code": 4326})
    policy = {"metric_bbox": {"xmin": 110, "ymin": 30, "xmax": 111, "ymax": 31}, "metric_crs_candidates": [{"crs": {"kind": "EPSG", "code": 4547}, "bbox": [109, 29, 112, 32], "unit": "m"}], "metric_crs_selector_version": "2.0.0"}
    plan = Compiler(_registry(), policy=policy, auto_reproject=True).compile(_draft(_step("buffer", "vector.buffer", {"vector": ref}, {"distance_m": 1})))
    assert any(task.op == "vector.reproject" for task in plan.tasks)
    assert "metric_crs_selector_version" in plan.canonical_json


def test_file_declared_crs_requires_provenance_wrapper():
    ref = ArtifactRef(artifact_id="f", type="File")
    row = {"validation_status": "valid", "type": "File", "crs": None, "schema": {"file": {"declared_crs": {"kind": "EPSG", "code": 4326}}}, "content_hash": "a" * 64, "storage_uri": "artifacts/sha256/aa/" + "a" * 64, "size_bytes": 1}
    with pytest.raises(CompileError) as exc:
        Compiler(_registry(), artifact_lookup=lambda _: row).compile(_draft(_step("load", "io.load_vector", {"file": ref})))
    assert exc.value.code == "INVALID_REF"


def test_metric_selector_rejects_geographic_and_non_metric_candidates():
    from app.runtime.compiler import metric_crs_selector
    candidates = [{"crs": {"kind": "WGS84"}, "bbox": [0, 0, 2, 2], "unit": "degree"}]
    with pytest.raises((CompileError, AwaitingInputDiagnostic)):
        metric_crs_selector({"xmin": 0, "ymin": 0, "xmax": 1, "ymax": 1}, candidates)


def test_direct_ir_models_reject_undocumented_types_and_empty_identity():
    with pytest.raises(ValueError):
        ArtifactRef(artifact_id="a", type="NotAnArtifact")
    with pytest.raises(ValueError):
        from app.runtime.contracts import OutputSpec
        OutputSpec(port="", artifact_type="Vector")
    with pytest.raises(ValueError):
        from app.runtime.contracts import CompiledTask
        CompiledTask(task_key="x", op="x", contract_version="", origin="planner")
    with pytest.raises(ValueError):
        from app.runtime.contracts import PlanIR
        PlanIR()


def test_manifest_has_real_callable_refs_and_contract_hash():
    import importlib
    manifest = _registry().public_operations_manifest()
    operation = manifest["operations"]["vector.buffer"]
    module, name = operation["handler_ref"].split(":", 1)
    assert callable(getattr(importlib.import_module(module), name))
    assert operation["contract_hash"] and operation["validator_ref"]


def test_checked_in_manifest_and_independent_baseline_are_present():
    from pathlib import Path
    manifest_path = Path("app/runtime/contracts/public_operations_manifest.json")
    baseline_path = Path(__file__).resolve().parents[3] / "docs" / "legacy_public_operations_baseline.json"
    assert manifest_path.is_file()
    assert baseline_path.is_file()
    manifest = __import__("json").loads(manifest_path.read_text(encoding="utf-8"))
    baseline = __import__("json").loads(baseline_path.read_text(encoding="utf-8"))
    assert manifest == _registry().public_operations_manifest()
    assert set(manifest["operations"]).issubset(set(baseline["operations"]))
    assert all(item.get("contract_hash") and item.get("handler_ref") and item.get("validator_ref") and item.get("test_node") for item in baseline["operations"].values())


def test_registry_rejects_unknown_and_duplicate_versions():
    registry = ToolContractRegistry()
    contract = ToolContract(
        name="x.test",
        version="1.0.0",
        input_ports={},
        output_ports={},
        parameter_schema={},
        resources=ResourceBudget(isolation="worker_async", timeout_seconds=1, memory_mb=1),
        retry_policy=RetryPolicy(max_attempts=1),
        cache_policy=CachePolicy(mode="disabled"),
    )
    registry.register(contract)
    with pytest.raises(ValueError, match="version conflict"):
        registry.register(contract)
    with pytest.raises(CompileError) as exc:
        Compiler(registry).compile(_draft(_step("x", "missing.tool")))
    assert exc.value.code == "UNKNOWN_TOOL"


def test_compiler_rejects_duplicate_keys_cycles_and_bad_refs():
    registry = _registry()
    duplicate = _draft(_step("a", "vector.buffer"), _step("a", "map.build"))
    with pytest.raises(CompileError) as exc:
        Compiler(registry).compile(duplicate)
    assert exc.value.code == "DUPLICATE_TASK_KEY"

    cycle = _draft(
        _step("a", "vector.buffer", {"vector": TaskOutputRef(task_key="b", port="vector")}),
        _step("b", "vector.buffer", {"vector": TaskOutputRef(task_key="a", port="vector")}),
    )
    with pytest.raises(CompileError) as exc:
        Compiler(registry).compile(cycle)
    assert exc.value.code == "DAG_CYCLE"

    with pytest.raises(CompileError) as exc:
        Compiler(registry).compile(_draft(_step("a", "vector.buffer", {"missing": ArtifactRef(artifact_id="a1", type="Vector")})))
    assert exc.value.code in {"INVALID_REF", "PORT_MISMATCH"}


def test_port_cardinality_and_artifact_selection_are_checked():
    registry = _registry()
    with pytest.raises(CompileError) as exc:
        Compiler(registry).compile(
            _draft(_step("buffer", "vector.buffer", {"vector": TaskOutputRef(task_key="source", port="vector", selection="all", ordinal=None)}))
        )
    assert exc.value.code == "INVALID_REF"
    with pytest.raises(ValueError):
        ArtifactRef(artifact_id="a", type="Vector", ordinal=-1)


def test_artifact_refs_must_be_complete_valid_cas_records():
    ref = ArtifactRef(artifact_id="a1", type="Vector", content_hash="a" * 64)
    registry = _registry()
    with pytest.raises(CompileError) as exc:
        Compiler(registry, artifact_lookup=lambda _: {"validation_status": "invalid"}).compile(
            _draft(_step("buffer", "vector.buffer", {"vector": ref}, {"distance_m": 5}))
        )
    assert exc.value.code == "INVALID_REF"


def test_crs_missing_is_awaiting_input_and_conflict_inserts_reproject():
    registry = _registry()
    with pytest.raises(AwaitingInputDiagnostic) as exc:
        Compiler(registry).compile(
            _draft(_step("buffer", "vector.buffer", {"vector": ArtifactRef(artifact_id="a", type="Vector")}))
        )
    assert exc.value.code == "CRS_REQUIRED"
    plan = Compiler(registry).compile(
        _draft(
            _step("buffer", "vector.buffer", {"vector": ArtifactRef(artifact_id="a", type="Vector", crs={"kind": "EPSG", "code": 4326})}, {"distance_m": 5})
        ),
        auto_reproject=True,
        target_crs={"kind": "EPSG", "code": 4548},
    )
    assert any(task.op == "vector.reproject" and task.origin == "compiler" for task in plan.tasks)
    assert [task.task_key for task in plan.tasks].index("buffer__reproject__vector") < [task.task_key for task in plan.tasks].index("buffer")


def test_gcj02_is_not_treated_as_an_epsg_code():
    with pytest.raises(ValueError):
        ArtifactRef(artifact_id="a", type="Vector", crs={"kind": "EPSG", "code": "GCJ02"})


def test_canonical_hash_ignores_task_and_binding_order():
    vector = ArtifactRef(artifact_id="a", type="Vector", crs={"kind": "EPSG", "code": 4326})
    first = Compiler(_registry()).compile(
        _draft(_step("a", "vector.buffer", {"vector": vector}, {"distance_m": 5}), _step("b", "map.build", {"layer": TaskOutputRef(task_key="a", port="vector")}))
    )
    second = Compiler(_registry()).compile(
        _draft(_step("b", "map.build", {"layer": TaskOutputRef(task_key="a", port="vector")}), _step("a", "vector.buffer", {"vector": vector}, {"distance_m": 5}))
    )
    assert first.plan_hash == second.plan_hash
    assert first.canonical_json == second.canonical_json


def test_policy_confirmation_retry_cache_and_code_mode_are_frozen():
    registry = ToolContractRegistry()
    side_effect = ToolContract(
        name="io.export",
        version="1.0.0",
        input_ports={"input": PortContract(artifact_type="Vector")},
        output_ports={"file": PortContract(artifact_type="File")},
        parameter_schema={"type": "object", "additionalProperties": False, "required": ["path"], "properties": {"path": {"type": "string"}}},
        side_effect=True,
        confirmation_policy=ConfirmationPolicy(mode="required", action_fields=["path"]),
        retry_policy=RetryPolicy(max_attempts=2, retryable_error_codes=["TIMEOUT"], backoff_seconds=[1]),
        cache_policy=CachePolicy(mode="ttl", ttl_seconds=60),
        resources=ResourceBudget(isolation="code_process", timeout_seconds=10, memory_mb=128),
        executor_type="code",
        validator="app.runtime.contracts:_validate_file_export",
    )
    registry.register(side_effect)
    vector = ArtifactRef(artifact_id="a", type="Vector", crs={"kind": "EPSG", "code": 4326})
    plan = Compiler(registry, policy={"allowed_path_root": "C:\\safe"}).compile(_draft(_step("export", "io.export", {"input": vector}, {"path": "out.gpkg"})))
    task = plan.tasks[0]
    assert task.confirmation_policy.mode == "required"
    assert task.action_hash
    assert task.retry_policy.max_attempts == 2
    assert task.cache_policy.ttl_seconds == 60
    assert task.executor_type == "code"
    assert task.resource_budget.memory_mb == 128


def test_registry_supports_distinct_versions_and_explicit_lookup():
    registry = ToolContractRegistry()
    base = ToolContract(name="x.test", version="1.0.0")
    newer = base.model_copy(update={"version": "2.0.0"})
    registry.register(base)
    registry.register(newer)
    assert registry.get("x.test", "1.0.0").version == "1.0.0"
    assert registry.get("x.test", "2.0.0").version == "2.0.0"
    with pytest.raises(ValueError, match="version conflict"):
        registry.register(base)


def test_source_output_ports_are_checked_even_when_consumer_precedes_source():
    vector = ArtifactRef(artifact_id="a", type="Vector", crs={"kind": "EPSG", "code": 4326})
    draft = _draft(
        _step("map", "map.build", {"layer": TaskOutputRef(task_key="buffer", port="missing")}),
        _step("buffer", "vector.buffer", {"vector": vector}, {"distance_m": 5}),
    )
    with pytest.raises(CompileError) as exc:
        Compiler(_registry()).compile(draft)
    assert exc.value.code == "PORT_MISMATCH"


def test_external_inputs_are_frozen_into_plan_ir_and_budget_is_enforced():
    ref = ArtifactRef(artifact_id="input-1", type="Vector", crs={"kind": "EPSG", "code": 4326})
    plan = Compiler(_registry()).compile(_draft(_step("buffer", "vector.buffer", {"vector": ref}, {"distance_m": 5}), external_inputs=[ref]))
    assert plan.input_artifact_ids == ["input-1"]
    with pytest.raises(CompileError) as exc:
        Compiler(_registry(), policy={"max_tasks": 0}).compile(_draft(_step("buffer", "vector.buffer", {"vector": ref}, {"distance_m": 5})))
    assert exc.value.code == "RESOURCE_BUDGET_EXCEEDED"


def test_selection_all_requires_a_many_source_and_input_port():
    default_many = PortContract(cardinality="many")
    assert default_many.min_items == 0
    assert default_many.max_items is None
    many = PortContract(artifact_type="Vector", cardinality="many", min_items=0, max_items=None)
    source = ToolContract(name="source", version="1.0.0", output_ports={"items": many})
    sink = ToolContract(name="sink", version="1.0.0", input_ports={"items": many}, output_ports={"out": many})
    registry = ToolContractRegistry([source, sink])
    plan = Compiler(registry).compile(_draft(_step("source", "source"), _step("sink", "sink", {"items": TaskOutputRef(task_key="source", port="items", selection="all", ordinal=None)})))
    sink_task = next(task for task in plan.tasks if task.task_key == "sink")
    assert sink_task.inputs["items"].selection == "all"


def test_external_only_artifacts_and_schema_mismatch_are_rejected_or_frozen():
    ref = ArtifactRef(artifact_id="input-1", type="Vector", crs={"kind": "EPSG", "code": 4326})
    with pytest.raises(CompileError) as exc:
        Compiler(_registry()).compile(_draft(external_inputs=[ref]))
    assert exc.value.code == "EMPTY_PLAN"
    with pytest.raises(CompileError) as exc:
        Compiler(_registry(), artifact_lookup=lambda _: {"validation_status": "invalid"}).compile(
            _draft(_step("buffer", "vector.buffer", {"vector": ref}, {"distance_m": 5}), external_inputs=[ref])
        )
    assert exc.value.code == "INVALID_REF"

    contract = ToolContract(
        name="schema.sink",
        version="1.0.0",
        input_ports={"vector": PortContract(artifact_type="Vector", schema={"required": ["name"]})},
        output_ports={"out": PortContract(artifact_type="Vector")},
    )
    with pytest.raises(CompileError) as exc:
        Compiler(ToolContractRegistry([contract]), artifact_lookup=lambda _: {"validation_status": "valid", "type": "Vector", "crs": ref.crs, "storage_uri": "artifacts/sha256/aa/" + "a" * 64, "content_hash": "a" * 64, "schema": {}, "size_bytes": 1}).compile(
            _draft(_step("sink", "schema.sink", {"vector": ref}))
        )
    assert exc.value.code == "TYPE_MISMATCH"


def test_crs_conflict_between_required_same_crs_inputs_is_diagnostic():
    contract = ToolContract(
        name="crs.join",
        version="1.0.0",
        input_ports={
            "left": PortContract(artifact_type="Vector", crs_policy={"required": True, "same_as": "right"}),
            "right": PortContract(artifact_type="Vector", crs_policy={"required": True}),
        },
        output_ports={"out": PortContract(artifact_type="Vector")},
    )
    left = ArtifactRef(artifact_id="left", type="Vector", crs={"kind": "EPSG", "code": 4326})
    right = ArtifactRef(artifact_id="right", type="Vector", crs={"kind": "EPSG", "code": 3857})
    with pytest.raises(CompileError) as exc:
        Compiler(ToolContractRegistry([contract])).compile(_draft(_step("join", "crs.join", {"left": left, "right": right})))
    assert exc.value.code == "CRS_CONFLICT"


def test_coordinate_contract_rejects_gcj02_in_parameter_crs():
    ref = ArtifactRef(artifact_id="coords", type="Table", crs={"kind": "EPSG", "code": 4326})
    with pytest.raises(CompileError) as exc:
        Compiler(_registry()).compile(_draft(_step("convert", "coordinate.convert", {"coordinates": ref}, {"source_crs": {"kind": "EPSG", "code": "GCJ02"}, "target_crs": {"kind": "EPSG", "code": 4326}})))
    assert exc.value.code == "GCJ02_NOT_EPSG"


def test_draft_bindings_are_frozen_and_compile_plan_accepts_registry_argument():
    ref = ArtifactRef(artifact_id="input-1", type="Vector", crs={"kind": "EPSG", "code": 4326})
    draft = _draft(_step("buffer", "vector.buffer", {"vector": ref}, {"distance_m": 5}))
    draft = draft.model_copy(update={"bindings": [{"target": "buffer.vector", "source": "input-1"}]})
    from app.runtime.compiler import compile_plan
    plan = compile_plan(draft, registry=_registry())
    assert plan.bindings == [{"source": "input-1", "target": "buffer.vector"}]


def test_parameter_schema_defaults_and_enum_values_are_canonicalized():
    contract = ToolContract(
        name="params.test",
        version="1.0.0",
        parameter_schema={"type": "object", "properties": {"distance_m": {"type": "number", "default": 10}, "mode": {"type": "string", "enum": ["fast", "safe"], "default": "safe"}}},
        output_ports={"out": PortContract(artifact_type="Vector")},
    )
    registry = ToolContractRegistry([contract])
    plan = Compiler(registry).compile(_draft(_step("p", "params.test")))
    assert plan.tasks[0].params == {"distance_m": 10, "mode": "safe"}
    with pytest.raises(CompileError) as exc:
        Compiler(registry).compile(_draft(_step("p", "params.test", params={"mode": "unsafe"})))
    assert exc.value.code == "INVALID_PARAMETER"


def test_malformed_draft_returns_stable_compile_error_instead_of_pydantic_error():
    with pytest.raises(CompileError) as exc:
        Compiler(_registry()).compile({"draft_version": "2", "tasks": "not-a-list"})
    assert exc.value.code == "PLAN_DRAFT_INVALID"
    assert exc.value.path == "draft"


def test_contract_models_are_strict_deep_frozen_and_accept_documented_steps_shape():
    ref = ArtifactRef(artifact_id="a", type="Vector", crs={"kind": "EPSG", "code": 4326})
    with pytest.raises((TypeError, ValueError)):
        ref.crs["code"] = 3857
    with pytest.raises(ValueError):
        ArtifactRef(artifact_id="a", type="Vector", ordinal=1.0)
    with pytest.raises(ValueError):
        ArtifactRef(artifact_id="a", type="Vector", selection="all", ordinal=0)
    with pytest.raises(ValueError):
        TaskOutputRef(task_key="t", port="p", selection="single", ordinal=None)
    with pytest.raises(ValueError):
        PlannerDraft(input_revision=True)
    assert PlannerDraft.model_validate({"user_goal": "documented", "steps": [{"id": "x", "op": "map.build"}]}).tasks[0].task_key == "x"


def test_required_task_and_ref_fields_are_non_empty():
    with pytest.raises(ValueError):
        PlannerStepDraft(task_key="", op="vector.buffer")
    with pytest.raises(ValueError):
        PlannerStepDraft(task_key="task", op="")
    with pytest.raises(ValueError):
        ArtifactRef(artifact_id="", type="Vector")
    with pytest.raises(ValueError):
        PlannerDraft(input_revision=0)


def test_nested_contract_lists_are_immutable():
    contract = _registry().get("vector.buffer")
    with pytest.raises((TypeError, ValueError)):
        contract.validators.append("mutated")


def test_registry_default_version_uses_numeric_semver_order():
    registry = ToolContractRegistry([ToolContract(name="x", version="9.0.0"), ToolContract(name="x", version="10.0.0")])
    assert registry.get("x").version == "10.0.0"


def test_required_ports_unknown_parameters_and_units_are_enforced():
    contract = ToolContract(
        name="strict.params",
        version="1.0.0",
        input_ports={"input": PortContract(artifact_type="Vector")},
        output_ports={"output": PortContract(artifact_type="Vector")},
        parameter_schema={"type": "object", "additionalProperties": False, "required": ["distance"], "properties": {"distance": {"type": "number", "unit": "m"}}},
    )
    registry = ToolContractRegistry([contract])
    ref = ArtifactRef(artifact_id="a", type="Vector", crs={"kind": "EPSG", "code": 4326})
    plan = Compiler(registry).compile(_draft(_step("x", "strict.params", {"input": ref}, {"distance": {"value": 2, "unit": "km"}})))
    assert plan.tasks[0].params["distance"] == 2000
    with pytest.raises(CompileError) as exc:
        Compiler(registry).compile(_draft(_step("x", "strict.params", {"input": ref}, {"distance": {"value": 2, "unit": "mile"}})))
    assert exc.value.code == "INVALID_PARAMETER"


def test_reprojection_key_conflict_is_stable_compile_error():
    ref = ArtifactRef(artifact_id="a", type="Vector", crs={"kind": "EPSG", "code": 4326})
    draft = _draft(_step("buffer__reproject__vector", "map.build", {"layer": ref}), _step("buffer", "vector.buffer", {"vector": ref}, {"distance_m": 5}))
    with pytest.raises(CompileError) as exc:
        Compiler(_registry()).compile(draft, auto_reproject=True, target_crs={"kind": "EPSG", "code": 4548})
    assert exc.value.code == "DUPLICATE_TASK_KEY"


def test_policy_models_reject_invalid_fallback_retry_and_cache_values():
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=2, backoff_seconds=[float("nan")])
    with pytest.raises(ValueError):
        CachePolicy(mode="disabled", ttl_seconds=30)
    with pytest.raises(ValueError):
        CachePolicy(mode="ttl")
    with pytest.raises(ValueError):
        ToolContract(name="fallback", version="1", fallback_policy={"mode": "ordered"})


def test_cache_and_action_fingerprints_include_compiler_and_artifact_identity():
    row = {"validation_status": "valid", "type": "Vector", "storage_uri": "artifacts/sha256/aa/" + "a" * 64, "content_hash": "a" * 64, "schema": {"fields": {"x": "number"}}, "crs": {"kind": "EPSG", "code": 4326}, "size_bytes": 1}
    ref = ArtifactRef(artifact_id="a", type="Vector", crs=row["crs"])
    contract = ToolContract(name="side", version="1.0.0", input_ports={"input": PortContract(artifact_type="Vector")}, output_ports={"out": PortContract(artifact_type="File")}, side_effect=True, confirmation_policy=ConfirmationPolicy(mode="required", action_fields=["path"]), parameter_schema={"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}}, cache_policy=CachePolicy(mode="permanent"))
    registry = ToolContractRegistry([contract])
    first = Compiler(registry, artifact_lookup=lambda _: row, compiler_version="1.0.0", policy={"allowed_path_root": "C:\\safe"}).compile(_draft(_step("side", "side", {"input": ref}, {"path": "result.txt"})))
    second = Compiler(registry, artifact_lookup=lambda _: {**row, "content_hash": "b" * 64, "storage_uri": "artifacts/sha256/bb/" + "b" * 64}, compiler_version="2.0.0", policy={"allowed_path_root": "C:\\safe"}).compile(_draft(_step("side", "side", {"input": ref}, {"path": "result.txt"})))
    assert first.tasks[0].cache_key is None
    assert second.tasks[0].cache_key is None
    assert first.tasks[0].execution_fingerprint != second.tasks[0].execution_fingerprint
    assert first.tasks[0].action_hash != second.tasks[0].action_hash


def test_side_effect_path_is_confined_and_code_execute_is_declared():
    with pytest.raises(CompileError) as exc:
        Compiler(_registry(), policy={"allowed_path_root": "C:\\safe"}).compile(_draft(_step("export", "io.export", {"input": ArtifactRef(artifact_id="a", type="File")}, {"path": "..\\escape.gpkg"})))
    assert exc.value.code == "INVALID_PARAMETER"
    assert _registry().get("code.execute").executor_type == "code"
    with pytest.raises(CompileError) as exc:
        Compiler(_registry()).compile(_draft(_step("run", "code.execute", params={"source_code": "x=1", "tool_fns": {}})))
    assert exc.value.code == "INVALID_PARAMETER"


def test_compiler_package_is_lightweight_without_loading_db_or_artifacts():
    import subprocess
    import sys
    result = subprocess.run([sys.executable, "-c", "import sys; import app.runtime.compiler; assert 'app.runtime.db' not in sys.modules and 'app.runtime.artifacts' not in sys.modules"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_required_input_ports_and_crs_shape_are_strict():
    contract = ToolContract(name="required", version="1.0.0", input_ports={"input": PortContract(artifact_type="Vector")}, output_ports={"out": PortContract(artifact_type="Vector")})
    with pytest.raises(CompileError) as exc:
        Compiler(ToolContractRegistry([contract])).compile(_draft(_step("x", "required")))
    assert exc.value.code == "PORT_MISMATCH"
    with pytest.raises(ValueError):
        ArtifactRef(artifact_id="a", type="Vector", crs={"kind": "EPSG", "code": "4326"})
    with pytest.raises(ValueError):
        ArtifactRef(artifact_id="a", type="Vector", crs={"kind": "EPSG", "code": 0})


def test_unit_parameter_objects_are_converted_to_canonical_base_units():
    contract = ToolContract(name="units", version="1.0.0", parameter_schema={"type": "object", "properties": {"distance": {"type": "number", "unit": "m"}, "timeout": {"type": "number", "unit": "s"}}}, output_ports={"out": PortContract(artifact_type="Vector")})
    plan = Compiler(ToolContractRegistry([contract])).compile(_draft(_step("x", "units", params={"distance": {"value": 2, "unit": "km"}, "timeout": {"value": 1, "unit": "min"}})))
    assert plan.tasks[0].params == {"distance": 2000, "timeout": 60}


def test_code_execute_freezes_source_hash_and_rejects_hidden_callable():
    registry = _registry()
    source = "print('ok')"
    import hashlib
    expected = hashlib.sha256(source.encode()).hexdigest()
    plan = Compiler(registry).compile(_draft(_step("run", "code.execute", params={"source_code": source})))
    assert plan.tasks[0].params["source_code_hash"] == expected
    with pytest.raises(CompileError) as exc:
        Compiler(registry).compile(_draft(_step("run", "code.execute", params={"source_code": source, "callable": "tool_fns"})))
    assert exc.value.code == "INVALID_PARAMETER"


def test_model_copy_cannot_reintroduce_mutable_nested_contracts():
    copied = _registry().get("vector.buffer").model_copy(update={"validators": ["one"]})
    with pytest.raises((TypeError, ValueError)):
        copied.validators.append("two")


def test_artifact_hash_and_output_ordinal_are_strictly_validated():
    with pytest.raises(ValueError):
        ArtifactRef(artifact_id="a", type="Vector", content_hash="not-a-sha256")
    many_out = PortContract(artifact_type="Vector", cardinality="many", min_items=0, max_items=2)
    source = ToolContract(name="source", version="1.0.0", output_ports={"items": many_out})
    sink = ToolContract(name="sink", version="1.0.0", input_ports={"items": many_out}, output_ports={"out": PortContract(artifact_type="Vector")})
    ref = TaskOutputRef(task_key="source", port="items", selection="single", ordinal=2)
    with pytest.raises(CompileError) as exc:
        Compiler(ToolContractRegistry([source, sink])).compile(_draft(_step("source", "source"), _step("sink", "sink", {"items": ref})))
    assert exc.value.code == "INVALID_REF"


def test_non_json_model_values_become_stable_compile_errors():
    contract = ToolContract(name="opaque", version="1.0.0", parameter_schema={"type": "object", "properties": {"payload": {}}}, output_ports={"out": PortContract(artifact_type="Vector")})
    with pytest.raises(CompileError) as exc:
        Compiler(ToolContractRegistry([contract])).compile(_draft(_step("opaque", "opaque", params={"payload": object()})))
    assert exc.value.code == "INVALID_PARAMETER"


def test_artifact_selection_all_is_only_valid_for_many_ports():
    ref = ArtifactRef(artifact_id="collection", type="Vector", selection="all", ordinal=None, crs={"kind": "EPSG", "code": 4326})
    many = PortContract(artifact_type="Vector", cardinality="many", min_items=0, max_items=None)
    contract = ToolContract(name="collect", version="1.0.0", input_ports={"items": many}, output_ports={"out": many})
    plan = Compiler(ToolContractRegistry([contract])).compile(_draft(_step("collect", "collect", {"items": ref})))
    assert plan.tasks[0].inputs["items"].selection == "all"


def test_parameter_boolean_length_and_finite_constraints_are_stable():
    contract = ToolContract(
        name="typed",
        version="1.0.0",
        parameter_schema={"type": "object", "additionalProperties": False, "properties": {
            "enabled": {"type": "boolean"},
            "name": {"type": "string", "minLength": 2, "maxLength": 4},
            "ratio": {"type": "number"},
        }},
        output_ports={"out": PortContract(artifact_type="Vector")},
    )
    registry = ToolContractRegistry([contract])
    def compile_with(params):
        return Compiler(registry).compile(_draft(_step("typed", "typed", params=params)))
    with pytest.raises(CompileError) as exc:
        compile_with({"enabled": 1})
    assert exc.value.code == "INVALID_PARAMETER"
    assert compile_with({"enabled": True, "name": "ok", "ratio": 1.0}).tasks[0].params["enabled"] is True
    with pytest.raises(CompileError) as exc:
        compile_with({"name": "x"})
    assert exc.value.code == "INVALID_PARAMETER"
    with pytest.raises(CompileError) as exc:
        compile_with({"ratio": float("inf")})
    assert exc.value.code == "INVALID_PARAMETER"


def test_artifact_lookup_requires_canonical_hash_metadata():
    ref = ArtifactRef(artifact_id="a", type="Vector", content_hash="a" * 64, crs={"kind": "EPSG", "code": 4326})
    bad = {"validation_status": "valid", "content_hash": "b" * 64, "storage_uri": "artifacts/sha256/bb/" + "b" * 64}
    with pytest.raises(CompileError) as exc:
        Compiler(ToolContractRegistry.with_builtins(), artifact_lookup=lambda _: bad).compile(_draft(_step("buffer", "vector.buffer", {"vector": ref}, {"distance_m": 1})))
    assert exc.value.code == "INVALID_REF"


def test_contract_identity_and_nested_tuple_are_strict():
    with pytest.raises(ValueError):
        ToolContract(name="", version="1.0.0")
    with pytest.raises(ValueError):
        ToolContract(name="x", version="1")
    contract = ToolContract(name="x", version="1.0.0", parameter_schema={"properties": {"tuple": {"default": ({"x": []},)}}})
    with pytest.raises((TypeError, ValueError)):
        contract.parameter_schema["properties"]["tuple"]["default"][0]["x"].append(1)


def test_crs_propagates_through_consumer_before_producer():
    vector = ArtifactRef(artifact_id="a", type="Vector", crs={"kind": "EPSG", "code": 4326})
    draft = _draft(
        _step("consumer", "vector.buffer", {"vector": TaskOutputRef(task_key="source", port="vector")}, {"distance_m": 1}),
        _step("source", "vector.buffer", {"vector": vector}, {"distance_m": 1}),
    )
    plan = Compiler(_registry()).compile(draft)
    assert plan.tasks[0].task_key == "source"
    assert plan.tasks[1].task_key == "consumer"


def test_task_output_ref_propagates_schema_and_rejects_schema_mismatch():
    source_contract = ToolContract(
        name="schema.source",
        version="1.0.0",
        input_ports={"input": PortContract(artifact_type="Vector")},
        output_ports={"vector": PortContract(artifact_type="Vector", schema={"required": ["name"]})},
    )
    sink_contract = ToolContract(
        name="schema.sink",
        version="1.0.0",
        input_ports={"vector": PortContract(artifact_type="Vector", schema={"required": ["name"]})},
        output_ports={"out": PortContract(artifact_type="Vector")},
    )
    source = ArtifactRef(artifact_id="a", type="Vector", crs={"kind": "EPSG", "code": 4326})
    registry = ToolContractRegistry([source_contract, sink_contract])
    plan = Compiler(registry).compile(_draft(
        _step("source", "schema.source", {"input": source}),
        _step("sink", "schema.sink", {"vector": TaskOutputRef(task_key="source", port="vector")}),
    ))
    assert plan.tasks[1].task_key == "sink"
    bad_sink = sink_contract.model_copy(update={"input_ports": {"vector": PortContract(artifact_type="Vector", schema={"required": ["missing"]})}})
    with pytest.raises(CompileError) as exc:
        Compiler(ToolContractRegistry([source_contract, bad_sink])).compile(_draft(
            _step("source", "schema.source", {"input": source}),
            _step("sink", "schema.sink", {"vector": TaskOutputRef(task_key="source", port="vector")}),
        ))
    assert exc.value.code == "TYPE_MISMATCH"


def test_task_output_ref_crs_required_and_same_as_are_not_bypassed():
    source_contract = ToolContract(name="crs.source", version="1.0.0", output_ports={"vector": PortContract(artifact_type="Vector")})
    required_sink = ToolContract(name="crs.required", version="1.0.0", input_ports={"vector": PortContract(artifact_type="Vector", crs_policy={"required": True})}, output_ports={"out": PortContract(artifact_type="Vector")})
    with pytest.raises(AwaitingInputDiagnostic) as exc:
        Compiler(ToolContractRegistry([source_contract, required_sink])).compile(_draft(
            _step("source", "crs.source"),
            _step("sink", "crs.required", {"vector": TaskOutputRef(task_key="source", port="vector")}),
        ))
    assert exc.value.code == "CRS_REQUIRED"

    joined = ToolContract(
        name="crs.join.ref",
        version="1.0.0",
        input_ports={
            "left": PortContract(artifact_type="Vector", crs_policy={"required": True, "same_as": "right"}),
            "right": PortContract(artifact_type="Vector", crs_policy={"required": True}),
        },
        output_ports={"out": PortContract(artifact_type="Vector")},
    )
    left = ArtifactRef(artifact_id="left", type="Vector", crs={"kind": "EPSG", "code": 4326})
    right = ArtifactRef(artifact_id="right", type="Vector", crs={"kind": "EPSG", "code": 3857})
    source_with_crs = ToolContract(name="crs.source2", version="1.0.0", input_ports={"input": PortContract(artifact_type="Vector")}, output_ports={"vector": PortContract(artifact_type="Vector")})
    with pytest.raises(CompileError) as exc:
        Compiler(ToolContractRegistry([source_with_crs, joined])).compile(_draft(
            _step("source", "crs.source2", {"input": left}),
            _step("join", "crs.join.ref", {"left": TaskOutputRef(task_key="source", port="vector"), "right": right}),
        ))
    assert exc.value.code == "CRS_CONFLICT"


def test_auto_reproject_task_output_chain_inserts_one_conversion_and_updates_metadata():
    vector = ArtifactRef(artifact_id="a", type="Vector", crs={"kind": "EPSG", "code": 4326})
    plan = Compiler(_registry()).compile(_draft(
        _step("consumer", "vector.buffer", {"vector": TaskOutputRef(task_key="source", port="vector")}, {"distance_m": 1}),
        _step("source", "vector.buffer", {"vector": vector}, {"distance_m": 1}),
    ), auto_reproject=True, target_crs={"kind": "EPSG", "code": 4548})
    conversions = [task for task in plan.tasks if task.origin == "compiler" and task.op == "vector.reproject"]
    assert [task.task_key for task in conversions] == ["source__reproject__vector"]
    source_task = next(task for task in plan.tasks if task.task_key == "source")
    consumer_task = next(task for task in plan.tasks if task.task_key == "consumer")
    assert source_task.outputs[0].crs == {"kind": "EPSG", "code": 4548}
    assert consumer_task.inputs["vector"].task_key == "source"
    assert conversions[0].outputs[0].artifact_type == "Vector"
    assert conversions[0].outputs[0].crs == {"kind": "EPSG", "code": 4548}


def test_task_output_crs_propagates_across_multiple_compiled_tasks():
    vector = ArtifactRef(artifact_id="a", type="Vector", crs={"kind": "EPSG", "code": 4326})
    required = ToolContract(
        name="crs.final",
        version="1.0.0",
        input_ports={"vector": PortContract(artifact_type="Vector", crs_policy={"required": True})},
        output_ports={"out": PortContract(artifact_type="Vector")},
    )
    registry = ToolContractRegistry.with_builtins()
    registry.register(required)
    plan = Compiler(registry).compile(_draft(
        _step("final", "crs.final", {"vector": TaskOutputRef(task_key="middle", port="vector")}),
        _step("middle", "vector.buffer", {"vector": TaskOutputRef(task_key="source", port="vector")}, {"distance_m": 1}),
        _step("source", "vector.buffer", {"vector": vector}, {"distance_m": 1}),
    ))
    assert plan.tasks[2].outputs[0].crs == {"kind": "EPSG", "code": 4326}


def test_auto_reproject_multi_hop_chain_does_not_duplicate_conversions():
    vector = ArtifactRef(artifact_id="a", type="Vector", crs={"kind": "EPSG", "code": 4326})
    plan = Compiler(_registry()).compile(_draft(
        _step("final", "vector.buffer", {"vector": TaskOutputRef(task_key="middle", port="vector")}, {"distance_m": 1}),
        _step("middle", "vector.buffer", {"vector": TaskOutputRef(task_key="source", port="vector")}, {"distance_m": 1}),
        _step("source", "vector.buffer", {"vector": vector}, {"distance_m": 1}),
    ), auto_reproject=True, target_crs={"kind": "EPSG", "code": 4548})
    assert [task.task_key for task in plan.tasks if task.origin == "compiler"] == ["source__reproject__vector"]
    assert all(task.outputs[0].crs == {"kind": "EPSG", "code": 4548} for task in plan.tasks)


def test_reprojection_conversion_preserves_source_schema_and_cardinality():
    many = PortContract(artifact_type="Vector", cardinality="many", min_items=1, max_items=5, schema={"fields": {"name": "string"}})
    producer = ToolContract(name="meta.producer", version="1.0.0", parameter_schema={"additionalProperties": True, "properties": {"target_crs": {"type": "object", "additionalProperties": True, "default": {"kind": "EPSG", "code": 4326}}}}, output_ports={"items": many})
    middle = ToolContract(name="meta.middle", version="1.0.0", input_ports={"items": many}, output_ports={"items": many})
    registry = ToolContractRegistry.with_builtins()
    registry.register(producer)
    registry.register(middle)
    plan = Compiler(registry).compile(_draft(
        _step("middle", "meta.middle", {"items": TaskOutputRef(task_key="producer", port="items", selection="all", ordinal=None)}),
        _step("producer", "meta.producer"),
    ), auto_reproject=True, target_crs={"kind": "EPSG", "code": 4548})
    conversion = next(task for task in plan.tasks if task.origin == "compiler")
    assert conversion.outputs[0].artifact_type == "Vector"
    assert conversion.outputs[0].cardinality == "many"
    assert conversion.outputs[0].min_items == 1
    assert conversion.outputs[0].max_items == 5
    assert conversion.outputs[0].schema == {"fields": {"name": "string"}}


def test_artifact_all_reprojection_preserves_many_selection():
    many = PortContract(artifact_type="Vector", cardinality="many", min_items=0, max_items=None)
    sink = ToolContract(name="many.sink", version="1.0.0", input_ports={"items": many}, output_ports={"out": many})
    ref = ArtifactRef(artifact_id="items", type="Vector", selection="all", ordinal=None, crs={"kind": "EPSG", "code": 4326})
    registry = ToolContractRegistry.with_builtins()
    registry.register(sink)
    plan = Compiler(registry).compile(_draft(_step("sink", "many.sink", {"items": ref})), auto_reproject=True, target_crs={"kind": "EPSG", "code": 4548})
    conversion = next(task for task in plan.tasks if task.origin == "compiler")
    assert conversion.inputs["vector"].selection == "all"
    assert conversion.inputs["vector"].ordinal is None
    assert conversion.outputs[0].cardinality == "many"
    assert plan.tasks[-1].inputs["items"].selection == "all"
    assert plan.tasks[-1].inputs["items"].ordinal is None


def test_same_as_checks_every_member_of_many_inputs():
    many = PortContract(artifact_type="Vector", cardinality="many", min_items=2, max_items=2, crs_policy={"required": True})
    join = ToolContract(name="many.join", version="1.0.0", input_ports={"left": many.model_copy(update={"crs_policy": {"required": True, "same_as": "right"}}), "right": many}, output_ports={"out": PortContract(artifact_type="Vector")})
    registry = ToolContractRegistry([join])
    left = [ArtifactRef(artifact_id="l1", type="Vector", crs={"kind": "EPSG", "code": 4326}), ArtifactRef(artifact_id="l2", type="Vector", crs={"kind": "EPSG", "code": 3857})]
    right = [ArtifactRef(artifact_id="r1", type="Vector", crs={"kind": "EPSG", "code": 4326}), ArtifactRef(artifact_id="r2", type="Vector", crs={"kind": "EPSG", "code": 4326})]
    with pytest.raises(CompileError) as exc:
        Compiler(registry).compile(_draft(_step("join", "many.join", {"left": left, "right": right})))
    assert exc.value.code == "CRS_CONFLICT"


def test_frozen_contract_in_place_updates_are_rejected():
    contract = _registry().get("vector.buffer")
    with pytest.raises((TypeError, ValueError)):
        contract.validators += ("extra",)
    with pytest.raises((TypeError, ValueError)):
        contract.input_ports |= {"extra": PortContract()}
    with pytest.raises((TypeError, ValueError)):
        contract.validators *= 2


def test_documented_steps_and_id_are_normalized_but_legacy_fact_shapes_rejected():
    draft = PlannerDraft.model_validate({"user_goal": "load", "steps": [{"id": "load", "op": "map.build"}]})
    assert draft.tasks[0].task_key == "load"
    with pytest.raises(ValueError):
        PlannerDraft.model_validate({"user_goal": "legacy", "task_plan": {}})
    with pytest.raises(CompileError) as exc:
        Compiler(_registry()).compile({"user_goal": "legacy", "tasks": [{"task_key": "map", "op": "map.build", "inputs": {"layer": 1}}]})
    assert exc.value.code == "PLAN_DRAFT_INVALID"


def test_nested_parameter_schema_is_recursively_strict():
    contract = ToolContract(
        name="nested.params",
        version="1.0.0",
        parameter_schema={"type": "object", "additionalProperties": False, "required": ["config"], "properties": {
            "config": {"type": "object", "additionalProperties": False, "required": ["enabled", "items"], "properties": {
                "enabled": {"type": "boolean"},
                "items": {"type": "array", "minItems": 1, "items": {"type": "integer", "minimum": 0}},
            }}
        }},
        output_ports={"out": PortContract(artifact_type="Vector")},
    )
    registry = ToolContractRegistry([contract])
    def compile_with(config):
        return Compiler(registry).compile(_draft(_step("nested", "nested.params", params={"config": config})))
    assert compile_with({"enabled": True, "items": [1]})
    with pytest.raises(CompileError) as exc:
        compile_with({"enabled": True, "items": [1], "unknown": 1})
    assert exc.value.code == "INVALID_PARAMETER"
    with pytest.raises(CompileError) as exc:
        compile_with({"enabled": 1, "items": [1]})
    assert exc.value.code == "INVALID_PARAMETER"
    with pytest.raises(CompileError) as exc:
        compile_with({"enabled": True, "items": ["1"]})
    assert exc.value.code == "INVALID_PARAMETER"


def test_side_effect_contract_requires_controlled_root_and_uses_file_export_name():
    ref = ArtifactRef(artifact_id="a", type="File")
    with pytest.raises(CompileError) as exc:
        Compiler(_registry()).compile(_draft(_step("export", "io.export", {"input": ref}, {"path": "out.gpkg"})))
    assert exc.value.code == "INVALID_PARAMETER"
    assert _registry().get("file.export").name == "file.export"
    vector = ArtifactRef(artifact_id="a", type="Vector", crs={"kind": "EPSG", "code": 4326})
    with pytest.raises(CompileError) as exc:
        Compiler(_registry(), policy={"allowed_path_root": "C:\\safe"}).compile(_draft(_step("export", "file.export", {"input": vector}, {"path": "out.gpkg"})))
    assert exc.value.code == "INVALID_PARAMETER"
    plan = Compiler(_registry(), policy={"allowed_path_root": "C:\\safe"}).compile(_draft(_step("export", "publish_file", {"input": ref}, {"path": "out.gpkg"})))
    assert plan.tasks[0].op == "publish_file"


def test_unsupported_table_crs_does_not_create_vector_reproject():
    contract = ToolContract(name="table.sink", version="1.0.0", input_ports={"input": PortContract(artifact_type="Table")}, output_ports={"out": PortContract(artifact_type="Table")})
    ref = ArtifactRef(artifact_id="t", type="Table", crs={"kind": "EPSG", "code": 4326})
    with pytest.raises(CompileError) as exc:
        Compiler(ToolContractRegistry([contract])).compile(_draft(_step("sink", "table.sink", {"input": ref})), auto_reproject=True, target_crs={"kind": "EPSG", "code": 4548})
    assert exc.value.code == "CRS_CONFLICT"


def test_gcj02_requires_coordinate_conversion_contract_not_vector_reproject():
    ref = ArtifactRef(artifact_id="gcj", type="Vector", crs={"kind": "GCJ02"})
    plan = Compiler(_registry()).compile(_draft(_step("buffer", "vector.buffer", {"vector": ref}, {"distance_m": 1})), auto_reproject=True, target_crs={"kind": "EPSG", "code": 4548})
    assert any(task.op == "vector.convert_datum" for task in plan.tasks)


def test_many_outputs_require_all_selection_and_registry_exposes_handler_manifest():
    many = PortContract(artifact_type="Vector", cardinality="many", min_items=0, max_items=None)
    source = ToolContract(name="many.source", version="1.0.0", output_ports={"items": many})
    sink = ToolContract(name="many.sink.single", version="1.0.0", input_ports={"items": many}, output_ports={"out": PortContract(artifact_type="Vector")})
    ref = TaskOutputRef(task_key="source", port="items", selection="single", ordinal=0)
    with pytest.raises(CompileError) as exc:
        Compiler(ToolContractRegistry([source, sink])).compile(_draft(_step("source", "many.source"), _step("sink", "many.sink.single", {"items": ref})))
    assert exc.value.code == "INVALID_REF"
    manifest = _registry().public_operations_manifest()
    assert manifest["operations"]["vector.buffer"]["handler_ref"]
    assert not manifest["operations"]["vector.buffer"]["handler_ref"].startswith("contract://")


def test_artifact_lookup_failure_is_a_stable_invalid_ref():
    ref = ArtifactRef(artifact_id="a", type="Vector", crs={"kind": "EPSG", "code": 4326})
    def lookup(_):
        raise RuntimeError("database unavailable")
    with pytest.raises(CompileError) as exc:
        Compiler(_registry(), artifact_lookup=lookup).compile(_draft(_step("buffer", "vector.buffer", {"vector": ref}, {"distance_m": 1})))
    assert exc.value.code == "INVALID_REF"


def test_frozen_containers_reject_ior_iadd_iimul_without_mutation():
    contract = _registry().get("vector.buffer")
    before = tuple(contract.validators)
    with pytest.raises(TypeError):
        contract.validators.__iadd__(["x"])
    assert tuple(contract.validators) == before
    with pytest.raises(TypeError):
        contract.validators.__imul__(2)
    assert tuple(contract.validators) == before
    mapping = contract.input_ports
    with pytest.raises(TypeError):
        mapping.__ior__({"extra": PortContract()})
    assert "extra" not in mapping


def test_artifact_lookup_metadata_type_crs_size_and_hash_are_verified():
    ref = ArtifactRef(artifact_id="a", type="Vector", crs={"kind": "EPSG", "code": 4326})
    base = {"validation_status": "valid", "content_hash": "a" * 64, "storage_uri": "artifacts/sha256/aa/" + "a" * 64, "type": "Raster", "crs": ref.crs, "schema": {}, "size_bytes": 1}
    with pytest.raises(CompileError) as exc:
        Compiler(_registry(), artifact_lookup=lambda _: base).compile(_draft(_step("buffer", "vector.buffer", {"vector": ref}, {"distance_m": 1})))
    assert exc.value.code == "TYPE_MISMATCH"
    with pytest.raises(CompileError) as exc:
        Compiler(_registry(), artifact_lookup=lambda _: {**base, "type": "Vector", "size_bytes": -1}).compile(_draft(_step("buffer", "vector.buffer", {"vector": ref}, {"distance_m": 1})))
    assert exc.value.code == "INVALID_REF"


def test_execution_fingerprint_is_explicit_and_side_effects_are_not_cached():
    row = {"validation_status": "valid", "content_hash": "a" * 64, "storage_uri": "artifacts/sha256/aa/" + "a" * 64, "type": "Vector", "crs": {"kind": "EPSG", "code": 4326}, "schema": {}, "size_bytes": 1}
    ref = ArtifactRef(artifact_id="a", type="Vector", crs=row["crs"])
    contract = ToolContract(name="fingerprint", version="1.0.0", input_ports={"input": PortContract(artifact_type="Vector")}, output_ports={"out": PortContract(artifact_type="File")}, side_effect=True, confirmation_policy=ConfirmationPolicy(mode="required"), parameter_schema={"type": "object", "additionalProperties": False, "required": ["path"], "properties": {"path": {"type": "string"}}}, cache_policy=CachePolicy(mode="permanent"))
    registry = ToolContractRegistry([contract])
    plan = Compiler(registry, artifact_lookup=lambda _: row, policy={"allowed_path_root": "C:\\safe"}).compile(_draft(_step("x", "fingerprint", {"input": ref}, {"path": "x.txt"})))
    assert plan.tasks[0].execution_fingerprint
    assert plan.tasks[0].cache_key is None
    assert plan.execution_fingerprint


def test_cycle_compile_error_path_is_hash_seed_stable():
    cycle = _draft(_step("b", "vector.buffer", {"vector": TaskOutputRef(task_key="a", port="vector")}, {"distance_m": 1}), _step("a", "vector.buffer", {"vector": TaskOutputRef(task_key="b", port="vector")}, {"distance_m": 1}))
    with pytest.raises(CompileError) as first:
        Compiler(_registry()).compile(cycle)
    with pytest.raises(CompileError) as second:
        Compiler(_registry()).compile(cycle)
    assert (first.value.code, first.value.path, str(first.value)) == (second.value.code, second.value.path, str(second.value))


def test_frozen_container_init_and_in_place_operations_are_sealed():
    contract = _registry().get("vector.buffer")
    validators = contract.validators
    mapping = contract.input_ports
    with pytest.raises(TypeError):
        validators.__init__(["tampered"])
    with pytest.raises(TypeError):
        mapping.__init__({"tampered": PortContract()})
    assert tuple(validators) == tuple(contract.validators)
    assert "tampered" not in mapping


def test_artifact_lookup_requires_complete_published_metadata():
    ref = ArtifactRef(artifact_id="a", type="Vector", crs={"kind": "EPSG", "code": 4326})
    incomplete = {"validation_status": "valid", "content_hash": "a" * 64, "storage_uri": "artifacts/sha256/aa/" + "a" * 64, "size_bytes": 1}
    with pytest.raises(CompileError) as exc:
        Compiler(_registry(), artifact_lookup=lambda _: incomplete).compile(_draft(_step("buffer", "vector.buffer", {"vector": ref}, {"distance_m": 1})))
    assert exc.value.code == "INVALID_REF"


def test_builtin_manifest_exports_traceable_contract_tests(tmp_path):
    import json
    import importlib
    registry = _registry()
    manifest = registry.public_operations_manifest()
    for name, operation in manifest["operations"].items():
        assert operation["handler_ref"]
        assert operation["validator"]
        assert operation["contract_tests"]
        assert operation["contract_tests"] == operation["manifest"]["contract_tests"]
        for reference in (operation["handler_ref"], operation["validator"]):
            module_name, callable_name = reference.split(":", 1)
            assert callable(getattr(importlib.import_module(module_name), callable_name))
    path = tmp_path / "public_operations_manifest.json"
    registry.write_public_operations_manifest(path)
    assert json.loads(path.read_text(encoding="utf-8")) == manifest
    baseline = {"operations": {name: {**operation, "test_node": operation["contract_tests"][0]} for name, operation in manifest["operations"].items()}}
    assert registry.reconcile_public_operations_manifest(manifest, baseline=baseline)


def test_file_export_is_managed_artifact_and_publish_file_is_explicit_side_effect():
    ref = ArtifactRef(artifact_id="a", type="Vector", crs={"kind": "EPSG", "code": 4326})
    with pytest.raises(CompileError) as exc:
        Compiler(_registry(), policy={"allowed_path_root": "C:\\safe"}).compile(_draft(_step("export", "file.export", {"input": ref}, {"path": "out.gpkg"})))
    assert exc.value.code == "INVALID_PARAMETER"
    registry = _registry()
    assert "publish_file" in registry


def test_execution_fingerprint_includes_interpreter_package_and_sandbox_policy():
    draft = _draft(_step("run", "code.execute", params={"source_code": "x = 1"}))
    first = Compiler(_registry(), policy={"environment_fingerprint": {"interpreter": "3.13", "packages": "p1", "sandbox_policy": "s1"}}).compile(draft)
    second = Compiler(_registry(), policy={"environment_fingerprint": {"interpreter": "3.13", "packages": "p2", "sandbox_policy": "s1"}}).compile(draft)
    assert first.tasks[0].execution_fingerprint != second.tasks[0].execution_fingerprint


def test_generated_conversion_execution_fingerprint_includes_environment_policy():
    ref = ArtifactRef(artifact_id="a", type="Vector", crs={"kind": "EPSG", "code": 4326})
    draft = _draft(_step("buffer", "vector.buffer", {"vector": ref}, {"distance_m": 1}))
    first = Compiler(_registry(), policy={"environment_fingerprint": {"packages": "a"}}).compile(draft, auto_reproject=True, target_crs={"kind": "EPSG", "code": 4548})
    second = Compiler(_registry(), policy={"environment_fingerprint": {"packages": "b"}}).compile(draft, auto_reproject=True, target_crs={"kind": "EPSG", "code": 4548})
    assert first.tasks[0].execution_fingerprint != second.tasks[0].execution_fingerprint


def test_frozen_containers_reject_dynamic_in_place_syntax():
    contract = _registry().get("vector.buffer")
    values = contract.validators
    with pytest.raises(TypeError):
        values += ["tampered"]
    with pytest.raises(TypeError):
        values *= 2
    mapping = contract.input_ports
    with pytest.raises(TypeError):
        mapping |= {"tampered": PortContract()}
    assert tuple(values) == tuple(contract.validators)
    assert "tampered" not in mapping


def test_code_execute_rejects_unsafe_source_before_ir_hashing():
    for source in ("import os", "import socket", "x = __import__('os')", "x = obj.__class__", "rpc.call()"):
        with pytest.raises(CompileError) as exc:
            Compiler(_registry()).compile(_draft(_step("run", "code.execute", params={"source_code": source})))
        assert exc.value.code == "INVALID_PARAMETER"


def test_many_auto_reprojection_uses_many_conversion_contract():
    many = PortContract(artifact_type="Vector", cardinality="many", min_items=0, max_items=None)
    contract = ToolContract(name="many.buffer", version="1.0.0", input_ports={"vectors": many}, output_ports={"out": many})
    refs = [ArtifactRef(artifact_id="a", type="Vector", crs={"kind": "EPSG", "code": 4326}), ArtifactRef(artifact_id="b", type="Vector", crs={"kind": "EPSG", "code": 4326})]
    reprojection = ToolContract(name="vector.reproject_many", version="1.0.0", input_ports={"vector": many}, output_ports={"vector": many}, parameter_schema={"type": "object", "required": ["target_crs"]})
    registry = ToolContractRegistry([contract, reprojection])
    plan = Compiler(registry).compile(_draft(_step("many", "many.buffer", {"vectors": refs})), auto_reproject=True, target_crs={"kind": "EPSG", "code": 4548})
    conversion = next(task for task in plan.tasks if task.origin == "compiler")
    assert conversion.op == "vector.reproject_many"
    assert conversion.outputs[0].cardinality == "many"


def test_artifact_lookup_default_type_is_resolved_from_real_metadata():
    ref = ArtifactRef(artifact_id="a", crs={"kind": "EPSG", "code": 4326})
    row = {"validation_status": "valid", "type": "Vector", "crs": ref.crs, "schema": {}, "content_hash": "a" * 64, "storage_uri": "artifacts/sha256/aa/" + "a" * 64, "size_bytes": 1}
    plan = Compiler(_registry(), artifact_lookup=lambda _: row).compile(_draft(_step("buffer", "vector.buffer", {"vector": ref}, {"distance_m": 1})))
    assert plan.tasks[0].inputs["vector"].type == "Vector"
    file_row = {**row, "type": "File", "crs": None, "schema": {"file": "geojson"}}
    with pytest.raises(AwaitingInputDiagnostic):
        Compiler(_registry(), artifact_lookup=lambda _: file_row).compile(_draft(_step("load", "io.load_vector", {"file": ArtifactRef(artifact_id="f", type="File")})))


def test_builtin_runtime_handlers_are_runtime_owned():
    import importlib

    for operation in _registry().public_operations_manifest()["operations"].values():
        module_name, function_name = operation["handler_ref"].split(":", 1)
        assert module_name in {"app.runtime.contracts", "app.runtime.tool_handlers"}
        assert callable(getattr(importlib.import_module(module_name), function_name))


def test_builtin_validators_reject_nonsense_parameters():
    import importlib

    registry = _registry()
    for operation in registry.public_operations_manifest()["operations"]:
        contract = registry.get(operation)
        assert contract.validator
        module_name, function_name = contract.validator.split(":", 1)
        validator = getattr(importlib.import_module(module_name), function_name)
        assert validator(nonsense_parameter="not-a-contract") is False


def test_registry_rejects_unresolvable_or_default_references():
    manifest = {"contract_tests": ["tests/unit/test_runtime_compiler_phase2.py::test_contract_models_parse_and_canonical_plan_is_serializable"]}
    for field, value in (("handler_ref", "missing.module"), ("handler_ref", "app.runtime.contracts:_default_handler"), ("validator", "missing.module")):
        kwargs = {"name": f"bad-{field}-{value}", "version": "1.0.0", "manifest": manifest, field: value}
        contract = ToolContract(**kwargs)
        with pytest.raises(ValueError):
            ToolContractRegistry().register(contract)


def test_metric_selector_rejects_wgs84_even_when_declared_as_meters():
    from app.runtime.compiler import metric_crs_selector

    with pytest.raises(CompileError) as exc:
        metric_crs_selector(
            {"xmin": 110, "ymin": 30, "xmax": 111, "ymax": 31},
            [{"crs": {"kind": "EPSG", "code": 4326}, "bbox": [109, 29, 112, 32], "unit": "m"}],
        )
    assert exc.value.code == "CRS_CONFLICT"


def test_runtime_executor_module_exists_for_compiled_task_dispatch():
    from app.runtime.executor import RuntimeExecutor

    assert RuntimeExecutor is not None
