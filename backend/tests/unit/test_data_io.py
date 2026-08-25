"""data_io 单元测试（TDD）。

覆盖维度（参考 GIS_Agent_技术文档.md §4.5 + §8.4 + docs/06_security.md §3）：
1. read_upload geojson 成功（UTF-8）
2. read_upload geojson GBK 降级成功
3. read_upload shp ZIP UTF-8 成功
4. read_upload shp ZIP GBK 成功
5. read_upload shp 带 .cpg 声明优先
6. read_upload 无 .prj 启发式识别
7. read_upload 所有编码失败返回 error
8. read_upload 拒绝不支持类型
9. export geojson 格式正确
10. _is_china_data 国内/国外判断
11. _to_gcj02 坐标偏转

测试 shp ZIP 全部动态生成（BytesIO / 临时文件），不依赖外部 fixtures。
"""

import io
import json
import os
import tempfile
import zipfile

import geopandas as gpd
import pytest
from shapely.geometry import Point

from app.tools.data_io import DataIO


# ============================================================
# 测试辅助：动态生成测试数据
# ============================================================

def _make_geojson_bytes(features: list, encoding: str = "utf-8") -> bytes:
    """构造 GeoJSON FeatureCollection 字节流。"""
    data = {"type": "FeatureCollection", "features": features}
    text = json.dumps(data, ensure_ascii=False)
    return text.encode(encoding)


def _make_china_point_feature(lng: float, lat: float, name: str) -> dict:
    """构造单个 Point Feature（中文 name 属性）。"""
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lng, lat]},
        "properties": {"name": name},
    }


def _make_shp_zip(
    gdf: gpd.GeoDataFrame,
    encoding: str = "utf-8",
    include_cpg: bool = False,
    include_prj: bool = True,
    cpg_content: str = "",
) -> bytes:
    """将 GeoDataFrame 写成 shapefile ZIP 包（内存）。

    Args:
        gdf: 要写入的地理数据
        encoding: fiona 写入编码（控制 .dbf 字符编码）
        include_cpg: 是否写入 .cpg 编码声明文件
        include_prj: 是否写入 .prj 投影文件
        cpg_content: .cpg 文件内容（如 "UTF-8" / "GBK"）
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".shp", delete=False)
    tmp_path = tmp.name
    tmp.close()

    try:
        # geopandas 1.x：to_file 的 encoding 参数传给 fiona
        gdf.to_file(tmp_path, driver="ESRI Shapefile", encoding=encoding)

        base = os.path.splitext(tmp_path)[0]
        exts = [".shp", ".shx", ".dbf"]
        if include_prj:
            exts.append(".prj")
        # cpg 由 fiona 自动生成（若 encoding 指定），但测试需要可控
        cpg_auto = base + ".cpg"
        if os.path.exists(cpg_auto):
            os.remove(cpg_auto)
        if include_cpg:
            with open(base + ".cpg", "w", encoding="ascii") as f:
                f.write(cpg_content or encoding)
            exts.append(".cpg")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for ext in exts:
                p = base + ext
                if os.path.exists(p):
                    zf.write(p, arcname="data" + ext)
        return buf.getvalue()
    finally:
        base = os.path.splitext(tmp_path)[0]
        for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
            p = base + ext
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


def _make_china_gdf() -> gpd.GeoDataFrame:
    """构造国内点数据 GeoDataFrame（WGS84，含中文属性）。"""
    gdf = gpd.GeoDataFrame(
        {"name": ["天安门", "王府井"]},
        geometry=[Point(116.3912, 39.9075), Point(116.4100, 39.9150)],
        crs="EPSG:4326",
    )
    return gdf


def _make_foreign_gdf() -> gpd.GeoDataFrame:
    """构造国外点数据 GeoDataFrame（WGS84，纽约）。"""
    gdf = gpd.GeoDataFrame(
        {"name": ["Times Square"]},
        geometry=[Point(-73.9855, 40.7580)],
        crs="EPSG:4326",
    )
    return gdf


# ============================================================
# 1. read_upload geojson 成功（UTF-8）
# ============================================================

def test_read_upload_geojson_utf8_success():
    """UTF-8 编码的 GeoJSON 正常解析，返回 GeoDataFrame。"""
    feat = _make_china_point_feature(116.3912, 39.9075, "天安门")
    raw = _make_geojson_bytes([feat], encoding="utf-8")

    result = DataIO().read_upload(raw, "test.geojson")

    assert result["status"] == "ok"
    assert result["feature_count"] == 1
    assert result["geometry_type"] == "Point"
    assert "data" in result
    assert isinstance(result["data"], gpd.GeoDataFrame)


# ============================================================
# 2. read_upload geojson GBK 降级成功
# ============================================================

def test_read_upload_geojson_gbk_fallback():
    """GBK 编码的 GeoJSON 通过降级机制成功解析。"""
    feat = _make_china_point_feature(116.3912, 39.9075, "天安门")
    raw = _make_geojson_bytes([feat], encoding="gbk")

    result = DataIO().read_upload(raw, "test.geojson")

    assert result["status"] == "ok"
    assert result["feature_count"] == 1
    # 中文属性应正确还原（不乱码）
    assert result["data"].iloc[0]["name"] == "天安门"


# ============================================================
# 3. read_upload shp ZIP UTF-8 成功
# ============================================================

def test_read_upload_shp_zip_utf8_success():
    """UTF-8 编码的 shp ZIP 包正常解析。"""
    gdf = _make_china_gdf()
    zip_bytes = _make_shp_zip(gdf, encoding="utf-8")

    result = DataIO().read_upload(zip_bytes, "test.zip")

    assert result["status"] == "ok"
    assert result["feature_count"] == 2
    assert result["geometry_type"] == "Point"


# ============================================================
# 4. read_upload shp ZIP GBK 成功
# ============================================================

def test_read_upload_shp_zip_gbk_success():
    """GBK 编码的 shp ZIP 包通过 chardet/轮询机制成功解析中文属性。"""
    gdf = _make_china_gdf()
    # 不写 .cpg，强制走 chardet 探测 + 轮询
    zip_bytes = _make_shp_zip(gdf, encoding="gbk", include_cpg=False)

    result = DataIO().read_upload(zip_bytes, "test.zip")

    assert result["status"] == "ok"
    assert result["feature_count"] == 2
    # 关键：中文属性不乱码
    names = list(result["data"]["name"])
    assert "天安门" in names
    assert "王府井" in names


# ============================================================
# 5. read_upload shp 带 .cpg 声明优先
# ============================================================

def test_read_upload_shp_cpg_priority():
    """.cpg 编码声明优先于 chardet 探测。"""
    gdf = _make_china_gdf()
    # 写 GBK 数据 + 显式 .cpg 声明 GBK
    zip_bytes = _make_shp_zip(
        gdf, encoding="gbk", include_cpg=True, cpg_content="GBK"
    )

    result = DataIO().read_upload(zip_bytes, "test.zip")

    assert result["status"] == "ok"
    names = list(result["data"]["name"])
    assert "天安门" in names


# ============================================================
# 6. read_upload 无 .prj 启发式识别
# ============================================================

def test_read_upload_shp_no_prj_heuristic():
    """无 .prj 时通过启发式识别坐标系（默认 WGS84）。"""
    gdf = _make_china_gdf()
    # 不写 .prj，数据坐标在经纬度范围内 -> 启发式识别为 EPSG:4326
    zip_bytes = _make_shp_zip(gdf, encoding="utf-8", include_prj=False)

    result = DataIO().read_upload(zip_bytes, "test.zip")

    assert result["status"] == "ok"
    assert result["feature_count"] == 2
    # 无 .prj 时应有 warning 提示
    assert any("prj" in w.lower() or "坐标系" in w for w in result["warnings"])


# ============================================================
# 7. read_upload 所有编码失败返回 error
# ============================================================

def test_read_upload_shp_all_encodings_fail():
    """所有编码尝试均失败时返回 status=error，不抛异常。"""
    # 构造一个损坏的 ZIP：合法 ZIP 结构但 .shp/.dbf 内容是乱码
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("data.shp", b"\x00\x01\x02garbage")
        zf.writestr("data.shx", b"\x00\x01\x02garbage")
        zf.writestr("data.dbf", b"\x00\x01\x02garbage")
    raw = buf.getvalue()

    result = DataIO().read_upload(raw, "broken.zip")

    assert result["status"] == "error"
    assert "编码" in result["message"] or "解析" in result["message"]
    assert "encodings_tried" in result


# ============================================================
# 8. read_upload 拒绝不支持类型
# ============================================================

def test_read_upload_reject_unsupported_type():
    """不支持文件类型返回 status=error。"""
    result = DataIO().read_upload(b"some content", "data.csv")

    assert result["status"] == "error"
    assert "不支持" in result["message"] or "类型" in result["message"]


# ============================================================
# 9. export geojson 格式正确
# ============================================================

def test_export_geojson_format():
    """export(fmt='geojson') 返回合法 GeoJSON bytes。"""
    gdf = _make_china_gdf()
    out = DataIO().export(gdf, fmt="geojson")

    assert isinstance(out, (bytes, str))
    if isinstance(out, bytes):
        text = out.decode("utf-8")
    else:
        text = out
    data = json.loads(text)
    assert data["type"] == "FeatureCollection"
    assert len(data["features"]) == 2


# ============================================================
# 10. _is_china_data 国内/国外判断
# ============================================================

def test_is_china_data_domestic():
    """国内坐标判断为 True。"""
    gdf = _make_china_gdf()
    assert DataIO()._is_china_data(gdf) is True


def test_is_china_data_foreign():
    """国外坐标判断为 False。"""
    gdf = _make_foreign_gdf()
    assert DataIO()._is_china_data(gdf) is False


# ============================================================
# 11. _to_gcj02 坐标偏转
# ============================================================

def test_to_gcj02_offsets_coordinates():
    """国内 WGS84 坐标经 _to_gcj02 后发生偏转（与原坐标不同）。"""
    from app.tools.geo_transform import wgs84_to_gcj02

    gdf = _make_china_gdf()
    original = gdf.geometry.iloc[0]
    transformed = DataIO()._to_gcj02(gdf)

    # 坐标应偏转
    t = transformed.geometry.iloc[0]
    expected = wgs84_to_gcj02(original.x, original.y)
    assert abs(t.x - expected[0]) < 1e-9
    assert abs(t.y - expected[1]) < 1e-9
    # 偏转量应非零（北京 GCJ02 偏移约几百米），且 x、y 均应发生偏移
    assert abs(t.x - original.x) > 0.001 and abs(t.y - original.y) > 0.001


def test_to_gcj02_foreign_no_offset():
    """国外坐标经 _to_gcj02 不偏转（out_of_china）。"""
    gdf = _make_foreign_gdf()
    original = gdf.geometry.iloc[0]
    transformed = DataIO()._to_gcj02(gdf)
    t = transformed.geometry.iloc[0]
    assert abs(t.x - original.x) < 1e-12
    assert abs(t.y - original.y) < 1e-12


# ============================================================
# 补充：read_upload 国内数据自动转 GCJ02
# ============================================================

def test_read_upload_china_data_converted_to_gcj02():
    """国内数据 read_upload 后 crs 标注为 GCJ02 且坐标已偏转。"""
    feat = _make_china_point_feature(116.3912, 39.9075, "天安门")
    raw = _make_geojson_bytes([feat], encoding="utf-8")

    result = DataIO().read_upload(raw, "test.geojson")

    assert result["status"] == "ok"
    assert result["crs"] == "GCJ02"
    # The numeric coordinates are GCJ02 even though GeoPandas retains EPSG:4326
    # for interoperability. Preserve the semantic label for downstream tools.
    assert result["data"].attrs["crs_label"] == "GCJ02"
    # 坐标应与原 WGS84 不同（已偏转）
    coord = result["data"].geometry.iloc[0].x
    assert abs(coord - 116.3912) > 0.001


def test_read_upload_foreign_data_keeps_wgs84():
    """国外数据 read_upload 不转 GCJ02，crs 标注为 WGS84/EPSG:4326。"""
    feat = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-73.9855, 40.7580]},
        "properties": {"name": "Times Square"},
    }
    raw = _make_geojson_bytes([feat], encoding="utf-8")

    result = DataIO().read_upload(raw, "test.geojson")

    assert result["status"] == "ok"
    assert "GCJ02" not in result["crs"] or result["crs"] != "GCJ02"
    # 坐标不偏转
    coord = result["data"].geometry.iloc[0].x
    assert abs(coord - (-73.9855)) < 1e-9


# ============================================================
# 补充：KML 支持
# ============================================================

def test_read_upload_kml_success():
    """KML 文件可被 read_upload 解析。"""
    kml = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>天安门</name>
      <Point><coordinates>116.3912,39.9075,0</coordinates></Point>
    </Placemark>
  </Document>
</kml>"""
    raw = kml.encode("utf-8")

    result = DataIO().read_upload(raw, "test.kml")

    assert result["status"] == "ok"
    assert result["feature_count"] == 1
