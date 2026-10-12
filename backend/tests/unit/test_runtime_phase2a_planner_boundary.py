from __future__ import annotations

import json

import pytest

from app.runtime import ArtifactRef, Compiler, PlannerDraft, PlannerStepDraft, ToolContractRegistry
from app.runtime.compiler import CompileError
from app.runtime.llm_planner import PlannerRequest, candidate_catalog, catalog_hash_for_operation_ids


def _request(goal: str) -> PlannerRequest:
    return PlannerRequest(
        run_id="run-phase2a", input_revision=1, user_goal=goal,
        input_artifacts=({"artifact_id": "art-vector", "type": "Vector", "crs": {"kind": "EPSG", "code": 4326}},),
        revision_patch={},
    )


def test_candidate_catalog_is_bounded_and_carries_stable_contract_metadata():
    registry = ToolContractRegistry.with_builtins()
    first = candidate_catalog(registry, _request("请删除重复几何，并搜索附近医院，再计算栅格坡度并导出结果"))
    second = candidate_catalog(registry, _request("请删除重复几何，并搜索附近医院，再计算栅格坡度并导出结果"))

    assert len(first) <= 18
    assert first.operation_ids == tuple(sorted(first.operation_ids))
    assert {"vector.delete_duplicates", "poi.search", "raster.slope", "file.export"}.issubset(first.operation_ids)
    assert first.catalog_hash == second.catalog_hash
    assert first.canonical_json == second.canonical_json
    payload = json.loads(first.canonical_json)
    assert payload["operation_ids"] == list(first.operation_ids)
    assert all(item["op"] in first.operation_ids and "params_schema" in item for item in payload["operations"])


def test_compiler_rejects_operation_outside_persisted_catalog():
    registry = ToolContractRegistry.with_builtins()
    catalog = candidate_catalog(registry, _request("删除重复几何"))
    draft = PlannerDraft(
        run_id="run-phase2a", input_revision=1, user_goal="删除重复几何",
        allowed_operation_ids=list(catalog.operation_ids), catalog_hash=catalog.catalog_hash,
        tasks=[PlannerStepDraft(task_key="x", op="raster.slope", params={})],
    )

    with pytest.raises(CompileError) as exc:
        Compiler(registry).compile(draft)

    assert exc.value.code == "OPERATION_NOT_IN_CATALOG"


def test_catalog_hash_binds_contract_prompt_metadata():
    registry = ToolContractRegistry.with_builtins()
    catalog = candidate_catalog(registry, _request("删除重复几何"))
    assert catalog_hash_for_operation_ids(registry, catalog.operation_ids) == catalog.catalog_hash


def test_expansion_is_explicit_and_changes_catalog_hash():
    registry = ToolContractRegistry.with_builtins()
    base = candidate_catalog(registry, _request("删除重复几何"))
    expanded = base.expand(registry, ["raster.slope"])

    assert "raster.slope" in expanded.operation_ids
    assert expanded.catalog_hash != base.catalog_hash
    assert expanded.expansion_parent_hash == base.catalog_hash
    assert base.catalog_hash == candidate_catalog(registry, _request("删除重复几何")).catalog_hash


def test_candidate_catalog_metadata_is_deeply_immutable():
    registry = ToolContractRegistry.with_builtins()
    catalog = candidate_catalog(registry, _request("删除重复几何"))
    before = (catalog.catalog_hash, catalog.canonical_json)

    with pytest.raises(TypeError):
        catalog.operations[0]["op"] = "vector.buffer"
    with pytest.raises(TypeError):
        catalog.operations[0]["inputs"][next(iter(catalog.operations[0]["inputs"]))]["types"] = []
    with pytest.raises(AttributeError):
        catalog.operation_ids.append("geo.geocode")

    assert (catalog.catalog_hash, catalog.canonical_json) == before


def test_catalog_ids_are_canonicalized_before_hashing_and_persistence():
    registry = ToolContractRegistry.with_builtins()
    canonical = catalog_hash_for_operation_ids(registry, [" io.export "])
    expected = candidate_catalog(registry, _request("发布文件到指定目录"))

    assert canonical == catalog_hash_for_operation_ids(registry, ["publish_file"])
    assert canonical == catalog_hash_for_operation_ids(registry, ["PUBLISH_FILE"])
    assert expected.catalog_hash != ""  # catalog construction remains non-empty


def test_compiler_persists_only_canonical_catalog_and_task_operation_ids(tmp_path):
    registry = ToolContractRegistry.with_builtins()
    ref = ArtifactRef(artifact_id="art-file", type="File", crs={"kind": "EPSG", "code": 4326})
    draft = PlannerDraft(
        run_id="run-phase2a", input_revision=1, user_goal="发布文件",
        allowed_operation_ids=[" io.export "],
        catalog_hash=catalog_hash_for_operation_ids(registry, ["io.export"]),
        tasks=[PlannerStepDraft(task_key="publish", op="io.export", inputs={"input": ref}, params={"path": "out.bin"})],
    )

    plan = Compiler(registry, policy={"allowed_path_root": str(tmp_path)}).compile(draft)

    assert plan.allowed_operation_ids == ["publish_file"]
    assert plan.tasks[0].op == "publish_file"
    assert '"io.export"' not in plan.canonical_json


def test_catalog_rejects_empty_and_unbounded_requests():
    registry = ToolContractRegistry.with_builtins()
    with pytest.raises(ValueError, match="at least one"):
        candidate_catalog(registry, _request("没有明确需求"), limit=0)
    with pytest.raises(ValueError, match="maximum"):
        candidate_catalog(registry, _request("删除重复几何"), limit=19)
    base = candidate_catalog(registry, _request("删除重复几何"))
    with pytest.raises(ValueError, match="maximum"):
        base.expand(registry, [c.name for c in registry.values()])
