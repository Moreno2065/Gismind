"""Tests for WorkspaceState / LayerRecord / resolver / geo-helper functions."""

from __future__ import annotations

from typing import Any

import pytest

from app.agents.workspace.layer_record import LayerRecord
from app.agents.workspace.resolver import _FINAL_REFS, _LATEST_REFS, resolve_ref
from app.agents.workspace.state import (
    WorkspaceState,
    _first_geometry_type,
    _infer_kind,
    _infer_metadata,
    _is_geo_like,
)


# ============================================================
# LayerRecord 基础
# ============================================================


def test_layer_record_defaults():
    record = LayerRecord(layer_id="l1", name="test")
    assert record.kind == "unknown"
    assert record.source is None
    assert record.parent_ids == []
    assert record.algorithm_id == ""
    assert record.parameters == {}
    assert record.metadata == {}


def test_layer_record_to_dict():
    record = LayerRecord(
        layer_id="l1",
        name="test",
        kind="vector",
        source="/path/to/file.geojson",
        parent_ids=["p1"],
        algorithm_id="buffer",
        parameters={"distance": 500},
        metadata={"crs": "EPSG:4326"},
    )
    d = record.to_dict()
    assert d["layer_id"] == "l1"
    assert d["name"] == "test"
    assert d["kind"] == "vector"
    assert d["source"] == "/path/to/file.geojson"
    assert d["parent_ids"] == ["p1"]
    assert d["algorithm_id"] == "buffer"
    assert d["parameters"] == {"distance": 500}
    assert d["metadata"] == {"crs": "EPSG:4326"}


def test_layer_record_to_dict_dict_source():
    """source 为 dict 时 to_dict 输出 '<dict>' 占位符。"""
    record = LayerRecord(
        layer_id="l1",
        name="inline",
        source={"type": "FeatureCollection", "features": []},
    )
    d = record.to_dict()
    assert d["source"] == "<dict>"


# ============================================================
# WorkspaceState 核心
# ============================================================


def test_add_layer_and_resolve():
    ws = WorkspaceState({})
    ws.add_layer(name="pois", kind="point", metadata={"crs": "EPSG:4326"})
    record = ws.resolve("pois")
    assert record.name == "pois"
    assert record.metadata["crs"] == "EPSG:4326"


def test_resolve_latest():
    ws = WorkspaceState({})
    ws.add_layer(name="a", kind="point")
    ws.add_layer(name="b", kind="polygon")
    assert ws.resolve("latest").name == "b"


def test_resolve_latest_chinese():
    ws = WorkspaceState({})
    ws.add_layer(name="first", kind="point")
    ws.add_layer(name="second", kind="polygon")
    assert ws.resolve("上一步结果").name == "second"
    assert ws.resolve("上个结果").name == "second"
    assert ws.resolve("最新结果").name == "second"
    assert ws.resolve("最新输出").name == "second"


def test_resolve_latest_empty_layers_returns_none():
    """latest 引用在无图层时返回 None（resolve 抛出 KeyError）。"""
    ws = WorkspaceState({})
    with pytest.raises(KeyError):
        ws.resolve("latest")


def test_resolve_final():
    ws = WorkspaceState({})
    ws.add_layer(name="a", kind="point")
    ws.add_layer(name="final_out", kind="polygon", metadata={"role": "final"})
    assert ws.resolve("final").name == "final_out"
    assert ws.resolve("final_result").name == "final_out"
    assert ws.resolve("最终结果").name == "final_out"


def test_resolve_final_no_final_layer():
    """final 引用在没有 role=final 图层时返回 None（resolve 抛出 KeyError）。"""
    ws = WorkspaceState({})
    ws.add_layer(name="a", kind="point")
    with pytest.raises(KeyError):
        ws.resolve("final")


def test_resolve_unknown_raises():
    ws = WorkspaceState({})
    with pytest.raises(KeyError, match="Unknown layer reference: nonexistent"):
        ws.resolve("nonexistent")


def test_has_layer():
    ws = WorkspaceState({})
    ws.add_layer(name="roads", kind="vector")
    assert ws.has_layer("roads") is True
    assert ws.has_layer("buildings") is False


def test_latest_layer():
    ws = WorkspaceState({})
    assert ws.latest_layer() is None
    ws.add_layer(name="a", kind="point")
    ws.add_layer(name="b", kind="polygon")
    assert ws.latest_layer().name == "b"


def test_to_dict_roundtrip():
    ws = WorkspaceState({})
    ws.add_layer(name="layer1", kind="vector", metadata={"crs": "EPSG:4326"})
    payload = ws.to_dict()
    ws2 = WorkspaceState.from_dict({}, payload)
    assert ws2.resolve("layer1").metadata["crs"] == "EPSG:4326"
    # aliases 也重建
    assert ws2.has_layer("layer1") is True


def test_multi_alias():
    """layer_id / name / lowercase name 三者都能 resolve 到同一 record。"""
    ws = WorkspaceState({})
    record = ws.add_layer(name="MyLayer", kind="vector")
    assert ws.resolve("MyLayer").layer_id == record.layer_id
    assert ws.resolve("mylayer").layer_id == record.layer_id
    assert ws.resolve(record.layer_id).layer_id == record.layer_id


def test_non_geo_data_not_in_layer():
    """非 GIS 数据（str / int / bool）不会自动进入 LayerRecord。

    测试通过模拟 executor 的注入逻辑来验证。
    """
    ws = WorkspaceState({})
    non_geo_values = {
        "name": "南京",
        "count": 42,
        "active": True,
        "coords": [118.78, 32.04],
    }
    for k, v in non_geo_values.items():
        assert not _is_geo_like(v), f"{k}={v!r} should not be geo-like"


def test_add_layer_generates_layer_id():
    ws = WorkspaceState({})
    record = ws.add_layer(name="roads", kind="vector")
    assert record.layer_id.startswith("roads_")
    assert len(record.layer_id) > len("roads_")


# ============================================================
# 别名解析（resolver.py）
# ============================================================


def test_all_latest_refs():
    """所有 _LATEST_REFS 成员都能正确解析。"""
    layers = {"a": "layer_a", "b": "layer_b"}
    for ref in _LATEST_REFS:
        lid = resolve_ref(ref, layers, {})
        assert lid == "b", f"ref={ref} should resolve to 'b', got {lid}"


def test_all_final_refs():
    """所有 _FINAL_REFS 成员都能正确解析。"""
    # 需要 mock 具有 metadata 属性
    class MockRecord:
        def __init__(self, role):
            self.metadata = {"role": role}

    layers = {
        "intermediate": MockRecord(""),
        "output": MockRecord("final"),
    }
    for ref in _FINAL_REFS:
        lid = resolve_ref(ref, layers, {})
        assert lid == "output", f"ref={ref} should resolve to 'output', got {lid}"


def test_resolve_ref_via_alias():
    """通过别名可以映射到 layer_id。"""
    layers = {"lid_1": "record_1"}
    aliases = {"my_data": "lid_1", "mydata": "lid_1"}
    assert resolve_ref("my_data", layers, aliases) == "lid_1"


def test_resolve_ref_lowercase():
    """别名不区分大小写。"""
    layers = {"lid_1": "record_1"}
    aliases = {"MyData": "lid_1"}
    assert resolve_ref("mydata", layers, aliases) == "lid_1"


# ============================================================
# _is_geo_like
# ============================================================


def test_is_geo_like_feature_collection():
    v = {"type": "FeatureCollection", "features": []}
    assert _is_geo_like(v) is True


def test_is_geo_like_dict_with_features():
    v = {"features": [{"geometry": {"type": "Point"}}]}
    assert _is_geo_like(v) is True


def test_is_geo_like_coordinates():
    v = {"coordinates": [118.78, 32.04], "type": "Point"}
    assert _is_geo_like(v) is True


def test_is_geo_like_shapely():
    """模拟 shapely geometry（type.__module__ 含 'shapely.geometry'）。"""
    class FakeShapelyPoint:
        pass
    FakeShapelyPoint.__module__ = "shapely.geometry"
    assert _is_geo_like(FakeShapelyPoint()) is True


def test_is_geo_like_geodataframe():
    """模拟 GeoDataFrame（有 geometry 属性）。"""

    class FakeGeoDF:
        geometry = None

    assert _is_geo_like(FakeGeoDF()) is True


def test_is_geo_like_false_for_primitives():
    assert _is_geo_like("hello") is False
    assert _is_geo_like(42) is False
    assert _is_geo_like(3.14) is False
    assert _is_geo_like(None) is False
    assert _is_geo_like([1, 2, 3]) is False


# ============================================================
# _infer_kind
# ============================================================


def test_infer_kind_geodataframe():
    class FakeGeoDF:
        geometry = None
        dtypes = None

    assert _infer_kind(FakeGeoDF()) == "vector"


def test_infer_kind_feature_collection():
    v = {"type": "FeatureCollection", "features": [{"geometry": {"type": "Polygon"}}]}
    assert _infer_kind(v) == "polygon"


def test_infer_kind_feature_collection_point():
    v = {"type": "FeatureCollection", "features": [{"geometry": {"type": "Point"}}]}
    assert _infer_kind(v) == "point"


def test_infer_kind_feature_collection_line():
    v = {"type": "FeatureCollection", "features": [{"geometry": {"type": "LineString"}}]}
    assert _infer_kind(v) == "vector"


def test_infer_kind_shapely_point():
    class FakePoint:
        pass
    FakePoint.__name__ = "Point"
    FakePoint.__module__ = "shapely.geometry"
    assert _infer_kind(FakePoint()) == "point"


def test_infer_kind_shapely_polygon():
    class FakePolygon:
        pass
    FakePolygon.__name__ = "Polygon"
    FakePolygon.__module__ = "shapely.geometry"
    assert _infer_kind(FakePolygon()) == "polygon"


def test_infer_kind_shapely_other():
    class FakeMultiLineString:
        pass
    FakeMultiLineString.__name__ = "MultiLineString"
    FakeMultiLineString.__module__ = "shapely.geometry"
    assert _infer_kind(FakeMultiLineString()) == "vector"


def test_infer_kind_unknown():
    assert _infer_kind(42) == "unknown"
    assert _infer_kind("hello") == "unknown"


# ============================================================
# _first_geometry_type
# ============================================================


def test_first_geometry_type_from_features():
    v = {"features": [{"geometry": {"type": "Polygon"}}]}
    assert _first_geometry_type(v) == "Polygon"


def test_first_geometry_type_from_geometries():
    v = {"geometries": [{"geometry": {"type": "Point"}}]}
    assert _first_geometry_type(v) == "Point"


def test_first_geometry_type_empty():
    assert _first_geometry_type({}) == ""
    assert _first_geometry_type({"features": []}) == ""


# ============================================================
# _infer_metadata
# ============================================================


def test_infer_metadata_geodataframe():
    class FakeGeoDF:
        crs = "EPSG:4326"

        def __len__(self):
            return 10

        columns = ["name", "population"]

    meta = _infer_metadata(FakeGeoDF())
    assert meta["crs"] == "EPSG:4326"
    assert meta["feature_count"] == 10
    assert meta["fields"] == ["name", "population"]


def test_infer_metadata_feature_collection():
    v = {
        "features": [
            {"geometry": {"type": "Polygon"}, "properties": {"name": "A"}},
        ],
    }
    meta = _infer_metadata(v)
    assert meta["feature_count"] == 1
    assert meta["geometry_type"] == "Polygon"


def test_infer_metadata_empty():
    v = {"features": []}
    meta = _infer_metadata(v)
    assert meta["feature_count"] == 0
    assert "geometry_type" not in meta


def test_infer_metadata_no_crs():
    class FakeObj:
        def __len__(self):
            return 5

    meta = _infer_metadata(FakeObj())
    assert meta["feature_count"] == 5
    assert "crs" not in meta
    assert "fields" not in meta


# ============================================================
# WorkspaceState._sv 共享引用
# ============================================================


def test_workspace_state_shared_ref():
    """WorkspaceState._sv 与传入的 dict 是同一引用。"""
    sv: dict[str, Any] = {"existing": "data"}
    ws = WorkspaceState(sv)
    sv["new_key"] = "value"
    assert ws._sv is sv
    assert ws._sv["new_key"] == "value"


def test_from_dict_writes_back():
    """from_dict 通过 _sv 共享引用回写。"""
    sv: dict[str, Any] = {}
    payload = {
        "layers": [
            {
                "name": "roads",
                "kind": "vector",
                "layer_id": "r1",
                "metadata": {},
            },
        ],
        "aliases": {"roads": "r1"},
    }
    ws = WorkspaceState.from_dict(sv, payload)
    assert ws._sv is sv
    assert ws.resolve("roads").layer_id == "r1"


# ============================================================
# LayerRecord.kind 类型字面量校验
# ============================================================


def test_layer_kind_literals():
    """所有有效的 LayerKind 字面量都可用。"""
    from app.agents.workspace.layer_record import LayerKind

    # 类型检查：确保这些字面量在类型注解中合法
    valid_kinds: set[str] = {"vector", "raster", "table", "point", "polygon", "unknown"}
    # 运行时无强制校验，但确保 LayerKind 值可被识别
    for kind_str in valid_kinds:
        record = LayerRecord(layer_id="l1", name="test", kind=kind_str)  # type: ignore[arg-type]
        assert record.kind == kind_str


# ============================================================
# 边缘情况
# ============================================================


def test_add_layer_with_all_params():
    ws = WorkspaceState({})
    record = ws.add_layer(
        name="buffer_out",
        kind="polygon",
        source="memory",
        parent_ids=["input_1"],
        algorithm_id="buffer",
        parameters={"distance": 500, "unit": "m"},
        metadata={"crs": "EPSG:4548", "feature_count": 15},
        layer_id="custom_id",
    )
    assert record.layer_id == "custom_id"
    assert record.name == "buffer_out"
    assert record.parent_ids == ["input_1"]
    assert record.parameters["distance"] == 500


def test_add_layer_auto_layer_id():
    ws = WorkspaceState({})
    r1 = ws.add_layer(name="a")
    r2 = ws.add_layer(name="a")  # 同名第二次
    # layer_id 应不同（因为 uuid 不同）
    assert r1.layer_id != r2.layer_id
    # 但别名映射应覆盖为最后一个
    resolved = ws.resolve("a")
    assert resolved.layer_id == r2.layer_id
