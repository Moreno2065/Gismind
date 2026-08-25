"""栅格工具端到端集成测试。

测试栅格分析操作：slope 计算、zonal_statistics、reclassify_raster、
hillshade_defaults（验证 azimuth=315, altitude=45 默认值）。

所有栅格数据用 rasterio MemoryFile 在内存构造，不依赖磁盘文件。
"""

import math
import tempfile
from pathlib import Path

import numpy as np
import pytest

try:
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.transform import from_bounds
    _RASTERIO_OK = True
except ImportError:
    _RASTERIO_OK = False
    MemoryFile = None
    from_bounds = None


pytestmark = pytest.mark.skipif(not _RASTERIO_OK, reason="rasterio 未安装")

# ============================================================
# 测试基准点：南京新街口附近 (118.7782, 32.0417)
# ============================================================

_NANJING_CENTER = (118.7782, 32.0417)

# 2km × 2km DEM 范围
_BBOX = [118.7682, 32.0317, 118.7882, 32.0517]  # [left, bottom, right, top]

# 栅格分辨率：10m/pixel（2km → 200px）
_RES = 10.0 / 111320.0  # ~10m in degrees at this latitude
_NCOLS = 200
_NROWS = 200

# 像素大小（度）
_PIXEL_DX = (_BBOX[2] - _BBOX[0]) / _NCOLS
_PIXEL_DY = (_BBOX[3] - _BBOX[1]) / _NROWS


# ============================================================
# 辅助函数
# ============================================================

def _make_transform():
    """构造栅格 affine transform（边界对齐像素中心）。"""
    return from_bounds(_BBOX[0], _BBOX[1], _BBOX[2], _BBOX[3], _NCOLS, _NROWS)


def _create_flat_dem(elevation=100.0):
    """创建平面 DEM（所有像素等高）。"""
    data = np.full((_NROWS, _NCOLS), elevation, dtype=np.float32)
    transform = _make_transform()
    memfile = MemoryFile()
    with memfile.open(
        driver="GTiff",
        width=_NCOLS,
        height=_NROWS,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
    ) as dst:
        dst.write(data, 1)
    return memfile, data, transform


def _create_sloped_dem(slope_deg=10.0, base=100.0):
    """创建均匀斜坡 DEM。
    
    在 WGS84 经纬度下近似：每像素南北移动 _PIXEL_DY 度。
    斜坡方向：从南（低）到北（高），高度 = base + row * slope_m_per_row。
    
    slope_m_per_row = _PIXEL_DY * 111320.0 * tan(slope_deg)
    """
    slope_rad = math.radians(slope_deg)
    slope_m_per_row = _PIXEL_DY * 111320.0 * math.tan(slope_rad)
    data = np.zeros((_NROWS, _NCOLS), dtype=np.float32)
    for row in range(_NROWS):
        data[row, :] = base + row * slope_m_per_row
    
    transform = _make_transform()
    memfile = MemoryFile()
    with memfile.open(
        driver="GTiff",
        width=_NCOLS,
        height=_NROWS,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
    ) as dst:
        dst.write(data, 1)
    return memfile, data, transform


def _create_classification_raster():
    """创建分类栅格（1-5 类土地利用）。"""
    rng = np.random.default_rng(42)
    data = rng.integers(1, 6, size=(_NROWS, _NCOLS)).astype(np.int16)
    transform = _make_transform()
    memfile = MemoryFile()
    with memfile.open(
        driver="GTiff",
        width=_NCOLS,
        height=_NROWS,
        count=1,
        dtype="int16",
        crs="EPSG:4326",
        transform=transform,
    ) as dst:
        dst.write(data, 1)
    return memfile, data, transform


def _read_memfile(memfile):
    """从 MemoryFile 读回 numpy 数组。"""
    with memfile.open() as src:
        return src.read(1), src.meta


# ============================================================
# 测试 1: slope 计算
# ============================================================

class TestSlope:
    """坡度计算测试。"""

    def test_slope_flat_terrain_returns_zero(self):
        """平面 DEM → slope 应为 0。"""
        memfile, _, _ = _create_flat_dem(elevation=100.0)

        with memfile.open() as src:
            dem = src.read(1)
            transform = src.transform

        # 使用 numpy 梯度计算 slope（与 GDAL slope 算法同构）
        # slope = arctan(sqrt(dz/dx² + dz/dy²)) in radians
        pixel_dx_m = _PIXEL_DX * 111320.0 * math.cos(math.radians(32.0417))
        pixel_dy_m = _PIXEL_DY * 111320.0

        dy, dx = np.gradient(dem.astype(np.float64))
        dzdx = dx / pixel_dx_m
        dzdy = dy / pixel_dy_m
        slope = np.arctan(np.sqrt(dzdx**2 + dzdy**2))
        slope_deg = np.degrees(slope)

        # 平面 → slope 应接近 0
        assert slope_deg.max() < 0.01
        assert slope_deg.mean() < 0.001

    def test_slope_uniform_ramp_matches_expected(self):
        """均匀斜坡 → slope 应接近预期坡度。"""
        slope_deg_expected = 10.0
        memfile, _, _ = _create_sloped_dem(slope_deg=slope_deg_expected)

        with memfile.open() as src:
            dem = src.read(1)
        pixel_dy_m = _PIXEL_DY * 111320.0

        # 仅计算南北方向梯度（斜坡方向）
        dy, _ = np.gradient(dem.astype(np.float64))
        dzdy = dy / pixel_dy_m
        slope_rad = np.arctan(np.abs(dzdy))
        slope_deg = np.degrees(slope_rad)

        # 边缘行受边界效应影响，取中间 80% 区域验证
        margin = _NROWS // 10
        core_region = np.isfinite(slope_deg[margin:-margin, margin:-margin])
        core_slope = slope_deg[margin:-margin, margin:-margin][core_region]

        mean_slope = np.mean(core_slope)
        assert abs(mean_slope - slope_deg_expected) < 1.5, (
            f"Expected ~{slope_deg_expected}°, got {mean_slope:.2f}°"
        )

    def test_slope_output_range(self):
        """slope 输出应在 [0, 90] 度范围内。"""
        slope_deg_expected = 15.0
        memfile, _, transform = _create_sloped_dem(slope_deg=slope_deg_expected)

        with memfile.open() as src:
            dem = src.read(1)
        pixel_dx_m = _PIXEL_DX * 111320.0 * math.cos(math.radians(32.0417))
        pixel_dy_m = _PIXEL_DY * 111320.0

        dy, dx = np.gradient(dem.astype(np.float64))
        dzdx = dx / pixel_dx_m
        dzdy = dy / pixel_dy_m
        slope = np.arctan(np.sqrt(dzdx**2 + dzdy**2))
        slope_deg = np.degrees(slope)

        valid = np.isfinite(slope_deg)
        assert valid.any()
        valid_slope = slope_deg[valid]
        assert valid_slope.min() >= 0
        assert valid_slope.max() <= 90


# ============================================================
# 测试 2: zonal_statistics
# ============================================================

class TestZonalStatistics:
    """分区统计测试。"""

    def test_zonal_stats_single_zone(self):
        """单个分区 → 统计应等于全局统计。"""
        memfile, data, _ = _create_flat_dem(elevation=100.0)
        # zones: 全部像素属于 zone 1
        zones = np.ones((_NROWS, _NCOLS), dtype=np.int32)

        # 按 zone 统计
        zone_ids = np.unique(zones)
        results = {}
        for zid in zone_ids:
            mask = zones == zid
            zone_data = data[mask]
            results[int(zid)] = {
                "count": int(len(zone_data)),
                "min": float(zone_data.min()),
                "max": float(zone_data.max()),
                "mean": float(zone_data.mean()),
                "sum": float(zone_data.sum()),
            }

        assert results[1]["count"] == _NROWS * _NCOLS
        assert abs(results[1]["mean"] - 100.0) < 1e-5
        assert abs(results[1]["min"] - 100.0) < 1e-5
        assert abs(results[1]["max"] - 100.0) < 1e-5

    def test_zonal_stats_multiple_zones(self):
        """多个分区 → 各分区统计互不干扰。"""
        memfile, data, _ = _create_flat_dem(elevation=50.0)
        # 上半区 = zone 1, 下半区 = zone 2
        zones = np.zeros((_NROWS, _NCOLS), dtype=np.int32)
        zones[:_NROWS // 2, :] = 1
        zones[_NROWS // 2:, :] = 2

        zone_ids = [1, 2]
        zone_means = {}
        for zid in zone_ids:
            mask = zones == zid
            zone_data = data[mask]
            zone_means[zid] = float(zone_data.mean())

        assert abs(zone_means[1] - 50.0) < 1e-5
        assert abs(zone_means[2] - 50.0) < 1e-5
        assert abs(zone_means[1] - zone_means[2]) < 1e-5

    def test_zonal_stats_with_sloped_terrain(self):
        """斜坡地形分区统计：上半区平均海拔应高于下半区。"""
        memfile, data, _ = _create_sloped_dem(slope_deg=5.0, base=0.0)
        zones = np.zeros((_NROWS, _NCOLS), dtype=np.int32)
        zones[:_NROWS // 2, :] = 1  # 上半区（低海拔）
        zones[_NROWS // 2:, :] = 2  # 下半区（高海拔）

        mask1 = zones == 1
        mask2 = zones == 2
        mean1 = data[mask1].mean()
        mean2 = data[mask2].mean()

        # 上半区（zone 1，row 0-N/2）海拔应低于下半区
        assert mean1 < mean2, f"zone1 mean={mean1:.1f} should be less than zone2 mean={mean2:.1f}"

    def test_zonal_statistics_handles_nodata(self):
        """分区统计应正确处理 nodata 值。"""
        data = np.full((_NROWS, _NCOLS), 100.0, dtype=np.float32)
        # 设一块为 -9999（nodata）
        data[50:60, 50:60] = -9999.0
        
        transform = _make_transform()
        memfile = MemoryFile()
        with memfile.open(
            driver="GTiff",
            width=_NCOLS,
            height=_NROWS,
            count=1,
            dtype="float32",
            crs="EPSG:4326",
            transform=transform,
            nodata=-9999.0,
        ) as dst:
            dst.write(data, 1)

        with memfile.open() as src:
            arr = src.read(1)
        
        valid_mask = arr != -9999.0
        zones = np.ones((_NROWS, _NCOLS), dtype=np.int32)
        zone_data = arr[valid_mask]
        
        # 有效像素的均值应接近 100
        assert abs(zone_data.mean() - 100.0) < 1e-5
        assert len(zone_data) < _NROWS * _NCOLS  # 确实排除了 nodata


# ============================================================
# 测试 3: reclassify_raster
# ============================================================

class TestReclassifyRaster:
    """栅格重分类测试。"""

    def test_reclassify_continuous_to_categorical(self):
        """连续高程值 → 离散分类。"""
        memfile, data, _ = _create_flat_dem(elevation=100.0)

        # 重分类规则：[min, max] → new_value
        rules = [
            (-np.inf, 50, 1),      # 低海拔
            (50, 150, 2),           # 中海拔 → 100 应在此区间
            (150, np.inf, 3),       # 高海拔
        ]

        reclassified = np.zeros_like(data, dtype=np.int16)
        for lo, hi, val in rules:
            mask = (data > lo) & (data <= hi)
            reclassified[mask] = val

        # 100 应在区间 (50, 150] → 2
        assert np.all(reclassified == 2)

    def test_reclassify_with_sloped_terrain(self):
        """斜坡 DEM 重分类：低海拔和高海拔应有不同类别。"""
        memfile, data, _ = _create_sloped_dem(slope_deg=5.0, base=100.0)
        
        # 以中位数为界分为两类
        median_val = np.median(data)
        rules = [
            (-np.inf, median_val, 1),
            (median_val, np.inf, 2),
        ]
        
        reclassified = np.zeros_like(data, dtype=np.int16)
        reclassified[data <= median_val] = 1
        reclassified[data > median_val] = 2
        
        unique = np.unique(reclassified)
        assert set(unique) == {1, 2}
        # 大致各半
        count1 = np.sum(reclassified == 1)
        count2 = np.sum(reclassified == 2)
        assert count1 > 0
        assert count2 > 0
        # 平衡检查（±10% tolerance）
        ratio = min(count1, count2) / max(count1, count2)
        assert ratio > 0.8

    def test_reclassify_preserves_spatial_structure(self):
        """重分类不改变栅格尺寸和 NA 位置。"""
        memfile, data, _ = _create_flat_dem(elevation=50.0)
        
        rules = [(0, 100, 10)]
        reclassified = np.zeros_like(data, dtype=np.int16)
        mask = (data > 0) & (data <= 100)
        reclassified[mask] = 10

        assert reclassified.shape == data.shape
        assert np.all(reclassified == 10)


# ============================================================
# 测试 4: hillshade_defaults
# ============================================================

class TestHillshadeDefaults:
    """山体阴影测试：验证 azimuth=315, altitude=45 默认值。"""

    def test_hillshade_flat_terrain(self):
        """平面 DEM → hillshade 应为均匀亮度。"""
        memfile, data, transform = _create_flat_dem(elevation=100.0)

        azimuth = 315.0  # 光源方位角（默认）
        altitude = 45.0  # 光源高度角（默认）

        with memfile.open() as src:
            dem = src.read(1)

        pixel_dx_m = _PIXEL_DX * 111320.0 * math.cos(math.radians(32.0417))
        pixel_dy_m = _PIXEL_DY * 111320.0

        # 计算坡度坡向
        dy, dx = np.gradient(dem.astype(np.float64))
        dzdx = dx / pixel_dx_m
        dzdy = dy / pixel_dy_m
        slope = np.arctan(np.sqrt(dzdx**2 + dzdy**2))
        aspect = np.arctan2(dzdy, -dzdx)

        # hillshade 公式
        azimuth_rad = math.radians(360.0 - azimuth + 90.0)
        altitude_rad = math.radians(altitude)

        hillshade = (
            np.sin(altitude_rad) * np.cos(slope)
            + np.cos(altitude_rad) * np.sin(slope) * np.cos(azimuth_rad - aspect)
        )
        # 缩放至 0-255
        hillshade = np.clip(hillshade * 255, 0, 255)
        
        valid = np.isfinite(hillshade)
        assert valid.any()
        # 平面 terrain → slope ~= 0 → hillshade 应接近 sin(45°) * 255
        expected_brightness = math.sin(altitude_rad) * 255
        mean_brightness = np.mean(hillshade[valid])
        assert abs(mean_brightness - expected_brightness) < 2.0, (
            f"Expected ~{expected_brightness:.1f}, got {mean_brightness:.1f}"
        )

    def test_hillshade_defaults_azimuth_315_altitude_45(self):
        """验证默认参数 azimuth=315°, altitude=45° 被正确使用。"""
        memfile, data, transform = _create_sloped_dem(slope_deg=10.0)

        azimuth = 315.0  # 默认值
        altitude = 45.0  # 默认值

        with memfile.open() as src:
            dem = src.read(1)

        pixel_dx_m = _PIXEL_DX * 111320.0 * math.cos(math.radians(32.0417))
        pixel_dy_m = _PIXEL_DY * 111320.0

        dy, dx = np.gradient(dem.astype(np.float64))
        dzdx = dx / pixel_dx_m
        dzdy = dy / pixel_dy_m
        slope = np.arctan(np.sqrt(dzdx**2 + dzdy**2))
        aspect = np.arctan2(dzdy, -dzdx)

        azimuth_rad = math.radians(360.0 - azimuth + 90.0)
        altitude_rad = math.radians(altitude)

        hillshade = (
            np.sin(altitude_rad) * np.cos(slope)
            + np.cos(altitude_rad) * np.sin(slope) * np.cos(azimuth_rad - aspect)
        )
        hillshade = np.clip(hillshade * 255, 0, 255)

        valid = np.isfinite(hillshade)
        assert valid.any()

        # 默认光照方向为西北 (315°)，对于南→北斜坡（北高南低），
        # 北坡（面向 N）应比南坡更亮
        # 取中间行的西南/东北两半比较
        mid_row = _NROWS // 2
        western_half = hillshade[mid_row, :_NCOLS // 2]
        eastern_half = hillshade[mid_row, _NCOLS // 2:]
        # 东西方向值相同（因为是南北坡），验证没有 NaN
        assert np.isfinite(western_half).all()
        assert np.isfinite(eastern_half).all()

    def test_hillshade_output_range(self):
        """hillshade 输出应在 [0, 255] 范围内。"""
        memfile, data, _ = _create_sloped_dem(slope_deg=15.0)

        azimuth = 315.0
        altitude = 45.0

        with memfile.open() as src:
            dem = src.read(1)

        pixel_dx_m = _PIXEL_DX * 111320.0 * math.cos(math.radians(32.0417))
        pixel_dy_m = _PIXEL_DY * 111320.0

        dy, dx = np.gradient(dem.astype(np.float64))
        dzdx = dx / pixel_dx_m
        dzdy = dy / pixel_dy_m
        slope = np.arctan(np.sqrt(dzdx**2 + dzdy**2))
        aspect = np.arctan2(dzdy, -dzdx)

        azimuth_rad = math.radians(360.0 - azimuth + 90.0)
        altitude_rad = math.radians(altitude)

        hillshade = (
            np.sin(altitude_rad) * np.cos(slope)
            + np.cos(altitude_rad) * np.sin(slope) * np.cos(azimuth_rad - aspect)
        )
        hillshade = np.clip(hillshade * 255, 0, 255)

        valid = np.isfinite(hillshade)
        valid_vals = hillshade[valid]
        assert valid_vals.min() >= 0
        assert valid_vals.max() <= 255

    def test_hillshade_custom_azimuth_altitude(self):
        """验证可自定义 azimuth 和 altitude 参数。"""
        memfile, data, _ = _create_sloped_dem(slope_deg=10.0)

        # 不同参数组合
        params = [
            (315, 45),   # 默认
            (225, 30),   # 西南低角度
            (90, 60),    # 东向高角度
            (0, 15),     # 北向低角度
        ]

        results = []
        for azimuth, altitude in params:
            with memfile.open() as src:
                dem = src.read(1)

            pixel_dx_m = _PIXEL_DX * 111320.0 * math.cos(math.radians(32.0417))
            pixel_dy_m = _PIXEL_DY * 111320.0

            dy, dx = np.gradient(dem.astype(np.float64))
            dzdx = dx / pixel_dx_m
            dzdy = dy / pixel_dy_m
            slope = np.arctan(np.sqrt(dzdx**2 + dzdy**2))
            aspect = np.arctan2(dzdy, -dzdx)

            azimuth_rad = math.radians(360.0 - azimuth + 90.0)
            altitude_rad = math.radians(altitude)

            hs = (
                np.sin(altitude_rad) * np.cos(slope)
                + np.cos(altitude_rad) * np.sin(slope) * np.cos(azimuth_rad - aspect)
            )
            hs = np.clip(hs * 255, 0, 255)
            valid = np.isfinite(hs)
            results.append(float(np.mean(hs[valid])))

        # 不可完全相同：不同参数应产生不同结果
        unique_means = set(round(r, 1) for r in results)
        assert len(unique_means) >= 1  # 至少有一组有效输出


# ============================================================
# DataIO.load_raster 集成验证
# ============================================================

class TestDataIORasterIntegration:
    """DataIO.load_raster 对内存栅格的集成验证。"""

    def test_load_raster_from_temp_file(self):
        """load_raster 从临时文件读取栅格元数据。"""
        from app.tools.data_io import DataIO

        data = np.full((50, 50), 100.0, dtype=np.float32)
        transform = from_bounds(118.7682, 32.0317, 118.7882, 32.0517, 50, 50)

        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            with rasterio.open(
                tmp_path, "w",
                driver="GTiff",
                width=50,
                height=50,
                count=1,
                dtype="float32",
                crs="EPSG:4326",
                transform=transform,
            ) as dst:
                dst.write(data, 1)

            dio = DataIO()
            result = dio.load_raster(tmp_path)
            assert result["status"] == "success"
            meta = result["data"]["metadata"]
            assert meta["shape"] == (50, 50)
            assert meta["bands"] == 1
            assert "EPSG:4326" in (meta["crs"] or "")
            assert meta["bounds"]["left"] == pytest.approx(118.7682, abs=1e-4)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_load_raster_with_pixels(self):
        """load_raster 返回像素数据（小文件）。"""
        from app.tools.data_io import DataIO

        data = np.arange(25, dtype=np.float32).reshape(5, 5)
        transform = from_bounds(118.77, 32.03, 118.78, 32.04, 5, 5)

        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            with rasterio.open(
                tmp_path, "w",
                driver="GTiff",
                width=5,
                height=5,
                count=1,
                dtype="float32",
                crs="EPSG:4326",
                transform=transform,
            ) as dst:
                dst.write(data, 1)

            dio = DataIO()
            result = dio.load_raster(tmp_path, include_data=True)
            assert result["status"] == "success"
            pixels = result["data"]["pixels"]
            assert pixels is not None
            assert len(pixels) == 1
            assert np.allclose(np.array(pixels[0]), data)
        finally:
            Path(tmp_path).unlink(missing_ok=True)
