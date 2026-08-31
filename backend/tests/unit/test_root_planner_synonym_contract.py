"""Contract tests for the authored live Root-Planner synonym suite."""

from __future__ import annotations

import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[3]
CASE_PATH = REPO_ROOT / "blackbox" / "root_planner_synonym_cases.json"
sys.path.insert(0, str(REPO_ROOT / "blackbox"))

import root_planner_synonym_suite as synonym_suite  # noqa: E402


def _cases() -> list[dict]:
    return json.loads(CASE_PATH.read_text(encoding="utf-8"))


def test_only_exact_coordinate_conversion_case_may_require_guardrail() -> None:
    guardrail_ids = {
        str(case["id"])
        for case in _cases()
        if "guardrail" in (case.get("allowed_planner_sources") or [])
    }

    assert guardrail_ids == {"RP02"}


def _feature_collection(features: list[dict]) -> dict:
    return {"type": "FeatureCollection", "features": features}


def _polygon_feature(properties: dict, ring: list[list[float]]) -> dict:
    return {
        "type": "Feature",
        "properties": properties,
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


def test_rp03_semantics_require_exact_station_records() -> None:
    result = _feature_collection([
        {
            "type": "Feature",
            "properties": {"name": "站点C", "class": "station"},
            "geometry": {"type": "Point", "coordinates": [118.81, 32.04]},
        },
        {
            "type": "Feature",
            "properties": {"name": "站点D", "class": "station"},
            "geometry": {"type": "Point", "coordinates": [118.82, 32.05]},
        },
    ])

    reasons, metrics = synonym_suite._semantic_assertions(
        {"id": "RP03"},
        [{"tool_name": "extract_by_attribute", "result": result}],
    )

    assert reasons == []
    assert metrics["selected_feature_count"] == 2
    assert metrics["selected_names"] == ["站点C", "站点D"]


def test_rp04_semantics_measure_expected_intersection_area() -> None:
    ring = [
        [118.78, 32.06],
        [118.81, 32.06],
        [118.81, 32.09],
        [118.78, 32.09],
        [118.78, 32.06],
    ]
    result = _feature_collection([
        _polygon_feature(
            {"region_1": "南京", "region_2": "lake"},
            ring,
        )
    ])

    reasons, metrics = synonym_suite._semantic_assertions(
        {"id": "RP04"},
        [{"tool_name": "overlay", "result": result}],
    )

    assert reasons == []
    assert metrics["overlay_feature_count"] == 1
    assert 8_000_000 < metrics["overlay_area_m2"] < 11_000_000


def test_rp05_semantics_require_three_class_pixel_counts() -> None:
    result = {
        "type": "raster",
        "value_kind": "reclassified",
        "value_range": [1.0, 3.0],
        "width": 100,
        "height": 10,
        "class_counts": {"1": 250, "2": 250, "3": 500},
    }

    reasons, metrics = synonym_suite._semantic_assertions(
        {"id": "RP05"},
        [{"tool_name": "reclassify_raster", "result": result}],
    )

    assert reasons == []
    assert metrics["raster_class_counts"] == {"1": 250, "2": 250, "3": 500}


def test_rp01_semantics_rejects_out_of_radius_poi_and_map_count_drift() -> None:
    result = {
        "pois": [
            {"name": "near", "location": [118.7845, 32.0429], "distance": 0.0, "crs": "GCJ02"},
            {"name": "far", "location": [118.8060, 32.0429], "distance": 1.0, "crs": "GCJ02"},
        ],
        "center": [118.7845, 32.0429], "radius_m": 500,
        "radius_tolerance_m": 5, "crs": "GCJ02",
    }

    reasons, metrics = synonym_suite._semantic_assertions(
        {"id": "RP01"},
        [{"tool_name": "query_poi", "result": result}],
        answer="找到 2 家咖啡馆。",
        current_map_feature_count=3,
    )

    assert any("radius" in reason for reason in reasons)
    assert any("map" in reason for reason in reasons)
    assert metrics["tool_poi_count"] == 2
    assert metrics["map_feature_count"] == 3


def test_rp01_semantics_uses_bounded_sse_summary_and_rendered_points_when_preview_is_truncated() -> None:
    completion = {
        "tool_name": "query_poi",
        "result": '{"pois": [{"name": "preview"... (truncated)',
        "semantic": {
            "kind": "poi_radius", "poi_count": 2,
            "center": [118.7845, 32.0429], "radius_m": 500,
            "radius_tolerance_m": 5, "crs": "GCJ02", "max_distance_m": 0,
        },
    }

    reasons, metrics = synonym_suite._semantic_assertions(
        {"id": "RP01"}, [completion],
        answer="找到 2 家咖啡馆。", current_map_feature_count=2,
        current_map_points=[[118.7845, 32.0429], [118.8060, 32.0429]],
    )

    assert any("outside radius" in reason for reason in reasons)
    assert metrics["tool_poi_count"] == 2


def test_rp06_semantics_compare_current_and_previous_poi_counts() -> None:
    previous = [{
        "tool_name": "query_poi",
        "result": {
            "pois": [{"name": f"蜜雪冰城{i}", "location": [118.7845, 32.0429]} for i in range(5)],
            "center": [118.7845, 32.0429], "radius_m": 500, "radius_tolerance_m": 5,
        },
    }]
    current = [{
        "tool_name": "query_poi",
        "result": {
            "pois": [{"name": f"茶百道{i}", "location": [118.7845, 32.0429]} for i in range(8)],
            "center": [118.7845, 32.0429], "radius_m": 500, "radius_tolerance_m": 5,
        },
    }]

    reasons, metrics = synonym_suite._semantic_assertions(
        {"id": "RP06"},
        current,
        prior_completions=previous,
        answer="茶百道有 8 个，上一轮蜜雪冰城有 5 个，因此茶百道更多。",
        current_map_feature_count=8,
        prior_map_feature_counts=[5],
    )

    assert reasons == []
    assert metrics["previous_poi_count"] == 5
    assert metrics["current_poi_count"] == 8
    assert metrics["previous_map_feature_count"] == 5
    assert metrics["current_map_feature_count"] == 8


def test_rp07_semantics_measure_buffer_growth_and_export_count() -> None:
    original_ring = [
        [118.76, 32.00],
        [118.78, 32.00],
        [118.78, 32.02],
        [118.76, 32.02],
        [118.76, 32.00],
    ]
    buffered_ring = [
        [118.75894, 31.99910],
        [118.78106, 31.99910],
        [118.78106, 32.02090],
        [118.75894, 32.02090],
        [118.75894, 31.99910],
    ]
    original = _feature_collection([
        _polygon_feature({"name": f"p{i}"}, original_ring) for i in range(4)
    ])
    buffered = _feature_collection([
        _polygon_feature({"name": f"p{i}"}, buffered_ring) for i in range(4)
    ])

    reasons, metrics = synonym_suite._semantic_assertions(
        {"id": "RP07"},
        [
            {"tool_name": "data_io_read", "result": {"data": original}},
            {"tool_name": "buffer", "result": json.dumps(buffered)},
            {"tool_name": "export_result", "result": {
                "feature_count": 4, "source_crs": "GCJ02", "crs": "EPSG:4326",
                "coordinate_transform": "GCJ02_TO_WGS84",
            }},
        ],
    )

    assert reasons == []
    assert metrics["buffer_feature_count"] == 4
    assert 2_000_000 < metrics["buffer_area_growth_m2"] < 5_000_000
    assert metrics["export_feature_count"] == 4
    assert metrics["export_source_crs"] == "GCJ02"
    assert metrics["export_crs"] == "EPSG:4326"
    assert metrics["export_coordinate_transform"] == "GCJ02_TO_WGS84"


def test_rp07_semantics_use_readable_export_when_trace_geometry_is_truncated(tmp_path) -> None:
    """Public trace previews are bounded; exported GeoJSON remains full evidence."""
    original_ring = [
        [118.76, 32.00], [118.78, 32.00], [118.78, 32.02],
        [118.76, 32.02], [118.76, 32.00],
    ]
    buffered_ring = [
        [118.75894, 31.99910], [118.78106, 31.99910], [118.78106, 32.02090],
        [118.75894, 32.02090], [118.75894, 31.99910],
    ]
    original = _feature_collection([
        _polygon_feature({"name": f"p{i}"}, original_ring) for i in range(4)
    ])
    buffered = _feature_collection([
        _polygon_feature({"name": f"p{i}"}, buffered_ring) for i in range(4)
    ])
    export_path = tmp_path / "buffered.geojson"
    export_path.write_text(json.dumps(buffered), encoding="utf-8")

    reasons, metrics = synonym_suite._semantic_assertions(
        {"id": "RP07"},
        [
            {"tool_name": "data_io_read", "result": {"data": original}},
            {"tool_name": "buffer", "result": '{"type":"FeatureCollection"... (truncated)'},
            {"tool_name": "export_result", "result": {
                "path": str(export_path), "feature_count": 4,
                "source_crs": "GCJ02", "crs": "EPSG:4326",
                "coordinate_transform": "GCJ02_TO_WGS84",
            }},
        ],
    )

    assert reasons == []
    assert metrics["buffer_feature_count"] == 4
    assert 2_000_000 < metrics["buffer_area_growth_m2"] < 5_000_000


def test_rp07_semantics_reject_missing_export_crs_transform() -> None:
    reasons, metrics = synonym_suite._semantic_assertions(
        {"id": "RP07"},
        [{"tool_name": "export_result", "result": {
            "feature_count": 4, "source_crs": "GCJ02", "crs": "EPSG:4326",
            "coordinate_transform": None,
        }}],
    )

    assert metrics["export_coordinate_transform"] is None
    assert any("coordinate transform" in reason for reason in reasons)
