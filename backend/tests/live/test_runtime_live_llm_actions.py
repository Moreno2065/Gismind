"""Opt-in real-provider grounded contract regression for the Runtime planner.

This suite never substitutes an LLM response.  It is intentionally separate
from deterministic unit/integration tests because it spends provider quota and
because availability is an observable release signal, not a reason to fake a
green result.  This is grounded: static execution facts are appended to the
natural-language prompt. It is not blind natural-language E2E acceptance.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.runtime import ArtifactStore, Compiler, RuntimeDB, RuntimeStore, RuntimeSupervisor, ToolContractRegistry
from app.runtime.compiler import CompileError
from app.runtime.llm_planner import OpenAICompatiblePlanner, PlannerError, PlannerRequest


pytestmark = pytest.mark.live_llm


@dataclass(frozen=True)
class LiveCase:
    case_id: str
    kind: str
    prompt: str
    expected_ops: tuple[str, ...]
    expected_origin: str
    semantic: dict[str, object]


_CORPUS_PATH = Path(__file__).with_name("runtime_live_intent_corpus.json")
_FACTS_PATH = Path(__file__).with_name("runtime_live_fixture_facts.json")
CORPUS_VERSION = "runtime-live-intent-v2"
EVALUATOR_VERSION = "compiled-contract-v2"


def _cases() -> list[LiveCase]:
    """Load the independently authored business-intent evidence corpus.

    This file is intentionally static and does not import the planner's
    aliases or derive prompts from the Runtime registry.  Registry equality is
    checked by the separate structural test below, so corpus drift is visible.
    """
    records = json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))
    return [
        LiveCase(
            case_id=str(item["case_id"]), kind=str(item["kind"]),
            prompt=str(item["prompt"]),
            expected_ops=tuple(str(op) for op in item["expected_ops"]),
            expected_origin=str(item["expected_origin"]),
            semantic=dict(item["semantic"]),
        )
        for item in records
    ]


def _static_fixture_facts() -> dict[str, object]:
    """Load declarations authored with the corpus, without consulting Runtime metadata."""
    facts = json.loads(_FACTS_PATH.read_text(encoding="utf-8"))
    if not isinstance(facts, dict):
        raise ValueError("live fixture facts must be a JSON object")
    return facts


def _input_artifacts() -> tuple[dict, ...]:
    """Build typed metadata from the static fixture declarations only."""
    declarations = _static_fixture_facts().get("artifact_declarations")
    if not isinstance(declarations, dict) or not declarations:
        raise ValueError("corpus facts must declare artifact_declarations")
    return tuple(dict(item) for item in declarations.values())


def _expand_static_refs(value: object, declarations: dict[str, object]) -> object:
    if isinstance(value, str) and value in declarations:
        declaration = declarations[value]
        if not isinstance(declaration, dict):
            raise ValueError(f"invalid artifact declaration: {value}")
        return {
            "kind": "artifact", "artifact_id": declaration["artifact_id"],
            "type": declaration["type"], "crs": declaration.get("crs"),
            "content_hash": declaration.get("content_hash"),
        }
    if isinstance(value, dict):
        return {key: _expand_static_refs(child, declarations) for key, child in value.items()}
    if isinstance(value, list):
        return [_expand_static_refs(child, declarations) for child in value]
    return value


def _case_witness(case: LiveCase) -> list[dict[str, object]]:
    facts = _static_fixture_facts()
    declarations = facts.get("artifact_declarations", {})
    witnesses = facts.get("operation_witnesses", {})
    if not isinstance(declarations, dict) or not isinstance(witnesses, dict):
        raise ValueError("corpus facts must declare static witnesses")
    blocks = []
    for operation in case.expected_ops:
        witness = witnesses.get(operation)
        if not isinstance(witness, dict):
            raise ValueError(f"missing static witness for {operation}")
        block = _expand_static_refs(witness, declarations)
        override = case.semantic.get("expected_params", {}).get(operation, {})
        if override:
            block["required_params"] = {**block.get("required_params", {}), **override}
        blocks.append(block)
    return blocks


def _configured() -> bool:
    from app.config import get_settings

    settings = get_settings()
    return bool(settings.LLM_API_KEY and settings.LLM_BASE_URL and settings.LLM_MODEL)


def test_grounded_corpus_covers_every_operation_without_operation_name_leakage():
    cases = _cases()
    facts = _static_fixture_facts()
    operations = set(facts["operation_witnesses"])

    assert len(cases) == 201
    assert {op for case in cases for op in case.expected_ops} == operations
    assert {case.kind for case in cases} == {"positive", "multi_step", "negative", "ambiguity", "unsupported"}
    assert all(op not in case.prompt for case in cases for op in case.expected_ops)
    assert all(case.semantic.get("requires_compile") for case in cases if case.kind in {"positive", "multi_step"})
    assert all(case.expected_origin == "needs_clarification" and not case.expected_ops for case in cases if case.kind in {"negative", "ambiguity", "unsupported"})
    assert all(
        isinstance(facts["operation_witnesses"].get(operation), dict)
        for case in cases if case.kind in {"positive", "multi_step"}
        for operation in case.expected_ops
    )


def _request_prompt(case: LiveCase) -> str:
    if not case.expected_ops:
        return case.prompt
    facts = "；".join(json.dumps(block, ensure_ascii=False, sort_keys=True) for block in _case_witness(case))
    return f"{case.prompt} 可执行请求事实（来自静态测试声明）：{facts}"


def _compiled_semantics(case: LiveCase, plan) -> tuple[bool, str | None, dict[str, object]]:
    """Check operation identity and graph facts from compiled PlanIR, never Draft text."""
    actual = [task.op for task in plan.tasks]
    expected = list(case.expected_ops)
    task_positions = {task.task_key: index for index, task in enumerate(plan.tasks)}
    dependencies: dict[str, str] = {}
    for task in plan.tasks:
        for port_name, raw in task.inputs.items():
            refs = raw if isinstance(raw, list) else [raw]
            for ref in refs:
                if not hasattr(ref, "task_key"):
                    continue
                source_position = task_positions.get(ref.task_key)
                if source_position is None or source_position >= task_positions[task.task_key]:
                    return False, "DEPENDENCY_ORDER_INVALID", {"compiled_task_order": actual}
                dependencies[f"{task.task_key}.{port_name}"] = f"{ref.task_key}.{ref.port}"
    if case.semantic.get("must_use_task_output_refs") and len(plan.tasks) > 1 and not dependencies:
        return False, "DEPENDENCY_REF_REQUIRED", {"compiled_task_order": actual}

    allowed = case.semantic.get("allowed_extra_ops", [])
    allowed = [str(op) for op in allowed] if isinstance(allowed, list) else []
    if allowed and not isinstance(case.semantic.get("allowed_extra_ops_rationale"), str):
        return False, "ALLOWED_EXTRA_OPS_RATIONALE_REQUIRED", {"compiled_task_order": actual}
    if not allowed and actual != expected:
        if actual[:len(expected)] == expected and len(actual) > len(expected):
            return False, "UNEXPECTED_OPERATION", {"compiled_task_order": actual, "unexpected_ops": actual[len(expected):]}
        return False, "OPERATION_SEQUENCE_MISMATCH", {"compiled_task_order": actual}
    if allowed:
        extras = list(actual)
        for operation in expected:
            if operation not in extras:
                return False, "EXPECTED_OPERATION_MISSING", {"compiled_task_order": actual}
            extras.remove(operation)
        unexpected = [operation for operation in extras if operation not in allowed]
        if unexpected:
            return False, "UNEXPECTED_OPERATION", {"compiled_task_order": actual, "unexpected_ops": unexpected}

    witnesses = _static_fixture_facts()["operation_witnesses"]
    for task in plan.tasks:
        if task.op not in expected:
            continue
        witness = witnesses[task.op]
        expected_params = dict(witness.get("required_params", {}))
        expected_params.update(case.semantic.get("expected_params", {}).get(task.op, {}))
        for name, value in expected_params.items():
            observed = task.params.get(name)
            if name == "path" and isinstance(observed, str) and isinstance(value, str):
                # The Compiler confines a relative requested destination under
                # the managed root; compare its relative suffix, not the root.
                normalized = observed.replace("\\", "/")
                expected_path = value.replace("\\", "/").lstrip("/")
                matches = normalized.endswith("/" + expected_path)
            else:
                matches = observed == value
            if not matches:
                return False, "BUSINESS_PARAMETER_MISMATCH", {
                    "compiled_task_order": actual, "task_key": task.task_key,
                    "parameter": name, "expected": value, "observed": observed,
                }

        declared_inputs = witness.get("required_inputs", {})
        for port_name, declared in declared_inputs.items():
            bound = task.inputs.get(port_name)
            if bound is None:
                return False, "INPUT_PORT_MISSING", {"compiled_task_order": actual, "task_key": task.task_key, "port": port_name}
            expected_ids = declared if isinstance(declared, list) else [declared]
            refs = bound if isinstance(bound, list) else [bound]
            if len(refs) != len(expected_ids):
                return False, "ARTIFACT_BINDING_MISMATCH", {
                    "compiled_task_order": actual, "task_key": task.task_key,
                    "port": port_name, "expected_count": len(expected_ids), "actual_count": len(refs),
                }
            actual_ids = []
            for ref in refs:
                if hasattr(ref, "artifact_id"):
                    actual_ids.append(ref.artifact_id)
                elif hasattr(ref, "task_key"):
                    # A multi-step plan may consume a preceding task instead
                    # of the static external Artifact witness on this port.
                    source = next((item for item in plan.tasks if item.task_key == ref.task_key), None)
                    if source is None or source.op not in expected:
                        return False, "DEPENDENCY_SOURCE_MISMATCH", {"compiled_task_order": actual, "task_key": task.task_key, "port": port_name}
                else:
                    return False, "ARTIFACT_BINDING_MISMATCH", {"compiled_task_order": actual, "task_key": task.task_key, "port": port_name}
            expected_counts = Counter(expected_ids)
            actual_counts = Counter(actual_ids)
            if any(actual_counts[artifact_id] > expected_counts[artifact_id] for artifact_id in actual_counts) or (
                len(actual_ids) == len(refs) and actual_counts != expected_counts
            ):
                return False, "ARTIFACT_BINDING_MISMATCH", {
                    "compiled_task_order": actual, "task_key": task.task_key,
                    "port": port_name, "expected_ids": expected_ids, "actual_ids": actual_ids,
                }
        if case.semantic.get("requires_confirmation") and task.op == "publish_file":
            if not task.side_effect or task.confirmation_policy.mode != "required" or not task.action_hash:
                return False, "CONFIRMATION_POLICY_MISSING", {"compiled_task_order": actual, "task_key": task.task_key}
        elif case.semantic.get("requires_confirmation") and len(plan.tasks) == 1:
            return False, "CONFIRMATION_POLICY_MISSING", {"compiled_task_order": actual, "task_key": task.task_key}

    return True, None, {"compiled_task_order": actual, "dependency_ports": dependencies}


def _gate_verdict(results: list[dict]) -> dict[str, object]:
    total = len(results)
    supported = [item for item in results if item["kind"] in {"positive", "multi_step"}]
    unsupported = [item for item in results if item["kind"] == "unsupported"]
    first_passes = sum(item["ok"] and item["repairs"] == 0 for item in supported)
    successes = sum(item["ok"] for item in supported)
    safety_failures = [item["case_id"] for item in results
                       if item["kind"] in {"negative", "ambiguity", "unsupported"} and not item["ok"]]
    first_pass_rate = first_passes / len(supported) if supported else 0.0
    final_compile_rate = successes / len(supported) if supported else 0.0
    task_fulfilled = sum(bool(item.get("task_fulfilled", item["ok"])) for item in supported)
    return {
        "total": total, "supported_total": len(supported),
        "unsupported_total": len(unsupported),
        "unsupported_safe": sum(item["ok"] for item in unsupported),
        "task_fulfilled": task_fulfilled,
        "first_passes": first_passes,
        "first_pass_rate": first_pass_rate,
        "successes": successes,
        "final_compile_rate": final_compile_rate,
        "safety_failures": safety_failures,
        "passed": total >= 201 and bool(supported) and first_pass_rate >= 0.80 and final_compile_rate >= 0.90 and not safety_failures,
    }


def _evaluate(case: LiveCase, planner: OpenAICompatiblePlanner, compiler: Compiler, artifacts: tuple[dict, ...]) -> dict:
    request = PlannerRequest(
        run_id=f"live-{case.case_id.replace(':', '-')}", input_revision=1,
        user_goal=_request_prompt(case), input_artifacts=artifacts, revision_patch={},
    )
    repairs = 0
    try:
        try:
            call = planner.create_draft(request)
        except PlannerError as exc:
            if exc.code != "LLM_DRAFT_INVALID":
                raise
            repairs = 1
            call = planner.repair_draft(
                request, rejected_draft=None,
                diagnostic={"code": exc.code, "path": "planner_response", "message": str(exc)},
            )
        actual = [task.op for task in call.draft.tasks]
        if case.expected_origin == "needs_clarification":
            # Clarification is intentionally not executable.  Still cross the
            # real Compiler boundary and assert that it rejects the empty
            # plan, rather than treating a PlannerDraft as a green result.
            compile_code = None
            try:
                compiler.compile(call.draft)
            except CompileError as exc:
                compile_code = exc.code
            ok = call.draft.origin == case.expected_origin and not actual and compile_code == "EMPTY_PLAN"
            return {
                "case_id": case.case_id, "kind": case.kind, "expected_ops": list(case.expected_ops), "actual_ops": actual,
                "expected_origin": case.expected_origin, "actual_origin": call.draft.origin,
                "compile_code": compile_code, "ok": ok, "latency_ms": call.latency_ms,
                "policy_ok": ok, "task_fulfilled": False,
                "failure": None if ok else "UNSAFE_EXECUTION_ORIGIN",
                "repairs": repairs,
            }
        if call.draft.origin != case.expected_origin:
            return {
                "case_id": case.case_id, "kind": case.kind, "expected_ops": list(case.expected_ops),
                "actual_ops": actual, "expected_origin": case.expected_origin,
                "actual_origin": call.draft.origin, "ok": False,
                "policy_ok": False, "task_fulfilled": False,
                "failure": "ORIGIN_MISMATCH", "repairs": repairs,
            }
        try:
            plan = compiler.compile(call.draft)
        except (CompileError, ValueError) as exc:
            if repairs:
                raise
            repairs = 1
            call = planner.repair_draft(
                request, rejected_draft=call.draft,
                diagnostic={
                    "code": getattr(exc, "code", "PLAN_COMPILE_FAILED"),
                    "path": getattr(exc, "path", "draft"), "message": str(exc),
                },
            )
            plan = compiler.compile(call.draft)
            actual = [task.op for task in plan.tasks]
        ok, failure, semantics = _compiled_semantics(case, plan)
        return {
            "case_id": case.case_id, "kind": case.kind, "expected_ops": list(case.expected_ops), "actual_ops": [task.op for task in plan.tasks],
            "ok": ok, "latency_ms": call.latency_ms, **semantics,
            "policy_ok": ok, "task_fulfilled": ok,
            "failure": failure,
            "repairs": repairs,
        }
    except Exception as exc:  # retain every failure for the aggregate verdict
        return {
            "case_id": case.case_id, "kind": case.kind, "expected_ops": list(case.expected_ops), "actual_ops": [],
            "expected_origin": case.expected_origin,
            "ok": False, "policy_ok": False, "task_fulfilled": False,
            "latency_ms": None, "failure": getattr(exc, "code", type(exc).__name__),
            "failure_detail": str(exc)[:500], "repairs": repairs,
        }


def test_real_llm_grounded_intent_compile_201_prompt_gate():
    if os.getenv("GISMIND_LIVE_LLM") != "1":
        pytest.skip("set GISMIND_LIVE_LLM=1 to call the configured real LLM")
    if not _configured():
        pytest.fail("GISMIND_LIVE_LLM=1 requires configured LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL")
    requested = int(os.getenv("GISMIND_LIVE_LLM_CASES", "201"))
    if requested < 201:
        pytest.fail("release gate requires at least 201 real LLM prompts")
    cases = _cases()
    assert len(cases) == 201
    planner = OpenAICompatiblePlanner.from_settings()
    workers = max(1, min(int(os.getenv("GISMIND_LIVE_LLM_CONCURRENCY", "4")), 8))
    artifacts = _input_artifacts()
    metadata = {
        item["artifact_id"]: {
            **item, "validation_status": "valid", "storage_uri": f"artifacts/sha256/{item['content_hash'][:2]}/{item['content_hash']}",
            "size_bytes": 1,
        }
        for item in artifacts
    }
    compiler = Compiler(
        ToolContractRegistry.with_builtins(), artifact_lookup=metadata.get,
        policy={"allowed_path_root": os.path.abspath(os.getenv("TEMP", "."))},
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda case: _evaluate(case, planner, compiler, artifacts), cases))
    verdict = _gate_verdict(results)
    failures = [item for item in results if not item["ok"]]
    print({
        "suite": "real_llm_grounded_intent_compile", "provider": planner.provider, "model": planner.model,
        "corpus_version": CORPUS_VERSION, "evaluator_version": EVALUATOR_VERSION,
        "corpus_sha256": hashlib.sha256(_CORPUS_PATH.read_bytes()).hexdigest(),
        "facts_sha256": hashlib.sha256(_FACTS_PATH.read_bytes()).hexdigest(),
        "total": len(results), "first_passes": verdict["first_passes"],
        "first_pass_rate": round(verdict["first_pass_rate"], 4), "successes": verdict["successes"],
        "final_compile_rate": round(verdict["final_compile_rate"], 4),
        "safety_failures": verdict["safety_failures"],
        "supported_total": verdict["supported_total"],
        "unsupported_total": verdict["unsupported_total"],
        "unsupported_safe": verdict["unsupported_safe"],
        "task_fulfilled": verdict["task_fulfilled"],
        "failure_samples": failures[:10],
    })
    report_path = os.getenv("GISMIND_LIVE_LLM_REPORT")
    if report_path:
        Path(report_path).write_text(json.dumps({
            "suite": "real_llm_grounded_intent_compile", "provider": planner.provider, "model": planner.model,
            "corpus_version": CORPUS_VERSION, "evaluator_version": EVALUATOR_VERSION,
            "corpus_sha256": hashlib.sha256(_CORPUS_PATH.read_bytes()).hexdigest(),
            "facts_sha256": hashlib.sha256(_FACTS_PATH.read_bytes()).hexdigest(),
            "total": len(results), "first_passes": verdict["first_passes"],
            "first_pass_rate": verdict["first_pass_rate"], "successes": verdict["successes"],
            "final_compile_rate": verdict["final_compile_rate"],
            "safety_failures": verdict["safety_failures"], "results": results,
            "supported_total": verdict["supported_total"],
            "unsupported_total": verdict["unsupported_total"],
            "unsupported_safe": verdict["unsupported_safe"],
            "task_fulfilled": verdict["task_fulfilled"],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    assert not verdict["safety_failures"], f"unsafe negative/ambiguity/unsupported outcomes: {verdict['safety_failures'][:10]}"
    assert verdict["first_pass_rate"] >= 0.80, f"real LLM first-pass compile rate {verdict['first_pass_rate']:.1%} is below 80%"
    assert verdict["final_compile_rate"] >= 0.90, f"real LLM final compile rate {verdict['final_compile_rate']:.1%} is below 90%; samples={failures[:10]}"


def test_real_llm_e2e_geocode_to_artifact(tmp_path):
    """Exercise the real provider, durable planner, child worker, and CAS.

    The separate 201-case gate measures grounded contract compliance and
    bounded JSON/contract compilation, not blind natural-language intent. This
    vertical case proves that a non-mocked model response also crosses the
    production Run -> PlannerAttempt -> PlanIR -> isolated Task -> Artifact
    boundary.  Geocoding uses the configured production provider, so this also
    proves that a genuine GIS handler result is validated and published rather
    than replaced with a fixture.
    """
    if os.getenv("GISMIND_LIVE_LLM") != "1":
        pytest.skip("set GISMIND_LIVE_LLM=1 to call the configured real LLM")
    if not _configured():
        pytest.fail("GISMIND_LIVE_LLM=1 requires configured LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL")

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("live-e2e-session")
    artifacts = ArtifactStore(tmp_path / "runtime-data", runtime)
    supervisor = RuntimeSupervisor(
        runtime, artifacts, planner=OpenAICompatiblePlanner.from_settings(), autostart=False,
    )
    runtime.create_run("live-e2e-run", "live-e2e-session")
    runtime.add_input_revision(
        "live-e2e-run", 1,
        {"prompt": "查询南京市玄武区的坐标。", "input_artifact_ids": []},
    )

    assert supervisor.run_once("live-e2e-worker", run_id="live-e2e-run") == {
        "status": "planned", "run_id": "live-e2e-run",
    }
    result = supervisor.run_once("live-e2e-worker", run_id="live-e2e-run")
    assert result is not None
    snapshot = runtime.get_run("live-e2e-run")
    assert snapshot["status"] == "completed"
    task = runtime._read(lambda c: c.execute(
        "SELECT task_id,tool_name,status FROM tasks WHERE plan_id=(SELECT active_plan_id FROM runs WHERE run_id=?)",
        ("live-e2e-run",),
    ).fetchone())
    assert task is not None and tuple(task[1:]) == ("geo.geocode", "succeeded")
    artifact_id = runtime._read(lambda c: c.execute(
        "SELECT artifact_id FROM task_outputs WHERE task_id=?", (task[0],)
    ).fetchone()[0])
    published = runtime.get_artifact(artifact_id)
    assert published is not None and published["type"] == "Coordinate"
    assert published["validation_status"] == "valid"
    payload = json.loads(artifacts.path_for(artifact_id).read_text(encoding="utf-8"))
    lng, lat = payload["location"]
    assert 118.5 <= float(lng) <= 119.2
    assert 31.8 <= float(lat) <= 32.3


def _live_runtime_or_skip(tmp_path):
    """Build a real Runtime boundary for opt-in semantic action tests."""
    if os.getenv("GISMIND_LIVE_LLM") != "1":
        pytest.skip("set GISMIND_LIVE_LLM=1 to call the configured real LLM")
    if not _configured():
        pytest.fail("GISMIND_LIVE_LLM=1 requires configured LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL")
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("live-action-session")
    artifacts = ArtifactStore(tmp_path / "runtime-data", runtime)
    supervisor = RuntimeSupervisor(
        runtime, artifacts, planner=OpenAICompatiblePlanner.from_settings(), autostart=False,
    )
    return runtime, artifacts, supervisor


def _run_real_action(runtime, supervisor, run_id: str, *, max_steps: int = 4):
    assert supervisor.run_once("live-action-worker", run_id=run_id) == {
        "status": "planned", "run_id": run_id,
    }
    for _ in range(max_steps):
        snapshot = runtime.get_run(run_id)
        if snapshot["status"] in {"completed", "failed", "cancelled", "awaiting_input"}:
            break
        supervisor.run_once("live-action-worker", run_id=run_id)
    return runtime.get_run(run_id)


def test_real_llm_e2e_vector_buffer_to_typed_artifact(tmp_path):
    """Real Planner -> Compiler -> Worker -> Vector handler, without a mock Draft."""
    runtime, artifacts, supervisor = _live_runtime_or_skip(tmp_path)
    source_payload = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature", "properties": {"name": "南京"},
            "geometry": {"type": "Point", "coordinates": [118.78, 32.04]},
        }],
    }
    source = artifacts.publish_staged(
        artifacts.stage_bytes(json.dumps(source_payload).encode("utf-8"), filename="points.geojson"),
        origin="upload", artifact_type="Vector", crs={"kind": "EPSG", "code": 4326}, schema={"fields": {"name": "string"}},
    )
    runtime.create_run_with_input(
        "live-vector-run", "live-action-session",
        message="请对附件中的点做 100 米缓冲分析，输出缓冲后的面数据。",
        input_artifact_ids=[source["artifact_id"]], command_id="live-vector-command",
        idempotency_key="live-vector-idempotency", command_payload={"kind": "live_vector"},
    )

    snapshot = _run_real_action(runtime, supervisor, "live-vector-run")
    assert snapshot["status"] == "completed"
    output = runtime._read(lambda c: c.execute(
        "SELECT artifact_id FROM task_outputs WHERE task_id IN (SELECT task_id FROM tasks WHERE plan_id=(SELECT active_plan_id FROM runs WHERE run_id=?)) ORDER BY created_at LIMIT 1",
        ("live-vector-run",),
    ).fetchone())
    assert output is not None
    published = runtime.get_artifact(output[0])
    assert published is not None and published["type"] == "Vector" and published["validation_status"] == "valid"
    payload = json.loads(artifacts.path_for(output[0]).read_text(encoding="utf-8"))
    assert payload["type"] == "FeatureCollection"
    assert payload["features"] and payload["features"][0]["geometry"]["type"] in {"Polygon", "MultiPolygon"}


def test_real_llm_e2e_raster_slope_preserves_geotiff(tmp_path):
    """Real Planner -> Raster handler must keep physical GeoTIFF bytes."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    runtime, artifacts, supervisor = _live_runtime_or_skip(tmp_path)
    source_path = tmp_path / "dem.tif"
    with rasterio.open(source_path, "w", driver="GTiff", height=3, width=3, count=1, dtype="float32", crs="EPSG:4326", transform=from_origin(118, 33, 0.01, 0.01)) as dataset:
        dataset.write(np.arange(9, dtype="float32").reshape(1, 3, 3))
    source = artifacts.publish_staged(
        artifacts.stage_file(source_path, filename="dem.tif"),
        origin="upload", artifact_type="Raster", crs={"kind": "EPSG", "code": 4326}, schema={}, media_type="image/tiff",
    )
    runtime.create_run_with_input(
        "live-raster-run", "live-action-session",
        message="请对附件中的 DEM 计算坡度，使用角度输出。",
        input_artifact_ids=[source["artifact_id"]], command_id="live-raster-command",
        idempotency_key="live-raster-idempotency", command_payload={"kind": "live_raster"},
    )

    snapshot = _run_real_action(runtime, supervisor, "live-raster-run")
    assert snapshot["status"] == "completed"
    output = runtime._read(lambda c: c.execute(
        "SELECT artifact_id FROM task_outputs WHERE task_id IN (SELECT task_id FROM tasks WHERE plan_id=(SELECT active_plan_id FROM runs WHERE run_id=?)) ORDER BY created_at LIMIT 1",
        ("live-raster-run",),
    ).fetchone())
    assert output is not None
    published = runtime.get_artifact(output[0])
    assert published is not None and published["type"] == "Raster" and published["validation_status"] == "valid"
    with rasterio.open(artifacts.path_for(output[0])) as dataset:
        assert dataset.count == 1 and dataset.width == 3 and dataset.height == 3
        assert dataset.crs.to_epsg() == 4326
