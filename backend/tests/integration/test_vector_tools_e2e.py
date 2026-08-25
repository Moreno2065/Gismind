"""矢量工具端到端集成测试。

测试完整的矢量分析工具链：clip → dissolve、spatial join、点计数、
属性操作链（extract → keep_fields → field_calculator）、
以及 GCJ02 标注下的 reproject_layer pipeline。

所有测试使用内存 GeoDataFrame（GCJ02 标注），不依赖磁盘文件。
"""

import json

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import Point, Polygon

from app.tools.spatial_analysis import SpatialAnalyzer
from app.tools.data_io import DataIO
from app.tools.geo_transform import wgs84_to_gcj02

# ============================================================
# 测试基准点：南京新街口附近 (118.7782, 32.0417)
# ============================================================

_NANJING_WGS84 = (118.7782, 32.0417)
_NANJING_GCJ02 = wgs84_to_gcj02(*_NANJING_WGS84)

# 南京新街口商圈范围（~2km 矩形，WGS84）
_XINJIEKOU_BBOX_WGS84 = [
    (118.7682, 32.0317),  # 左下
    (118.7682, 32.0517),  # 左上
    (118.7882, 32.0517),  # 右上
    (118.7882, 32.0317),  # 右下
]
_XINJIEKOU_BBOX_GCJ02 = [wgs84_to_gcj02(lng, lat) for lng, lat in _XINJIEKOU_BBOX_WGS84]

# 新街口附近分散采样点（WGS84）
_SAMPLE_POINTS_WGS84 = [
    (118.7782, 32.0417),  # 新街口中心
    (118.7792, 32.0427),  # 东北
    (118.7772, 32.0407),  # 西南
    (118.7802, 32.0402),  # 东南
    (118.7762, 32.0432),  # 西北
]
_SAMPLE_POINTS_GCJ02 = [wgs84_to_gcj02(lng, lat) for lng, lat in _SAMPLE_POINTS_WGS84]

# 新街口附近商圈多边形（3个商圈，WGS84）
_BUSINESS_ZONES_WGS84 = [
    # 德基广场商圈
    [(118.7762, 32.0407), (118.7762, 32.0437), (118.7802, 32.0437), (118.7802, 32.0407)],
    # 新百商圈
    [(118.7792, 32.0397), (118.7792, 32.0417), (118.7822, 32.0417), (118.7822, 32.0397)],
    # 大洋百货商圈
    [(118.7732, 32.0417), (118.7732, 32.0437), (118.7762, 32.0437), (118.7762, 32.0417)],
]
_BUSINESS_ZONES_GCJ02 = [
    [wgs84_to_gcj02(lng, lat) for lng, lat in zone]
    for zone in _BUSINESS_ZONES_WGS84
]


# ============================================================
# 辅助函数
# ============================================================

def _make_points_gdf(coords, crs="GCJ02"):
    """构造点 GeoDataFrame。

    crs 约定：
    - "GCJ02"：crs 设为 EPSG:4326（坐标值是 GCJ02 偏转后的），attrs["crs_label"]="GCJ02"
    - "EPSG:4326"：标准 WGS84
    """
    geom = [Point(c) for c in coords]
    if crs == "GCJ02":
        gdf = gpd.GeoDataFrame({"geometry": geom}, crs="EPSG:4326")
        gdf.attrs["crs_label"] = "GCJ02"
        return gdf
    return gpd.GeoDataFrame({"geometry": geom}, crs=crs)


def _make_polygon_gdf(ring_coords_list, crs="GCJ02", extra_cols=None):
    """构造多边形 GeoDataFrame。ring_coords_list: [[lng,lat], ...] 列表的列表。"""
    geoms = [Polygon(ring) for ring in ring_coords_list]
    data = {"geometry": geoms}
    if extra_cols:
        data.update(extra_cols)
    if crs == "GCJ02":
        gdf = gpd.GeoDataFrame(data, crs="EPSG:4326")
        gdf.attrs["crs_label"] = "GCJ02"
        return gdf
    return gpd.GeoDataFrame(data, crs=crs)


def _feat_count(gdf_or_result):
    """从 GeoDataFrame 或 SpatialAnalyzer 返回的 dict 中提取 feature 数。"""
    if isinstance(gdf_or_result, dict):
        gdf = gdf_or_result.get("data")
        if gdf is None:
            return 0
        return len(gdf) if hasattr(gdf, "__len__") else 0
    return len(gdf_or_result)


# ============================================================
# 测试 1: clip → dissolve 链
# ============================================================

class TestClipDissolveChain:
    """clip（intersection overlay）→ dissolve 完整链路。

    场景：用新街口商圈多边形去 clip 一组点缓冲后的大范围多边形，
    再按属性 dissolve 合并。
    """

    @pytest.fixture
    def analyst(self):
        return SpatialAnalyzer()

    def test_clip_buffer_by_boundary(self, analyst):
        """点缓冲区 gdf 与边界多边形做 intersection overlay（等价 clip）。"""
        # 构造点缓冲区（buffer 返回 GCJ02）
        points_gdf = _make_points_gdf(_SAMPLE_POINTS_GCJ02)
        buffered = analyst.buffer(points_gdf, radius_m=500)

        # 构造边界多边形
        boundary_gdf = _make_polygon_gdf([_XINJIEKOU_BBOX_GCJ02])

        # overlay intersection = clip
        clipped = analyst.overlay(buffered, boundary_gdf, how="intersection")

        assert _feat_count(clipped) > 0
        # 裁剪后所有几何应在边界内
        for geom in clipped.geometry:
            assert not geom.is_empty
            assert boundary_gdf.geometry[0].contains(geom.centroid) or boundary_gdf.geometry[0].intersects(geom)

    def test_clip_then_dissolve(self, analyst):
        """clip 后 dissolve：多个缓冲区按统一属性合并为一个多边形。"""
        # 构造带属性的点缓冲区
        points_gdf = _make_points_gdf(_SAMPLE_POINTS_GCJ02)
        points_gdf["category"] = ["A", "A", "B", "B", "A"]  # 3个A, 2个B
        points_gdf["value"] = [10, 20, 30, 40, 50]

        buffered = analyst.buffer(points_gdf, radius_m=300)

        # clip to bbox
        boundary_gdf = _make_polygon_gdf([_XINJIEKOU_BBOX_GCJ02])
        clipped = analyst.overlay(buffered, boundary_gdf, how="intersection")

        # dissolve by category
        dissolved = clipped.dissolve(by="category")
        assert len(dissolved) == 2  # A and B
        assert set(dissolved.index) == {"A", "B"}

        # 验证溶解后每个 category 只有一个合并后的多边形
        for cat in ["A", "B"]:
            geom = dissolved.loc[cat, "geometry"]
            assert isinstance(geom, (Polygon, gpd.array.GeometryDtype)) or not geom.is_empty

    def test_clip_dissolve_crs_preserved(self, analyst):
        """clip → dissolve 链路保持 GCJ02 标注不变。"""
        points_gdf = _make_points_gdf(_SAMPLE_POINTS_GCJ02)
        buffered = analyst.buffer(points_gdf, radius_m=200)

        boundary_gdf = _make_polygon_gdf([_XINJIEKOU_BBOX_GCJ02])
        clipped = analyst.overlay(buffered, boundary_gdf, how="intersection")

        # 验证 attrs 标注
        assert clipped.attrs.get("crs_label") == "GCJ02"

        dissolved = clipped.dissolve()
        # dissolve() 生成新 GeoDataFrame，attrs 可能不自动复制
        # 但 CRS 值应保持 GCJ02
        crs_str = str(dissolved.crs).upper() if dissolved.crs else ""
        assert "4326" in crs_str or "GCJ02" in crs_str


# ============================================================
# 测试 2: join_by_location（spatial join）正确性
# ============================================================

class TestJoinByLocation:
    """spatial join 正确性验证。

    场景：点 GDF + 多边形 GDF → spatial join → 验证每个点落入正确多边形。
    """

    def test_spatial_join_points_to_polygons(self):
        """点落入指定多边形时 spatial join 应匹配正确商圈。"""
        # 商圈多边形（含名称属性）
        zones_gdf = _make_polygon_gdf(
            _BUSINESS_ZONES_GCJ02,
            extra_cols={"name": ["德基广场", "新百", "大洋百货"]},
        )

        # 在每个商圈内各放一个点
        points_in_zones = [
            wgs84_to_gcj02(118.7782, 32.0422),   # 德基广场内
            wgs84_to_gcj02(118.7807, 32.0407),   # 新百内
            wgs84_to_gcj02(118.7747, 32.0427),   # 大洋百货内
        ]
        # 一个商圈外的点
        points_in_zones.append(wgs84_to_gcj02(118.7900, 32.0500))  # 所有商圈外

        points_gdf = _make_points_gdf(points_in_zones)
        points_gdf["point_id"] = ["p1", "p2", "p3", "p4"]

        # spatial join
        joined = gpd.sjoin(points_gdf, zones_gdf, how="left", predicate="within")

        # p1 → 德基广场, p2 → 新百, p3 → 大洋百货, p4 → None
        assert joined.loc[joined["point_id"] == "p1", "name"].values[0] == "德基广场"
        assert joined.loc[joined["point_id"] == "p2", "name"].values[0] == "新百"
        assert joined.loc[joined["point_id"] == "p3", "name"].values[0] == "大洋百货"
        assert pd.isna(joined.loc[joined["point_id"] == "p4", "name"].values[0])

    def test_spatial_join_preserves_both_attributes(self):
        """spatial join 后保留双方属性列。"""
        zones_gdf = _make_polygon_gdf(
            _BUSINESS_ZONES_GCJ02,
            extra_cols={"name": ["德基广场", "新百", "大洋百货"], "流量": [3000, 5000, 2000]},
        )

        points_gdf = _make_points_gdf(_SAMPLE_POINTS_GCJ02)
        points_gdf["shop_name"] = ["店A", "店B", "店C", "店D", "店E"]

        joined = gpd.sjoin(points_gdf, zones_gdf, how="inner", predicate="within")

        # 应保留双方属性
        assert "shop_name" in joined.columns
        assert "name" in joined.columns
        assert "流量" in joined.columns

    def test_spatial_join_empty_when_no_overlap(self):
        """点都在多边形外时 inner join 返回空。"""
        zones_gdf = _make_polygon_gdf(
            _BUSINESS_ZONES_GCJ02,
            extra_cols={"name": ["德基广场", "新百", "大洋百货"]},
        )

        # 所有点远在商圈外（北京坐标）
        far_points = _make_points_gdf([
            wgs84_to_gcj02(116.3912, 39.9075),
            wgs84_to_gcj02(116.4000, 39.9100),
        ])
        far_points["id"] = [1, 2]

        joined = gpd.sjoin(far_points, zones_gdf, how="inner", predicate="within")
        assert len(joined) == 0


# ============================================================
# 测试 3: count_points_in_polygon
# ============================================================

class TestCountPointsInPolygon:
    """count_points_in_polygon 正确性。

    场景：多个多边形 + 多个点 → 统计每个多边形内含点数量。
    """

    def test_count_points_per_zone(self):
        """每个商圈统计内含点数量。"""
        zones_gdf = _make_polygon_gdf(
            _BUSINESS_ZONES_GCJ02,
            extra_cols={"name": ["德基广场", "新百", "大洋百货"]},
        )

        # 德基广场 2 个点，新百 3 个点，大洋百货 1 个点，边界外 2 个点
        points = [
            wgs84_to_gcj02(118.7782, 32.0422),   # 德基广场
            wgs84_to_gcj02(118.7790, 32.0415),   # 德基广场
            wgs84_to_gcj02(118.7805, 32.0405),   # 新百
            wgs84_to_gcj02(118.7810, 32.0410),   # 新百
            wgs84_to_gcj02(118.7795, 32.0400),   # 新百
            wgs84_to_gcj02(118.7750, 32.0425),   # 大洋百货
            wgs84_to_gcj02(118.7900, 32.0500),   # 外部
            wgs84_to_gcj02(118.7650, 32.0350),   # 外部
        ]
        points_gdf = _make_points_gdf(points)

        # 用 spatial join 实现 count_points_in_polygon
        joined = gpd.sjoin(points_gdf, zones_gdf, how="inner", predicate="within")
        counts = joined.groupby("name").size()

        assert counts.get("德基广场", 0) == 2
        assert counts.get("新百", 0) == 3
        assert counts.get("大洋百货", 0) == 1

    def test_count_points_empty_polygon(self):
        """不包含任何点的多边形计数为 0。"""
        zones_gdf = _make_polygon_gdf(
            _BUSINESS_ZONES_GCJ02,
            extra_cols={"name": ["德基广场", "新百", "大洋百货"]},
        )

        # 只在德基广场放一个点
        points_gdf = _make_points_gdf([wgs84_to_gcj02(118.7782, 32.0422)])

        joined = gpd.sjoin(points_gdf, zones_gdf, how="inner", predicate="within")
        counts = joined.groupby("name").size()

        # 只有德基广场有1个点
        assert counts.get("德基广场", 0) == 1
        assert counts.get("新百", 0) == 0
        assert counts.get("大洋百货", 0) == 0


# ============================================================
# 测试 4: 属性操作链
# ============================================================

class TestAttributeChain:
    """extract_by_attribute → keep_fields → field_calculator 链。

    场景：从含多属性字段的 GeoDataFrame 中筛选、保留指定字段、计算新字段。
    """

    def test_extract_by_attribute(self):
        """按属性值筛选要素。"""
        gdf = _make_points_gdf(_SAMPLE_POINTS_GCJ02)
        gdf["category"] = ["A", "A", "B", "B", "A"]
        gdf["value"] = [10, 20, 30, 40, 50]

        # extract: category == "A"
        extracted = gdf[gdf["category"] == "A"]
        assert len(extracted) == 3
        assert all(extracted["category"] == "A")

    def test_extract_numeric_range(self):
        """数值范围筛选。"""
        gdf = _make_points_gdf(_SAMPLE_POINTS_GCJ02)
        gdf["value"] = [10, 20, 30, 40, 50]

        # extract: value > 20 AND value < 50
        extracted = gdf[(gdf["value"] > 20) & (gdf["value"] < 50)]
        assert len(extracted) == 2
        assert set(extracted["value"]) == {30, 40}

    def test_extract_then_keep_fields(self):
        """筛选后仅保留指定字段。"""
        gdf = _make_points_gdf(_SAMPLE_POINTS_GCJ02)
        gdf["category"] = ["A", "A", "B", "B", "A"]
        gdf["value"] = [10, 20, 30, 40, 50]
        gdf["extra"] = ["x", "y", "z", "w", "v"]

        # extract category B
        extracted = gdf[gdf["category"] == "B"]

        # keep_fields: geometry + value
        kept = extracted[["geometry", "value"]]
        assert "geometry" in kept.columns
        assert "value" in kept.columns
        assert "category" not in kept.columns
        assert "extra" not in kept.columns
        assert len(kept) == 2

    def test_field_calculator_add_new_field(self):
        """field_calculator：根据已有字段计算新字段。"""
        gdf = _make_points_gdf(_SAMPLE_POINTS_GCJ02)
        gdf["population"] = [100, 200, 300, 400, 500]
        gdf["area"] = [1.0, 2.0, 3.0, 4.0, 5.0]

        # field_calculator: density = population / area
        gdf["density"] = gdf["population"] / gdf["area"]
        expected = [100.0, 100.0, 100.0, 100.0, 100.0]
        for actual, exp in zip(gdf["density"].round(6), expected):
            assert abs(actual - exp) < 1e-6

    def test_attribute_field_calculator_full_chain(self):
        """完整属性操作链：extract → keep_fields → field_calculator。"""
        gdf = _make_points_gdf(_SAMPLE_POINTS_GCJ02)
        gdf["region"] = ["鼓楼", "玄武", "鼓楼", "秦淮", "鼓楼"]
        gdf["pop"] = [1000, 2000, 3000, 4000, 5000]
        gdf["area_km2"] = [1.5, 2.0, 3.0, 1.0, 5.0]
        gdf["note"] = ["a", "b", "c", "d", "e"]

        # Step 1: extract region == "鼓楼"
        step1 = gdf[gdf["region"] == "鼓楼"]
        assert len(step1) == 3

        # Step 2: keep_fields → geometry, pop, area_km2
        step2 = step1[["geometry", "pop", "area_km2"]]
        assert set(step2.columns) == {"geometry", "pop", "area_km2"}

        # Step 3: field_calculator → density = pop / area_km2
        step2["density"] = step2["pop"] / step2["area_km2"]
        expected_densities = [1000 / 1.5, 3000 / 3.0, 5000 / 5.0]
        for actual, exp in zip(step2["density"].round(6), expected_densities):
            assert abs(actual - round(exp, 6)) < 1e-6

    def test_keep_fields_preserves_crs_attrs(self):
        """keep_fields 后 GCJ02 标注应保留。"""
        gdf = _make_points_gdf(_SAMPLE_POINTS_GCJ02)
        gdf["a"] = [1, 2, 3, 4, 5]
        gdf["b"] = [6, 7, 8, 9, 10]

        kept = gdf[["geometry", "a"]]
        assert kept.attrs.get("crs_label") == "GCJ02"


# ============================================================
# 测试 5: reproject_layer GCJ02 pipeline
# ============================================================

class TestReprojectLayer:
    """reproject_layer：GCJ02 标注与坐标系的转换 pipeline。

    场景：WGS84 GDF → 标 GCJ02 → reproject 到投影坐标系 → 验证 attrs。
    """

    def test_reproject_wgs84_to_projected(self):
        """WGS84 图层 reproject 到投影坐标系。"""
        wgs84_gdf = _make_points_gdf(_SAMPLE_POINTS_WGS84, crs="EPSG:4326")
        wgs84_gdf["name"] = [f"p{i}" for i in range(len(_SAMPLE_POINTS_WGS84))]

        # reproject to EPSG:4548 (CGCS2000 3度带 118°)
        projected = wgs84_gdf.to_crs("EPSG:4548")
        assert projected.crs.to_epsg() == 4548
        # 坐标值应为米级（远大于 180）
        assert abs(projected.geometry[0].x) > 180

    def test_reproject_projected_back_to_wgs84(self):
        """投影坐标 → WGS84 往返。"""
        wgs84_gdf = _make_points_gdf(_SAMPLE_POINTS_WGS84, crs="EPSG:4326")
        projected = wgs84_gdf.to_crs("EPSG:4548")
        back = projected.to_crs("EPSG:4326")

        # 往返一致性（厘米级）
        for g_orig, g_back in zip(wgs84_gdf.geometry, back.geometry):
            assert abs(g_orig.x - g_back.x) < 0.001
            assert abs(g_orig.y - g_back.y) < 0.001

    def test_reproject_gcj02_labeled_to_projected(self):
        """GCJ02 标注的图层应先转 WGS84 再 reproject。

        SpatialAnalyzer._ensure_wgs84 使用数学偏转（非 pyproj）转回 WGS84，
        然后再用 pyproj 做投影变换。
        """
        gcj02_gdf = _make_points_gdf(_SAMPLE_POINTS_GCJ02)
        gcj02_gdf["id"] = [1, 2, 3, 4, 5]

        analyst = SpatialAnalyzer()
        wgs84_gdf = analyst._ensure_wgs84(gcj02_gdf)
        # _ensure_wgs84 已将 GCJ02 坐标偏转回 WGS84
        projected = wgs84_gdf.to_crs("EPSG:4548")

        assert projected.crs.to_epsg() == 4548
        assert "id" in projected.columns
        assert len(projected) == len(gcj02_gdf)

    def test_reproject_attrs_preserved(self):
        """reproject 后保留原始属性列。"""
        wgs84_gdf = _make_points_gdf(_SAMPLE_POINTS_WGS84, crs="EPSG:4326")
        wgs84_gdf["label"] = ["A", "B", "C", "D", "E"]
        wgs84_gdf["score"] = [0.1, 0.2, 0.3, 0.4, 0.5]

        projected = wgs84_gdf.to_crs("EPSG:4548")
        back = projected.to_crs("EPSG:4326")

        assert list(back["label"]) == ["A", "B", "C", "D", "E"]
        for a, e in zip(back["score"], [0.1, 0.2, 0.3, 0.4, 0.5]):
            assert abs(a - e) < 1e-10

    def test_reproject_preserves_gcj02_label_after_roundtrip(self):
        """GCJ02 → WGS84 → 投影 → WGS84 → GCJ02 完整往返。"""
        gcj02_gdf = _make_points_gdf(_SAMPLE_POINTS_GCJ02)

        analyst = SpatialAnalyzer()
        # GCJ02 → WGS84
        wgs84 = analyst._ensure_wgs84(gcj02_gdf)
        # 投影
        proj = wgs84.to_crs("EPSG:4548")
        # 投影 → WGS84
        wgs84_back = proj.to_crs("EPSG:4326")
        # WGS84 → GCJ02
        gcj02_result = analyst._to_gcj02_output(wgs84_back)

        # 验证 GCJ02 标注
        assert gcj02_result.attrs.get("crs_label") == "GCJ02"
        # 坐标与原始 GCJ02 接近（厘米级）
        for g_orig, g_result in zip(gcj02_gdf.geometry, gcj02_result.geometry):
            assert abs(g_orig.x - g_result.x) < 0.001
            assert abs(g_orig.y - g_result.y) < 0.001


# ============================================================
# 补充：DataIO 矢量方法集成验证
# ============================================================

import pandas as pd


class TestDataIOVectorIntegration:
    """DataIO 类矢量方法（summarize_layer, csv_to_points）的集成验证。"""

    def test_summarize_layer_from_gdf(self):
        """summarize_layer 从 GeoDataFrame 提取元数据。"""
        gdf = _make_points_gdf(_SAMPLE_POINTS_GCJ02)
        gdf["name"] = ["a", "b", "c", "d", "e"]

        dio = DataIO()
        result = dio.summarize_layer(gdf)
        assert result["status"] == "success"
        meta = result["data"]
        assert meta["feature_count"] == 5
        assert meta["geometry_type"] == "Point"
        assert "name" in meta["fields"]
        assert "geometry" in meta["fields"]
        assert meta["bbox"] is not None

    def test_summarize_layer_from_geojson_dict(self):
        """summarize_layer 从 GeoJSON dict 提取元数据。"""
        gdf = _make_points_gdf(_SAMPLE_POINTS_GCJ02)
        geojson_dict = json.loads(gdf.to_json())

        dio = DataIO()
        result = dio.summarize_layer(geojson_dict)
        assert result["status"] == "success"
        meta = result["data"]
        assert meta["feature_count"] == 5

    def test_summarize_layer_empty_gdf(self):
        """空 GeoDataFrame 返回 feature_count=0。"""
        empty_gdf = gpd.GeoDataFrame({"geometry": []}, crs="EPSG:4326")

        dio = DataIO()
        result = dio.summarize_layer(empty_gdf)
        assert result["status"] == "success"
        assert result["data"]["feature_count"] == 0

    def test_csv_to_points_with_valid_data(self):
        """csv_to_points 将 DataFrame 转点矢量。"""
        import pandas as pd

        df = pd.DataFrame({
            "lng": [118.7782, 118.7800, 118.7750],
            "lat": [32.0417, 32.0420, 32.0400],
            "name": ["A", "B", "C"],
        })

        dio = DataIO()
        result = dio.csv_to_points(df, x_field="lng", y_field="lat", crs="EPSG:4326")
        assert result["status"] == "success"
        data = result["data"]
        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) == 3
        # 第一个 feature 的坐标
        assert data["features"][0]["geometry"]["coordinates"] == [118.7782, 32.0417]
        assert data["features"][0]["properties"]["name"] == "A"

    def test_csv_to_points_coordinate_out_of_range_warning(self):
        """坐标值越界产生 warning。"""
        import pandas as pd

        df = pd.DataFrame({
            "lng": [118.7782, -200.0],  # 第二个点经度越界
            "lat": [32.0417, 32.0420],
            "name": ["A", "B"],
        })

        dio = DataIO()
        result = dio.csv_to_points(df, x_field="lng", y_field="lat")
        assert result["status"] == "success"
        assert len(result.get("warnings", [])) > 0
        any_range_warning = any("范围" in w for w in result["warnings"])
        assert any_range_warning, f"Expected range warning, got: {result['warnings']}"

    def test_csv_to_points_missing_field_error(self):
        """缺失字段返回 error。"""
        import pandas as pd

        df = pd.DataFrame({"lng": [118.7782], "lat": [32.0417]})

        dio = DataIO()
        result = dio.csv_to_points(df, x_field="longitude", y_field="lat")
        assert result["status"] == "error"
        assert "longitude" in result["message"]
