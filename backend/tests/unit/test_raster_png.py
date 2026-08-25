"""Unit tests for RasterAnalyzer PNG base64 output (RasterLayer dict).

Uses rasterio.MemoryFile to construct in-memory test rasters.
"""
from __future__ import annotations

import io
import os

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app.tools.raster_analysis import RasterAnalyzer


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture
def analyzer():
    return RasterAnalyzer()


def _make_mem_raster(
    array: np.ndarray,
    crs: str = "EPSG:32650",
    transform=None,
    nodata: float | None = None,
) -> str:
    """创建临时 .tif 栅格文件，返回文件路径。"""
    import tempfile

    h, w = array.shape
    if transform is None:
        transform = from_origin(0, h * 30, 30, -30)
    profile = {
        "driver": "GTiff",
        "height": h,
        "width": w,
        "count": 1,
        "dtype": array.dtype,
        "crs": crs,
        "transform": transform,
        "nodata": nodata,
    }
    tmp = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
    tmp_path = tmp.name
    tmp.close()
    with rasterio.open(tmp_path, "w", **profile) as dst:
        dst.write(array, 1)
    return tmp_path


def _make_flat_dem(rows=100, cols=100, elevation=100.0):
    """平面 DEM：所有像元值相同。"""
    arr = np.ones((rows, cols), dtype=np.float32) * elevation
    transform = from_origin(0, rows * 30, 30, -30)
    return _make_mem_raster(arr, crs="EPSG:32650", transform=transform)


def _make_slope_dem(rows=100, cols=100, slope_pct=10.0):
    """创建有坡度的 DEM（斜率为 slope_pct 百分比，沿 x 方向）。"""
    dz_per_cell = 30.0 * slope_pct / 100.0
    arr = np.zeros((rows, cols), dtype=np.float32)
    for i in range(cols):
        arr[:, i] = i * dz_per_cell
    transform = from_origin(0, rows * 30, 30, -30)
    return _make_mem_raster(arr, crs="EPSG:32650", transform=transform)


# ------------------------------------------------------------------
# PNG output tests
# ------------------------------------------------------------------

def test_slope_png_output(analyzer):
    """平面 DEM 的 slope 返回的 RasterLayer 含 png_b64 且可解码为有效 PNG。"""
    src_path = _make_flat_dem(50, 50, 100.0)
    result = analyzer.slope(src_path, degree=True)
    assert result["status"] == "success"
    data = result["data"]
    assert "png_b64" in data
    assert isinstance(data["png_b64"], str)
    assert len(data["png_b64"]) > 0
    # 解码验证为有效 PNG
    png_bytes = __import__("base64").b64decode(data["png_b64"])
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n", "Not a valid PNG header"
    # 用 PIL 验证可打开
    from PIL import Image
    img = Image.open(io.BytesIO(png_bytes))
    assert img.mode == "RGB"
    os.unlink(src_path)
    os.unlink(data["dst_path"])


def test_aspect_png_colormap(analyzer):
    """aspect 方法返回的 RasterLayer colormap 为 'aspect'。"""
    src_path = _make_slope_dem(50, 50, slope_pct=10.0)
    result = analyzer.aspect(src_path)
    assert result["status"] == "success"
    data = result["data"]
    assert data["type"] == "raster"
    assert data["colormap"] == "aspect"
    assert data["value_kind"] == "aspect_degrees"
    os.unlink(src_path)
    os.unlink(data["dst_path"])


def test_hillshade_grayscale(analyzer):
    """hillshade 方法返回的 RasterLayer colormap 为 'grayscale'。"""
    src_path = _make_flat_dem(50, 50, 100.0)
    result = analyzer.hillshade(src_path, azimuth=315, altitude=45)
    assert result["status"] == "success"
    data = result["data"]
    assert data["type"] == "raster"
    assert data["colormap"] == "grayscale"
    assert data["value_kind"] == "hillshade"
    os.unlink(src_path)
    os.unlink(data["dst_path"])


def test_downsample_large_raster(analyzer):
    """2000x2000 DEM 输出 width/height ≤ 1024。"""
    rows, cols = 2000, 2000
    arr = np.random.rand(rows, cols).astype(np.float32) * 500
    src_path = _make_mem_raster(arr)
    result = analyzer.slope(src_path, degree=True)
    assert result["status"] == "success"
    data = result["data"]
    assert data["width"] <= 1024
    assert data["height"] <= 1024
    # 降采样后宽高比应大致保持
    aspect_in = cols / rows
    aspect_out = data["width"] / data["height"]
    assert abs(aspect_out - aspect_in) < 0.05
    os.unlink(src_path)
    os.unlink(data["dst_path"])


def test_invalid_colormap_fallback(analyzer):
    """无效 cmap 名降级到 grayscale 且不报错。"""
    arr = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32)
    rgb = analyzer._apply_colormap(arr, cmap_name="bogus")
    assert rgb.shape == (2, 2, 3)
    assert rgb.dtype == np.uint8
    # 降级到 grayscale → R=G=B
    np.testing.assert_array_equal(rgb[:, :, 0], rgb[:, :, 1])
    np.testing.assert_array_equal(rgb[:, :, 1], rgb[:, :, 2])


def test_raster_layer_has_bbox(analyzer):
    """RasterLayer bbox 为 4 个 float 值。"""
    src_path = _make_flat_dem(50, 50, 100.0)
    result = analyzer.slope(src_path, degree=True)
    assert result["status"] == "success"
    data = result["data"]
    assert "bbox" in data
    bbox = data["bbox"]
    assert isinstance(bbox, list)
    assert len(bbox) == 4
    for v in bbox:
        assert isinstance(v, float)
    # 有效边界：minx ≤ maxx
    assert bbox[0] <= bbox[2]
    # miny 不一定 < maxy（南北朝向取决于 CRS），但不为 NaN
    assert all(np.isfinite(b) for b in bbox)
    os.unlink(src_path)
    os.unlink(data["dst_path"])


def test_value_range_present(analyzer):
    """value_range 是 [min, max] 两个 float。"""
    src_path = _make_slope_dem(50, 50, slope_pct=10.0)
    result = analyzer.terrain_ruggedness_index(src_path)
    assert result["status"] == "success"
    data = result["data"]
    assert "value_range" in data
    vr = data["value_range"]
    assert isinstance(vr, list)
    assert len(vr) == 2
    assert isinstance(vr[0], float)
    assert isinstance(vr[1], float)
    assert vr[0] <= vr[1]
    os.unlink(src_path)


def test_polygonize_not_raster_layer(analyzer):
    """polygonize_raster 不返回 type=raster。"""
    arr = np.zeros((20, 20), dtype=np.uint8)
    arr[5:15, 5:15] = 1
    src_path = _make_mem_raster(arr)
    result = analyzer.polygonize_raster(src_path)
    assert result["status"] == "success"
    data = result["data"]
    # polygonize 返回 GeoDataFrame，不是 raster dict
    assert not isinstance(data, dict) or data.get("type") != "raster"
    import geopandas as gpd
    assert isinstance(data, gpd.GeoDataFrame)
    os.unlink(src_path)


def test_contour_not_raster_layer(analyzer):
    """contour 不返回 type=raster。"""
    src_path = _make_slope_dem(50, 50, slope_pct=5.0)
    result = analyzer.contour(src_path, interval=1.0)
    assert result["status"] == "success"
    data = result["data"]
    # contour 返回 GeoDataFrame，不是 raster dict
    assert not isinstance(data, dict) or data.get("type") != "raster"
    import geopandas as gpd
    assert isinstance(data, gpd.GeoDataFrame)
    os.unlink(src_path)
