"""Guardrail pipeline 集成测试。

测试 preflight 规则在真实 handler 执行中被触发：
1. 地理坐标系 gdf 调 buffer → buffer_requires_projected_crs blocking error
2. 两个不同 CRS 图层调 join_by_location → crs_consistency blocking error
3. 调用 export_result 到已存在路径 → output_path_overwrite warning
4. csv_to_points 坐标值越界 → coordinate_value_range warning

测试直接调用 preflight 规则函数 + run_with_preflight 编排器，
验证 blocking error 抛出 PreflightError 且 warning 注入 result.data。
"""

import os
import tempfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point, Polygon

from app.agents.preflight import run_with_preflight, PreflightError, ValidationIssue
from app.agents.preflight.registry import preflight_for
from app.agents.preflight.rules_buffer import _check_buffer_crs
from app.agents.preflight.rules_overlay import _check_overlay_crs
from app.agents.preflight.rules_layer import _check_layer_exists, _check_field_exists
from app.agents.preflight.rules_overwrite import _check_output_overwrite
from app.agents.workspace.state import WorkspaceState
from app.tools.data_io import DataIO
from app.tools.geo_transform import wgs84_to_gcj02


# ============================================================
# 测试基准点：南京新街口附近 (118.7782, 32.0417)
# ============================================================

_NANJING_GCJ02 = wgs84_to_gcj02(118.7782, 32.0417)


# ============================================================
# 辅助函数
# ============================================================

def _make_workspace_with_layer(name, crs, fields=None, kind="vector", feature_count=10):
    """构造含一个图层的 WorkspaceState。"""
    metadata = {"crs": crs}
    if fields:
        metadata["fields"] = fields
    if feature_count is not None:
        metadata["feature_count"] = feature_count
    ws = WorkspaceState({})
    ws.add_layer(name=name, kind=kind, metadata=metadata)
    return ws


def _result_with_data(features=None, feature_count=None):
    """构造模拟 ToolResult。"""

    class _MockResult:
        def __init__(self, data):
            self.data = data

    data = {}
    if features is not None:
        data["features"] = features
    if feature_count is not None:
        data["feature_count"] = feature_count
    return _MockResult(data)


# ============================================================
# 测试 1: buffer → buffer_requires_projected_crs blocking error
# ============================================================

class TestBufferCRSGuardrail:
    """buffer 工具在地理坐标系下应触发 preflight 阻断。"""

    def test_buffer_with_geographic_crs_blocks(self):
        """EPSG:4326 地理坐标系 → buffer blocking error。"""
        ws = _make_workspace_with_layer("points", crs="EPSG:4326", feature_count=5)

        ctx = {
            "tool_name": "buffer",
            "workspace": ws,
            "kwargs": {"input_ref": "points", "radius_m": 500},
        }

        issues = preflight_for("buffer_layer", ctx)
        blocking = [i for i in issues if i.severity == "error"]

        assert len(blocking) >= 1
        err_codes = {i.code for i in blocking}
        assert "buffer_crs_mismatch" in err_codes

    def test_buffer_with_epsg4490_blocks(self):
        """EPSG:4490 (CGCS2000 地理坐标系) → blocking error。"""
        ws = _make_workspace_with_layer("data", crs="EPSG:4490", feature_count=3)

        ctx = {
            "tool_name": "buffer",
            "workspace": ws,
            "kwargs": {"input_ref": "data", "radius_m": 1000},
        }

        issues = preflight_for("buffer_layer", ctx)
        blocking = [i for i in issues if i.severity == "error"]

        assert any(i.code == "buffer_crs_mismatch" for i in blocking)

    def test_buffer_with_wgs84_label_blocks(self):
        """CRS 标签含 "WGS84" → blocking error。"""
        ws = _make_workspace_with_layer("roads", crs="WGS84", feature_count=8)

        ctx = {
            "tool_name": "buffer",
            "workspace": ws,
            "kwargs": {"input_ref": "roads", "radius_m": 200},
        }

        issues = preflight_for("buffer_layer", ctx)
        blocking = [i for i in issues if i.severity == "error"]
        assert len(blocking) >= 1

    def test_buffer_with_projected_crs_passes(self):
        """EPSG:4548 (投影坐标系) → 应通过检查。"""
        ws = _make_workspace_with_layer("data", crs="EPSG:4548", feature_count=5)

        ctx = {
            "tool_name": "buffer",
            "workspace": ws,
            "kwargs": {"input_ref": "data", "radius_m": 500},
        }

        issues = preflight_for("buffer_layer", ctx)
        blocking = [i for i in issues if i.severity == "error"]
        assert len(blocking) == 0

    def test_buffer_with_utm_passes(self):
        """EPSG:32650 (UTM zone 50N) → 应通过检查。"""
        ws = _make_workspace_with_layer("buildings", crs="EPSG:32650", feature_count=7)

        ctx = {
            "tool_name": "buffer",
            "workspace": ws,
            "kwargs": {"input_ref": "buildings", "radius_m": 100},
        }

        issues = preflight_for("buffer_layer", ctx)
        blocking = [i for i in issues if i.severity == "error"]
        assert len(blocking) == 0

    def test_buffer_preflight_error_contains_repair_hint(self):
        """blocking issue 应包含 repair suggestion。"""
        ws = _make_workspace_with_layer("points", crs="EPSG:4326", feature_count=5)

        ctx = {
            "tool_name": "buffer",
            "workspace": ws,
            "kwargs": {"input_ref": "points", "radius_m": 500},
        }

        issues = preflight_for("buffer_layer", ctx)
        buffer_issues = [i for i in issues if i.code == "buffer_crs_mismatch"]
        assert len(buffer_issues) == 1
        assert buffer_issues[0].repair is not None
        assert buffer_issues[0].repair.action == "reproject_layer"


# ============================================================
# 测试 2: join_by_location / overlay → crs_consistency blocking error
# ============================================================

class TestCRSConsistencyGuardrail:
    """两个不同 CRS 图层做叠加/空间连接应触发 CRS 一致性检查。"""

    def test_overlay_different_crs_blocks(self):
        """图层 A(EPSG:4326) vs 图层 B(EPSG:4548) → blocking error。"""
        ws = WorkspaceState({})
        ws.add_layer(name="layer_a", kind="vector", metadata={"crs": "EPSG:4326", "feature_count": 10})
        ws.add_layer(name="layer_b", kind="vector", metadata={"crs": "EPSG:4548", "feature_count": 5})

        ctx = {
            "tool_name": "overlay",
            "workspace": ws,
            "kwargs": {"input_ref": "layer_a", "overlay_ref": "layer_b"},
        }

        issues = preflight_for("clip_layer", ctx)
        blocking = [i for i in issues if i.severity == "error"]

        assert len(blocking) >= 1
        err_codes = {i.code for i in blocking}
        assert "overlay_crs_mismatch" in err_codes

    def test_intersect_different_crs_blocks(self):
        """intersect 两个不同 CRS → blocking error。"""
        ws = WorkspaceState({})
        ws.add_layer(name="roads", kind="polygon", metadata={"crs": "EPSG:4326", "feature_count": 20})
        ws.add_layer(name="boundary", kind="polygon", metadata={"crs": "EPSG:32650", "feature_count": 1})

        ctx = {
            "tool_name": "intersect",
            "workspace": ws,
            "kwargs": {"input_ref": "roads", "overlay_ref": "boundary"},
        }

        issues = preflight_for("intersect_layer", ctx)
        blocking = [i for i in issues if i.severity == "error"]
        assert "overlay_crs_mismatch" in {i.code for i in blocking}

    def test_difference_different_crs_blocks(self):
        """difference 操作不同 CRS → blocking error。"""
        ws = WorkspaceState({})
        ws.add_layer(name="full_area", kind="polygon", metadata={"crs": "EPSG:4326", "feature_count": 1})
        ws.add_layer(name="holes", kind="polygon", metadata={"crs": "EPSG:3857", "feature_count": 3})

        ctx = {
            "tool_name": "difference",
            "workspace": ws,
            "kwargs": {"input_ref": "full_area", "overlay_ref": "holes"},
        }

        issues = preflight_for("difference_layer", ctx)
        blocking = [i for i in issues if i.severity == "error"]
        assert "overlay_crs_mismatch" in {i.code for i in blocking}

    def test_overlay_same_crs_passes(self):
        """两个图层 CRS 一致 → 应通过检查。"""
        ws = WorkspaceState({})
        ws.add_layer(name="layer_a", kind="polygon", metadata={"crs": "EPSG:4326", "feature_count": 10})
        ws.add_layer(name="layer_b", kind="polygon", metadata={"crs": "EPSG:4326", "feature_count": 5})

        ctx = {
            "tool_name": "intersect",
            "workspace": ws,
            "kwargs": {"input_ref": "layer_a", "overlay_ref": "layer_b"},
        }

        issues = preflight_for("intersect_layer", ctx)
        blocking = [i for i in issues if i.severity == "error"]
        assert len(blocking) == 0

    def test_overlay_crs_mismatch_repair_has_patch(self):
        """overlay_crs_mismatch issue 应含 repair patch 建议。"""
        ws = WorkspaceState({})
        ws.add_layer(name="a", kind="vector", metadata={"crs": "EPSG:4326", "feature_count": 5})
        ws.add_layer(name="b", kind="vector", metadata={"crs": "EPSG:4548", "feature_count": 3})

        ctx = {
            "tool_name": "clip",
            "workspace": ws,
            "kwargs": {"input_ref": "a", "overlay_ref": "b"},
        }

        issues = preflight_for("clip_layer", ctx)
        mismatch = [i for i in issues if i.code == "overlay_crs_mismatch"]
        assert len(mismatch) == 1
        assert mismatch[0].repair is not None
        assert mismatch[0].repair.kind == "confirm_action"


# ============================================================
# 测试 3: export_result → output_path_overwrite warning
# ============================================================

class TestOverwriteGuardrail:
    """export_result 输出路径已存在 → output_overwrite warning。"""

    def test_export_to_existing_path_warns(self):
        """已存在的路径 → output_overwrite warning（非 blocking）。"""
        with tempfile.NamedTemporaryFile(suffix=".gpkg", delete=False) as tmp:
            existing_path = tmp.name

        try:
            ctx = {
                "tool_name": "export_result",
                "workspace": None,
                "kwargs": {"output_path": existing_path},
            }

            issues = preflight_for("export_result", ctx)
            assert len(issues) > 0
            overwrite_issues = [i for i in issues if i.code == "output_exists"]
            assert len(overwrite_issues) == 1
            assert overwrite_issues[0].severity == "warning"
            assert overwrite_issues[0].repair.kind == "confirm_overwrite"
        finally:
            Path(existing_path).unlink(missing_ok=True)

    def test_export_to_new_path_no_warning(self):
        """不存在的路径 → 无 output_overwrite warning。"""
        with tempfile.TemporaryDirectory() as tmp:
            new_path = os.path.join(tmp, "nonexistent.gpkg")

            ctx = {
                "tool_name": "export_result",
                "workspace": None,
                "kwargs": {"output_path": new_path},
            }

            issues = preflight_for("export_result", ctx)
            overwrite_issues = [i for i in issues if i.code == "output_exists"]
            assert len(overwrite_issues) == 0

    def test_export_empty_path_no_warning(self):
        """空 output_path → 无 warning。"""
        ctx = {
            "tool_name": "export_result",
            "workspace": None,
            "kwargs": {"output_path": ""},
        }

        issues = preflight_for("export_result", ctx)
        assert len(issues) == 0

    def test_run_with_preflight_does_not_block_on_warning(self):
        """output_overwrite 是 warning，不应抛 PreflightError，handler 应照常执行。"""
        called = []

        def fake_handler(*args, **kwargs):
            called.append(1)
            return _result_with_data(feature_count=5)

        with tempfile.NamedTemporaryFile(suffix=".gpkg", delete=False) as tmp:
            existing_path = tmp.name

        try:
            result = run_with_preflight(
                tool_name="export_result",
                semantic_action="export_result",
                fn=fake_handler,
                args=(),
                kwargs={"output_path": existing_path},
                workspace=None,
            )
            assert len(called) == 1  # handler 确实被执行了
            # output_overwrite 是 preflight warning，不会被注入到 result.data
            # postflight_warnings 仅来自 postflight 检查（空结果等）
            # 此处仅验证 handler 未被阻断
        finally:
            Path(existing_path).unlink(missing_ok=True)


# ============================================================
# 测试 4: csv_to_points → coordinate_value_range warning
# ============================================================

class TestCoordinateRangeGuardrail:
    """csv_to_points 坐标值越界 → coordinate_value_range warning。"""

    def test_coordinate_out_of_longitude_range_warns(self):
        """经度 > 180 → warning。"""
        dio = DataIO()
        df = pd.DataFrame({
            "lng": [118.7782, 200.0, 118.7800],
            "lat": [32.0417, 32.0420, 32.0400],
            "name": ["A", "B", "C"],
        })

        result = dio.csv_to_points(df, x_field="lng", y_field="lat")
        assert result["status"] == "success"

        warnings = result.get("warnings", [])
        range_warnings = [w for w in warnings if "范围" in w]
        assert len(range_warnings) >= 1, f"Expected range warning, got: {warnings}"

        # 验证输出数据
        data = result["data"]
        # 应有 warning 提示坐标越界（具体过滤行为取决于实现）
        coords = [f["geometry"]["coordinates"] for f in data["features"]]
        assert [118.7782, 32.0417] in coords

    def test_coordinate_out_of_latitude_range_warns(self):
        """纬度 > 90 → warning。"""
        dio = DataIO()
        df = pd.DataFrame({
            "lng": [118.7782, 118.7800],
            "lat": [32.0417, 100.0],  # lat 越界
            "name": ["A", "B"],
        })

        result = dio.csv_to_points(df, x_field="lng", y_field="lat")
        assert result["status"] == "success"

        warnings = result.get("warnings", [])
        range_warnings = [w for w in warnings if "范围" in w]
        assert len(range_warnings) >= 1

    def test_coordinate_negative_longitude_out_of_range_warns(self):
        """经度 < -180 → warning。"""
        dio = DataIO()
        df = pd.DataFrame({
            "lng": [118.7782, -200.0],
            "lat": [32.0417, 32.0420],
            "name": ["A", "B"],
        })

        result = dio.csv_to_points(df, x_field="lng", y_field="lat")
        assert result["status"] == "success"

        warnings = result.get("warnings", [])
        range_warnings = [w for w in warnings if "范围" in w]
        assert len(range_warnings) >= 1

    def test_all_valid_coordinates_no_warning(self):
        """所有坐标在合法范围内 → 无 range warning。"""
        dio = DataIO()
        df = pd.DataFrame({
            "lng": [118.7782, 118.7800, 118.7750],
            "lat": [32.0417, 32.0420, 32.0400],
            "name": ["A", "B", "C"],
        })

        result = dio.csv_to_points(df, x_field="lng", y_field="lat")
        assert result["status"] == "success"
        # 无 range-related warning
        range_warnings = [w for w in result.get("warnings", []) if "范围" in w]
        assert len(range_warnings) == 0

    def test_swapped_xy_fields_triggers_warning(self):
        """x_field 和 y_field 选反时触发坐标范围警告。"""
        dio = DataIO()
        df = pd.DataFrame({
            "lat_col": [32.0417, 32.0420],   # 被当成 x_field
            "lng_col": [118.7782, 118.7800], # 被当成 y_field（仍在 [-90,90] 外）
        })

        result = dio.csv_to_points(df, x_field="lat_col", y_field="lng_col")
        assert result["status"] == "success"

        # lat_col 值在 [-180,180] 内 → 可能不触发 range warning
        # 但 lng_col 值 118.x 超出 [-90,90] lat 范围
        range_warnings = [w for w in result.get("warnings", []) if "范围" in w]
        assert len(range_warnings) >= 1


# ============================================================
# 补充：run_with_preflight 编排器集成验证
# ============================================================

class TestRunWithPreflightIntegration:
    """验证 run_with_preflight 的完整编排：preflight → handler → postflight。"""

    def test_blocking_preflight_raises_preflight_error(self):
        """blocking preflight issue → 抛 PreflightError，handler 不被调用。"""
        ws = _make_workspace_with_layer("points", crs="EPSG:4326", feature_count=5)
        called = []

        def fake_handler(*args, **kwargs):
            called.append(1)
            return _result_with_data(feature_count=5)

        with pytest.raises(PreflightError) as exc_info:
            run_with_preflight(
                tool_name="buffer",
                semantic_action="buffer_layer",
                fn=fake_handler,
                args=(),
                kwargs={"input_ref": "points", "radius_m": 500},
                workspace=ws,
            )

        assert len(called) == 0  # handler 未被调用
        assert len(exc_info.value.issues) > 0

    def test_passing_preflight_executes_handler(self):
        """preflight 通过 → handler 正常执行。"""
        ws = _make_workspace_with_layer("data", crs="EPSG:4548", feature_count=5)
        called = []

        def fake_handler(*args, **kwargs):
            called.append(1)
            return _result_with_data(features=[{"type": "Feature", "geometry": {}}], feature_count=1)

        result = run_with_preflight(
            tool_name="buffer",
            semantic_action="buffer_layer",
            fn=fake_handler,
            args=(),
            kwargs={"input_ref": "data", "radius_m": 500},
            workspace=ws,
        )

        assert len(called) == 1
        assert result.data["feature_count"] == 1

    def test_postflight_warns_on_empty_result(self):
        """空结果 → postflight 注入 warning。"""
        ws = _make_workspace_with_layer("data", crs="EPSG:4548", feature_count=5)
        ws.add_layer(name="boundary", kind="vector",
                     metadata={"crs": "EPSG:4548", "feature_count": 3})

        def fake_handler(*args, **kwargs):
            return _result_with_data(features=[], feature_count=0)

        result = run_with_preflight(
            tool_name="clip",
            semantic_action="clip_layer",
            fn=fake_handler,
            args=(),
            kwargs={"input_ref": "data", "overlay_ref": "boundary"},
            workspace=ws,
        )

        assert result.data["feature_count"] == 0
        warnings = result.data.get("postflight_warnings", [])
        assert any("空" in w for w in warnings)

    def test_layer_not_found_blocks(self):
        """layer_exists 规则：不存在的图层 → blocking error。"""
        ws = WorkspaceState({})
        # 不添加任何图层

        ctx = {
            "tool_name": "buffer",
            "workspace": ws,
            "kwargs": {"input_ref": "nonexistent_layer"},
        }

        issues = preflight_for("buffer_layer", ctx)
        blocking = [i for i in issues if i.severity == "error"]
        assert len(blocking) >= 1
        assert any(i.code == "layer_not_found" for i in blocking)

    def test_field_not_found_blocks(self):
        """field_exists 规则：不存在的字段 → blocking error。"""
        ws = _make_workspace_with_layer("data", crs="EPSG:4548", fields=["name", "value", "geometry"])

        ctx = {
            "tool_name": "field_calculator",
            "workspace": ws,
            "kwargs": {"input_ref": "data", "field": "nonexistent_field"},
        }

        issues = preflight_for("field_calculator", ctx)
        blocking = [i for i in issues if i.severity == "error"]
        assert len(blocking) >= 1
        assert any(i.code == "field_not_found" for i in blocking)

    def test_field_exists_passes_for_existing_field(self):
        """field_exists 规则：存在的字段 → 通过。"""
        ws = _make_workspace_with_layer("data", crs="EPSG:4548", fields=["population", "area", "geometry"])

        ctx = {
            "tool_name": "extract_by_attribute",
            "workspace": ws,
            "kwargs": {"input_ref": "data", "attribute": "population"},
        }

        issues = preflight_for("extract_by_attribute", ctx)
        blocking = [i for i in issues if i.severity == "error"]
        blocking_codes = {i.code for i in blocking}
        assert "field_not_found" not in blocking_codes
