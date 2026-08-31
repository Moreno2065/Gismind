"""Regression tests for the runtime contracts used by the real E2E path."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agents.context import _ToolContext
from app.agents.dispatcher import (
    _build_map_from_geojson,
    _dispatch_single,
    assemble_node,
    dispatch_node,
    planner_router_node,
)
from app.agents.build_sub_agent import _route_after_verifier_native
from app.agents.events.current import reset_current_handler, set_current_handler
from app.agents.registry import get_spec
from app.agents.native_tool_mode import native_reference_data
from app.agents.schemas import SubTask, TaskPlan
from app.agents.tool_execution import (
    _build_code_mode_tool_fns,
    _dict_to_gdf,
    _handle_buffer,
    _handle_geo_transform,
    _handle_convex_hull,
    _handle_clip_layer,
    _handle_count_points_in_polygon,
    _handle_dissolve_layer,
    _handle_data_io_read,
    _handle_aspect,
    _handle_extract_by_attribute,
    _handle_export_result,
    _handle_field_calculator,
    _handle_fix_geometries,
    _handle_join_by_nearest,
    _handle_isochrone,
    _handle_load_vector,
    _handle_map_layer_build,
    _handle_merge_layers,
    _handle_overlay,
    _handle_proactive_clarification,
    _handle_query_poi,
    _handle_reproject_layer,
    _handle_reclassify_raster,
    _handle_slope,
    _handle_voronoi,
    _handle_zonal_statistics,
    _read_upload_from_redis,
)
from app.api.upload import _persist_upload
from app.api.upload import validate_file_type
from app.config import settings
from app.tools.data_io import DataIO
from app.tools.map_layer import MapLayerBuilder
from app.tools.raster_analysis import RasterAnalyzer
from app.tools.spatial_analysis import SpatialAnalyzer
from app.utils.redis import make_key


def _ctx(name: str, params: dict | None = None, *, instances: dict | None = None):
    return _ToolContext(
        tool_call_id="diag",
        tool_name=name,
        iteration=0,
        params=params or {},
        results_data={},
        instances=instances or {},
    )


def test_code_mode_tools_accept_direct_artifact_values():
    feature = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [118.78, 32.04]},
        "properties": {"name": "test"},
    }
    fn = _build_code_mode_tool_fns(get_spec("viz"), session_vars={})["map_layer_build"]

    result = fn(geometry_from=[feature])

    assert result["layers"][0]["features"] == [feature]


def test_spatial_tools_accept_geojson_feature_lists_from_dependency_artifacts():
    feature = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [118.78, 32.04]},
        "properties": {"name": "test"},
    }

    gdf = _dict_to_gdf([feature])

    assert gdf is not None
    assert len(gdf) == 1
    assert gdf.geometry.iloc[0].geom_type == "Point"


def test_spatial_tools_accept_poi_lists_from_dependency_artifacts():
    pois = [{
        "name": "测试咖啡店",
        "location": [118.78, 32.04],
        "crs": "GCJ02",
    }]

    gdf = _dict_to_gdf(pois)

    assert gdf is not None
    assert len(gdf) == 1
    assert gdf.attrs["crs_label"] == "GCJ02"


def test_query_poi_normalizes_provider_json_for_downstream_map_layer():
    """A provider JSON string must remain a renderable DAG artifact."""

    class StringPayloadPoi:
        def search_poi_tool(self, *_args, **_kwargs):
            return {
                "status": "success",
                "data": json.dumps({
                    "pois": [{
                        "name": "测试咖啡店",
                        "location": [118.78, 32.04],
                        "crs": "GCJ02",
                    }],
                }),
                "source": "Amap",
            }

    poi_result = _handle_query_poi(_ToolContext(
        tool_call_id="poi-json",
        tool_name="query_poi",
        iteration=0,
        params={"query": "咖啡", "location": [118.78, 32.04]},
        instances={"poi": StringPayloadPoi()},
    ))

    assert poi_result.status == "success"
    assert poi_result.data["pois"][0]["name"] == "测试咖啡店"

    map_result = _handle_map_layer_build(_ToolContext(
        tool_call_id="map-poi-json",
        tool_name="map_layer_build",
        iteration=0,
        params={"data_from": 0},
        results_data={0: poi_result.data},
        instances={"layer_builder": MapLayerBuilder()},
    ))

    assert map_result.status == "success"
    assert map_result.data["layers"][0]["features"][0]["properties"]["name"] == "测试咖啡店"


def test_spatial_tools_unwrap_dependency_artifact_payloads():
    feature_collection = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [118.78, 32.04]},
            "properties": {"name": "test"},
        }],
    }

    gdf = _dict_to_gdf({
        "result_tool_name": "data_io_read",
        "geojson": feature_collection,
        "result": feature_collection,
    })

    assert gdf is not None
    assert len(gdf) == 1


def test_spatial_tools_parse_feature_collection_with_nan_properties():
    feature_collection = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [118.78, 32.04]},
            "properties": {"distance": float("nan")},
        }],
    }

    gdf = _dict_to_gdf(feature_collection)

    assert gdf is not None
    assert len(gdf) == 1


def test_extended_vector_handlers_return_serializable_geojson_data():
    polygons = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[118.77, 32.03], [118.78, 32.03], [118.78, 32.04], [118.77, 32.04], [118.77, 32.03]]],
                },
                "properties": {"region": "A"},
            },
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[118.78, 32.03], [118.79, 32.03], [118.79, 32.04], [118.78, 32.04], [118.78, 32.03]]],
                },
                "properties": {"region": "A"},
            },
        ],
    }
    points = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [118.77, 32.03]},
                "properties": {},
            },
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [118.79, 32.04]},
                "properties": {},
            },
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [118.78, 32.05]},
                "properties": {},
            },
        ],
    }
    analyzer = SpatialAnalyzer()

    dissolve = _handle_dissolve_layer(_ToolContext(
        tool_call_id="dissolve",
        tool_name="dissolve_layer",
        iteration=1,
        params={"geometry_from": 0, "by": "region"},
        results_data={0: polygons},
        instances={"analyzer": analyzer},
    ))
    fixed = _handle_fix_geometries(_ToolContext(
        tool_call_id="fix",
        tool_name="fix_geometries",
        iteration=1,
        params={"geometry_from": 0},
        results_data={0: polygons},
        instances={"analyzer": analyzer},
    ))
    reprojected = _handle_reproject_layer(_ToolContext(
        tool_call_id="reproject",
        tool_name="reproject_layer",
        iteration=1,
        params={"geometry_from": 0, "target_crs": "EPSG:3857"},
        results_data={0: polygons},
        instances={"analyzer": analyzer},
    ))
    hull = _handle_convex_hull(_ToolContext(
        tool_call_id="hull",
        tool_name="convex_hull",
        iteration=1,
        params={"geometry_from": 0},
        results_data={0: points},
        instances={"analyzer": analyzer},
    ))

    for result in (dissolve, fixed, reprojected, hull):
        assert result.status == "success"
        assert result.data["type"] == "FeatureCollection"
        assert result.data["features"]


def test_successful_native_tool_result_cannot_be_sent_back_for_refinement():
    route = _route_after_verifier_native({
        "verifier_output": {"approved": False, "reason": "LLM disagreed"},
        "tool_results": [{"tool_name": "dissolve_layer", "status": "success", "data": {"features": []}}],
    })

    assert route == "finalize"


def test_documented_extended_handlers_use_real_spatial_analyzer_contracts():
    polygons = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[118.76, 32.02], [118.80, 32.02], [118.80, 32.06], [118.76, 32.06], [118.76, 32.02]]],
            },
            "properties": {"name": "street-a", "region": "A"},
        }],
    }
    second_polygon = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[118.80, 32.02], [118.82, 32.02], [118.82, 32.04], [118.80, 32.04], [118.80, 32.02]]],
            },
            "properties": {"name": "street-b", "region": "B"},
        }],
    }
    points = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [118.77, 32.03]},
                "properties": {"name": "p1", "class": "poi"},
            },
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [118.79, 32.05]},
                "properties": {"name": "s1", "class": "station"},
            },
        ],
    }
    stations = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [118.791, 32.051]},
            "properties": {"station_name": "bus-1"},
        }],
    }
    analyzer = SpatialAnalyzer()

    def call(handler, name, params, refs):
        return handler(_ToolContext(
            tool_call_id=name,
            tool_name=name,
            iteration=1,
            params=params,
            results_data=refs,
            instances={"analyzer": analyzer},
        ))

    clipped = call(_handle_clip_layer, "clip_layer", {"input_ref": 0, "overlay_ref": 1}, {0: points, 1: polygons})
    merged = call(_handle_merge_layers, "merge_layers", {"layers_from": [polygons, second_polygon]}, {})
    counted = call(_handle_count_points_in_polygon, "count_points_in_polygon", {"polygons_from": 0, "points_from": 1}, {0: polygons, 1: points})
    nearest = call(_handle_join_by_nearest, "join_by_nearest", {"input_ref": 0, "other_ref": 1}, {0: points, 1: stations})
    extracted = call(_handle_extract_by_attribute, "extract_by_attribute", {"geometry_from": 0, "expression": "class == 'station'"}, {0: points})
    calculated = call(_handle_field_calculator, "field_calculator", {"geometry_from": 0, "field_name": "area_km2", "expression": "$area/1e6"}, {0: polygons})

    for result in (clipped, merged, counted, nearest, extracted, calculated):
        assert result.status == "success", result.message
        assert result.data["type"] == "FeatureCollection"
        assert result.data["features"]
    assert len(merged.data["features"]) == 2
    assert counted.data["features"][0]["properties"]["count"] == 2
    assert len(extracted.data["features"]) == 1
    assert extracted.data["features"][0]["properties"]["class"] == "station"
    area_km2 = calculated.data["features"][0]["properties"]["area_km2"]
    assert 1 < area_km2 < 100


def test_merge_layers_uses_all_dependency_layers_when_model_selects_one_index():
    layer_a = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [118.77, 32.03]},
            "properties": {"name": "a"},
        }],
    }
    layer_b = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [118.79, 32.05]},
            "properties": {"name": "b"},
        }],
    }
    result = _handle_merge_layers(_ToolContext(
        tool_call_id="merge-deps",
        tool_name="merge_layers",
        iteration=1,
        params={"layers_from": 0},
        results_data={0: layer_a, 1: layer_b},
        instances={"analyzer": SpatialAnalyzer()},
    ))

    assert result.status == "success"
    assert [f["properties"]["name"] for f in result.data["features"]] == ["a", "b"]


def test_two_input_handler_repairs_duplicate_model_references_from_dag_order():
    polygons = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[118.7, 32.0], [118.9, 32.0], [118.9, 32.1], [118.7, 32.1], [118.7, 32.0]]],
            },
            "properties": {},
        }],
    }
    points = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [118.8, 32.05]},
            "properties": {},
        }],
    }

    class CapturingAnalyzer:
        def __init__(self):
            self.types = None

        def count_points_in_polygon(self, pts, polys):
            self.types = (pts.geometry.iloc[0].geom_type, polys.geometry.iloc[0].geom_type)
            return {"status": "success", "data": polys}

    analyzer = CapturingAnalyzer()
    result = _handle_count_points_in_polygon(_ToolContext(
        tool_call_id="count-deps",
        tool_name="count_points_in_polygon",
        iteration=1,
        params={"polygons_from": 0, "points_from": 0},
        results_data={0: polygons, 1: points},
        instances={"analyzer": analyzer},
    ))

    assert result.status == "success"
    assert analyzer.types == ("Point", "Polygon")


def test_native_reference_catalog_contains_each_dependency_once_in_dag_order():
    first = {"status": "success", "location": [118.78, 32.04]}
    second = {"status": "success", "location": [118.80, 32.06]}
    state = {
        "session_vars": {
            "upload_file_ids": ["file_x"],
            "upload_0": {"file_id": "file_x"},
            "dep_t1": {"locations": [first], "result": first, "result_tool_name": "geo_code"},
            "locations": [first],
            "result": first,
            "dep_t2": {"locations": [second], "result": second, "result_tool_name": "geo_code"},
        },
        "tool_results": [],
    }

    assert native_reference_data(state) == {0: first, 1: second}


def test_convex_hull_aggregates_all_geocoded_dependency_points():
    refs = {
        0: {"status": "success", "location": [118.78, 32.04], "formatted_address": "A"},
        1: {"status": "success", "location": [118.80, 32.03], "formatted_address": "B"},
        2: {"status": "success", "location": [118.79, 32.06], "formatted_address": "C"},
        3: {"status": "success", "location": [118.76, 32.05], "formatted_address": "D"},
    }
    result = _handle_convex_hull(_ToolContext(
        tool_call_id="hull-many",
        tool_name="convex_hull",
        iteration=1,
        params={"geometry_from": 0},
        results_data=refs,
        instances={"analyzer": SpatialAnalyzer()},
    ))

    assert result.status == "success"
    assert len(result.data["features"]) == 1
    assert result.data["features"][0]["geometry"]["type"] == "Polygon"


def test_voronoi_handler_unwraps_real_analyzer_success_envelope():
    refs = {
        0: {"location": [118.85, 32.05]},
        1: {"location": [118.79, 32.02]},
        2: {"location": [118.785, 32.041]},
        3: {"location": [118.802, 32.072]},
    }
    result = _handle_voronoi(_ToolContext(
        tool_call_id="voronoi-real",
        tool_name="voronoi",
        iteration=1,
        params={"points_from": 0},
        results_data=refs,
        instances={"analyzer": SpatialAnalyzer()},
    ))

    assert result.status == "success"
    assert result.data["type"] == "FeatureCollection"
    assert result.data["features"]


def test_isochrone_uses_local_distance_envelope_when_route_sampling_is_empty():
    class EmptyRouteAnalyzer(SpatialAnalyzer):
        def isochrone(self, origin, mode, time_min):
            return {"status": "empty", "message": "provider unavailable"}

    result = _handle_isochrone(_ToolContext(
        tool_call_id="iso-local",
        tool_name="isochrone",
        iteration=1,
        params={"location_from": 0, "mode": "walking", "time_min": 15},
        results_data={0: {"location": [121.475233, 31.228818]}},
        instances={"analyzer": EmptyRouteAnalyzer()},
    ))

    assert result.status == "success"
    assert result.data["type"] == "FeatureCollection"
    assert result.data["features"]
    assert result.data["_approximate"] is True


def test_isochrone_serializes_success_geometry_envelope():
    from shapely.geometry import Point

    class SuccessfulRouteAnalyzer:
        def isochrone(self, origin, mode, time_min):
            return {"status": "success", "data": {"geometry": Point(origin).buffer(0.01)}}

    result = _handle_isochrone(_ToolContext(
        tool_call_id="iso-success",
        tool_name="isochrone",
        iteration=1,
        params={"location_from": 0, "mode": "walking", "time_min": 15},
        results_data={0: {"location": [121.475233, 31.228818]}},
        instances={"analyzer": SuccessfulRouteAnalyzer()},
    ))

    assert result.status == "success"
    assert result.data["type"] == "FeatureCollection"
    assert result.data["features"][0]["geometry"]["type"] == "Polygon"


def test_projected_geojson_can_be_buffered_without_inventing_invalid_epsg():
    import geopandas as gpd
    from shapely.geometry import Point
    from app.agents.tool_execution import _gdf_to_dict

    projected = gpd.GeoDataFrame(
        {"name": ["p"]},
        geometry=[Point(118.78, 32.04)],
        crs="EPSG:4326",
    ).to_crs("EPSG:4548")
    serialized = _gdf_to_dict(projected)
    assert serialized["_crs_label"] == "EPSG:4548"
    result = _handle_buffer(_ToolContext(
        tool_call_id="buffer-projected",
        tool_name="buffer",
        iteration=1,
        params={"geometry_from": 0, "radius_m": 500},
        results_data={0: serialized},
        instances={"analyzer": SpatialAnalyzer()},
    ))

    assert result.status == "success"
    assert result.data["features"]


def test_map_layer_build_renders_all_nonempty_dependency_geometries():
    point_layer = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [118.78, 32.04]},
            "properties": {"name": "a"},
        }],
    }
    polygon_layer = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [[[118.7, 32.0], [118.8, 32.0], [118.8, 32.1], [118.7, 32.0]]]},
            "properties": {"name": "b"},
        }],
    }
    result = _handle_map_layer_build(_ToolContext(
        tool_call_id="map-many",
        tool_name="map_layer_build",
        iteration=1,
        params={"geometry_from": 0},
        results_data={0: point_layer, 1: polygon_layer},
        instances={"layer_builder": MapLayerBuilder()},
    ))

    assert result.status == "success"
    assert len(result.data["layers"]) == 2


def test_map_layer_build_passes_through_raster_dependency_layer():
    """Raster results are already frontend-renderable ImageOverlay layers."""
    raster_layer = {
        "type": "raster",
        "png_b64": "iVBORw0KGgo=",
        "bbox": [118.7, 31.95, 118.9, 32.15],
        "width": 10,
        "height": 10,
        "value_kind": "reclassified",
        "opacity": 0.7,
    }

    result = _handle_map_layer_build(_ToolContext(
        tool_call_id="map-raster",
        tool_name="map_layer_build",
        iteration=1,
        params={"data_from": 0},
        results_data={0: raster_layer},
        instances={"layer_builder": MapLayerBuilder()},
    ))

    assert result.status == "success"
    assert result.data == {"layers": [raster_layer]}


def test_export_result_materializes_relative_model_filename_inside_workspace(tmp_path, monkeypatch):
    geojson = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [118.78, 32.04]},
            "properties": {"name": "p"},
        }],
    }
    monkeypatch.setattr(settings, "APP_WORKSPACE_DIR", str(tmp_path))
    result = _handle_export_result(_ToolContext(
        tool_call_id="export-safe",
        tool_name="export_result",
        iteration=1,
        params={"data_from": 0, "format": "geojson", "output_path": "result.geojson"},
        results_data={0: geojson},
        instances={"data_io": DataIO()},
    ))

    assert result.status == "success"
    output_path = Path(result.data["path"])
    assert output_path.is_relative_to(tmp_path.resolve())
    assert output_path.exists()


def _write_test_dem(path: Path) -> None:
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=10,
        height=10,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(118.70, 32.15, 0.02, 0.02),
        nodata=-9999,
    ) as dataset:
        dataset.write((np.arange(100, dtype="float32").reshape(10, 10) + 10), 1)


def test_raster_handlers_unwrap_uploaded_and_derived_path_artifacts(tmp_path, monkeypatch):
    dem_path = tmp_path / "dem.tif"
    _write_test_dem(dem_path)
    uploaded = {
        "status": "ok",
        "data": {"raster_path": str(dem_path), "metadata": {"crs": "EPSG:4326"}},
    }
    analyzer = RasterAnalyzer()

    slope = _handle_slope(_ToolContext(
        tool_call_id="slope-wrapper",
        tool_name="slope",
        iteration=1,
        params={"dem_path": uploaded},
        instances={"raster_analyzer": analyzer},
    ))
    assert slope.status == "success"
    assert Path(slope.data["dst_path"]).exists()

    # Model-generated relative names are not user-facing export requests.  A
    # stale file of that name must not become a reused/colliding output.
    monkeypatch.chdir(tmp_path)
    reclassified = _handle_reclassify_raster(_ToolContext(
        tool_call_id="reclass-wrapper",
        tool_name="reclassify_raster",
        iteration=1,
        params={
            "src_path": slope.data,
            "bins": [15, 30],
            "values": [1, 2, 3],
            "dst_path": "slope_reclassified.tif",
        },
        instances={"raster_analyzer": analyzer},
    ))
    assert reclassified.status == "success"
    output_path = Path(reclassified.data["dst_path"])
    assert output_path.exists()
    assert output_path != tmp_path / "slope_reclassified.tif"
    import rasterio
    with rasterio.open(output_path) as output:
        assert output.read(1).max() <= 3


def test_aspect_handler_matches_analyzer_signature():
    captured = {}

    class Analyzer:
        def aspect(self, dem_path, dst_path=None):
            captured.update(dem_path=dem_path, dst_path=dst_path)
            return {"status": "success", "data": {"type": "raster", "dst_path": "aspect.tif"}}

    result = _handle_aspect(_ToolContext(
        tool_call_id="aspect-signature",
        tool_name="aspect",
        iteration=1,
        params={"dem_path": "dem.tif", "degree": True},
        instances={"raster_analyzer": Analyzer()},
    ))

    assert result.status == "success"
    assert captured == {"dem_path": "dem.tif", "dst_path": None}


def test_zonal_statistics_accepts_uploaded_raster_and_in_memory_vector(tmp_path, monkeypatch):
    dem_path = tmp_path / "dem.tif"
    _write_test_dem(dem_path)
    monkeypatch.setattr(settings, "APP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    zones = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[118.70, 31.95], [118.90, 31.95], [118.90, 32.15], [118.70, 32.15], [118.70, 31.95]]],
            },
            "properties": {"name": "zone"},
        }],
    }
    result = _handle_zonal_statistics(_ToolContext(
        tool_call_id="zonal-wrapper",
        tool_name="zonal_statistics",
        iteration=1,
        params={
            "raster_path": {"data": {"raster_path": str(dem_path)}},
            "vector_path": {"status": "ok", "data": zones},
            "stats": ["mean", "min", "max"],
        },
        instances={"raster_analyzer": RasterAnalyzer()},
    ))

    assert result.status == "success"
    assert result.data["type"] == "FeatureCollection"
    assert result.data["features"]


def test_documented_raster_prompts_remain_valid_fallback_steps():
    cases = [
        ("加载 DEM，计算坡度、坡向、山体阴影，并叠加显示", ["file_dem"]),
        ("用行政区分区统计 DEM 的平均海拔", ["file_dem", "file_admin"]),
        ("把坡度分成 0-15° / 15-30° / >30° 三档", ["file_dem"]),
    ]
    for prompt, uploads in cases:
        result = planner_router_node({
            "user_input": prompt,
            "upload_file_ids": uploads,
            "messages": [],
        }, llm=_UnavailablePlannerLLM())
        assert result["planner_source"] == "fallback"
        raster_tasks = [
            task for task in result["task_plan"]["tasks"]
            if task["tool_name"] in {"slope", "aspect", "hillshade", "zonal_statistics", "reclassify_raster"}
        ]
        assert raster_tasks
        assert {task["agent_role"] for task in raster_tasks} == {"geometer"}


def test_documented_attribute_prompts_remain_valid_fallback_steps():
    cases = [
        ("添加一个面积字段 area_km2", ["file_parcels"], "field_calculator"),
        ("筛选出 class 是 station 的所有要素", ["file_points"], "extract_by_attribute"),
    ]
    for prompt, uploads, tool_name in cases:
        result = planner_router_node({
            "user_input": prompt,
            "upload_file_ids": uploads,
            "messages": [],
        }, llm=_UnavailablePlannerLLM())
        assert result["planner_source"] == "fallback"
        task = next(task for task in result["task_plan"]["tasks"] if task["tool_name"] == tool_name)
        assert task["agent_role"] == "geometer"
        assert tool_name in get_spec("geometer").tool_names


def test_assemble_surfaces_raster_layer_as_map():
    raster_layer = {
        "type": "raster",
        "png_b64": "iVBORw0KGgo=",
        "bbox": [118.7, 31.95, 118.9, 32.15],
        "width": 10,
        "height": 10,
        "value_kind": "slope_degrees",
    }
    result = assemble_node({
        "user_input": "计算坡度",
        "dispatcher_events": [],
        "sub_results": {
            "slope": [{
                "agent_role": "geometer",
                "status": "success",
                "artifacts": {"result": raster_layer, "result_tool_name": "slope"},
                "iteration_used": 1,
            }],
        },
    })

    assert result["final_output"]["status"] == "success"
    assert result["final_output"]["layers"] == [raster_layer]


class _StaticPlannerLLM:
    def __init__(self, payload: dict):
        self.payload = payload

    def invoke(self, *_args, **_kwargs):
        return AIMessage(content=json.dumps(self.payload, ensure_ascii=False))


class _UnavailablePlannerLLM:
    """Force the explicit compatibility fallback without faking a valid plan."""

    def invoke(self, *_args, **_kwargs):
        raise RuntimeError("planner unavailable")


def test_invalid_root_tool_for_point_count_uses_documented_fallback():
    wrong_plan = {
        "task_plan": {
            "instructions": [{"id": "i1", "text": "统计每个街道里有多少个 POI"}],
            "tasks": [{
                "id": "t1",
                "agent_role": "geometer",
                "tool_name": "point_counter",
                "goal": "统计点数量",
                "depends_on": [],
                "instruction_id": "i1",
            }],
        }
    }
    result = planner_router_node({
        "user_input": "统计每个街道里有多少个 POI",
        "upload_file_ids": ["file_streets", "file_pois"],
        "messages": [],
    }, llm=_StaticPlannerLLM(wrong_plan))

    tools = [task["tool_name"] for task in result["task_plan"]["tasks"]]
    assert result["planner_source"] == "fallback"
    assert tools == ["data_io_read", "data_io_read", "count_points_in_polygon"]


def test_root_planner_falls_back_when_documented_voronoi_prompt_gets_empty_plan():
    result = planner_router_node({
        "user_input": "把这 4 个 POI 做泰森多边形：中山陵、夫子庙、新街口、玄武湖",
        "upload_file_ids": [],
        "messages": [],
    }, llm=_StaticPlannerLLM({"task_plan": {"instructions": [], "tasks": []}}))

    tools = [task["tool_name"] for task in result["task_plan"]["tasks"]]
    assert result["planner_source"] == "fallback"
    assert tools.count("geo_code") == 4
    assert tools[-1] == "voronoi"


@pytest.mark.parametrize(("prompt", "uploads", "expected_tools"), [
    ("南京新街口的经纬度是多少", [], ["geo_code"]),
    ("南京新街口 500 米内有多少蜜雪冰城", [], ["geo_code", "query_poi"]),
    ("找出南京夫子庙 1km 内的所有地铁站，然后把 1km 缓冲区画出来", [], ["geo_code", "query_poi", "buffer", "map_layer_build"]),
    ("南京新街口 500m 蜜雪冰城覆盖区与夫子庙 500m 蜜雪冰城覆盖区，求交集并标出来", [], ["geo_code", "geo_code", "query_poi", "query_poi", "buffer", "buffer", "overlay", "map_layer_build"]),
    ("画一个上海人民广场步行 15 分钟可达范围", [], ["geo_code", "isochrone"]),
    ("把这个文件按字段 class 分级设色显示", ["file_points"], ["data_io_read", "map_layer_build"]),
    ("再加一层，把所有 class 是 poi 的点放大 2 倍", [], ["map_layer_build"]),
    ("用南京市行政区划裁剪这个 POI 图层", ["file_pois", "file_admin"], ["data_io_read", "data_io_read", "clip_layer"]),
    ("按区域字段融合相邻地块", ["file_parcels"], ["data_io_read", "dissolve_layer"]),
    ("把玄武湖图层和紫金山图层合并成一个", ["file_lake", "file_mountain"], ["data_io_read", "data_io_read", "merge_layers"]),
    ("给每个 POI 关联最近的公交站点", ["file_pois", "file_stations"], ["data_io_read", "data_io_read", "join_by_nearest"]),
    ("加载 DEM，计算坡度、坡向、山体阴影，并叠加显示", ["file_dem"], ["data_io_read", "slope", "aspect", "hillshade"]),
    ("用行政区分区统计 DEM 的平均海拔", ["file_dem", "file_admin"], ["data_io_read", "data_io_read", "zonal_statistics"]),
    ("添加一个面积字段 area_km2", ["file_parcels"], ["data_io_read", "field_calculator"]),
    ("把这个图层从 GCJ02 转为 WGS84", ["file_points"], ["data_io_read", "reproject_layer"]),
    ("筛选出 class 是 station 的所有要素", ["file_points"], ["data_io_read", "extract_by_attribute"]),
    ("计算这组 POI 的外包凸包：中山陵、夫子庙、新街口、玄武湖", [], ["geo_code", "geo_code", "geo_code", "geo_code", "convex_hull"]),
    ("把坡度分成 0-15° / 15-30° / >30° 三档", ["file_dem"], ["data_io_read", "slope", "reclassify_raster"]),
    ("修复上传图层几何，重投影到 EPSG:4548，做 500 米缓冲，融合后导出 GeoJSON", ["file_parcels"], ["data_io_read", "fix_geometries", "reproject_layer", "buffer", "dissolve_layer", "export_result"]),
])
def test_documented_prompt_has_deterministic_nonempty_fallback_dag(prompt, uploads, expected_tools):
    result = planner_router_node({
        "user_input": prompt,
        "upload_file_ids": uploads,
        "messages": [],
    }, llm=_UnavailablePlannerLLM())

    tasks = result["task_plan"]["tasks"]
    assert result["planner_source"] == "fallback"
    assert [task["tool_name"] for task in tasks] == expected_tools
    TaskPlan.model_validate(result["task_plan"])
    for task in tasks:
        assert task["tool_name"] in get_spec(task["agent_role"]).tool_names


@pytest.mark.asyncio
async def test_dispatch_injects_dependency_artifacts_and_root_session_vars(monkeypatch):
    captured: dict = {}

    def fake_run_sub_agent(**kwargs):
        captured.update(kwargs)
        return {
            "agent_role": kwargs["agent_role"],
            "tool_results": [],
            "final_output": {"summary": "ok"},
            "iteration": 1,
        }

    monkeypatch.setattr("app.agents.build_sub_agent.run_sub_agent", fake_run_sub_agent)
    task = SubTask(
        id="viz1",
        agent_role="viz",
        goal="render",
        depends_on=["poi1"],
        tool_name="map_layer_build",
    )
    results = {
        "poi1": [{
            "status": "success",
            "agent_role": "poi",
            "artifacts": {"pois": [{"name": "x", "location": [118.78, 32.04]}]},
        }]
    }

    await _dispatch_single(
        {"session_vars": {"upload_file_ids": ["file_1"]}, "session_id": "s"},
        task,
        results,
        {},
        [],
    )

    assert captured["session_vars"]["upload_file_ids"] == ["file_1"]
    assert captured["session_vars"]["pois"][0]["name"] == "x"
    assert captured["session_vars"]["dep_poi1"]["pois"][0]["name"] == "x"
    assert captured["required_tool_name"] == "map_layer_build"


def test_dispatch_executes_same_role_workflow_dag_and_carries_each_edge(monkeypatch):
    """A same-role chain is six deterministic Root steps, not one Judge-driven loop."""
    calls: list[tuple[str, dict]] = []
    geojson = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [118.78, 32.04]},
            "properties": {},
        }],
    }

    def fake_run_sub_agent(**kwargs):
        tool_name = kwargs["required_tool_name"]
        calls.append((tool_name, dict(kwargs["session_vars"])))
        data = (
            {"path": "workspace/result.geojson", "format": "GeoJSON"}
            if tool_name == "export_result"
            else geojson
        )
        return {
            "agent_role": kwargs["agent_role"],
            "tool_results": [{
                "tool_name": tool_name,
                "status": "success",
                "data": data,
            }],
            "final_output": {
                "status": "success",
                "results": [{
                    "tool_name": tool_name,
                    "status": "success",
                    "data": data,
                }],
            },
            "iteration": 1,
        }

    monkeypatch.setattr("app.agents.build_sub_agent.run_sub_agent", fake_run_sub_agent)
    tool_names = [
        "data_io_read",
        "fix_geometries",
        "reproject_layer",
        "buffer",
        "dissolve_layer",
        "export_result",
    ]
    tasks = []
    for index, tool_name in enumerate(tool_names):
        tasks.append({
            "id": f"t{index}",
            "agent_role": "geometer",
            "tool_name": tool_name,
            "goal": tool_name,
            "depends_on": [] if index == 0 else [f"t{index - 1}"],
            "instruction_id": f"i{max(index, 1)}",
        })

    result = dispatch_node({
        "task_plan": {
            "instructions": [
                {"id": f"i{index}", "text": tool_name}
                for index, tool_name in enumerate(tool_names[1:], start=1)
            ],
            "tasks": tasks,
        },
        "session_vars": {
            "upload_file_ids": ["file_1"],
            "upload_0": {"file_id": "file_1"},
        },
        "sub_results": {},
        "dispatched": {},
        "dispatcher_events": [],
        "root_iteration": 0,
        "session_id": "s",
        "run_id": "",
    })

    assert [tool_name for tool_name, _ in calls] == tool_names
    for index, (_tool_name, session_vars) in enumerate(calls[1:], start=1):
        assert session_vars["result"] == geojson
        assert session_vars[f"dep_t{index - 1}"]["result"] == geojson
    assert result["sub_results"]["t5"][-1]["artifacts"]["result"] == {
        "path": "workspace/result.geojson",
        "format": "GeoJSON",
    }


def test_dispatcher_events_use_single_dict_handler():
    class FakeLLM:
        def bind(self, **kwargs):
            return self

        def invoke(self, *_args, **_kwargs):
            return SimpleNamespace(content=json.dumps({
                "task_plan": {
                    "instructions": [{"id": "i1", "text": "locate Nanjing"}],
                    "tasks": [{
                        "id": "t1",
                        "goal": "locate Nanjing",
                        "agent_role": "geo",
                        "tool_name": "geo_code",
                        "depends_on": [],
                        "instruction_id": "i1",
                    }]
                }
            }))

    seen: list[dict] = []
    token = set_current_handler(seen.append)
    try:
        planner_router_node({"user_input": "Nanjing", "session_id": "s"}, llm=FakeLLM())
    finally:
        reset_current_handler(token)

    assert [event["event"] for event in seen] == ["run.thought", "run.plan"]
    assert seen[1]["upload_file_ids"] == []
    assert seen[1]["tasks"] == [{
        "id": "t1",
        "agent_role": "geo",
        "tool_name": "geo_code",
        "goal": "locate Nanjing",
        "depends_on": [],
        "instruction_id": "i1",
        "tool_args": {},
        "status": "pending",
    }]


def test_root_planner_retries_invalid_instruction_coverage():
    invalid = {
        "task_plan": {
            "instructions": [
                {"id": "i1", "text": "查询地铁站"},
                {"id": "i2", "text": "绘制缓冲区"},
            ],
            "tasks": [{
                "id": "t1",
                "goal": "查询地铁站",
                "agent_role": "poi",
                "tool_name": "query_poi",
                "depends_on": [],
                "instruction_id": "i1",
            }],
        },
    }
    valid = {
        "task_plan": {
            "instructions": invalid["task_plan"]["instructions"],
            "tasks": [
                invalid["task_plan"]["tasks"][0],
                {
                    "id": "t2",
                    "goal": "绘制缓冲区",
                    "agent_role": "geometer",
                    "tool_name": "buffer",
                    "depends_on": [],
                    "instruction_id": "i2",
                },
            ],
        },
    }

    class RepairingLLM:
        def __init__(self):
            self.responses = [invalid, valid]
            self.calls = 0

        def invoke(self, *_args, **_kwargs):
            response = self.responses[self.calls]
            self.calls += 1
            return SimpleNamespace(content=json.dumps(response, ensure_ascii=False))

    llm = RepairingLLM()
    result = planner_router_node({"user_input": "查询地铁站，然后绘制缓冲区"}, llm=llm)

    assert llm.calls == 2
    assert [task["instruction_id"] for task in result["task_plan"]["tasks"]] == ["i1", "i2"]


def test_root_planner_retries_tool_assigned_to_wrong_role():
    class RepairingLLM:
        def __init__(self):
            self.calls = 0

        def invoke(self, *_args, **_kwargs):
            self.calls += 1
            tool_name = "buffer" if self.calls == 1 else "geo_transform"
            return SimpleNamespace(content=json.dumps({
                "task_plan": {
                    "instructions": [{"id": "i1", "text": "检查坐标"}],
                    "tasks": [{
                        "id": "t1",
                        "goal": "检查坐标",
                        "agent_role": "geo",
                        "tool_name": tool_name,
                        "depends_on": [],
                        "instruction_id": "i1",
                    }],
                },
            }))

    llm = RepairingLLM()
    result = planner_router_node({"user_input": "检查坐标"}, llm=llm)

    assert llm.calls == 2
    assert result["task_plan"]["tasks"][0]["tool_name"] == "geo_transform"


def test_documented_prompt_uses_fallback_after_malformed_root_llm():
    """A compatibility catalog is an explicit fallback, never a planner bypass."""
    class MalformedLLM:
        def __init__(self):
            self.calls = 0

        def invoke(self, *_args, **_kwargs):
            self.calls += 1
            return SimpleNamespace(content="not-json")

    llm = MalformedLLM()
    result = planner_router_node(
        {"user_input": "南京新街口的经纬度是多少？", "session_id": "s"},
        llm=llm,
    )

    assert llm.calls == 2
    assert result["planner_source"] == "fallback"
    assert result.get("should_stop") is not True
    assert [task["tool_name"] for task in result["task_plan"]["tasks"]] == ["geo_code"]


def test_uploaded_region_count_prompt_uses_data_then_poi_after_unavailable_root_llm():
    class UnavailableLLM:
        def __init__(self):
            self.calls = 0

        def invoke(self, *_args, **_kwargs):
            self.calls += 1
            raise RuntimeError("planner unavailable")

    llm = UnavailableLLM()

    result = planner_router_node(
        {
            "user_input": "这个区有多少蜜雪冰城",
            "upload_file_ids": ["file_region"],
            "session_id": "s",
        },
        llm=llm,
    )

    assert llm.calls == 1
    assert result["planner_source"] == "fallback"
    assert [task["tool_name"] for task in result["task_plan"]["tasks"]] == [
        "data_io_read", "query_poi",
    ]


def test_followup_brand_query_reuses_previous_location_after_root_llm_failure():
    class UnavailableLLM:
        def __init__(self):
            self.calls = 0

        def invoke(self, *_args, **_kwargs):
            self.calls += 1
            raise RuntimeError("planner unavailable")

    llm = UnavailableLLM()

    result = planner_router_node(
        {
            "user_input": "再查下茶百道，对比密度",
            "messages": [HumanMessage(content="南京新街口500米内蜜雪冰城")],
            "session_id": "s",
        },
        llm=llm,
    )

    assert llm.calls == 1
    assert result["planner_source"] == "fallback"
    tasks = result["task_plan"]["tasks"]
    assert [task["tool_name"] for task in tasks] == ["geo_code", "query_poi"]
    assert "南京新街口" in tasks[0]["goal"]
    assert "茶百道" in tasks[1]["goal"]


def test_prior_upload_ids_are_carried_forward_for_same_session():
    from app.agents.tool_execution import _carry_forward_upload_file_ids

    class FakeApp:
        def get_state(self, _config):
            return SimpleNamespace(values={"upload_file_ids": ["file_previous"]})

    assert _carry_forward_upload_file_ids(FakeApp(), {"configurable": {"thread_id": "s"}}, []) == [
        "file_previous"
    ]
    assert _carry_forward_upload_file_ids(FakeApp(), {"configurable": {"thread_id": "s"}}, ["file_new"]) == [
        "file_new"
    ]


def test_prior_checkpoint_turn_supplies_semantic_history_when_redis_history_is_empty():
    from app.agents.tool_execution import _checkpoint_turn_history

    class FakeApp:
        def get_state(self, _config):
            return SimpleNamespace(values={
                "user_input": "南京新街口500米内蜜雪冰城",
                "final_output": {"summary": "找到 3 个 POI"},
            })

    messages = _checkpoint_turn_history(FakeApp(), {"configurable": {"thread_id": "s"}})

    assert [message.content for message in messages] == [
        "南京新街口500米内蜜雪冰城",
        "找到 3 个 POI",
    ]


def test_query_poi_uses_representative_point_of_uploaded_polygon_reference():
    polygon = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[118.7, 32.0], [118.9, 32.0], [118.9, 32.1], [118.7, 32.1], [118.7, 32.0]]],
            },
            "properties": {},
        }],
    }

    class CapturingPOI:
        def __init__(self):
            self.location = None

        def search_poi_tool(self, query, location, radius, **_kwargs):
            self.location = location
            return {"status": "success", "data": {"pois": []}, "source": "test"}

    poi = CapturingPOI()
    result = _handle_query_poi(_ToolContext(
        tool_call_id="poi-region",
        tool_name="query_poi",
        iteration=1,
        params={"query": "蜜雪冰城", "location_from": 0, "radius": 5000},
        results_data={0: polygon},
        instances={"poi": poi, "geo_coder": MagicMock()},
    ))

    assert result.status == "success"
    assert poi.location == pytest.approx((118.8, 32.05), abs=0.001)


def test_query_poi_handler_preserves_provider_unavailable_as_an_error():
    class UnavailablePOI:
        def search_poi_tool(self, query, location, radius, **_kwargs):
            return {
                "status": "error",
                "error_code": "POI_SOURCE_UNAVAILABLE",
                "data": {
                    "pois": [],
                    "provider_status": {"Amap": "unavailable", "OSM": "unavailable"},
                },
                "source": None,
                "message": "POI 数据源暂时不可用",
            }

    result = _handle_query_poi(_ToolContext(
        tool_call_id="poi-unavailable",
        tool_name="query_poi",
        iteration=0,
        params={"query": "茶百道", "location": [118.785349, 32.040633], "radius": 500},
        results_data={},
        instances={"poi": UnavailablePOI(), "geo_coder": MagicMock()},
    ))

    assert result.status == "error"
    assert result.error_code == "POI_SOURCE_UNAVAILABLE"
    assert result.data["provider_status"] == {
        "Amap": "unavailable",
        "OSM": "unavailable",
    }


def test_root_planner_receives_loaded_conversation_history():
    class CapturingLLM:
        def __init__(self):
            self.messages = []

        def invoke(self, messages, *_args, **_kwargs):
            self.messages = messages
            return SimpleNamespace(content='{"task_plan":{"tasks":[]}}')

    llm = CapturingLLM()
    planner_router_node(
        {
            "user_input": "继续画出来",
            "messages": [HumanMessage(content="上一轮找南京公园"), AIMessage(content="找到三个")],
        },
        llm=llm,
    )

    contents = [message.content for message in llm.messages]
    assert "上一轮找南京公园" in contents
    assert "找到三个" in contents
    assert contents[-1] == "继续画出来"


def test_assemble_uses_injected_llm_transport():
    class FakeLLM:
        def invoke(self, *_args, **_kwargs):
            return SimpleNamespace(content='{"reply":"注入成功"}')

    result = assemble_node(
        {"user_input": "你好", "sub_results": {}, "dispatcher_events": []},
        llm=FakeLLM(),
    )

    assert result["final_output"]["summary"] == "注入成功"


def test_polygon_geojson_map_has_bbox():
    geojson = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[118.0, 31.0], [119.0, 31.0], [119.0, 32.0], [118.0, 31.0]]],
            },
        }],
    }

    result = _build_map_from_geojson(geojson)

    assert result["bbox"] == [118.0, 31.0, 119.0, 32.0]


def test_assemble_computes_bbox_for_explicit_viz_layers():
    class FakeLLM:
        def invoke(self, *_args, **_kwargs):
            return SimpleNamespace(content='{"reply":"地图已生成"}')

    layer = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[118.0, 31.0], [119.0, 31.0], [119.0, 32.0], [118.0, 31.0]]],
            },
        }],
    }
    state = {
        "user_input": "把结果画出来",
        "dispatcher_events": [],
        "sub_results": {
            "viz": [{
                "agent_role": "viz",
                "status": "success",
                "artifacts": {"layers": [layer]},
                "iteration_used": 1,
            }],
        },
    }

    result = assemble_node(state, llm=FakeLLM())

    assert result["final_output"]["bbox"] == [118.0, 31.0, 119.0, 32.0]


def test_geo_transform_invalid_params_returns_structured_error():
    result = _handle_geo_transform(_ctx("geo_transform", {"operation": "haversine"}))

    assert result.status == "error"
    assert result.error_code == "INVALID_PARAMS"


def test_code_mode_preserves_structured_tool_errors():
    fn = _build_code_mode_tool_fns(get_spec("geo"), session_vars={})["geo_transform"]

    result = fn(operation="haversine")

    assert result == {
        "status": "error",
        "message": "haversine 需要 p1 和 p2 参数",
        "error_code": "INVALID_PARAMS",
    }


def test_kernel_tool_selection_and_skill_loading_persist_for_next_iteration():
    session_vars: dict = {}
    fns = _build_code_mode_tool_fns(get_spec("geometer"), session_vars=session_vars)

    toolkit = fns["select_toolkit"](toolkits=["vector_analysis"])
    skill = fns["load_skill"](name="meter_buffer")

    assert toolkit["active_toolkits"] == ["vector_analysis"]
    assert session_vars["__enabled_toolkits__"] == ["vector_analysis"]
    assert "meter_buffer" in session_vars["__loaded_skills__"]
    assert "米" in session_vars["__loaded_skills__"]["meter_buffer"]


def test_data_io_read_proxy_returns_json_safe_geojson(monkeypatch):
    raw = json.dumps({
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [118.78, 32.04]},
            "properties": {"name": "uploaded"},
        }],
    }).encode()

    async def fake_read(_file_id):
        return raw, "uploaded.geojson"

    monkeypatch.setattr("app.agents.tool_execution._read_upload_from_redis", fake_read)
    fn = _build_code_mode_tool_fns(get_spec("geometer"), session_vars={})["data_io_read"]

    result = fn(file_id="file_test")

    assert result["data"]["type"] == "FeatureCollection"
    assert result["data"]["features"][0]["properties"]["name"] == "uploaded"
    assert result["data"]["_crs_label"] == "GCJ02"
    json.dumps(result)


def test_data_io_read_classifies_an_expired_upload_as_error_not_empty(monkeypatch):
    """Missing persisted bytes are an invalid file reference, not a valid zero-row dataset."""
    from app.agents.tool_execution import _handle_data_io_read

    async def expired(_file_id):
        return None

    monkeypatch.setattr("app.agents.tool_execution._read_upload_from_redis", expired)
    result = _handle_data_io_read(_ToolContext(
        tool_call_id="expired-upload",
        tool_name="data_io_read",
        iteration=1,
        params={"file_id": "file_expired"},
        results_data={},
        instances={},
    ))

    assert result.status == "error"
    assert result.error_code == "UPLOAD_EXPIRED"
    assert result.message == "上传文件已过期或不存在"


def test_uploaded_gcj02_geometry_is_not_offset_a_second_time(monkeypatch):
    """Upload serialization must retain the label that protects spatial math."""
    raw = json.dumps({
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [118.78, 32.04]},
            "properties": {"name": "uploaded"},
        }],
    }).encode()

    async def fake_read(_file_id):
        return raw, "uploaded.geojson"

    monkeypatch.setattr("app.agents.tool_execution._read_upload_from_redis", fake_read)
    uploaded = _build_code_mode_tool_fns(get_spec("geometer"), session_vars={})["data_io_read"](
        file_id="file_test",
    )["data"]

    input_coord = uploaded["features"][0]["geometry"]["coordinates"]
    result = SpatialAnalyzer().simplify_geometry(_dict_to_gdf(uploaded), tolerance=0)
    output_coord = result["data"].geometry.iloc[0].coords[0]

    assert output_coord[0] == pytest.approx(input_coord[0], abs=1e-6)
    assert output_coord[1] == pytest.approx(input_coord[1], abs=1e-6)


def test_map_layer_build_accepts_data_io_read_result_shape():
    feature = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [118.78, 32.04]},
        "properties": {"name": "uploaded"},
    }
    upload_result = {
        "status": "ok",
        "data": {"type": "FeatureCollection", "features": [feature]},
        "feature_count": 1,
    }
    fn = _build_code_mode_tool_fns(get_spec("viz"), session_vars={})["map_layer_build"]

    result = fn(geometry_from=upload_result)

    assert result["layers"][0]["features"] == [feature]


def test_proactive_clarification_returns_sorted_tool_records():
    result = _handle_proactive_clarification(_ctx("proactive_clarification"))

    assert result.status == "success"
    names = [item["name"] for item in result.data["available_tools"]]
    assert names == sorted(names)


def test_sync_data_io_handler_does_not_enter_async_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "APP_WORKSPACE_DIR", str(tmp_path))
    ctx = _ctx(
        "load_vector",
        {"file_path": str(tmp_path / "missing.geojson")},
        instances={"data_io": DataIO()},
    )

    result = _handle_load_vector(ctx)

    assert result.status == "error"
    assert "读取失败" in (result.message or "")


@pytest.mark.asyncio
async def test_upload_payload_is_on_disk_and_redis_contains_metadata_only(
    tmp_path, monkeypatch, fake_redis
):
    monkeypatch.setattr(settings, "APP_WORKSPACE_DIR", str(tmp_path))

    await _persist_upload("file_disk", b'{"type":"FeatureCollection","features":[]}', "a.geojson")

    raw = await fake_redis.get(make_key("upload", "file_disk"))
    payload = json.loads(raw)
    stored = Path(payload["storage_path"])
    assert stored.read_bytes().startswith(b'{"type"')
    assert stored.is_relative_to((tmp_path / "uploads").resolve())
    assert "content_b64" not in payload


@pytest.mark.asyncio
async def test_upload_reader_returns_original_filename_and_bytes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "APP_WORKSPACE_DIR", str(tmp_path))
    content = b'{"type":"FeatureCollection","features":[]}'
    await _persist_upload("file_read", content, "map.geojson")

    record = await _read_upload_from_redis("file_read")

    assert record == (content, "map.geojson")


def test_upload_type_accepts_documented_geotiff_extensions():
    assert validate_file_type("dem.tif") == ".tif"
    assert validate_file_type("dem.tiff") == ".tiff"


def test_data_io_read_materializes_uploaded_geotiff_for_raster_tools(tmp_path, monkeypatch):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    source = tmp_path / "source.tif"
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        height=8,
        width=8,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(118.7, 32.1, 0.001, 0.001),
    ) as dst:
        dst.write(np.arange(64, dtype="float32").reshape(1, 8, 8))

    async def fake_read(_file_id):
        return source.read_bytes(), "dem.tif"

    monkeypatch.setattr(settings, "APP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setattr("app.agents.tool_execution._read_upload_from_redis", fake_read)
    result = _handle_data_io_read(_ctx(
        "data_io_read",
        {"file_id": "file_dem"},
        instances={"data_io": DataIO()},
    ))

    assert result.status == "success"
    raster_path = Path(result.data["data"]["raster_path"])
    assert raster_path.exists()
    assert raster_path.is_relative_to((tmp_path / "workspace" / "uploads").resolve())
    assert result.data["data"]["metadata"]["shape"] == (8, 8)


@pytest.mark.asyncio
async def test_upload_persistence_cleans_only_expired_payload_directories(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "APP_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "UPLOAD_TTL_S", 60)
    upload_root = tmp_path / "uploads"
    old_dir = upload_root / "old_file"
    fresh_dir = upload_root / "fresh_file"
    old_dir.mkdir(parents=True)
    fresh_dir.mkdir(parents=True)
    (old_dir / "original.geojson").write_text("old", encoding="utf-8")
    (fresh_dir / "original.geojson").write_text("fresh", encoding="utf-8")
    expired = time.time() - 120
    os.utime(old_dir, (expired, expired))

    await _persist_upload("new_file", b'{}', "new.geojson")

    assert not old_dir.exists()
    assert fresh_dir.exists()
    assert (upload_root / "new_file" / "original.geojson").exists()


@pytest.mark.asyncio
async def test_upload_cleanup_retries_a_transient_windows_file_lock(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "APP_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "UPLOAD_TTL_S", 1)
    upload_root = tmp_path / "uploads"
    old_dir = upload_root / "locked_file"
    old_dir.mkdir(parents=True)
    (old_dir / "original.geojson").write_text("old", encoding="utf-8")
    expired = time.time() - 120
    os.utime(old_dir, (expired, expired))

    real_rmtree = shutil.rmtree
    attempts = 0

    def transient_lock(path):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError(5, "file is temporarily locked", str(path))
        return real_rmtree(path)

    monkeypatch.setattr("app.api.upload.shutil.rmtree", transient_lock)

    await _persist_upload("new_after_lock", b'{}', "new.geojson")

    assert attempts == 2
    assert not old_dir.exists()
    assert (upload_root / "new_after_lock" / "original.geojson").exists()


@pytest.mark.asyncio
async def test_upload_cleanup_treats_an_already_removed_candidate_as_success(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "APP_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "UPLOAD_TTL_S", 1)
    old_dir = tmp_path / "uploads" / "already_removed"
    old_dir.mkdir(parents=True)

    real_stat = Path.stat
    real_is_dir = Path.is_dir
    vanished = 0
    armed = False

    def existing_directory(path):
        nonlocal armed
        if path.name == "already_removed":
            armed = True
            return True
        return real_is_dir(path)

    def disappearing_stat(path, *args, **kwargs):
        nonlocal vanished
        if armed and path.name == "already_removed":
            vanished += 1
            raise FileNotFoundError(2, "removed by another cleanup worker", str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "is_dir", existing_directory)
    monkeypatch.setattr(Path, "stat", disappearing_stat)
    warning = MagicMock()
    monkeypatch.setattr("app.api.upload.logger.warning", warning)

    await _persist_upload("new_after_race", b'{}', "new.geojson")

    assert vanished >= 1
    warning.assert_not_called()
    assert (tmp_path / "uploads" / "new_after_race" / "original.geojson").exists()
