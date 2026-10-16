"""Executable semantic postconditions for the Runtime contract boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.runtime import ToolContractRegistry


def _fc(features, *, physical_crs="EPSG:4326"):
    payload = {"type": "FeatureCollection", "features": features}
    if physical_crs:
        payload["_physical_crs"] = physical_crs
    return payload


def _feature(geometry, properties=None):
    return {"type": "Feature", "geometry": geometry, "properties": properties or {}}


def test_every_public_contract_has_a_registered_callable_postcondition():
    from app.runtime.postconditions import resolve_postcondition

    registry = ToolContractRegistry.with_builtins()
    contracts = list(registry.values())
    assert len(contracts) >= 50
    assert all(contract.postconditions for contract in contracts)
    for contract in contracts:
        for reference in contract.postconditions:
            assert callable(resolve_postcondition(reference))


def test_buffer_postcondition_rejects_silent_or_non_geometric_output():
    from app.runtime.postconditions import validate_vector_buffer

    inputs = {"vector": {"data": _fc([_feature({"type": "Point", "coordinates": [0, 0]})])}}
    params = {"distance_m": 100}
    assert validate_vector_buffer(result={"status": "success", "data": _fc([])}, inputs=inputs, params=params) is False
    assert validate_vector_buffer(result={"status": "success", "data": {"distance_m": 100}}, inputs=inputs, params=params) is False


def test_buffer_postcondition_checks_distance_metadata_when_present():
    from app.runtime.postconditions import validate_vector_buffer
    from app.runtime.tool_handlers import handle_vector_buffer

    inputs = {"vector": {"data": _fc([_feature({"type": "Point", "coordinates": [0, 0]})])}}
    good = handle_vector_buffer(**inputs, distance_m=100)
    good["data"]["buffer_distance_m"] = 100
    bad = {"status": "success", "data": {**good["data"], "buffer_distance_m": 101}}
    assert validate_vector_buffer(result=good, inputs=inputs, params={"distance_m": 100}) is True
    assert validate_vector_buffer(result=bad, inputs=inputs, params={"distance_m": 100}) is False


def test_delete_duplicates_preserves_schema_and_never_increases_feature_count():
    from app.runtime.postconditions import validate_vector_delete_duplicates

    source = _fc([
        _feature({"type": "Point", "coordinates": [0, 0]}, {"name": "a"}),
        _feature({"type": "Point", "coordinates": [0, 0]}, {"name": "a"}),
    ])
    inputs = {"input": {"data": source}}
    result = {"status": "success", "data": _fc([_feature({"type": "Point", "coordinates": [0, 0]}, {"name": "a"})])}
    assert validate_vector_delete_duplicates(result=result, inputs=inputs, params={}) is True
    larger = {"status": "success", "data": _fc([*result["data"]["features"], _feature({"type": "Point", "coordinates": [1, 1]}, {"other": 1})])}
    assert validate_vector_delete_duplicates(result=larger, inputs=inputs, params={}) is False


def test_reproject_postconditions_require_actual_target_crs():
    from app.runtime.postconditions import validate_vector_reproject, validate_raster_reproject

    vector = {"data": _fc([_feature({"type": "Point", "coordinates": [0, 0]})]), "crs": {"kind": "EPSG", "code": 4326}}
    good = {"status": "success", "data": _fc([_feature({"type": "Point", "coordinates": [0, 0]})], physical_crs="EPSG:3857")}
    bad = {"status": "success", "data": _fc([_feature({"type": "Point", "coordinates": [1, 1]})], physical_crs="EPSG:4326")}
    assert validate_vector_reproject(result=good, inputs={"vector": vector}, params={"target_crs": {"kind": "EPSG", "code": 3857}}) is True
    assert validate_vector_reproject(result=bad, inputs={"vector": vector}, params={"target_crs": {"kind": "EPSG", "code": 3857}}) is False

    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.transform import from_origin

    profile = {"driver": "GTiff", "height": 2, "width": 2, "count": 1, "dtype": "float32", "crs": "EPSG:3857", "transform": from_origin(0, 2, 1, 1)}
    with MemoryFile() as memory:
        with memory.open(**profile) as dataset:
            import numpy as np
            dataset.write(np.array([[1, 2], [3, 4]], dtype="float32"), 1)
        raster_result = {"status": "success", "data": memory.read()}
    raster_input = {"data": raster_result["data"], "crs": {"kind": "EPSG", "code": 4326}}
    assert validate_raster_reproject(result=raster_result, inputs={"raster": raster_input}, params={"target_crs": {"kind": "EPSG", "code": 3857}}) is True


def test_vector_reproject_postcondition_rejects_relabelled_or_detached_coordinates():
    from pyproj import Transformer

    from app.runtime.postconditions import validate_vector_reproject

    source = {"data": _fc([
        _feature({"type": "Point", "coordinates": [118.78, 32.04]}, {"record_id": 1}),
        _feature({"type": "Point", "coordinates": [118.79, 32.05]}, {"record_id": 2}),
    ]), "crs": {"kind": "EPSG", "code": 4326}}
    transformer = Transformer.from_crs(4326, 3857, always_xy=True)
    projected = [
        _feature({"type": "Point", "coordinates": list(transformer.transform(*point))}, {"record_id": index})
        for index, point in ((1, [118.78, 32.04]), (2, [118.79, 32.05]))
    ]
    good = {"status": "success", "data": _fc(projected, physical_crs="EPSG:3857")}
    relabelled = {"status": "success", "data": _fc([
        _feature({"type": "Point", "coordinates": [118.78, 32.04]}, {"record_id": 1}),
        projected[1],
    ], physical_crs="EPSG:3857")}
    detached = {"status": "success", "data": _fc([
        _feature(projected[0]["geometry"], {"record_id": 2}),
        _feature(projected[1]["geometry"], {"record_id": 1}),
    ], physical_crs="EPSG:3857")}
    params = {"target_crs": {"kind": "EPSG", "code": 3857}}
    assert validate_vector_reproject(good, {"vector": source}, params) is True
    assert validate_vector_reproject(relabelled, {"vector": source}, params) is False
    assert validate_vector_reproject(detached, {"vector": source}, params) is False


def test_real_vector_reprojection_satisfies_coordinate_postcondition():
    from app.runtime.postconditions import validate_vector_reproject
    from app.runtime.tool_handlers import handle_vector_reproject

    source = {"data": _fc([
        _feature({"type": "Point", "coordinates": [118.78, 32.04]}, {"record_id": 1}),
        _feature({"type": "Point", "coordinates": [118.79, 32.05]}, {"record_id": 2}),
    ]), "crs": {"kind": "EPSG", "code": 4326}}
    params = {"target_crs": {"kind": "EPSG", "code": 3857}}
    result = handle_vector_reproject(vector=source, **params)
    assert result["status"] == "success", result
    assert validate_vector_reproject(result, {"vector": source}, params) is True


def test_vector_reproject_postcondition_accepts_reordered_nullable_records():
    from pyproj import Transformer

    from app.runtime.postconditions import validate_vector_reproject

    source = {"data": _fc([
        _feature({"type": "Point", "coordinates": [118.78, 32.04]}, {"record_id": 1, "note": None}),
        _feature({"type": "Point", "coordinates": [118.79, 32.05]}, {"record_id": 2, "note": "west"}),
    ]), "crs": {"kind": "EPSG", "code": 4326}}
    transformer = Transformer.from_crs(4326, 3857, always_xy=True)
    output = _fc([
        _feature({"type": "Point", "coordinates": list(transformer.transform(118.79, 32.05))},
                 {"record_id": 2, "note": "west"}),
        _feature({"type": "Point", "coordinates": list(transformer.transform(118.78, 32.04))},
                 {"record_id": 1, "note": None}),
    ], physical_crs="EPSG:3857")
    assert validate_vector_reproject(
        {"status": "success", "data": output}, {"vector": source},
        {"target_crs": {"kind": "EPSG", "code": 3857}},
    ) is True


def test_export_and_reproject_reversed_records_do_not_quadratically_scan(monkeypatch):
    from shapely.geometry.base import BaseGeometry

    from app.runtime.postconditions import validate_file_export, validate_vector_reproject

    features = [
        _feature({"type": "Point", "coordinates": [118.0 + index / 1000, 32.0]}, {"kind": "same"})
        for index in range(150)
    ]
    source = {"type": "Vector", "crs": {"kind": "EPSG", "code": 4326}, "data": _fc(features)}
    reversed_features = list(reversed(features))
    original_equals_exact = BaseGeometry.equals_exact
    comparisons = 0

    def counted_equals_exact(self, other, tolerance=0.0, *args, **kwargs):
        nonlocal comparisons
        comparisons += 1
        return original_equals_exact(self, other, tolerance, *args, **kwargs)

    monkeypatch.setattr(BaseGeometry, "equals_exact", counted_equals_exact)
    exported = {"status": "success", "data": json.dumps({
        "type": "FeatureCollection", "features": reversed_features,
    }).encode("utf-8")}
    reprojected = {"status": "success", "data": _fc(reversed_features)}
    assert validate_file_export(exported, {"input": source}, {"format": "geojson"}) is True
    assert validate_vector_reproject(
        reprojected, {"vector": source}, {"target_crs": {"kind": "EPSG", "code": 4326}},
    ) is True
    assert comparisons <= 8 * len(features)


def test_reproject_postcondition_accepts_roundoff_within_tolerance_only():
    from app.runtime.postconditions import validate_vector_reproject

    source = {"type": "Vector", "crs": {"kind": "EPSG", "code": 4326}, "data": _fc([
        _feature({"type": "Point", "coordinates": [118.78, 32.04]}, {"id": 1}),
        _feature({"type": "Point", "coordinates": [118.79, 32.05]}, {"id": 2}),
    ])}
    params = {"target_crs": {"kind": "EPSG", "code": 4326}}

    def result_with_offset(offset):
        return {"status": "success", "data": _fc([
            _feature({"type": "Point", "coordinates": [118.79 + offset, 32.05]}, {"id": 2}),
            _feature({"type": "Point", "coordinates": [118.78 + offset, 32.04]}, {"id": 1}),
        ])}

    assert validate_vector_reproject(result_with_offset(5e-8), {"vector": source}, params) is True
    assert validate_vector_reproject(result_with_offset(5e-6), {"vector": source}, params) is False


@pytest.mark.parametrize("validator", ["export", "reproject"])
@pytest.mark.parametrize("mutation", ["drop_z", "change_later_z", "boolean_to_number"])
def test_record_postconditions_reject_lost_3d_coordinates_and_boolean_type(validator, mutation):
    from app.runtime.postconditions import validate_file_export, validate_vector_reproject

    feature = _feature({"type": "LineString", "coordinates": [[118.7, 32.0, 3.0], [118.8, 32.1, 4.0]]},
                       {"flag": True, "count": 1})
    source = {"type": "Vector", "crs": {"kind": "EPSG", "code": 4326}, "data": _fc([feature])}
    changed = json.loads(json.dumps(feature))
    if mutation == "drop_z":
        changed["geometry"]["coordinates"] = [[118.7, 32.0], [118.8, 32.1]]
    elif mutation == "change_later_z":
        changed["geometry"]["coordinates"][1][2] = 999.0
    else:
        changed["properties"]["flag"] = 1
    if validator == "export":
        result = {"status": "success", "data": json.dumps({
            "type": "FeatureCollection", "features": [changed],
        }).encode("utf-8")}
        accepted = validate_file_export(result, {"input": source}, {"format": "geojson"})
    else:
        result = {"status": "success", "data": _fc([changed])}
        accepted = validate_vector_reproject(
            result, {"vector": source}, {"target_crs": {"kind": "EPSG", "code": 4326}},
        )
    assert accepted is False


@pytest.mark.parametrize("validator", ["export", "reproject"])
def test_record_postconditions_keep_3d_and_numeric_equivalence(validator):
    from app.runtime.postconditions import validate_file_export, validate_vector_reproject

    feature = _feature({"type": "LineString", "coordinates": [[118.7, 32.0, 3.0], [118.8, 32.1, 4.0]]},
                       {"flag": True, "count": 1})
    source = {"type": "Vector", "crs": {"kind": "EPSG", "code": 4326}, "data": _fc([feature])}
    output = json.loads(json.dumps(feature))
    output["properties"]["count"] = 1.0
    output["geometry"]["coordinates"][1][2] += 5e-8
    if validator == "export":
        result = {"status": "success", "data": json.dumps({
            "type": "FeatureCollection", "features": [output],
        }).encode("utf-8")}
        accepted = validate_file_export(result, {"input": source}, {"format": "geojson"})
    else:
        result = {"status": "success", "data": _fc([output])}
        accepted = validate_vector_reproject(
            result, {"vector": source}, {"target_crs": {"kind": "EPSG", "code": 4326}},
        )
    assert accepted is True


@pytest.mark.parametrize("export_format", ["geojson", "gpkg"])
def test_real_3d_export_and_reproject_satisfy_record_postconditions(export_format):
    from app.runtime.contracts import _handle_file_export
    from app.runtime.postconditions import validate_file_export, validate_vector_reproject
    from app.runtime.tool_handlers import handle_vector_reproject

    source = {"type": "Vector", "crs": {"kind": "EPSG", "code": 4326}, "data": _fc([
        _feature({"type": "LineString", "coordinates": [[118.7, 32.0, 3.0], [118.8, 32.1, 4.0]]},
                 {"flag": True, "count": 1}),
    ])}
    exported = _handle_file_export(input=source, format=export_format)
    assert exported["status"] == "success", exported
    assert validate_file_export(exported, {"input": source}, {"format": export_format}) is True
    params = {"target_crs": {"kind": "EPSG", "code": 3857}}
    reprojected = handle_vector_reproject(vector=source, **params)
    assert reprojected["status"] == "success", reprojected
    assert validate_vector_reproject(reprojected, {"vector": source}, params) is True


def test_slope_requires_readable_geotiff_with_crs_and_transform():
    from app.runtime.postconditions import validate_raster_slope

    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.transform import from_origin

    profile = {"driver": "GTiff", "height": 2, "width": 2, "count": 1, "dtype": "float32", "crs": "EPSG:4326", "transform": from_origin(0, 2, 1, 1)}
    with MemoryFile() as memory:
        with memory.open(**profile) as dataset:
            import numpy as np
            dataset.write(np.array([[1, 2], [3, 4]], dtype="float32"), 1)
        valid = memory.read()
    assert validate_raster_slope(result={"status": "success", "data": valid}, inputs={}, params={}) is True
    assert validate_raster_slope(result={"status": "success", "data": b"not-a-raster"}, inputs={}, params={}) is False


def test_provider_postconditions_distinguish_valid_empty_from_malformed():
    from app.runtime.postconditions import validate_geo_geocode, validate_poi_search

    assert validate_geo_geocode(result={"status": "empty", "data": {"candidates": []}, "message": "no match"}, inputs={}, params={"address": "none"}, empty_policy="allow") is True
    assert validate_geo_geocode(result={"status": "empty", "data": None}, inputs={}, params={"address": "none"}, empty_policy="allow") is False
    assert validate_geo_geocode(result={"status": "success", "source": "Amap", "data": {"location": [118.7, 32.0]}}, inputs={}, params={"address": "nanjing"}) is True
    assert validate_poi_search(result={"status": "success", "source": "Amap", "data": {"pois": []}}, inputs={}, params={"query": "school"}) is True
    assert validate_poi_search(result={"status": "success", "data": {"oops": []}}, inputs={}, params={"query": "school"}) is False


def test_file_postconditions_require_readable_payload_and_confined_publish_path(tmp_path):
    from app.runtime.postconditions import validate_file_export, validate_publish_file

    payload = b'{"type":"FeatureCollection","features":[]}'
    input_file = {"data": payload, "path": str(tmp_path / "input.geojson")}
    result = {"status": "success", "data": payload}
    (tmp_path / "out.geojson").write_bytes(payload)
    assert validate_file_export(result=result, inputs={"input": input_file}, params={}) is True
    assert validate_file_export(result={"status": "success", "data": b""}, inputs={"input": input_file}, params={}) is False
    assert validate_publish_file(result=result, inputs={"input": input_file}, params={"path": str(tmp_path / "out.geojson")}) is True
    assert validate_publish_file(result=result, inputs={"input": input_file}, params={"path": "..\\outside.geojson"}) is False
