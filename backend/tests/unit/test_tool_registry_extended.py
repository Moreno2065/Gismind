"""测试所有新 ToolSpec 已注册 + SubAgentSpec 包含新工具 + handler 已注册。"""
import pytest
from app.agents.registry import TOOL_SPECS, REGISTRY, get_spec
from app.agents.tool_execution import _TOOL_REGISTRY


# ============================================================
# ToolSpec 注册检查
# ============================================================

NEW_VECTOR_ANALYSIS_TOOLS = [
    "clip_layer", "dissolve_layer", "merge_layers",
    "join_by_location", "join_by_nearest", "count_points_in_polygon",
    "extract_by_location", "convex_hull", "bounding_boxes",
]

NEW_VECTOR_TRANSFORM_TOOLS = [
    "centroid_layer", "point_on_surface", "simplify_geometry",
    "fix_geometries", "check_validity", "multipart_to_singlepart",
    "delete_duplicate_geometries", "snap_geometries",
    "reproject_layer", "batch_reproject_layers",
]

NEW_ATTRIBUTE_TOOLS = [
    "extract_by_attribute", "keep_fields", "rename_field", "field_calculator",
]

NEW_RASTER_TOOLS = [
    "reproject_raster", "clip_raster_by_mask", "clip_raster_by_extent",
    "raster_calculator", "zonal_statistics", "raster_sampling",
    "rasterize_vector", "polygonize_raster", "slope", "aspect",
    "hillshade", "contour", "reclassify_raster",
    "terrain_ruggedness_index", "topographic_position_index", "roughness",
]

NEW_IO_TOOLS = [
    "load_vector", "load_raster", "load_csv",
    "csv_to_points", "summarize_layer", "export_result",
]

ALL_NEW_TOOLS = (
    NEW_VECTOR_ANALYSIS_TOOLS
    + NEW_VECTOR_TRANSFORM_TOOLS
    + NEW_ATTRIBUTE_TOOLS
    + NEW_RASTER_TOOLS
    + NEW_IO_TOOLS
)


@pytest.mark.parametrize("tool_name", ALL_NEW_TOOLS)
def test_tool_spec_registered(tool_name):
    """每个新工具都在 TOOL_SPECS 中注册。"""
    assert tool_name in TOOL_SPECS, f"{tool_name} 未在 TOOL_SPECS 中注册"


def test_new_tools_executor_type():
    """向量/栅格/属性工具 executor_type 为 inline，is_async 为 False。"""
    inline_tools = (
        NEW_VECTOR_ANALYSIS_TOOLS
        + NEW_VECTOR_TRANSFORM_TOOLS
        + NEW_ATTRIBUTE_TOOLS
        + NEW_RASTER_TOOLS
        + ["csv_to_points", "summarize_layer"]
    )
    for name in inline_tools:
        spec = TOOL_SPECS[name]
        assert spec.executor_type == "inline", f"{name} executor_type 应为 inline"
        assert spec.is_async is False, f"{name} is_async 应为 False"


def test_io_tools_executor_type_async():
    """IO 工具 load_vector/load_raster/load_csv/export_result 为 async。"""
    async_tools = ["load_vector", "load_raster", "load_csv", "export_result"]
    for name in async_tools:
        spec = TOOL_SPECS[name]
        assert spec.executor_type == "async", f"{name} executor_type 应为 async"
        assert spec.is_async is True, f"{name} is_async 应为 True"


# ============================================================
# Handler 注册检查
# ============================================================

@pytest.mark.parametrize("tool_name", ALL_NEW_TOOLS)
def test_handler_registered(tool_name):
    """每个新工具都有对应的 handler。"""
    assert tool_name in _TOOL_REGISTRY, f"{tool_name} handler 未注册"


# ============================================================
# SubAgentSpec 检查
# ============================================================

GEOMETER_NEW_TOOLS = [
    "clip_layer", "dissolve_layer", "merge_layers",
    "join_by_location", "join_by_nearest", "count_points_in_polygon",
    "extract_by_location", "centroid_layer", "point_on_surface",
    "simplify_geometry", "fix_geometries", "check_validity",
    "reproject_layer", "convex_hull", "bounding_boxes",
]

CODER_NEW_TOOLS = [
    "extract_by_attribute", "keep_fields", "rename_field",
    "field_calculator", "slope", "aspect", "hillshade",
    "contour", "raster_calculator", "zonal_statistics",
]


@pytest.mark.parametrize("tool_name", GEOMETER_NEW_TOOLS)
def test_geometer_has_new_tools(tool_name):
    """geometer SubAgentSpec 包含新增的矢量分析/变换工具。"""
    spec = get_spec("geometer")
    assert tool_name in spec.tool_names, f"geometer 缺少 {tool_name}"


@pytest.mark.parametrize("tool_name", CODER_NEW_TOOLS)
def test_coder_has_new_tools(tool_name):
    """coder SubAgentSpec 包含新增的属性/栅格工具。"""
    spec = get_spec("coder")
    assert tool_name in spec.tool_names, f"coder 缺少 {tool_name}"


def test_coder_still_has_code_executor():
    spec = get_spec("coder")
    assert "code_executor" in spec.tool_names


def test_geometer_still_has_original_tools():
    spec = get_spec("geometer")
    for t in ["buffer", "overlay", "voronoi", "isochrone", "geo_code"]:
        assert t in spec.tool_names, f"geometer 丢失原有工具 {t}"


def test_total_tool_count():
    """确保 TOOL_SPECS 总数合理增长。"""
    # 原有约 17 个 + 新增 44 个 = 约 61 个
    assert len(TOOL_SPECS) >= 60, f"预期至少 60 个 ToolSpec，实际 {len(TOOL_SPECS)}"
