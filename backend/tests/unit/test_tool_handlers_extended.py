"""测试 handler 正常执行和 error path。"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.agents.context import _ToolContext
from app.agents.tool_execution import (
    _TOOL_REGISTRY,
    _handle_clip_layer,
    _handle_dissolve_layer,
    _handle_slope,
    _handle_load_vector,
    _handle_extract_by_attribute,
    _handle_count_points_in_polygon,
    _handle_reproject_layer,
    _handle_centroid_layer,
    _handle_summarize_layer,
    _handle_csv_to_points,
    _handle_export_result,
    _handle_check_validity,
    _handle_merge_layers,
    _handle_convex_hull,
    _handle_geo_code,
)


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------


def _make_ctx(tool_name, params=None, results_data=None):
    return _ToolContext(
        tool_call_id="test-001",
        tool_name=tool_name,
        iteration=1,
        params=params or {},
        results_data=results_data or {},
    )


def _simple_gdf_dict():
    """返回一个简单的 FeatureCollection dict（模拟 GeoJSON）。"""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [121.0, 31.0]},
                "properties": {"id": 1},
            }
        ],
    }


# ------------------------------------------------------------------
# Vector analysis handlers — error paths
# ------------------------------------------------------------------


def test_clip_layer_missing_refs():
    ctx = _make_ctx("clip_layer")
    result = _handle_clip_layer(ctx)
    assert result.status == "empty"


def test_dissolve_layer_missing_ref():
    ctx = _make_ctx("dissolve_layer")
    result = _handle_dissolve_layer(ctx)
    assert result.status == "empty"


def test_count_points_in_polygon_missing_refs():
    ctx = _make_ctx("count_points_in_polygon")
    result = _handle_count_points_in_polygon(ctx)
    assert result.status == "empty"


def test_convex_hull_missing_ref():
    ctx = _make_ctx("convex_hull")
    result = _handle_convex_hull(ctx)
    assert result.status == "empty"


def test_merge_layers_missing_ref():
    ctx = _make_ctx("merge_layers")
    result = _handle_merge_layers(ctx)
    assert result.status == "empty"


def test_extract_by_attribute_missing_expression():
    ctx = _make_ctx("extract_by_attribute", params={"input_ref": 0})
    result = _handle_extract_by_attribute(ctx)
    assert result.status == "empty"


# ------------------------------------------------------------------
# Vector analysis handlers — 正常路径（mock analyzer）
# ------------------------------------------------------------------

class FakeGeoDataFrame:
    """假 GeoDataFrame，用于验证 handler 调用了 analyzer 并返回了 ToolResult。"""
    attrs = {}

    def to_json(self):
        return '{"type":"FeatureCollection","features":[]}'


@patch("app.agents.tool_execution._dict_to_gdf")
def test_clip_layer_success(mock_dict_to_gdf):
    fake_gdf = FakeGeoDataFrame()
    mock_dict_to_gdf.return_value = fake_gdf

    gdf_dict = _simple_gdf_dict()
    ctx = _make_ctx("clip_layer", params={"input_ref": 0, "overlay_ref": 1}, results_data={0: gdf_dict, 1: gdf_dict})
    ctx.analyzer = MagicMock()
    ctx.analyzer.clip.return_value = fake_gdf

    result = _handle_clip_layer(ctx)
    assert result.status == "success"
    assert result.tool_name == "clip_layer"
    assert result.source == "computed"


@patch("app.agents.tool_execution._dict_to_gdf")
def test_dissolve_layer_success(mock_dict_to_gdf):
    fake_gdf = FakeGeoDataFrame()
    mock_dict_to_gdf.return_value = fake_gdf

    gdf_dict = _simple_gdf_dict()
    ctx = _make_ctx("dissolve_layer", params={"geometry_from": 0, "by": "region"}, results_data={0: gdf_dict})
    ctx.analyzer = MagicMock()
    ctx.analyzer.dissolve.return_value = fake_gdf

    result = _handle_dissolve_layer(ctx)
    assert result.status == "success"


@patch("app.agents.tool_execution._dict_to_gdf")
def test_count_points_in_polygon_success(mock_dict_to_gdf):
    fake_gdf = FakeGeoDataFrame()
    mock_dict_to_gdf.return_value = fake_gdf

    gdf_dict = _simple_gdf_dict()
    ctx = _make_ctx("count_points_in_polygon", params={"polygons_from": 0, "points_from": 1}, results_data={0: gdf_dict, 1: gdf_dict})
    ctx.analyzer = MagicMock()
    ctx.analyzer.count_points_in_polygon.return_value = fake_gdf

    result = _handle_count_points_in_polygon(ctx)
    assert result.status == "success"


@patch("app.agents.tool_execution._dict_to_gdf")
def test_reproject_layer_success(mock_dict_to_gdf):
    fake_gdf = FakeGeoDataFrame()
    mock_dict_to_gdf.return_value = fake_gdf

    gdf_dict = _simple_gdf_dict()
    ctx = _make_ctx("reproject_layer", params={"geometry_from": 0, "target_crs": "EPSG:3857"}, results_data={0: gdf_dict})
    ctx.analyzer = MagicMock()
    ctx.analyzer.reproject.return_value = fake_gdf

    result = _handle_reproject_layer(ctx)
    assert result.status == "success"


@patch("app.agents.tool_execution._dict_to_gdf")
def test_centroid_layer_success(mock_dict_to_gdf):
    fake_gdf = FakeGeoDataFrame()
    mock_dict_to_gdf.return_value = fake_gdf

    gdf_dict = _simple_gdf_dict()
    ctx = _make_ctx("centroid_layer", params={"geometry_from": 0}, results_data={0: gdf_dict})
    ctx.analyzer = MagicMock()
    ctx.analyzer.centroid.return_value = fake_gdf

    result = _handle_centroid_layer(ctx)
    assert result.status == "success"


@patch("app.agents.tool_execution._dict_to_gdf")
def test_extract_by_attribute_success(mock_dict_to_gdf):
    fake_gdf = FakeGeoDataFrame()
    mock_dict_to_gdf.return_value = fake_gdf

    gdf_dict = _simple_gdf_dict()
    ctx = _make_ctx("extract_by_attribute", params={"geometry_from": 0, "expression": "population > 1000"}, results_data={0: gdf_dict})
    ctx.analyzer = MagicMock()
    ctx.analyzer.extract_by_attribute.return_value = fake_gdf

    result = _handle_extract_by_attribute(ctx)
    assert result.status == "success"


# ------------------------------------------------------------------
# Raster handlers
# ------------------------------------------------------------------


def test_slope_missing_dem_path():
    ctx = _make_ctx("slope")
    result = _handle_slope(ctx)
    assert result.status == "error"
    assert "dem_path" in result.message.lower()


def test_slope_success():
    ctx = _make_ctx("slope", params={"dem_path": "/tmp/dem.tif"})
    ctx.raster_analyzer = MagicMock()
    ctx.raster_analyzer.slope.return_value = {"status": "success", "data": {"slope_path": "/tmp/slope.tif"}}

    result = _handle_slope(ctx)
    assert result.status == "success"


# ------------------------------------------------------------------
# check_validity — dict 返回
# ------------------------------------------------------------------


@patch("app.agents.tool_execution._dict_to_gdf")
def test_check_validity_dict_result(mock_dict_to_gdf):
    fake_gdf = FakeGeoDataFrame()
    mock_dict_to_gdf.return_value = fake_gdf

    gdf_dict = _simple_gdf_dict()
    ctx = _make_ctx("check_validity", params={"geometry_from": 0}, results_data={0: gdf_dict})
    ctx.analyzer = MagicMock()
    ctx.analyzer.check_validity.return_value = {"status": "success", "data": {"issues": []}}

    result = _handle_check_validity(ctx)
    assert result.status == "success"


# ------------------------------------------------------------------
# IO handlers — error paths
# ------------------------------------------------------------------


def test_geo_code_preserves_provider_error_for_sse_and_ui():
    ctx = _make_ctx("geo_code", params={"address": "南京新街口"})
    ctx.geo_coder = MagicMock()
    ctx.geo_coder.geocode = AsyncMock(return_value={
        "status": "error",
        "message": "高德地理编码服务异常: INVALID_USER_KEY",
        "error_code": "GEOCODE_FAILED",
    })

    result = _handle_geo_code(ctx)

    assert result.status == "error"
    assert result.message == "高德地理编码服务异常: INVALID_USER_KEY"
    assert result.error_code == "GEOCODE_FAILED"


def test_load_vector_missing_path():
    ctx = _make_ctx("load_vector")
    # load_vector handler 是同步的，但需要 mock _run_async 避免真实 IO
    # error path 在 _run_async 之前就返回
    result = _handle_load_vector(ctx)
    assert result.status == "error"


def test_export_result_missing_data():
    ctx = _make_ctx("export_result")
    result = _handle_export_result(ctx)
    assert result.status == "error"


def test_csv_to_points_missing_data():
    ctx = _make_ctx("csv_to_points")
    result = _handle_csv_to_points(ctx)
    assert result.status == "empty"


# ------------------------------------------------------------------
# summarize_layer
# ------------------------------------------------------------------


def test_summarize_layer_missing_ref():
    ctx = _make_ctx("summarize_layer")
    result = _handle_summarize_layer(ctx)
    assert result.status == "empty"


@patch("app.agents.tool_execution._dict_to_gdf")
def test_summarize_layer_success(mock_dict_to_gdf):
    fake_gdf = FakeGeoDataFrame()
    mock_dict_to_gdf.return_value = fake_gdf

    gdf_dict = _simple_gdf_dict()
    ctx = _make_ctx("summarize_layer", params={"geometry_from": 0}, results_data={0: gdf_dict})
    ctx.data_io = MagicMock()
    ctx.data_io.summarize_layer.return_value = {"status": "success", "data": {"rows": 100}}

    result = _handle_summarize_layer(ctx)
    assert result.status == "success"
