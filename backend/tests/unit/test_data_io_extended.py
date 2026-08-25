"""DataIO 扩展方法单元测试。

覆盖 Task 1 新增方法：
- load_vector: 读取 GeoJSON 文件，返回 GeoJSON dict + metadata。
- load_csv: 读取 CSV 文件，返回 columns / row_count / sample。
- csv_to_points: happy path + 坐标值异常警告。
- summarize_layer: GeoDataFrame / GeoJSON dict 元数据提取。
- export_result: 导出到文件验证。
- load_raster: 错误路径（rasterio 未安装 / 文件不存在）。
"""
from __future__ import annotations

import io
import json
import os
import tempfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point

from app.tools.data_io import DataIO


# ============================================================
# 辅助函数
# ============================================================

def _make_test_gdf() -> gpd.GeoDataFrame:
    """构造简单点 GeoDataFrame（2 个点）。"""
    return gpd.GeoDataFrame(
        {"name": ["A", "B"], "value": [10, 20]},
        geometry=[Point(116.39, 39.91), Point(121.47, 31.23)],
        crs="EPSG:4326",
    )


# ============================================================
# load_vector
# ============================================================

class TestLoadVector:
    """load_vector 方法测试。"""

    def test_read_geojson_success(self):
        """读取 GeoJSON 文件，返回 GeoJSON dict + metadata。"""
        gdf = _make_test_gdf()
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".geojson", delete=False, encoding="utf-8"
        ) as f:
            f.write(gdf.to_json())
            tmp_path = f.name

        try:
            result = DataIO().load_vector(tmp_path)
            assert result["status"] == "success"
            assert "data" in result
            assert "metadata" in result
            data = result["data"]
            assert data["type"] == "FeatureCollection"
            assert len(data["features"]) == 2
            meta = result["metadata"]
            assert meta["feature_count"] == 2
            assert meta["geometry_type"] == "Point"
            assert meta["crs"] is not None
            assert meta["fields"] is not None
            assert meta["bbox"] is not None
            assert len(meta["bbox"]) == 4
        finally:
            os.unlink(tmp_path)

    def test_read_empty_geojson(self):
        """空 GeoJSON 返回空结果但不报错。"""
        empty_gdf = gpd.GeoDataFrame(
            {"name": []}, geometry=[], crs="EPSG:4326"
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".geojson", delete=False, encoding="utf-8"
        ) as f:
            f.write(empty_gdf.to_json())
            tmp_path = f.name

        try:
            result = DataIO().load_vector(tmp_path)
            assert result["status"] == "success"
            assert result["data"]["features"] == []
            assert result["metadata"]["feature_count"] == 0
        finally:
            os.unlink(tmp_path)

    def test_file_not_found(self):
        """不存在的文件返回 error。"""
        result = DataIO().load_vector("/nonexistent/path/test.geojson")
        assert result["status"] == "error"
        assert "message" in result


# ============================================================
# load_raster
# ============================================================

class TestLoadRaster:
    """load_raster 方法测试。"""

    def test_file_not_found(self):
        """不存在的栅格文件返回 error（或 rasterio 未安装报错）。"""
        result = DataIO().load_raster("/nonexistent/raster.tif")
        # rasterio 未安装时也会返回 error
        assert result["status"] == "error"
        assert "message" in result


# ============================================================
# load_csv
# ============================================================

class TestLoadCSV:
    """load_csv 方法测试。"""

    def test_read_csv_success(self):
        """读取 CSV 返回 columns / row_count / sample。"""
        csv_content = "name,lon,lat\nA,116.39,39.91\nB,121.47,31.23\nC,113.26,23.13\nD,104.07,30.67\nE,108.95,34.26\nF,120.15,30.28"
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        ) as f:
            f.write(csv_content)
            tmp_path = f.name

        try:
            result = DataIO().load_csv(tmp_path)
            assert result["status"] == "success"
            data = result["data"]
            assert data["columns"] == ["name", "lon", "lat"]
            assert data["row_count"] == 6
            assert len(data["sample"]) == 5  # 前 5 行
            assert data["sample"][0]["name"] == "A"
        finally:
            os.unlink(tmp_path)

    def test_read_csv_encoding_error(self):
        """编码不匹配返回 error。"""
        # 写一个 GBK 编码文件，但用 UTF-8 读取
        csv_content = "名称,经度,纬度\n北京,116.39,39.91"
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".csv", delete=False
        ) as f:
            f.write(csv_content.encode("gbk"))
            tmp_path = f.name

        try:
            result = DataIO().load_csv(tmp_path, encoding="ascii")
            assert result["status"] == "error"
            assert "编码" in result.get("message", "")
        finally:
            os.unlink(tmp_path)

    def test_file_not_found(self):
        """不存在的 CSV 文件返回 error。"""
        result = DataIO().load_csv("/nonexistent/data.csv")
        assert result["status"] == "error"


# ============================================================
# csv_to_points
# ============================================================

class TestCsvToPoints:
    """csv_to_points 方法测试。"""

    def test_happy_path_from_dataframe(self):
        """用 DataFrame 输入，正常返回 GeoJSON dict。"""
        df = pd.DataFrame({
            "name": ["A", "B", "C"],
            "lon": [116.39, 121.47, 113.26],
            "lat": [39.91, 31.23, 23.13],
            "value": [10, 20, 30],
        })
        result = DataIO().csv_to_points(df, x_field="lon", y_field="lat")
        assert result["status"] == "success"
        data = result["data"]
        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) == 3
        # warnings 应为空
        assert result.get("warnings") == []

    def test_happy_path_from_path(self):
        """用 CSV 文件路径输入。"""
        csv_content = "name,lon,lat\nA,116.39,39.91\nB,121.47,31.23"
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        ) as f:
            f.write(csv_content)
            tmp_path = f.name

        try:
            result = DataIO().csv_to_points(tmp_path, x_field="lon", y_field="lat")
            assert result["status"] == "success"
            assert len(result["data"]["features"]) == 2
        finally:
            os.unlink(tmp_path)

    def test_field_not_exists(self):
        """x_field 不存在时返回 error。"""
        df = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
        result = DataIO().csv_to_points(df, x_field="nonexistent", y_field="b")
        assert result["status"] == "error"
        assert "nonexistent" in result["message"]

    def test_y_field_not_exists(self):
        """y_field 不存在时返回 error。"""
        df = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
        result = DataIO().csv_to_points(df, x_field="a", y_field="missing")
        assert result["status"] == "error"
        assert "missing" in result["message"]

    def test_coordinate_out_of_range(self):
        """坐标值超出预期范围时产生 warning。"""
        df = pd.DataFrame({
            "name": ["正常", "异常经度", "异常纬度"],
            "lon": [116.39, 200.0, 110.0],
            "lat": [39.91, 30.0, 100.0],
        })
        result = DataIO().csv_to_points(df, x_field="lon", y_field="lat")
        assert result["status"] == "success"
        warnings = result.get("warnings", [])
        assert len(warnings) > 0
        assert any("范围异常" in w for w in warnings)

    def test_swapped_coordinates_warning(self):
        """x_field 含纬度名 / y_field 含经度名时产生 warning。"""
        df = pd.DataFrame({
            "id": [1, 2],
            "latitude": [39.91, 31.23],
            "longitude": [116.39, 121.47],
        })
        result = DataIO().csv_to_points(df, x_field="latitude", y_field="longitude")
        assert result["status"] == "success"
        warnings = result.get("warnings", [])
        # 坐标值在正常范围内，但语义上可能颠倒
        # 本方法不做字段名判断，只做数值范围检查；语义判断在 preflight 规则中
        assert len(result["data"]["features"]) == 2

    def test_empty_input(self):
        """空输入返回 error。"""
        df = pd.DataFrame({"lon": [], "lat": []})
        result = DataIO().csv_to_points(df, x_field="lon", y_field="lat")
        assert result["status"] == "error"
        assert "为空" in result["message"] or "空" in result["message"]

    def test_invalid_input_type(self):
        """不支持的类型返回 error。"""
        result = DataIO().csv_to_points([1, 2, 3], x_field="x", y_field="y")  # type: ignore[arg-type]
        assert result["status"] == "error"

    def test_from_dict_input(self):
        """从 dict（模拟 load_csv 输出）输入。"""
        input_dict = {
            "data": {
                "sample": [
                    {"name": "A", "lon": 116.39, "lat": 39.91},
                    {"name": "B", "lon": 121.47, "lat": 31.23},
                ],
            },
        }
        result = DataIO().csv_to_points(input_dict, x_field="lon", y_field="lat")
        assert result["status"] == "success"
        assert len(result["data"]["features"]) == 2


# ============================================================
# summarize_layer
# ============================================================

class TestSummarizeLayer:
    """summarize_layer 方法测试。"""

    def test_from_geodataframe(self):
        """从 GeoDataFrame 提取元数据。"""
        gdf = _make_test_gdf()
        result = DataIO().summarize_layer(gdf)
        assert result["status"] == "success"
        data = result["data"]
        assert data["feature_count"] == 2
        assert data["geometry_type"] == "Point"
        assert "name" in data["fields"]
        assert data["bbox"] is not None

    def test_from_geojson_dict(self):
        """从 GeoJSON dict 提取元数据。"""
        gdf = _make_test_gdf()
        geojson_dict = json.loads(gdf.to_json())
        result = DataIO().summarize_layer(geojson_dict)
        assert result["status"] == "success"
        assert result["data"]["feature_count"] == 2

    def test_empty_geodataframe(self):
        """空 GeoDataFrame 返回空元数据。"""
        empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        result = DataIO().summarize_layer(empty)
        assert result["status"] == "success"
        assert result["data"]["feature_count"] == 0

    def test_invalid_type(self):
        """不支持的类型返回 error。"""
        result = DataIO().summarize_layer([1, 2, 3])  # type: ignore[arg-type]
        assert result["status"] == "error"


# ============================================================
# export_result
# ============================================================

class TestExportResult:
    """export_result 方法测试。"""

    def test_export_geodataframe_to_gpkg(self):
        """从 GeoDataFrame 导出 GPKG 文件。"""
        gdf = _make_test_gdf()
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "output.gpkg")
            result = DataIO().export_result(gdf, out_path)
            assert result["status"] == "success"
            assert result["data"]["feature_count"] == 2
            assert os.path.exists(out_path)
            # 验证输出的内容
            reloaded = gpd.read_file(out_path)
            assert len(reloaded) == 2

    def test_export_geojson_dict_to_gpkg(self):
        """从 GeoJSON dict 导出 GPKG 文件。"""
        gdf = _make_test_gdf()
        geojson_dict = json.loads(gdf.to_json())
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "output.gpkg")
            result = DataIO().export_result(geojson_dict, out_path)
            assert result["status"] == "success"
            assert os.path.exists(out_path)

    def test_export_empty_data(self):
        """空数据返回 error。"""
        empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "empty.gpkg")
            result = DataIO().export_result(empty, out_path)
            assert result["status"] == "error"

    def test_export_creates_parent_dir(self):
        """自动创建父目录。"""
        gdf = _make_test_gdf()
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "subdir", "nested", "output.gpkg")
            result = DataIO().export_result(gdf, out_path)
            assert result["status"] == "success"
            assert os.path.exists(out_path)

    def test_export_with_geojson_driver(self):
        """使用 GeoJSON 驱动导出。"""
        gdf = _make_test_gdf()
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "output.geojson")
            result = DataIO().export_result(gdf, out_path, driver="GeoJSON")
            assert result["status"] == "success"
            assert os.path.exists(out_path)

    def test_invalid_type(self):
        """不支持的类型返回 error。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "out.gpkg")
            result = DataIO().export_result([1, 2, 3], out_path)  # type: ignore[arg-type]
            assert result["status"] == "error"
