"""Data I/O 端到端集成测试。

测试 load_vector → summarize_layer → csv_to_points → export_result 完整链路。
所有文件用 tempfile 创建，不依赖磁盘固定文件。
"""

import io
import json
import os
import tempfile
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point

from app.tools.data_io import DataIO
from app.tools.geo_transform import wgs84_to_gcj02


# ============================================================
# 测试基准点：南京新街口附近 (118.7782, 32.0417)
# ============================================================

_NANJING_WGS84 = (118.7782, 32.0417)
_NANJING_GCJ02 = wgs84_to_gcj02(*_NANJING_WGS84)


# ============================================================
# 辅助函数
# ============================================================

def _make_geojson_file(features, temp_dir):
    """创建临时 GeoJSON 文件，返回路径。"""
    path = os.path.join(temp_dir, "test.geojson")
    data = {"type": "FeatureCollection", "features": features}
    Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _make_shp_zip_from_gdf(gdf, temp_dir):
    """从 GeoDataFrame 创建临时 shapefile ZIP，返回路径。"""
    # 先写临时 .shp
    import os as _os
    shp_path = _os.path.join(temp_dir, "temp.shp")
    gdf.to_file(shp_path, driver="ESRI Shapefile", encoding="utf-8")
    base = shp_path.replace(".shp", "")

    zip_path = _os.path.join(temp_dir, "data.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
            p = base + ext
            if os.path.exists(p):
                zf.write(p, arcname="data" + ext)
    return zip_path


def _make_csv_file(df, temp_dir, encoding="utf-8"):
    """创建临时 CSV 文件，返回路径。"""
    path = os.path.join(temp_dir, "data.csv")
    df.to_csv(path, index=False, encoding=encoding)
    return path


def _sample_features():
    """构造南京新街口附近的测试点要素。"""
    points = [
        (118.7782, 32.0417),
        (118.7792, 32.0427),
        (118.7772, 32.0407),
        (118.7802, 32.0402),
        (118.7762, 32.0432),
    ]
    names = ["新街口中心", "德基广场", "大洋百货", "新百", "金陵饭店"]

    features = []
    for (lng, lat), name in zip(points, names):
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lng, lat]},
            "properties": {
                "name": name,
                "region": "鼓楼区" if lng < 118.7782 else "玄武区",
                "value": lng * 10 + lat,
            },
        })
    return features


def _sample_df():
    """构造与 _sample_features 对应的 DataFrame。"""
    return pd.DataFrame({
        "lng": [118.7782, 118.7792, 118.7772, 118.7802, 118.7762],
        "lat": [32.0417, 32.0427, 32.0407, 32.0402, 32.0432],
        "name": ["新街口中心", "德基广场", "大洋百货", "新百", "金陵饭店"],
        "region": ["鼓楼区", "玄武区", "鼓楼区", "玄武区", "鼓楼区"],
    })


# ============================================================
# 核心链路测试
# ============================================================

class TestLoadVector:
    """load_vector 完整流程。"""

    def test_load_vector_geojson(self):
        """load_vector 读取 GeoJSON 文件。"""
        dio = DataIO()
        features = _sample_features()

        with tempfile.TemporaryDirectory() as tmp:
            geojson_path = _make_geojson_file(features, tmp)
            result = dio.load_vector(geojson_path)

        assert result["status"] == "success"
        assert result["data"]["type"] == "FeatureCollection"
        assert len(result["data"]["features"]) == 5

        meta = result["metadata"]
        assert meta["feature_count"] == 5
        assert meta["geometry_type"] == "Point"
        assert "name" in meta["fields"]
        assert meta["bbox"] is not None
        assert len(meta["bbox"]) == 4

    def test_load_vector_metadata(self):
        """load_vector 返回的 metadata 包含必要字段。"""
        dio = DataIO()
        features = _sample_features()

        with tempfile.TemporaryDirectory() as tmp:
            geojson_path = _make_geojson_file(features, tmp)
            result = dio.load_vector(geojson_path)

        meta = result["metadata"]
        required_keys = {"crs", "geometry_type", "feature_count", "fields", "bbox"}
        missing = required_keys - set(meta.keys())
        assert len(missing) == 0, f"Missing metadata keys: {missing}"

    def test_load_vector_empty_geojson(self):
        """load_vector 空 GeoJSON 返回 feature_count=0。"""
        dio = DataIO()

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "empty.geojson")
            Path(path).write_text(
                json.dumps({"type": "FeatureCollection", "features": []}),
                encoding="utf-8",
            )
            result = dio.load_vector(path)

        assert result["status"] == "success"
        assert result["metadata"]["feature_count"] == 0
        assert result["data"]["features"] == []

    def test_load_vector_nonexistent_file_error(self):
        """load_vector 不存在的文件返回 error。"""
        dio = DataIO()
        result = dio.load_vector("/nonexistent/path/file.geojson")
        assert result["status"] == "error"
        assert "message" in result

    def test_load_vector_shp_zip(self):
        """load_vector 读取 shapefile ZIP。"""
        dio = DataIO()
        gdf = gpd.GeoDataFrame(
            {"name": ["A", "B"]},
            geometry=[Point(118.7782, 32.0417), Point(118.7800, 32.0420)],
            crs="EPSG:4326",
        )

        with tempfile.TemporaryDirectory() as tmp:
            zip_path = _make_shp_zip_from_gdf(gdf, tmp)
            result = dio.load_vector(zip_path)

        assert result["status"] == "success"
        assert result["metadata"]["feature_count"] == 2


class TestSummarizeLayer:
    """summarize_layer 完整流程。"""

    def test_summarize_from_load_vector_output(self):
        """从 load_vector 输出调用 summarize_layer。"""
        dio = DataIO()
        features = _sample_features()

        with tempfile.TemporaryDirectory() as tmp:
            geojson_path = _make_geojson_file(features, tmp)
            loaded = dio.load_vector(geojson_path)
            summary = dio.summarize_layer(loaded["data"])

        assert summary["status"] == "success"
        sm = summary["data"]
        assert sm["feature_count"] == 5
        assert sm["geometry_type"] == "Point"
        assert sm["bbox"] is not None

    def test_summarize_from_gdf(self):
        """summarize_layer 直接从 GeoDataFrame 提取元数据。"""
        dio = DataIO()
        gdf = gpd.GeoDataFrame(
            {"name": ["A", "B", "C"]},
            geometry=[Point(118.7782, 32.0417), Point(118.78, 32.042), Point(118.775, 32.04)],
            crs="EPSG:4326",
        )

        result = dio.summarize_layer(gdf)
        assert result["status"] == "success"
        assert result["data"]["feature_count"] == 3
        assert "name" in result["data"]["fields"]

    def test_summarize_layer_wrong_type_error(self):
        """不支持的输入类型返回 error。"""
        dio = DataIO()
        result = dio.summarize_layer("not_a_gdf")
        assert result["status"] == "error"


class TestCSVToPoints:
    """csv_to_points 完整流程。"""

    def test_csv_to_points_from_dataframe(self):
        """DataFrame → csv_to_points → 验证 GeoJSON 结构。"""
        dio = DataIO()
        df = _sample_df()

        result = dio.csv_to_points(df, x_field="lng", y_field="lat")
        assert result["status"] == "success"

        data = result["data"]
        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) == 5

        # 验证坐标
        first = data["features"][0]
        assert first["geometry"]["type"] == "Point"
        assert first["geometry"]["coordinates"] == [118.7782, 32.0417]
        assert first["properties"]["name"] == "新街口中心"

    def test_csv_to_points_from_file_path(self):
        """CSV 文件路径 → csv_to_points。"""
        dio = DataIO()
        df = _sample_df()

        with tempfile.TemporaryDirectory() as tmp:
            csv_path = _make_csv_file(df, tmp)
            result = dio.csv_to_points(csv_path, x_field="lng", y_field="lat")

        assert result["status"] == "success"
        assert len(result["data"]["features"]) == 5

    def test_csv_to_points_from_dict(self):
        """load_csv 结果 dict → csv_to_points。"""
        dio = DataIO()
        result = dio.csv_to_points(
            {"data": {"sample": _sample_df().to_dict(orient="records")}},
            x_field="lng",
            y_field="lat",
        )
        assert result["status"] == "success"
        assert len(result["data"]["features"]) == 5

    def test_csv_to_points_with_custom_crs(self):
        """自定义 CRS 参数。"""
        dio = DataIO()
        df = _sample_df()

        result = dio.csv_to_points(df, x_field="lng", y_field="lat", crs="EPSG:4326")
        assert result["status"] == "success"
        # GeoJSON 输出每个 feature 不含 crs（在顶层）
        data = result["data"]
        out_crs = data.get("crs", None)
        if out_crs:
            assert "4326" in str(out_crs) or "EPSG" in str(out_crs)

    def test_csv_to_points_missing_critical_field(self):
        """缺失 x_field → error。"""
        dio = DataIO()
        df = _sample_df()

        result = dio.csv_to_points(df, x_field="longitude", y_field="lat")
        assert result["status"] == "error"
        assert "longitude" in result["message"]

    def test_csv_to_points_empty_dataframe(self):
        """空 DataFrame → error。"""
        dio = DataIO()
        df = pd.DataFrame({"lng": [], "lat": []})

        result = dio.csv_to_points(df, x_field="lng", y_field="lat")
        assert result["status"] == "error"


class TestExportResult:
    """export_result 完整流程。"""

    def test_export_from_gdf_to_geopackage(self):
        """GeoDataFrame → export_result → 验证文件存在且有内容。"""
        dio = DataIO()
        gdf = gpd.GeoDataFrame(
            {"name": ["A", "B"]},
            geometry=[Point(118.7782, 32.0417), Point(118.7800, 32.0420)],
            crs="EPSG:4326",
        )

        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "output.gpkg")
            result = dio.export_result(gdf, out_path, driver="GPKG")

            assert result["status"] == "success"
            assert result["data"]["feature_count"] == 2
            assert os.path.exists(out_path)
            assert os.path.getsize(out_path) > 0

    def test_export_from_geojson_dict(self):
        """GeoJSON dict → export_result。"""
        dio = DataIO()
        features = _sample_features()
        geojson_dict = {"type": "FeatureCollection", "features": features[:3]}  # 3 features

        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "output.geojson")
            result = dio.export_result(geojson_dict, out_path, driver="GeoJSON")

            assert result["status"] == "success"
            assert result["data"]["feature_count"] == 3
            assert os.path.exists(out_path)
            # 验证可读回
            back = gpd.read_file(out_path)
            assert len(back) == 3

    def test_export_result_creates_parent_dirs(self):
        """export_result 自动创建父目录。"""
        dio = DataIO()
        gdf = gpd.GeoDataFrame(
            {"name": ["A"]},
            geometry=[Point(118.7782, 32.0417)],
            crs="EPSG:4326",
        )

        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "deeply", "nested", "dir", "output.gpkg")
            result = dio.export_result(gdf, out_path, driver="GPKG")

            assert result["status"] == "success"
            assert os.path.exists(out_path)

    def test_export_empty_gdf_error(self):
        """空 GeoDataFrame → error。"""
        dio = DataIO()
        empty_gdf = gpd.GeoDataFrame({"geometry": []}, crs="EPSG:4326")

        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "output.gpkg")
            result = dio.export_result(empty_gdf, out_path, driver="GPKG")

            assert result["status"] == "error"
            assert "空" in result["message"]

    def test_export_roundtrip_preserves_data(self):
        """export → load_vector 往返验证数据完整性。"""
        dio = DataIO()
        features = _sample_features()

        with tempfile.TemporaryDirectory() as tmp:
            # Step 1: load_vector
            geojson_path = _make_geojson_file(features, tmp)
            loaded = dio.load_vector(geojson_path)

            # Step 2: export_result
            out_path = os.path.join(tmp, "exported.gpkg")
            dio.export_result(loaded["data"], out_path, driver="GPKG")

            # Step 3: load_vector 再读回
            reloaded = dio.load_vector(out_path)
            assert reloaded["status"] == "success"
            assert reloaded["metadata"]["feature_count"] == 5


class TestDataIOExport:
    """DataIO.export 方法集成验证。"""

    def test_export_to_geojson(self):
        """export geojson 返回正确 bytes。"""
        dio = DataIO()
        gdf = gpd.GeoDataFrame(
            {"name": ["A", "B"]},
            geometry=[Point(118.7782, 32.0417), Point(118.7800, 32.0420)],
            crs="EPSG:4326",
        )

        data = dio.export(gdf, fmt="geojson")
        assert isinstance(data, bytes)
        parsed = json.loads(data.decode("utf-8"))
        assert parsed["type"] == "FeatureCollection"
        assert len(parsed["features"]) == 2

    def test_export_to_shp(self):
        """export shp 返回 ZIP bytes。"""
        dio = DataIO()
        gdf = gpd.GeoDataFrame(
            {"name": ["A"]},
            geometry=[Point(118.7782, 32.0417)],
            crs="EPSG:4326",
        )

        data = dio.export(gdf, fmt="shp")
        assert isinstance(data, bytes)
        # 验证是合法 ZIP
        buf = io.BytesIO(data)
        assert zipfile.is_zipfile(buf)
        with zipfile.ZipFile(buf) as zf:
            names = zf.namelist()
            assert any(n.endswith(".shp") for n in names)

    def test_export_to_kml(self):
        """export kml 返回 XML bytes。"""
        dio = DataIO()
        gdf = gpd.GeoDataFrame(
            {"name": ["A"]},
            geometry=[Point(118.7782, 32.0417)],
            crs="EPSG:4326",
        )

        data = dio.export(gdf, fmt="kml")
        assert isinstance(data, bytes)
        text = data.decode("utf-8")
        assert "kml" in text.lower() or "KML" in text
        assert "A" in text

    def test_export_unsupported_format_raises(self):
        """不支持的格式抛出异常。"""
        dio = DataIO()
        gdf = gpd.GeoDataFrame(
            {"name": ["A"]},
            geometry=[Point(118.7782, 32.0417)],
            crs="EPSG:4326",
        )

        with pytest.raises(ValueError, match="不支持的导出格式"):
            dio.export(gdf, fmt="pdf")

    def test_export_empty_gdf_geojson(self):
        """空 GDF export geojson 返回空 FeatureCollection。"""
        dio = DataIO()
        empty_gdf = gpd.GeoDataFrame({"geometry": []}, crs="EPSG:4326")

        data = dio.export(empty_gdf, fmt="geojson")
        parsed = json.loads(data.decode("utf-8"))
        assert parsed["type"] == "FeatureCollection"
        assert parsed["features"] == []

    def test_export_empty_gdf_shp_raises(self):
        """空 GDF 不应 export 为 shp。"""
        dio = DataIO()
        empty_gdf = gpd.GeoDataFrame({"geometry": []}, crs="EPSG:4326")

        with pytest.raises(ValueError, match="空数据"):
            dio.export(empty_gdf, fmt="shp")


# ============================================================
# 完整端到端链路
# ============================================================

class TestFullE2EChain:
    """load_vector → summarize → csv_to_points → export 完整链路。"""

    def test_full_chain_geojson_input(self):
        """GeoJSON 输入 → load → summarize → export 验证。"""
        dio = DataIO()
        features = _sample_features()

        with tempfile.TemporaryDirectory() as tmp:
            # Step 1: load_vector
            in_path = _make_geojson_file(features, tmp)
            loaded = dio.load_vector(in_path)
            assert loaded["status"] == "success"

            # Step 2: summarize_layer
            summary = dio.summarize_layer(loaded["data"])
            assert summary["status"] == "success"
            assert summary["data"]["feature_count"] == 5

            # Step 3: export_result
            out_path = os.path.join(tmp, "final.gpkg")
            result = dio.export_result(loaded["data"], out_path, driver="GPKG")
            assert result["status"] == "success"
            assert result["data"]["feature_count"] == 5

            # Step 4: verify roundtrip
            reloaded = dio.load_vector(out_path)
            assert reloaded["status"] == "success"
            assert reloaded["metadata"]["feature_count"] == 5

    def test_full_chain_csv_input(self):
        """CSV 输入 → csv_to_points → summarize → export 验证。"""
        dio = DataIO()
        df = _sample_df()

        with tempfile.TemporaryDirectory() as tmp:
            # Step 1: csv_to_points
            points_result = dio.csv_to_points(df, x_field="lng", y_field="lat")
            assert points_result["status"] == "success"

            # Step 2: summarize_layer
            summary = dio.summarize_layer(points_result["data"])
            assert summary["status"] == "success"
            assert summary["data"]["feature_count"] == 5

            # Step 3: export_result
            out_path = os.path.join(tmp, "from_csv.gpkg")
            export_result = dio.export_result(points_result["data"], out_path, driver="GPKG")
            assert export_result["status"] == "success"

            # Step 4: verify roundtrip
            reloaded = dio.load_vector(out_path)
            assert reloaded["status"] == "success"
            assert reloaded["metadata"]["feature_count"] == 5

    def test_full_chain_with_encoding_detection(self):
        """GBK 编码 CSV → csv_to_points → 验证（pandas 自动检测编码）。"""
        dio = DataIO()

        # 构造含中文字段的 CSV（UTF-8 with BOM 可被 pandas 自动识别）
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = os.path.join(tmp, "cn_data.csv")
            df = pd.DataFrame({
                "lng": [118.7782, 118.7800],
                "lat": [32.0417, 32.0420],
                "name": ["新街口", "玄武湖"],
            })
            df.to_csv(csv_path, index=False, encoding="utf-8")

            result = dio.csv_to_points(csv_path, x_field="lng", y_field="lat")
            assert result["status"] == "success"
            assert len(result["data"]["features"]) == 2

            # 验证中文属性
            names = [f["properties"]["name"] for f in result["data"]["features"]]
            assert "新街口" in names
            assert "玄武湖" in names

    def test_full_chain_read_upload_geojson(self):
        """read_upload GeoJSON bytes → summarize → export。"""
        dio = DataIO()
        features = _sample_features()
        geojson_bytes = json.dumps(
            {"type": "FeatureCollection", "features": features}, ensure_ascii=False
        ).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            # Step 1: read_upload
            upload_result = dio.read_upload(geojson_bytes, "data.geojson")
            assert upload_result["status"] == "ok"
            assert upload_result["feature_count"] == 5

            # Step 2: summarize
            summary = dio.summarize_layer(upload_result["data"])
            assert summary["status"] == "success"

            # Step 3: export
            out_path = os.path.join(tmp, "upload_export.gpkg")
            export_result = dio.export_result(upload_result["data"], out_path, driver="GPKG")
            assert export_result["status"] == "success"

    def test_full_chain_read_upload_shp_zip(self):
        """read_upload shapefile ZIP → summarize → export。"""
        dio = DataIO()
        gdf = gpd.GeoDataFrame(
            {"name": ["测试点1", "测试点2"]},
            geometry=[Point(118.7782, 32.0417), Point(118.7800, 32.0420)],
            crs="EPSG:4326",
        )

        with tempfile.TemporaryDirectory() as tmp:
            zip_path = _make_shp_zip_from_gdf(gdf, tmp)
            zip_bytes = Path(zip_path).read_bytes()

            # Step 1: read_upload
            upload_result = dio.read_upload(zip_bytes, "data.zip")
            assert upload_result["status"] == "ok"
            assert upload_result["feature_count"] >= 2

            # Step 2: summarize
            summary = dio.summarize_layer(upload_result["data"])
            assert summary["status"] == "success"

            # Step 3: export
            out_path = os.path.join(tmp, "shp_export.gpkg")
            export_result = dio.export_result(upload_result["data"], out_path, driver="GPKG")
            assert export_result["status"] == "success"
