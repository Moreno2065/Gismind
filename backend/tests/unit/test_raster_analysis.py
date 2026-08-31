"""Unit tests for RasterAnalyzer.

使用 rasterio.MemoryFile 构造内存栅格作为测试数据。
"""
from __future__ import annotations

import os
import tempfile

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


def _make_in_memory_raster(
    array: np.ndarray,
    crs: str = "EPSG:32650",
    transform=None,
    nodata: float | None = None,
) -> str:
    """在 rasterio MemoryFile 中创建栅格，返回文件路径。

    Args:
        array: 2D numpy 数组。
        crs: CRS 字符串。
        transform: affine transform，None 则默认左上角 (0, 3000)，30m 分辨率 north-up。
        nodata: nodata 值。

    Returns:
        内存栅格文件路径（可用于 rasterio.open）。
    """
    h, w = array.shape
    if transform is None:
        # north-up: y 范围 [0, h*30]，方便测试中的坐标 (500,1500) 等重叠
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
    # 使用 NamedTemporaryFile 而非 MemoryFile，确保路径可被 rasterstats 访问
    tmp = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
    tmp_path = tmp.name
    tmp.close()
    with rasterio.open(tmp_path, "w", **profile) as dst:
        dst.write(array, 1)
    return tmp_path


def _make_flat_dem(rows=100, cols=100, elevation=100.0):
    """平面 DEM：所有像元值相同。slope=0, aspect=-1, hillshade=255*cos(altitude)。"""
    arr = np.ones((rows, cols), dtype=np.float32) * elevation
    transform = from_origin(0, rows * 30, 30, -30)
    return _make_in_memory_raster(arr, crs="EPSG:32650", transform=transform)


def _make_slope_dem(rows=100, cols=100, slope_pct=10.0):
    """创建有坡度的 DEM。斜率为 slope_pct 百分比（沿 x 方向）。"""
    # 每 30m 像元升高 30 * slope_pct/100 米
    dz_per_cell = 30.0 * slope_pct / 100.0
    arr = np.zeros((rows, cols), dtype=np.float32)
    for i in range(cols):
        arr[:, i] = i * dz_per_cell
    transform = from_origin(0, rows * 30, 30, -30)
    return _make_in_memory_raster(arr, crs="EPSG:32650", transform=transform)


# ------------------------------------------------------------------
# reproject_raster
# ------------------------------------------------------------------

def test_reproject_raster(analyzer):
    """重投影 EPSG:32650 → EPSG:4326。"""
    src_path = _make_flat_dem(50, 50, 200.0)
    result = analyzer.reproject_raster(src_path, "EPSG:4326")
    assert result["status"] == "success"
    assert "dst_path" in result["data"]
    assert "transform" in result["data"]
    assert result["data"]["crs"] == "EPSG:4326"
    # 验证输出文件可读
    with rasterio.open(result["data"]["dst_path"]) as dst:
        assert dst.crs.to_string() == "EPSG:4326"
    os.unlink(src_path)
    os.unlink(result["data"]["dst_path"])


def test_reproject_raster_nonexistent(analyzer):
    """不存在文件应返回 error。"""
    result = analyzer.reproject_raster("/nonexistent/path.tif", "EPSG:4326")
    assert result["status"] == "error"


# ------------------------------------------------------------------
# clip_raster_by_mask
# ------------------------------------------------------------------

def test_clip_raster_by_mask(analyzer):
    """用矩形掩膜裁剪栅格。"""
    import geopandas as gpd
    from shapely.geometry import Polygon

    # 20x20 栅格, x=[100,120], y=[10,30], EPSG:4326
    arr = np.ones((20, 20), dtype=np.float32) * 200.0
    src_path = _make_in_memory_raster(arr, crs="EPSG:4326", transform=from_origin(100, 30, 1, 1))
    mask_gdf = gpd.GeoDataFrame(
        {"geometry": [Polygon([(105, 25), (115, 25), (115, 15), (105, 15)])]},
        crs="EPSG:4326",
    )
    result = analyzer.clip_raster_by_mask(src_path, mask_gdf)
    assert result["status"] == "success"
    assert "dst_path" in result["data"]
    with rasterio.open(result["data"]["dst_path"]) as dst:
        assert dst.height > 0
    os.unlink(src_path)
    os.unlink(result["data"]["dst_path"])


def test_clip_raster_by_mask_shapefile(analyzer):
    """用 shapefile 路径掩膜裁剪。"""
    import geopandas as gpd
    from shapely.geometry import Polygon

    arr = np.ones((20, 20), dtype=np.float32) * 200.0
    src_path = _make_in_memory_raster(arr, crs="EPSG:4326", transform=from_origin(100, 30, 1, 1))
    mask_gdf = gpd.GeoDataFrame(
        {"geometry": [Polygon([(105, 25), (115, 25), (115, 15), (105, 15)])]},
        crs="EPSG:4326",
    )
    tmp_shp = tempfile.NamedTemporaryFile(suffix=".shp", delete=False)
    shp_path = tmp_shp.name
    tmp_shp.close()
    mask_gdf.to_file(shp_path)

    result = analyzer.clip_raster_by_mask(src_path, shp_path)
    assert result["status"] == "success"
    os.unlink(src_path)
    os.unlink(result["data"]["dst_path"])
    for f in [shp_path, shp_path.replace(".shp", ".shx"), shp_path.replace(".shp", ".dbf"),
              shp_path.replace(".shp", ".prj"), shp_path.replace(".shp", ".cpg")]:
        if os.path.exists(f):
            os.unlink(f)


# ------------------------------------------------------------------
# clip_raster_by_extent
# ------------------------------------------------------------------

def test_clip_raster_by_extent(analyzer):
    """按 bbox 裁剪栅格。"""
    arr = np.ones((20, 20), dtype=np.float32) * 200.0
    src_path = _make_in_memory_raster(arr, crs="EPSG:4326", transform=from_origin(100, 30, 1, 1))
    # bbox: (minx, miny, maxx, maxy) in the CRS convention (south-up: miny is bottom, maxy is top)
    bbox = (105, 15, 115, 25)
    result = analyzer.clip_raster_by_extent(src_path, bbox)
    assert result["status"] == "success"
    assert "dst_path" in result["data"]
    with rasterio.open(result["data"]["dst_path"]) as dst:
        assert dst.height > 0
    os.unlink(src_path)
    os.unlink(result["data"]["dst_path"])


# ------------------------------------------------------------------
# raster_calculator
# ------------------------------------------------------------------

def test_raster_calculator_add(analyzer):
    """栅格计算器：a + b"""
    arr_a = np.ones((50, 50), dtype=np.float32) * 10
    arr_b = np.ones((50, 50), dtype=np.float32) * 5
    path_a = _make_in_memory_raster(arr_a)
    path_b = _make_in_memory_raster(arr_b)

    result = analyzer.raster_calculator({"a": path_a, "b": path_b}, "a + b")
    assert result["status"] == "success"
    with rasterio.open(result["data"]["dst_path"]) as dst:
        out_arr = dst.read(1)
    np.testing.assert_allclose(out_arr, 15.0, atol=1e-6)

    os.unlink(path_a)
    os.unlink(path_b)
    os.unlink(result["data"]["dst_path"])


def test_raster_calculator_numpy_fn(analyzer):
    """栅格计算器：np.sqrt(a)"""
    arr_a = np.ones((50, 50), dtype=np.float32) * 100
    path_a = _make_in_memory_raster(arr_a)

    result = analyzer.raster_calculator({"a": path_a}, "np.sqrt(a)")
    assert result["status"] == "success"
    with rasterio.open(result["data"]["dst_path"]) as dst:
        out_arr = dst.read(1)
    np.testing.assert_allclose(out_arr, 10.0, atol=1e-6)

    os.unlink(path_a)
    os.unlink(result["data"]["dst_path"])


def test_raster_calculator_invalid_expression(analyzer):
    """不安全的表达式应被拦截。"""
    arr_a = np.ones((50, 50), dtype=np.float32) * 10
    path_a = _make_in_memory_raster(arr_a)

    result = analyzer.raster_calculator({"a": path_a}, "__import__('os').system('echo')")
    # 被 safe_eval 拦截，因为 __import__ 不在 safe_globals 中
    assert result["status"] == "error"

    os.unlink(path_a)


# ------------------------------------------------------------------
# zonal_statistics
# ------------------------------------------------------------------

def test_zonal_statistics(analyzer):
    """分区统计：矩形区域内统计。"""
    import geopandas as gpd
    from shapely.geometry import Polygon

    arr = np.ones((20, 20), dtype=np.float32) * 50
    src_path = _make_in_memory_raster(arr, crs="EPSG:4326", transform=from_origin(100, 30, 1, 1))
    vector = gpd.GeoDataFrame(
        {"geometry": [Polygon([(105, 25), (115, 25), (115, 15), (105, 15)])]},
        crs="EPSG:4326",
    )
    result = analyzer.zonal_statistics(src_path, vector, stats=["mean", "min", "max", "sum", "std", "count"])
    assert result["status"] == "success"
    if result["data"]:
        feat = result["data"][0]
        props = feat.get("properties", feat) if isinstance(feat, dict) else {}
        mean_val = props.get("mean")
        assert mean_val is not None
        assert abs(mean_val - 50.0) < 1.0

    os.unlink(src_path)


# ------------------------------------------------------------------
# raster_sampling
# ------------------------------------------------------------------

def test_raster_sampling(analyzer):
    """采样点值。"""
    import geopandas as gpd
    from shapely.geometry import Point

    arr = np.arange(20 * 20, dtype=np.float32).reshape(20, 20)
    src_path = _make_in_memory_raster(arr, crs="EPSG:4326", transform=from_origin(100, 30, 1, 1))

    points = gpd.GeoDataFrame(
        {"geometry": [Point(105.5, 24.5), Point(115.5, 15.5)]},
        crs="EPSG:4326",
    )
    result = analyzer.raster_sampling(src_path, points)
    assert result["status"] == "success"
    assert len(result["data"]) == 2
    for sample in result["data"]:
        assert sample["value"] is not None

    os.unlink(src_path)


# ------------------------------------------------------------------
# rasterize_vector
# ------------------------------------------------------------------

def test_rasterize_vector(analyzer):
    """矢量转栅格。"""
    import geopandas as gpd
    from shapely.geometry import Polygon

    poly = Polygon([(105, 25), (115, 25), (115, 15), (105, 15)])
    gdf = gpd.GeoDataFrame({"geometry": [poly]}, crs="EPSG:4326")

    result = analyzer.rasterize_vector(
        gdf,
        out_shape=(20, 20),
        transform=from_origin(100, 30, 1, 1),
        crs="EPSG:4326",
    )
    assert result["status"] == "success"
    with rasterio.open(result["data"]["dst_path"]) as dst:
        arr = dst.read(1)
    # 检查多边形区域有值
    assert arr.sum() > 0

    os.unlink(result["data"]["dst_path"])


# ------------------------------------------------------------------
# polygonize_raster
# ------------------------------------------------------------------

def test_polygonize_raster(analyzer):
    """栅格转矢量。"""
    arr = np.zeros((50, 50), dtype=np.uint8)
    arr[10:20, 10:20] = 1
    src_path = _make_in_memory_raster(arr)

    result = analyzer.polygonize_raster(src_path)
    assert result["status"] == "success"
    gdf = result["data"]
    assert len(gdf) > 0

    os.unlink(src_path)


# ------------------------------------------------------------------
# slope
# ------------------------------------------------------------------

def test_slope_flat_terrain(analyzer):
    """平面 DEM slope=0。"""
    src_path = _make_flat_dem(50, 50, 100.0)
    result = analyzer.slope(src_path, degree=True)
    assert result["status"] == "success"
    with rasterio.open(result["data"]["dst_path"]) as dst:
        slope_arr = dst.read(1)
    # 平面 slope 应接近 0
    assert np.max(slope_arr) < 1e-4

    os.unlink(src_path)
    os.unlink(result["data"]["dst_path"])


def test_slope_known_value(analyzer):
    """已知坡度 DEM 验证。"""
    # 10% 坡度 → arctan(0.1) ≈ 5.71 度
    src_path = _make_slope_dem(50, 50, slope_pct=10.0)
    result = analyzer.slope(src_path, degree=True)
    assert result["status"] == "success"
    with rasterio.open(result["data"]["dst_path"]) as dst:
        slope_arr = dst.read(1)
    # 内部区域（不考虑边缘）应接近 5.71 度
    interior = slope_arr[5:-5, 5:-5]
    expected = np.degrees(np.arctan(0.1))
    assert abs(np.mean(interior) - expected) < 0.5

    os.unlink(src_path)
    os.unlink(result["data"]["dst_path"])


# ------------------------------------------------------------------
# aspect
# ------------------------------------------------------------------

def test_aspect_flat_terrain(analyzer):
    """平面 DEM aspect=-1。"""
    src_path = _make_flat_dem(50, 50, 100.0)
    result = analyzer.aspect(src_path)
    assert result["status"] == "success"
    with rasterio.open(result["data"]["dst_path"]) as dst:
        aspect_arr = dst.read(1)
    # 平面 aspect 应为 -1
    assert np.all(aspect_arr == -1.0)

    os.unlink(src_path)
    os.unlink(result["data"]["dst_path"])


def test_aspect_east_facing(analyzer):
    """向东倾斜（x 正方向下降）= 东向坡 (~90°)。"""
    arr = np.zeros((50, 50), dtype=np.float32)
    for i in range(50):
        arr[:, i] = (50 - i) * 10.0  # x 增加时高程下降
    src_path = _make_in_memory_raster(arr)
    result = analyzer.aspect(src_path)
    assert result["status"] == "success"
    with rasterio.open(result["data"]["dst_path"]) as dst:
        aspect_arr = dst.read(1)
    interior = aspect_arr[5:-5, 5:-5]
    # dzdx < 0（x 增加高程下降）→ 东向坡，方位角 ≈ 90
    expected = 90.0
    assert abs(np.nanmean(interior) - expected) < 15.0

    os.unlink(src_path)
    os.unlink(result["data"]["dst_path"])


# ------------------------------------------------------------------
# hillshade
# ------------------------------------------------------------------

def test_hillshade_flat_terrain(analyzer):
    """平面 DEM 山体阴影应均匀。"""
    import math  # noqa: F811

    src_path = _make_flat_dem(50, 50, 100.0)
    result = analyzer.hillshade(src_path, azimuth=315, altitude=45)
    assert result["status"] == "success"
    with rasterio.open(result["data"]["dst_path"]) as dst:
        hs_arr = dst.read(1)
    # 平面 slope=0 → hillshade = 255 * cos(altitude_rad)
    # azimuth=315, altitude=45 → cos(45°) * cos(0) + sin(45°) * sin(0) * cos(...) = cos(45°)
    expected = int(255 * math.cos(math.radians(45)))
    # 允许少许舍入误差
    assert abs(int(hs_arr[10, 10]) - expected) <= 1

    os.unlink(src_path)
    os.unlink(result["data"]["dst_path"])


def test_hillshade_accepts_float_source_nodata_for_uint8_output(analyzer):
    src_path = _make_in_memory_raster(
        np.arange(100, dtype=np.float32).reshape(10, 10),
        nodata=-9999.0,
    )

    result = analyzer.hillshade(src_path)

    assert result["status"] == "success"
    with rasterio.open(result["data"]["dst_path"]) as dataset:
        assert dataset.dtypes[0] == "uint8"
        assert dataset.nodata is None or 0 <= dataset.nodata <= 255

    os.unlink(src_path)
    os.unlink(result["data"]["dst_path"])


def test_hillshade_accepts_float_source_nodata_with_explicit_output(analyzer, tmp_path):
    src_path = _make_in_memory_raster(
        np.arange(100, dtype=np.float32).reshape(10, 10),
        nodata=-9999.0,
    )
    dst_path = tmp_path / "hillshade.tif"

    result = analyzer.hillshade(src_path, dst_path=str(dst_path))

    assert result["status"] == "success"
    with rasterio.open(dst_path) as dataset:
        assert dataset.dtypes[0] == "uint8"
        assert dataset.nodata is None or 0 <= dataset.nodata <= 255

    os.unlink(src_path)


# ------------------------------------------------------------------
# contour
# ------------------------------------------------------------------

def test_contour(analyzer):
    """生成等高线。"""
    src_path = _make_slope_dem(50, 50, slope_pct=5.0)
    result = analyzer.contour(src_path, interval=1.0)
    assert result["status"] == "success"
    gdf = result["data"]
    assert len(gdf) > 0
    assert "elevation" in gdf.columns
    assert "geometry" in gdf.columns

    os.unlink(src_path)


# ------------------------------------------------------------------
# reclassify_raster
# ------------------------------------------------------------------

def test_reclassify_range(analyzer):
    """区间重分类：bins=3, values=4。"""
    arr = np.array([[1, 3, 5], [7, 9, 11]], dtype=np.float32)
    src_path = _make_in_memory_raster(arr)
    result = analyzer.reclassify_raster(
        src_path,
        bins=[3, 6, 9],
        values=[10, 20, 30, 40],
    )
    assert result["status"] == "success"
    with rasterio.open(result["data"]["dst_path"]) as dst:
        out = dst.read(1)
    # <3 → 10, [3,6) → 20, [6,9) → 30, >=9 → 40
    expected = np.array([[10, 20, 20], [30, 40, 40]], dtype=np.float32)
    np.testing.assert_array_equal(out, expected)
    assert result["data"]["class_counts"] == {
        "10": 1,
        "20": 2,
        "30": 1,
        "40": 2,
    }

    os.unlink(src_path)
    os.unlink(result["data"]["dst_path"])


def test_reclassify_excludes_source_nodata_from_classes_and_statistics(analyzer):
    """A finite source nodata sentinel must never become a reclassified class."""
    arr = np.array([[1.0, -9999.0]], dtype=np.float32)
    src_path = _make_in_memory_raster(arr, nodata=-9999.0)
    result = analyzer.reclassify_raster(
        src_path,
        bins=[5.0],
        values=[1.0, 2.0],
    )

    assert result["status"] == "success"
    data = result["data"]
    assert data["class_counts"] == {"1": 1}
    assert data["valid_pixel_count"] == 1
    assert data["nodata_pixel_count"] == 1

    os.unlink(src_path)
    os.unlink(data["dst_path"])


def test_reclassify_range_assigns_exact_boundary_values_to_the_upper_class(analyzer):
    """15° and 30° belong to [15, 30) and [30, +∞) respectively."""
    arr = np.array([[14.999, 15.0, 29.999, 30.0]], dtype=np.float32)
    src_path = _make_in_memory_raster(arr)
    result = analyzer.reclassify_raster(
        src_path,
        bins=[15, 30],
        values=[1, 2, 3],
    )

    assert result["status"] == "success"
    with rasterio.open(result["data"]["dst_path"]) as dst:
        out = dst.read(1)
    np.testing.assert_array_equal(out, np.array([[1, 2, 2, 3]], dtype=np.float32))

    os.unlink(src_path)
    os.unlink(result["data"]["dst_path"])


def test_reclassify_range_snaps_float_noise_at_authored_boundaries(analyzer):
    """Slope round-off near 15°/30° must snap into the authored upper bins."""
    arr = np.array([[14.9999, 14.99999, 29.9999, 29.99999]], dtype=np.float32)
    src_path = _make_in_memory_raster(arr)
    result = analyzer.reclassify_raster(
        src_path,
        bins=[15, 30],
        values=[1, 2, 3],
    )

    assert result["status"] == "success"
    with rasterio.open(result["data"]["dst_path"]) as dst:
        out = dst.read(1)
    np.testing.assert_array_equal(out, np.array([[1, 2, 3, 3]], dtype=np.float32))

    os.unlink(src_path)
    os.unlink(result["data"]["dst_path"])


def test_reclassify_value_replace(analyzer):
    """逐值替换：bins=3, values=3。"""
    arr = np.array([[1, 2, 3], [1, 2, 3]], dtype=np.float32)
    src_path = _make_in_memory_raster(arr)
    result = analyzer.reclassify_raster(
        src_path,
        bins=[1, 2, 3],
        values=[100, 200, 300],
    )
    assert result["status"] == "success"
    with rasterio.open(result["data"]["dst_path"]) as dst:
        out = dst.read(1)
    expected = np.array([[100, 200, 300], [100, 200, 300]], dtype=np.float32)
    np.testing.assert_array_equal(out, expected)

    os.unlink(src_path)
    os.unlink(result["data"]["dst_path"])


def test_reclassify_bad_lengths(analyzer):
    """bins/values 长度不匹配应返回 error。"""
    arr = np.ones((10, 10), dtype=np.float32)
    src_path = _make_in_memory_raster(arr)
    result = analyzer.reclassify_raster(src_path, bins=[1, 2], values=[10])
    assert result["status"] == "error"

    os.unlink(src_path)


# ------------------------------------------------------------------
# terrain_ruggedness_index
# ------------------------------------------------------------------

def test_terrain_ruggedness_index_flat(analyzer):
    """平面 DEM TRI=0。"""
    src_path = _make_flat_dem(50, 50, 100.0)
    result = analyzer.terrain_ruggedness_index(src_path)
    assert result["status"] == "success"
    data = result["data"]
    assert data["type"] == "raster"
    assert data["value_kind"] == "tri"
    # 平面 TRI 应接近 0
    assert abs(data["value_range"][0]) < 1e-3
    assert abs(data["value_range"][1]) < 1e-3

    os.unlink(src_path)


def test_terrain_ruggedness_index_nonzero(analyzer):
    """非平面 DEM TRI > 0。"""
    # 随机噪声
    np.random.seed(42)
    arr = np.random.rand(50, 50).astype(np.float32) * 100
    src_path = _make_in_memory_raster(arr)
    result = analyzer.terrain_ruggedness_index(src_path)
    assert result["status"] == "success"
    data = result["data"]
    assert data["type"] == "raster"
    assert data["value_kind"] == "tri"
    # 非平面 TRI value_range 应 > 0
    assert data["value_range"][1] > 0

    os.unlink(src_path)


# ------------------------------------------------------------------
# topographic_position_index
# ------------------------------------------------------------------

def test_topographic_position_index_single_peak(analyzer):
    """中心高峰 TPI > 0，边缘低洼 TPI < 0。"""
    arr = np.zeros((20, 20), dtype=np.float32)
    arr[8:12, 8:12] = 100.0  # 中心 4x4 高峰
    src_path = _make_in_memory_raster(arr)
    result = analyzer.topographic_position_index(src_path, radius=1)
    assert result["status"] == "success"
    data = result["data"]
    assert data["type"] == "raster"
    assert data["value_kind"] == "tpi"
    # 高峰区域应产生正 TPI 和负 TPI 变化
    # TPI 可正可负，value_range 可包含正负
    assert data["value_range"][1] > 0  # 峰值 >0
    assert data["value_range"][0] < 0  # 低洼 <0

    os.unlink(src_path)


# ------------------------------------------------------------------
# roughness
# ------------------------------------------------------------------

def test_roughness_flat(analyzer):
    """平面 DEM roughness=0。"""
    src_path = _make_flat_dem(50, 50, 100.0)
    result = analyzer.roughness(src_path)
    assert result["status"] == "success"
    data = result["data"]
    assert data["type"] == "raster"
    assert data["value_kind"] == "roughness"
    # 平面 粗糙度 ~ 0
    assert abs(data["value_range"][0]) < 1e-3
    assert abs(data["value_range"][1]) < 1e-3

    os.unlink(src_path)


def test_roughness_nonzero(analyzer):
    """非平面 DEM roughness > 0。"""
    np.random.seed(42)
    arr = np.random.rand(50, 50).astype(np.float32) * 100
    src_path = _make_in_memory_raster(arr)
    result = analyzer.roughness(src_path)
    assert result["status"] == "success"
    data = result["data"]
    assert data["type"] == "raster"
    assert data["value_kind"] == "roughness"
    # 非平面 DEM roughness > 0
    assert data["value_range"][1] > 0

    os.unlink(src_path)
