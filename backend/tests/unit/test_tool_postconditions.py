"""Semantic postconditions must reject plausible-looking but wrong GIS outputs."""

from __future__ import annotations

import json

import pytest

from app.agents.postconditions import validate_tool_postconditions


def _feature(geometry: dict, **properties: object) -> dict:
    return {"type": "Feature", "properties": properties, "geometry": geometry}


def _collection(*features: dict, crs: str = "WGS84") -> dict:
    return {
        "type": "FeatureCollection",
        "features": list(features),
        "_crs_label": crs,
    }


def _square(min_x: float, min_y: float, max_x: float, max_y: float) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [[
            [min_x, min_y], [max_x, min_y], [max_x, max_y], [min_x, max_y], [min_x, min_y],
        ]],
    }


def _diamond(center_x: float, center_y: float, delta_x: float, delta_y: float) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [[
            [center_x, center_y + delta_y], [center_x + delta_x, center_y],
            [center_x, center_y - delta_y], [center_x - delta_x, center_y],
            [center_x, center_y + delta_y],
        ]],
    }


def _codes(tool_name: str, data: object, *, params: dict | None = None, dependencies: dict[int, object] | None = None) -> set[str]:
    return {
        issue.code
        for issue in validate_tool_postconditions(
            tool_name,
            data,
            params=params or {},
            dependencies=dependencies or {},
        )
    }


def test_poi_postcondition_requires_computed_distance_and_radius_contract() -> None:
    center = [118.7845, 32.0429]
    valid = {
        "pois": [{
            "name": "near", "location": center, "distance": 0.0,
            "crs": "GCJ02", "source": "Amap",
        }],
        "query": "奶茶", "center": center, "radius_m": 500,
        "radius_tolerance_m": 5, "crs": "GCJ02",
    }
    invalid = {
        **valid,
        "pois": [{
            "name": "far", "location": [118.806, 32.0429], "distance": 1.0,
            "crs": "GCJ02", "source": "Amap",
        }],
    }

    assert _codes("query_poi", valid) == set()
    assert {"POI_OUTSIDE_RADIUS", "POI_DISTANCE_MISMATCH"} <= _codes("query_poi", invalid)


def test_buffer_postcondition_checks_area_and_radius_against_input_point() -> None:
    point = _collection(_feature({"type": "Point", "coordinates": [118.7845, 32.0429]}, name="origin"))
    # 100 m at this latitude is about 0.00106 degrees latitude.
    valid = _collection(_feature(_diamond(118.7845, 32.0429, 0.00106, 0.00090), name="origin"))
    invalid = _collection(_feature(_square(118.78449, 32.04289, 118.78451, 32.04291), name="origin"))

    assert _codes("buffer", valid, params={"radius_m": 100}, dependencies={0: point}) == set()
    assert "BUFFER_RADIUS_MISMATCH" in _codes(
        "buffer", invalid, params={"radius_m": 100}, dependencies={0: point},
    )


def test_buffer_postcondition_checks_requested_distance_for_polygon_inputs() -> None:
    """Area growth alone cannot prove that a polygon was buffered by the requested metres."""
    source = _collection(_feature(_square(118.7840, 32.0424, 118.7850, 32.0434), parcel="a"))
    # Only about 5-6 m of expansion, despite claiming a 100 m buffer.
    undersized = _collection(_feature(_square(118.78395, 32.04235, 118.78505, 32.04345), parcel="a"))

    assert "BUFFER_RADIUS_MISMATCH" in _codes(
        "buffer",
        undersized,
        params={"radius_m": 100, "geometry_from": 0},
        dependencies={0: source},
    )


def test_overlay_and_clip_postconditions_check_topology_properties_and_crs() -> None:
    source = _collection(_feature(_square(0, 0, 2, 2), city="Nanjing"))
    mask = _collection(_feature(_square(1, 1, 3, 3), land="park"))
    intersection = _collection(_feature(_square(1, 1, 2, 2), city="Nanjing", land="park"))
    bad_topology = _collection(_feature(_square(2.5, 2.5, 3, 3), city="Nanjing", land="park"))
    clipped = _collection(_feature(_square(1, 1, 2, 2), city="Nanjing"))

    assert _codes("overlay", intersection, params={"how": "intersection"}, dependencies={0: source, 1: mask}) == set()
    assert "OVERLAY_TOPOLOGY_INVALID" in _codes(
        "overlay", bad_topology, params={"how": "intersection"}, dependencies={0: source, 1: mask},
    )
    assert _codes("clip_layer", clipped, dependencies={0: source, 1: mask}) == set()
    assert "CLIP_PROPERTY_LOST" in _codes(
        "clip_layer", _collection(_feature(_square(1, 1, 2, 2))), dependencies={0: source, 1: mask},
    )

    mixed_crs = {**mask, "_crs_label": "GCJ02"}
    assert "CRS_MISMATCH" in _codes(
        "overlay", intersection, params={"how": "intersection"}, dependencies={0: source, 1: mixed_crs},
    )


def test_overlay_and_clip_reject_incomplete_but_in_bounds_results() -> None:
    """A tiny valid subset is not the requested full intersection/clip/union."""
    source = _collection(_feature(_square(0, 0, 2, 2), city="Nanjing"))
    mask = _collection(_feature(_square(1, 1, 3, 3), land="park"))
    partial_intersection = _collection(
        _feature(_square(1, 1, 1.5, 1.5), city="Nanjing", land="park")
    )
    full_intersection_only = _collection(
        _feature(_square(1, 1, 2, 2), city="Nanjing", land="park")
    )

    assert "OVERLAY_TOPOLOGY_INVALID" in _codes(
        "overlay",
        partial_intersection,
        params={"how": "intersection", "geometry_a_from": 0, "geometry_b_from": 1},
        dependencies={0: source, 1: mask},
    )
    assert "OVERLAY_TOPOLOGY_INVALID" in _codes(
        "overlay",
        full_intersection_only,
        params={"how": "union", "geometry_a_from": 0, "geometry_b_from": 1},
        dependencies={0: source, 1: mask},
    )
    assert "CLIP_TOPOLOGY_INVALID" in _codes(
        "clip_layer",
        partial_intersection,
        params={"input_ref": 0, "overlay_ref": 1},
        dependencies={0: source, 1: mask},
    )


def test_reclassify_postcondition_excludes_nodata_from_class_counts() -> None:
    valid = {
        "class_counts": {"1": 3, "2": 2},
        "valid_pixel_count": 5,
        "nodata_pixel_count": 1,
        "total_pixel_count": 6,
    }
    invalid = {"class_counts": {"1": 3, "2": 2}, "valid_pixel_count": 6}

    assert _codes("reclassify_raster", valid) == set()
    assert {"RASTER_NODATA_COUNT_MISSING", "RASTER_CLASS_COUNT_MISMATCH"} <= _codes(
        "reclassify_raster", invalid,
    )

    invalid_partition = {
        "class_counts": {"1": 3, "2": 2},
        "valid_pixel_count": 5,
        "nodata_pixel_count": 0,
        "total_pixel_count": 6,
    }
    assert "RASTER_PIXEL_PARTITION_MISMATCH" in _codes(
        "reclassify_raster", invalid_partition,
    )


def test_export_postcondition_rereads_geojson_and_checks_feature_count(tmp_path) -> None:
    exported = _collection(
        _feature({"type": "Point", "coordinates": [118.7, 32.0]}, name="a"),
        _feature({"type": "Point", "coordinates": [118.8, 32.1]}, name="b"),
    )
    path = tmp_path / "result.geojson"
    path.write_text(json.dumps(exported), encoding="utf-8")

    valid = {
        "path": str(path), "feature_count": 2,
        "source_crs": "WGS84", "crs": "EPSG:4326", "coordinate_transform": None,
    }
    invalid = {**valid, "feature_count": 3}
    mismatched_source = _collection(
        *exported["features"],
        _feature({"type": "Point", "coordinates": [118.9, 32.2]}, name="c"),
    )
    assert _codes("export_result", valid, dependencies={0: exported}) == set()
    assert {"EXPORT_COUNT_MISMATCH", "EXPORT_SOURCE_COUNT_MISMATCH"} <= _codes(
        "export_result", invalid, dependencies={0: mismatched_source},
    )


def test_export_postcondition_requires_gcj02_to_wgs84_provenance(tmp_path) -> None:
    source = _collection(
        _feature({"type": "Point", "coordinates": [118.785349, 32.040633]}, name="新街口"),
        crs="GCJ02",
    )
    path = tmp_path / "gcj02-export.geojson"
    path.write_text(json.dumps({**source, "_crs_label": "WGS84"}), encoding="utf-8")
    valid = {
        "path": str(path), "feature_count": 1,
        "source_crs": "GCJ02", "crs": "EPSG:4326",
        "coordinate_transform": "GCJ02_TO_WGS84",
    }

    assert _codes("export_result", valid, dependencies={0: source}) == set()
    assert "EXPORT_CRS_TRANSFORM_MISSING" in _codes(
        "export_result", {**valid, "coordinate_transform": None}, dependencies={0: source},
    )
    assert "EXPORT_SOURCE_CRS_MISMATCH" in _codes(
        "export_result", {**valid, "source_crs": "WGS84"}, dependencies={0: source},
    )


def test_postconditions_resolve_the_declared_reference_instead_of_the_first_catalog_item(tmp_path) -> None:
    """Unrelated catalog entries must not become the semantic source by position."""
    unrelated = _collection(_feature({"type": "Point", "coordinates": [120.0, 30.0]}, name="wrong"))
    point = _collection(_feature({"type": "Point", "coordinates": [118.7845, 32.0429]}, name="origin"))
    buffered = _collection(_feature(_diamond(118.7845, 32.0429, 0.00106, 0.00090), name="origin"))
    assert _codes(
        "buffer",
        buffered,
        params={"radius_m": 100, "geometry_from": 1},
        dependencies={0: unrelated, 1: point},
    ) == set()

    source = _collection(_feature(_square(0, 0, 2, 2), city="Nanjing"))
    mask = _collection(_feature(_square(1, 1, 3, 3), land="park"))
    intersection = _collection(_feature(_square(1, 1, 2, 2), city="Nanjing", land="park"))
    assert _codes(
        "overlay",
        intersection,
        params={"how": "intersection", "geometry_a_from": 1, "geometry_b_from": 2},
        dependencies={0: unrelated, 1: source, 2: mask},
    ) == set()

    path = tmp_path / "selected-source.geojson"
    path.write_text(json.dumps(source), encoding="utf-8")
    wrong_count_source = _collection(
        *source["features"],
        _feature({"type": "Point", "coordinates": [4, 4]}, city="Other"),
    )
    assert _codes(
        "export_result",
        {
            "path": str(path), "feature_count": 1,
            "source_crs": "WGS84", "crs": "EPSG:4326", "coordinate_transform": None,
        },
        params={"data_from": 1},
        dependencies={0: wrong_count_source, 1: source},
    ) == set()


def test_postconditions_fail_closed_when_the_declared_dependency_is_missing() -> None:
    point = _collection(_feature({"type": "Point", "coordinates": [118.7845, 32.0429]}, name="origin"))
    buffered = _collection(_feature(_diamond(118.7845, 32.0429, 0.00106, 0.00090), name="origin"))

    assert "POSTCONDITION_DEPENDENCY_MISSING" in _codes(
        "buffer",
        buffered,
        params={"radius_m": 100, "geometry_from": 7},
        dependencies={0: point},
    )


def test_vector_postconditions_require_explicit_crs_metadata() -> None:
    source = _collection(_feature({"type": "Point", "coordinates": [118.7845, 32.0429]}, name="origin"))
    source.pop("_crs_label")
    buffered = _collection(_feature(_diamond(118.7845, 32.0429, 0.00106, 0.00090), name="origin"))

    assert "CRS_METADATA_MISSING" in _codes(
        "buffer",
        buffered,
        params={"radius_m": 100, "geometry_from": 0},
        dependencies={0: source},
    )


def test_clip_property_validation_does_not_require_values_from_features_outside_the_mask() -> None:
    source = _collection(
        _feature(_square(0, 0, 2, 2), city="kept"),
        _feature(_square(10, 10, 12, 12), city="outside"),
    )
    mask = _collection(_feature(_square(1, 1, 3, 3), zone="mask"))
    clipped = _collection(_feature(_square(1, 1, 2, 2), city="kept"))

    assert _codes(
        "clip_layer",
        clipped,
        params={"input_ref": 0, "overlay_ref": 1},
        dependencies={0: source, 1: mask},
    ) == set()
