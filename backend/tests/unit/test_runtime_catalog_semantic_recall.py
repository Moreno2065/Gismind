"""Business-language retrieval regressions independent of production aliases.

The frozen 2000-case corpus is a known regression set, not a held-out benchmark.
No test in this module calls a provider or creates upload files.
"""
from __future__ import annotations

from collections import Counter

import pytest

from app.runtime.contracts import PortContract, ToolContract, ToolContractRegistry
from app.runtime.llm_planner import PlannerRequest, candidate_catalog
from tests.live.prompt_corpus_2000 import CORPUS_SHA256, corpus_digest, load_cases


def upload(kind="Vector", geometry="Polygon", artifact_id="source"):
    fields = {"分类码": "object", "对象号": "int64"}
    return {"artifact_id": artifact_id, "type": "File", "schema": {
        "fields": fields,
        "inspection": {"version": "2", "source": "physical_file", "artifact_type": kind,
                       "fields": fields, "geometry_types": [geometry]},
    }}


def catalog_for(goal, artifacts, *, limit=18):
    request = PlannerRequest(run_id="semantic-recall", input_revision=1, user_goal=goal,
                             input_artifacts=tuple(artifacts), revision_patch={})
    return candidate_catalog(ToolContractRegistry.with_builtins(), request, limit=limit)


@pytest.mark.parametrize("goal,operation,geometry", [
    ("测量站向外延伸150米，求每站的覆盖区。", "vector.buffer", "Point"),
    ("标出观测点周边二百米以内的地带。", "vector.buffer", "Point"),
    ("请找每个湖泊的重心位置。", "vector.centroid", "Polygon"),
    ("把城区边界换为各自的面积重心。", "vector.centroid", "Polygon"),
    ("求全部观测站的凸壳。", "vector.convex_hull", "Point"),
    ("用面积最小的凸面包住勘测点。", "vector.convex_hull", "Point"),
    ("把分离的小岛拆为独立的要素。", "vector.multipart_to_singlepart", "MultiPolygon"),
    ("多面对象的每个部分单独作为一行。", "vector.multipart_to_singlepart", "MultiPolygon"),
    ("属性相同的地块合并，抹掉组内边界。", "vector.dissolve", "Polygon"),
    ("按照分类码消除内部边界，连接各组地块。", "vector.dissolve", "Polygon"),
])
def test_independent_paraphrases_get_operation_and_loader(goal, operation, geometry):
    catalog = catalog_for(goal, [upload(geometry=geometry)], limit=3)
    assert set(catalog.operation_ids) == {operation, "io.load_vector"}


@pytest.mark.parametrize("goal,operation,excluded", [
    ("仅取第一层在第二层区域里的几何部分。", "vector.clip", "vector.extract_location"),
    ("沿第二份边界截取第一份地块。", "vector.clip", "vector.extract_location"),
    ("选出落在参照面内的完整地块，不切开几何。", "vector.extract_location", "vector.clip"),
    ("范围内的对象整条保留，几何形状不变。", "vector.extract_location", "vector.clip"),
    ("将乙图层的记录追加至甲图层，保持每个对象。", "vector.merge", "vector.dissolve"),
    ("两批记录汇到一份数据里，不消除边界。", "vector.merge", "vector.dissolve"),
    ("按类别相同的地块消除边界，再合并。", "vector.dissolve", "vector.merge"),
])
def test_composed_meaning_distinguishes_nearby_operations(goal, operation, excluded):
    catalog = catalog_for(goal, [upload(), upload(artifact_id="reference")], limit=3)
    assert operation in catalog.operation_ids
    assert excluded not in catalog.operation_ids
    assert "io.load_vector" in catalog.operation_ids


@pytest.mark.parametrize("goal,operation,excluded", [
    ("把备注字段拆成独立的列之前，先只保留备注字段。", "attribute.keep_fields", "vector.multipart_to_singlepart"),
    ("重心是列名，请将字段重心更名为中心描述。", "attribute.rename_field", "vector.centroid"),
    ("把覆盖距离字段更名为米数。", "attribute.rename_field", "vector.buffer"),
    ("按类别汇总数值的字段统计摘要。", "io.summarize", "vector.merge"),
])
def test_related_words_alone_do_not_override_explicit_other_operation(goal, operation, excluded):
    catalog = catalog_for(goal, [upload()], limit=3)
    assert operation in catalog.operation_ids
    assert excluded not in catalog.operation_ids


def test_partial_auxiliary_port_match_does_not_outrank_complete_input_match():
    registry = ToolContractRegistry()
    for contract in [
        ToolContract(name="io.load_vector", version="1.0.0", input_ports={"file": PortContract(artifact_type="File")}, output_ports={"vector": PortContract(artifact_type="Vector")}),
        ToolContract(name="a.cross_domain", version="1.0.0", input_ports={"mask": PortContract(artifact_type="Vector"), "raster": PortContract(artifact_type="Raster")}, output_ports={"output": PortContract(artifact_type="Table")}),
        ToolContract(name="z.vector_operation", version="1.0.0", input_ports={"input": PortContract(artifact_type="Vector")}, output_ports={"output": PortContract(artifact_type="Table")}),
    ]:
        registry.register(contract)
    req = PlannerRequest(run_id="input-affinity", input_revision=1, user_goal="处理数据", input_artifacts=(upload(),), revision_patch={})
    assert set(candidate_catalog(registry, req, limit=3).operation_ids) == {"io.load_vector", "z.vector_operation"}


@pytest.mark.parametrize("goal,operation,artifacts,loaders", [
    ("以面边界掩膜裁剪影像。", "raster.clip_mask", [upload(), upload("Raster", artifact_id="raster")], {"io.load_vector", "io.load_raster"}),
    ("按区域进行分区统计。", "raster.zonal_statistics", [upload(), upload("Raster", artifact_id="raster")], {"io.load_vector", "io.load_raster"}),
    ("在采样点位置提取像元值。", "raster.sample", [upload(geometry="Point"), upload("Raster", artifact_id="raster")], {"io.load_vector", "io.load_raster"}),
    ("将矢量面栅格化。", "raster.vectorize", [upload()], {"io.load_vector"}),
])
def test_cross_domain_operations_and_dependencies_remain_available(goal, operation, artifacts, loaders):
    catalog = catalog_for(goal, artifacts)
    assert {operation, *loaders}.issubset(catalog.operation_ids)
    assert len(catalog) <= 18


# This mapping is test-owned business intent, never generated from _ALIASES.
EXPECTED = {
    "buffer": "vector.buffer", "centroid": "vector.centroid", "hull": "vector.convex_hull",
    "bbox": "vector.bounding_boxes", "explode": "vector.multipart_to_singlepart",
    "dedup": "vector.delete_duplicates", "filter": "attribute.filter", "keep": "attribute.keep_fields",
    "rename": "attribute.rename_field", "calculate": "attribute.calculate", "reproject": "vector.reproject",
    "dissolve": "vector.dissolve", "csvpoints": "io.csv_to_points", "rastercalc": "raster.calculate",
    "clip": "vector.clip", "merge": "vector.merge",
}


def test_known_1600_positive_corpus_recall_and_loader_closure():
    cases = load_cases()
    assert len(cases) == 2000 and corpus_digest(cases) == CORPUS_SHA256
    totals, failures = Counter(), []
    for case in cases:
        if case.kind != "success":
            continue
        totals[case.family] += 1
        kind = "Table" if case.family == "csvpoints" else "Raster" if case.family == "rastercalc" else "Vector"
        geometry = "Point" if case.family in {"buffer", "hull", "dedup"} else "MultiPolygon" if case.family == "explode" else "Polygon"
        artifacts = [upload(kind, geometry)]
        if case.family in {"clip", "merge"}:
            artifacts.append(upload(kind, geometry, "reference"))
        for artifact in artifacts:
            artifact["schema"]["file"] = {"extension": "." + case.file_format}
            artifact["schema"]["inspection"]["format"] = case.file_format
        catalog = catalog_for(case.prompt, artifacts)
        loader = {"Vector": "io.load_vector", "Raster": "io.load_raster", "Table": "io.load_csv"}[kind]
        assert len(catalog) <= 18
        assert loader in catalog.operation_ids, case.case_id
        if EXPECTED[case.family] not in catalog.operation_ids:
            failures.append(case.case_id)
    assert dict(totals) == {family: 100 for family in EXPECTED}
    assert not failures, {"misses": len(failures), "case_ids": failures}
