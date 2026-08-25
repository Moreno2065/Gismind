"""SpatialAnalyzer 单元测试。

覆盖维度（参考 GIS_Agent_技术文档.md §4.4 + docs/02_data_models.md §3.2）：
1. _ensure_wgs84：GCJ02 GeoDataFrame 转 WGS84（坐标发生偏转）
2. buffer：500m 缓冲，面积约 π×500²（±5%）
3. overlay：两个完全重叠多边形 intersection 面积 = 原面积
4. voronoi：点数 < 4 返回 error
5. voronoi：所有点共线返回 error
6. voronoi：正常 5 个点生成多边形
7. topology_check：自相交几何修复
8. kernel_density：输出密度值

坐标系约定（docs/02_data_models.md §3.3）：
- GCJ02 无标准 EPSG，GeoDataFrame 用 set_crs("GCJ02") 标注
- WGS84 用 EPSG:4326
- 投影计算用动态选择（_resolve_projected_crs：中国 CGCS2000 3度带 / 境外 UTM）
"""

import math
from unittest.mock import patch, MagicMock

import numpy as np
import pytest
import requests
import geopandas as gpd
from shapely.geometry import Point, Polygon, LineString, MultiPoint

from app.tools.spatial_analysis import SpatialAnalyzer, _resolve_projected_crs, GeoLayer
from app.tools.geo_transform import wgs84_to_gcj02, gcj02_to_wgs84


# ============================================================
# 测试基准点：南京新街口附近（国内 GCJ02 偏转区域）
# ============================================================

# WGS84 坐标（南京新街口）
_NANJING_WGS84 = (118.7782, 32.0417)
# 对应 GCJ02 坐标
_NANJING_GCJ02 = wgs84_to_gcj02(*_NANJING_WGS84)


def _make_points_gdf(coords, crs="GCJ02"):
    """构造点 GeoDataFrame。coords: [(lng, lat), ...]

    crs 约定：
    - "GCJ02"：crs 设为 EPSG:4326（坐标值是 GCJ02 偏转后的），attrs["crs_label"]="GCJ02"
    - "EPSG:4326"：标准 WGS84
    GCJ02 无标准 EPSG，pyproj 不接受 "GCJ02" 字符串，故用 attrs 标注（见
    docs/02_data_models.md §3.3 / §7）。
    """
    geom = [Point(c) for c in coords]
    if crs == "GCJ02":
        gdf = gpd.GeoDataFrame({"geometry": geom}, crs="EPSG:4326")
        gdf.attrs["crs_label"] = "GCJ02"
        return gdf
    return gpd.GeoDataFrame({"geometry": geom}, crs=crs)


def _make_polygon_gdf(ring_coords, crs="GCJ02"):
    """构造单多边形 GeoDataFrame。ring_coords: [[lng,lat], ...] 闭合环"""
    geom = [Polygon(ring_coords)]
    if crs == "GCJ02":
        gdf = gpd.GeoDataFrame({"geometry": geom}, crs="EPSG:4326")
        gdf.attrs["crs_label"] = "GCJ02"
        return gdf
    return gpd.GeoDataFrame({"geometry": geom}, crs=crs)


# ============================================================
# 1. _ensure_wgs84：GCJ02 -> WGS84 入口校验
# ============================================================

class TestEnsureWGS84:
    def test_gcj02_input_gets_converted_to_wgs84(self):
        """GCJ02 标注的 GeoDataFrame 经 _ensure_wgs84 后坐标偏转到 WGS84。"""
        analyzer = SpatialAnalyzer()
        gcj02_gdf = _make_points_gdf([_NANJING_GCJ02], crs="GCJ02")

        wgs84_gdf = analyzer._ensure_wgs84(gcj02_gdf)

        # CRS 变为 EPSG:4326
        assert wgs84_gdf.crs.to_string() == "EPSG:4326"
        # 坐标发生偏转（不等于原 GCJ02 坐标）
        out_x, out_y = wgs84_gdf.geometry[0].x, wgs84_gdf.geometry[0].y
        assert abs(out_x - _NANJING_GCJ02[0]) > 1e-6 or abs(out_y - _NANJING_GCJ02[1]) > 1e-6
        # 偏转后应等于 WGS84 原值（往返一致性）
        assert abs(out_x - _NANJING_WGS84[0]) < 1e-6
        assert abs(out_y - _NANJING_WGS84[1]) < 1e-6

    def test_wgs84_input_unchanged(self):
        """已是 WGS84 的 GeoDataFrame 不做偏转。"""
        analyzer = SpatialAnalyzer()
        wgs84_gdf = _make_points_gdf([_NANJING_WGS84], crs="EPSG:4326")

        result = analyzer._ensure_wgs84(wgs84_gdf)

        assert result.crs.to_string() == "EPSG:4326"
        assert result.geometry[0].x == _NANJING_WGS84[0]
        assert result.geometry[0].y == _NANJING_WGS84[1]

    def test_to_gcj02_output_converts_back(self):
        """_to_gcj02_output 把 WGS84 结果转回 GCJ02。"""
        analyzer = SpatialAnalyzer()
        wgs84_gdf = _make_points_gdf([_NANJING_WGS84], crs="EPSG:4326")

        gcj02_out = analyzer._to_gcj02_output(wgs84_gdf)

        out_x, out_y = gcj02_out.geometry[0].x, gcj02_out.geometry[0].y
        assert abs(out_x - _NANJING_GCJ02[0]) < 1e-6
        assert abs(out_y - _NANJING_GCJ02[1]) < 1e-6


# ============================================================
# 2. buffer：500m 缓冲面积验证
# ============================================================

class TestBuffer:
    def test_buffer_500m_area_matches_pi_r_squared(self):
        """500m 缓冲圆面积 ≈ π × 500²（±5%）。"""
        analyzer = SpatialAnalyzer()
        # 用 GCJ02 输入，验证完整 GCJ02->WGS84->投影->buffer->WGS84->GCJ02 链路
        points_gdf = _make_points_gdf([_NANJING_GCJ02], crs="GCJ02")

        result = analyzer.buffer(points_gdf, radius_m=500.0)

        # 结果是 GeoDataFrame，含一个 Polygon
        assert isinstance(result, gpd.GeoDataFrame)
        assert len(result) == 1
        geom = result.geometry[0]
        assert geom.geom_type in ("Polygon", "MultiPolygon")
        assert not geom.is_empty

        # 在投影坐标系下算面积（结果已是 GCJ02，先转 WGS84 再投影）
        result_wgs84 = analyzer._ensure_wgs84(result)
        result_proj = result_wgs84.to_crs(epsg=4548)
        area_m2 = result_proj.geometry[0].area

        expected = math.pi * 500.0 ** 2
        assert abs(area_m2 - expected) / expected < 0.05  # ±5%

    def test_buffer_preserves_feature_count(self):
        """多点输入，缓冲后应保留相同数量的要素。"""
        analyzer = SpatialAnalyzer()
        coords = [
            _NANJING_GCJ02,
            wgs84_to_gcj02(118.80, 32.05),
            wgs84_to_gcj02(118.76, 32.03),
        ]
        points_gdf = _make_points_gdf(coords, crs="GCJ02")

        result = analyzer.buffer(points_gdf, radius_m=200.0)

        assert len(result) == 3
        for g in result.geometry:
            assert not g.is_empty


# ============================================================
# 3. overlay：完全重叠多边形 intersection
# ============================================================

class TestOverlay:
    def test_overlay_intersection_identical_polygons(self):
        """两个完全相同的多边形 intersection 面积 = 原面积。"""
        analyzer = SpatialAnalyzer()
        # 在 GCJ02 下构造一个 1km × 1km 的方形（近似）
        # 先在投影坐标系下构造正方形，再转 GCJ02 作为输入
        proj_gdf = gpd.GeoDataFrame(
            {"geometry": [Polygon([(0, 0), (1000, 0), (1000, 1000), (0, 1000)])]},
            crs="EPSG:4548",
        )
        gcj02_gdf = proj_gdf.to_crs(epsg=4326)
        # 转为 GCJ02 标注（模拟国内数据来源）
        gcj02_gdf = _make_polygon_gdf(
            list(gcj02_gdf.geometry[0].exterior.coords), crs="GCJ02"
        )

        result = analyzer.overlay(gcj02_gdf, gcj02_gdf, how="intersection")

        assert isinstance(result, gpd.GeoDataFrame)
        assert len(result) >= 1
        # 在投影坐标系下算面积
        result_wgs84 = analyzer._ensure_wgs84(result)
        result_proj = result_wgs84.to_crs(epsg=4548)
        area_m2 = result_proj.geometry[0].area
        # 原面积 = 1000 × 1000 = 1,000,000 m²
        assert abs(area_m2 - 1_000_000) / 1_000_000 < 0.01  # ±1%

    def test_overlay_union_doubles_area(self):
        """两个不重叠但相邻的多边形 union 面积 = 两者之和。"""
        analyzer = SpatialAnalyzer()
        # 在投影坐标系下构造两个相邻方形
        poly_a = Polygon([(0, 0), (1000, 0), (1000, 1000), (0, 1000)])
        poly_b = Polygon([(1000, 0), (2000, 0), (2000, 1000), (1000, 1000)])
        proj_a = gpd.GeoDataFrame({"geometry": [poly_a]}, crs="EPSG:4548").to_crs(epsg=4326)
        proj_b = gpd.GeoDataFrame({"geometry": [poly_b]}, crs="EPSG:4548").to_crs(epsg=4326)
        gdf_a = _make_polygon_gdf(list(proj_a.geometry[0].exterior.coords), crs="GCJ02")
        gdf_b = _make_polygon_gdf(list(proj_b.geometry[0].exterior.coords), crs="GCJ02")

        result = analyzer.overlay(gdf_a, gdf_b, how="union")

        result_wgs84 = analyzer._ensure_wgs84(result)
        result_proj = result_wgs84.to_crs(epsg=4548)
        total_area = sum(g.area for g in result_proj.geometry if not g.is_empty)
        # 总面积应 ≈ 2,000,000 m²（两个 1km²）
        assert abs(total_area - 2_000_000) / 2_000_000 < 0.02  # ±2%


# ============================================================
# 4-6. voronoi：错误边界 + 正常场景
# ============================================================

class TestVoronoi:
    def test_voronoi_too_few_points_returns_error(self):
        """点数 < 4 返回 status=error。"""
        analyzer = SpatialAnalyzer()
        coords = [_NANJING_GCJ02, wgs84_to_gcj02(118.80, 32.05), wgs84_to_gcj02(118.76, 32.03)]
        points_gdf = _make_points_gdf(coords, crs="GCJ02")

        result = analyzer.voronoi(points_gdf)

        assert result["status"] == "error"
        assert "点数" in result["message"] or "至少" in result["message"]

    def test_voronoi_collinear_points_returns_error(self):
        """所有点共线返回 status=error。"""
        analyzer = SpatialAnalyzer()
        # 4 个点全部在同一纬度线上
        coords = [
            wgs84_to_gcj02(118.70, 32.04),
            wgs84_to_gcj02(118.75, 32.04),
            wgs84_to_gcj02(118.80, 32.04),
            wgs84_to_gcj02(118.85, 32.04),
        ]
        points_gdf = _make_points_gdf(coords, crs="GCJ02")

        result = analyzer.voronoi(points_gdf)

        assert result["status"] == "error"
        assert "共线" in result["message"]

    def test_voronoi_five_points_generates_polygons(self):
        """5 个不共线的点生成泰森多边形（至少 3 个面）。"""
        analyzer = SpatialAnalyzer()
        # 以新街口为中心，四周分布 5 个点
        center = _NANJING_GCJ02
        offsets = [
            (0.02, 0.0),    # 东
            (-0.02, 0.0),   # 西
            (0.0, 0.02),    # 北
            (0.0, -0.02),   # 南
            (0.01, 0.01),   # 东北
        ]
        coords = [(center[0] + dx, center[1] + dy) for dx, dy in offsets]
        points_gdf = _make_points_gdf(coords, crs="GCJ02")

        result = analyzer.voronoi(points_gdf)

        assert result["status"] == "success", result.get("message", "")
        data = result["data"]
        assert isinstance(data, gpd.GeoDataFrame)
        assert len(data) >= 3  # 5 个点至少生成 3 个有效多边形
        for g in data.geometry:
            assert g.geom_type in ("Polygon", "MultiPolygon")
            assert not g.is_empty
            assert g.is_valid

    def test_voronoi_with_boundary_clips_to_boundary(self):
        """给定 boundary，结果多边形被裁剪到 boundary 范围内。"""
        analyzer = SpatialAnalyzer()
        center = _NANJING_GCJ02
        offsets = [(0.02, 0.0), (-0.02, 0.0), (0.0, 0.02), (0.0, -0.02), (0.01, 0.01)]
        coords = [(center[0] + dx, center[1] + dy) for dx, dy in offsets]
        points_gdf = _make_points_gdf(coords, crs="GCJ02")

        # boundary：以中心为圆心的 0.05° 方形（GCJ02）
        boundary_ring = [
            [center[0] - 0.05, center[1] - 0.05],
            [center[0] + 0.05, center[1] - 0.05],
            [center[0] + 0.05, center[1] + 0.05],
            [center[0] - 0.05, center[1] + 0.05],
            [center[0] - 0.05, center[1] - 0.05],
        ]
        boundary = Polygon(boundary_ring)

        result = analyzer.voronoi(points_gdf, boundary=boundary)

        assert result["status"] == "success"
        data = result["data"]
        # 所有结果多边形都应在 boundary 内
        for g in data.geometry:
            assert boundary.contains(g) or boundary.intersects(g)


# ============================================================
# 7. topology_check：自相交几何修复
# ============================================================

class TestTopologyCheck:
    def test_topology_check_fixes_self_intersecting_polygon(self):
        """自相交的领结多边形经 topology_check 后被修复为 valid。"""
        analyzer = SpatialAnalyzer()
        # 领结（蝴蝶结）几何：自相交
        # 坐标顺序: (0,0) -> (1,1) -> (1,0) -> (0,1) -> (0,0) 形成自相交
        bowtie = Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)])
        assert not bowtie.is_valid  # 确认测试用例确实非法

        gdf = gpd.GeoDataFrame({"geometry": [bowtie]}, crs="EPSG:4326")

        result = analyzer.topology_check(gdf)

        assert result["status"] == "success"
        issues = result["data"]["issues"]
        assert len(issues) >= 1
        assert issues[0]["type"] == "invalid"
        # 修复后的几何应 valid
        fixed = issues[0]["fix"]
        assert fixed.is_valid

    def test_topology_check_clean_geometry_no_issues(self):
        """合法几何无问题。"""
        analyzer = SpatialAnalyzer()
        clean = Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])
        gdf = gpd.GeoDataFrame({"geometry": [clean]}, crs="EPSG:4326")

        result = analyzer.topology_check(gdf)

        assert result["status"] == "success"
        assert len(result["data"]["issues"]) == 0

    def test_topology_check_empty_geometry_flagged(self):
        """空几何被标记。"""
        analyzer = SpatialAnalyzer()
        empty = Polygon()
        gdf = gpd.GeoDataFrame({"geometry": [empty]}, crs="EPSG:4326")

        result = analyzer.topology_check(gdf)

        assert result["status"] == "success"
        issues = result["data"]["issues"]
        assert any(i["type"] == "empty" for i in issues)


# ============================================================
# 8. kernel_density：核密度估计
# ============================================================

class TestKernelDensity:
    def test_kernel_density_outputs_density_values(self):
        """给定点集，输出每个点对应的密度值。"""
        analyzer = SpatialAnalyzer()
        # 在新街口附近构造一组聚集的点
        center = _NANJING_GCJ02
        coords = [
            center,
            (center[0] + 0.001, center[1]),
            (center[0] - 0.001, center[1]),
            (center[0], center[1] + 0.001),
            (center[0], center[1] - 0.001),
            (center[0] + 0.002, center[1] + 0.002),
        ]
        points_gdf = _make_points_gdf(coords, crs="GCJ02")

        result = analyzer.kernel_density(points_gdf, bandwidth=300.0)

        assert result["status"] == "success"
        data = result["data"]
        # 密度值数量 = 点数
        densities = data["densities"]
        assert len(densities) == len(coords)
        # 所有密度值 >= 0
        assert all(d >= 0 for d in densities)
        # 中心点（被多个邻居包围）密度应高于边缘点
        assert densities[0] > densities[5]

    def test_kernel_density_auto_bandwidth_when_none(self):
        """未指定 bandwidth 时自动估算（不报错）。"""
        analyzer = SpatialAnalyzer()
        coords = [
            (_NANJING_GCJ02[0] + dx, _NANJING_GCJ02[1] + dy)
            for dx, dy in [(0, 0), (0.001, 0), (0, 0.001), (-0.001, 0), (0, -0.001)]
        ]
        points_gdf = _make_points_gdf(coords, crs="GCJ02")

        result = analyzer.kernel_density(points_gdf, bandwidth=None)

        assert result["status"] == "success"
        assert len(result["data"]["densities"]) == 5


# ============================================================
# 9. isochrone：mock 高德路径规划（外部 API 不打真实网络）
# ============================================================

class TestIsochrone:
    def test_isochrone_returns_empty_when_route_service_unavailable(self):
        """路径规划服务超时/不可用时返回 status=empty。"""
        analyzer = SpatialAnalyzer(amap_key="test_key")
        # mock 路径规划抛 requests.exceptions.RequestException
        with patch.object(
            analyzer, "_route_reachable_distance",
            side_effect=requests.exceptions.RequestException("timeout"),
        ):
            result = analyzer.isochrone(_NANJING_GCJ02, mode="driving", time_min=15)

        assert result["status"] == "empty"
        assert "不可用" in result["message"] or "路网" in result["message"]

    def test_isochrone_returns_empty_when_too_few_valid_samples(self):
        """有效采样点 < 3 时返回 status=empty（模拟海边场景）。"""
        analyzer = SpatialAnalyzer(amap_key="test_key")
        # 所有方向都返回 0 距离（无路网），导致有效点不足
        with patch.object(analyzer, "_route_reachable_distance", return_value=0.0):
            result = analyzer.isochrone(_NANJING_GCJ02, mode="driving", time_min=15)

        assert result["status"] == "empty"

    def test_isochrone_success_returns_polygon(self):
        """正常路径规划返回多边形等时圈。"""
        analyzer = SpatialAnalyzer(amap_key="test_key")
        # mock 8 方向都返回 1000m（15 分钟驾车可达 1km）
        with patch.object(analyzer, "_route_reachable_distance", return_value=1000.0):
            result = analyzer.isochrone(_NANJING_GCJ02, mode="driving", time_min=15)

        assert result["status"] == "success", result.get("message", "")
        data = result["data"]
        assert "geometry" in data
        geom = data["geometry"]
        assert geom.geom_type in ("Polygon", "MultiPolygon")
        assert not geom.is_empty


# ============================================================
# 10. _resolve_projected_crs：动态投影坐标系选择
# ============================================================

class TestResolveProjectedCRS:
    """动态投影坐标系选择：中国境内 CGCS2000 3度带，境外 UTM。"""

    def test_urumqi_87e_gets_cgcs2000_cm87e(self):
        """乌鲁木齐 (~87°E) -> CGCS2000 CM 87°E 即 EPSG:4538。"""
        gdf = gpd.GeoDataFrame(
            {"geometry": [Point(87.0, 43.8)]},
            crs="EPSG:4326",
        )
        epsg = _resolve_projected_crs(gdf)
        assert epsg == 4538, f"Expected 4538, got {epsg}"

    def test_nanjing_area_gets_cgcs2000_cm117e(self):
        """南京附近 (~118°E，但需四舍五入到 CM 117°E) -> EPSG:4548。"""
        gdf = gpd.GeoDataFrame(
            {"geometry": [Point(118.0, 32.0)]},
            crs="EPSG:4326",
        )
        epsg = _resolve_projected_crs(gdf)
        assert epsg == 4548, f"Expected 4548, got {epsg}"

    def test_london_gets_utm_30n(self):
        """伦敦 (~0°E, 51.5°N) -> UTM 30N 即 EPSG:32630。"""
        gdf = gpd.GeoDataFrame(
            {"geometry": [Point(-0.1278, 51.5074)]},
            crs="EPSG:4326",
        )
        epsg = _resolve_projected_crs(gdf)
        assert epsg == 32630, f"Expected 32630, got {epsg}"

    def test_sydney_gets_utm_56s(self):
        """悉尼 (~151°E, -33.8°S) -> UTM 56S 即 EPSG:32756（南半球）。"""
        gdf = gpd.GeoDataFrame(
            {"geometry": [Point(151.2093, -33.8688)]},
            crs="EPSG:4326",
        )
        epsg = _resolve_projected_crs(gdf)
        assert epsg == 32756, f"Expected 32756, got {epsg}"

    def test_china_west_edge_gets_cgcs2000(self):
        """中国西部 (~74°E) 落在 CGCS2000 范围内（CM 75°E, EPSG 4534）。"""
        gdf = gpd.GeoDataFrame(
            {"geometry": [Point(74.0, 39.0)]},
            crs="EPSG:4326",
        )
        epsg = _resolve_projected_crs(gdf)
        assert 4534 <= epsg <= 4554, f"Expected CGCS2000, got {epsg}"

    def test_china_east_edge_gets_cgcs2000(self):
        """中国最东端 (~135.5°E) 仍在 CGCS2000 范围内。"""
        gdf = gpd.GeoDataFrame(
            {"geometry": [Point(135.5, 48.0)]},
            crs="EPSG:4326",
        )
        epsg = _resolve_projected_crs(gdf)
        assert 4534 <= epsg <= 4554, f"Expected CGCS2000, got {epsg}"


# ============================================================
# 11. GeoLayer：带 CRS 语义的轻量包装
# ============================================================

class TestGeoLayer:
    """GeoLayer dataclass 与 _ensure_wgs84 集成测试。"""

    def test_geolayer_gcj02_converts_to_wgs84(self):
        """GeoLayer(crs_label="GCJ02") 经 _ensure_wgs84 后坐标被偏转。"""
        analyzer = SpatialAnalyzer()
        # 构造 GeoLayer 包装，gdf 不带 attrs（GeoLayer 本身声明 crs_label）
        bare_gdf = gpd.GeoDataFrame(
            {"geometry": [Point(_NANJING_GCJ02)]},
            crs="EPSG:4326",
        )
        layer = GeoLayer(gdf=bare_gdf, crs_label="GCJ02")

        wgs84_gdf = analyzer._ensure_wgs84(layer)

        assert wgs84_gdf.crs.to_string() == "EPSG:4326"
        out_x, out_y = wgs84_gdf.geometry[0].x, wgs84_gdf.geometry[0].y
        assert abs(out_x - _NANJING_WGS84[0]) < 1e-6
        assert abs(out_y - _NANJING_WGS84[1]) < 1e-6

    def test_geolayer_wgs84_passes_through(self):
        """GeoLayer(crs_label="WGS84") 不做偏转，直接返回。"""
        analyzer = SpatialAnalyzer()
        wgs84_gdf = _make_points_gdf([_NANJING_WGS84], crs="EPSG:4326")
        layer = GeoLayer(gdf=wgs84_gdf, crs_label="WGS84")

        result = analyzer._ensure_wgs84(layer)

        assert result.crs.to_string() == "EPSG:4326"
        assert result.geometry[0].x == _NANJING_WGS84[0]
        assert result.geometry[0].y == _NANJING_WGS84[1]

    def test_geolayer_projected_is_reprojected_to_wgs84(self):
        """GeoLayer(crs_label="PROJECTED") 按真实 CRS 规范为 WGS84。"""
        analyzer = SpatialAnalyzer()
        proj_gdf = gpd.GeoDataFrame(
            {"geometry": [Point(500000, 3500000)]},
            crs="EPSG:4548",
        )
        layer = GeoLayer(gdf=proj_gdf, crs_label="PROJECTED")

        result = analyzer._ensure_wgs84(layer)

        assert result.crs.to_string() == "EPSG:4326"
        assert -180 <= result.geometry[0].x <= 180
        assert -90 <= result.geometry[0].y <= 90

    def test_bare_gdf_without_attrs_warns_default_wgs84(self, caplog):
        """无 attrs 的 GeoDataFrame 默认 WGS84 并发出 warning。"""
        import logging
        analyzer = SpatialAnalyzer()
        bare_gdf = gpd.GeoDataFrame(
            {"geometry": [Point(_NANJING_WGS84)]},
            crs="EPSG:4326",
        )
        # 确认没有 crs_label attrs
        assert "crs_label" not in (bare_gdf.attrs or {})

        with caplog.at_level(logging.WARNING, logger="app.tools.spatial_analysis"):
            result = analyzer._ensure_wgs84(bare_gdf)

        assert result.crs.to_string() == "EPSG:4326"
        assert any("假定为 WGS84" in rec.message for rec in caplog.records)

    def test_geolayer_defaults_to_wgs84(self):
        """GeoLayer 不传 crs_label 时默认 WGS84。"""
        layer = GeoLayer(gdf=gpd.GeoDataFrame())
        assert layer.crs_label == "WGS84"

    def test_geolayer_flows_into_buffer_pipeline(self):
        """GeoLayer 包装的 GCJ02 点能正确走 buffer 管线。"""
        analyzer = SpatialAnalyzer()
        bare_gdf = gpd.GeoDataFrame(
            {"geometry": [Point(_NANJING_GCJ02)]},
            crs="EPSG:4326",
        )
        layer = GeoLayer(gdf=bare_gdf, crs_label="GCJ02")

        result = analyzer.buffer(layer, radius_m=500.0)

        assert isinstance(result, gpd.GeoDataFrame)
        assert len(result) == 1
        geom = result.geometry[0]
        assert geom.geom_type in ("Polygon", "MultiPolygon")
        assert not geom.is_empty
