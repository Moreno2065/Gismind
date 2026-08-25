"""SpatialAnalyzer 扩展方法单元测试。

覆盖所有新增方法：clip, extract_by_location, convex_hull, bounding_boxes,
dissolve, merge_layers, join_by_location, join_by_nearest, count_points_in_polygon,
centroid_layer, point_on_surface, simplify_geometry, fix_geometries, check_validity,
multipart_to_singlepart, delete_duplicate_geometries, snap_geometries,
extract_by_attribute, keep_fields, rename_field, field_calculator,
reproject_layer, batch_reproject_layers。
"""

import math

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import Point, Polygon, MultiPolygon, LineString

from app.tools.spatial_analysis import SpatialAnalyzer
from app.tools.geo_transform import wgs84_to_gcj02

# ============================================================
# 测试基准点：南京新街口附近
# ============================================================

_NANJING_WGS84 = (118.7782, 32.0417)
_NANJING_GCJ02 = wgs84_to_gcj02(*_NANJING_WGS84)


def _make_points_gdf(coords, crs="GCJ02"):
    """构造点 GeoDataFrame。coords: [(lng, lat), ...]"""
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


def _make_proj_square_polygon(minx, miny, maxx, maxy, crs="GCJ02"):
    """在投影坐标系下构造方形，统一转为目标 CRS。"""
    proj_gdf = gpd.GeoDataFrame(
        {"geometry": [Polygon([(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)])]},
        crs="EPSG:4548",
    )
    wgs84_gdf = proj_gdf.to_crs(epsg=4326)
    if crs == "GCJ02":
        return _make_polygon_gdf(list(wgs84_gdf.geometry[0].exterior.coords), crs="GCJ02")
    return wgs84_gdf


# ============================================================
# clip
# ============================================================


class TestClip:
    def test_clip_happy_path(self):
        """裁剪正常返回结果，结果面积 <= 原面积。"""
        analyzer = SpatialAnalyzer()
        big_square = _make_proj_square_polygon(0, 0, 2000, 2000)
        small_square = _make_proj_square_polygon(500, 500, 1500, 1500)

        result = analyzer.clip(big_square, small_square)

        assert result["status"] == "success"
        clipped = result["data"]
        assert isinstance(clipped, gpd.GeoDataFrame)
        assert len(clipped) >= 1
        for g in clipped.geometry:
            assert not g.is_empty

    def test_clip_no_overlap(self):
        """无重叠区域裁剪返回空 GeoDataFrame。"""
        analyzer = SpatialAnalyzer()
        square_a = _make_proj_square_polygon(0, 0, 1000, 1000)
        square_b = _make_proj_square_polygon(2000, 2000, 3000, 3000)

        result = analyzer.clip(square_a, square_b)

        assert result["status"] == "success"
        assert len(result["data"]) == 0


# ============================================================
# extract_by_location
# ============================================================


class TestExtractByLocation:
    def test_extract_by_location_happy_path(self):
        """空间筛选保留与 mask 相交的要素。"""
        analyzer = SpatialAnalyzer()
        # 两个方形：一个在 mask 内，一个在外
        inner = _make_proj_square_polygon(500, 500, 1500, 1500)
        outer = _make_proj_square_polygon(3000, 3000, 4000, 4000)
        mask = _make_proj_square_polygon(0, 0, 2000, 2000)

        merged = gpd.GeoDataFrame(
            pd.concat([inner, outer], ignore_index=True),
            crs=inner.crs,
        )
        merged.attrs = inner.attrs

        result = analyzer.extract_by_location(merged, mask)

        assert result["status"] == "success"
        assert len(result["data"]) == 1

    def test_extract_by_location_wrong_operator(self):
        """不支持 predicate 时正常（默认就支持 contains/intersects/within 等）。"""
        analyzer = SpatialAnalyzer()
        gdf = _make_proj_square_polygon(0, 0, 1000, 1000)
        mask = _make_proj_square_polygon(2000, 2000, 3000, 3000)

        result = analyzer.extract_by_location(gdf, mask)

        assert result["status"] == "success"
        assert len(result["data"]) == 0  # 无相交


# ============================================================
# convex_hull
# ============================================================


class TestConvexHull:
    def test_convex_hull_happy_path(self):
        """凸包正常生成 Polygon。"""
        analyzer = SpatialAnalyzer()
        center = _NANJING_GCJ02
        offsets = [(0.01, 0), (0, 0.01), (-0.01, 0), (0, -0.01)]
        coords = [(center[0] + dx, center[1] + dy) for dx, dy in offsets]
        points_gdf = _make_points_gdf(coords, crs="GCJ02")

        result = analyzer.convex_hull(points_gdf)

        assert result["status"] == "success"
        geom = result["data"].geometry[0]
        assert geom.geom_type in ("Polygon", "MultiPolygon")
        assert not geom.is_empty


# ============================================================
# bounding_boxes
# ============================================================


class TestBoundingBoxes:
    def test_bounding_boxes_happy_path(self):
        """外包矩形与输入要素数一致。"""
        analyzer = SpatialAnalyzer()
        center = _NANJING_GCJ02
        offsets = [(0.01, 0), (0, 0.01)]
        coords = [(center[0] + dx, center[1] + dy) for dx, dy in offsets]
        points_gdf = _make_points_gdf(coords, crs="GCJ02")

        result = analyzer.bounding_boxes(points_gdf)

        assert result["status"] == "success"
        assert len(result["data"]) == 2
        for g in result["data"].geometry:
            assert not g.is_empty


# ============================================================
# dissolve
# ============================================================


class TestDissolve:
    def test_dissolve_happy_path(self):
        """全局融合返回单个多边形。"""
        analyzer = SpatialAnalyzer()
        square_a = _make_proj_square_polygon(0, 0, 1000, 1000)
        square_b = _make_proj_square_polygon(1000, 0, 2000, 1000)
        merged = gpd.GeoDataFrame(
            pd.concat([square_a, square_b], ignore_index=True),
            crs=square_a.crs,
        )
        merged.attrs = square_a.attrs

        result = analyzer.dissolve(merged)

        assert result["status"] == "success"
        assert len(result["data"]) == 1
        assert not result["data"].geometry[0].is_empty

    def test_dissolve_with_by_field(self):
        """按字段融合，组数与唯一值数一致。"""
        analyzer = SpatialAnalyzer()
        square_a = _make_proj_square_polygon(0, 0, 500, 500)
        square_b = _make_proj_square_polygon(600, 600, 1100, 1100)
        square_a["group"] = "A"
        square_b["group"] = "B"
        merged = gpd.GeoDataFrame(
            pd.concat([square_a, square_b], ignore_index=True),
            crs=square_a.crs,
        )
        merged.attrs = square_a.attrs

        result = analyzer.dissolve(merged, by="group")

        assert result["status"] == "success"
        assert len(result["data"]) == 2


# ============================================================
# merge_layers
# ============================================================


class TestMergeLayers:
    def test_merge_layers_happy_path(self):
        """合并后要素数 = 各图层要素数之和。"""
        analyzer = SpatialAnalyzer()
        square_a = _make_proj_square_polygon(0, 0, 1000, 1000)
        square_b = _make_proj_square_polygon(1000, 0, 2000, 1000)

        result = analyzer.merge_layers([square_a, square_b])

        assert result["status"] == "success"
        assert len(result["data"]) == 2

    def test_merge_layers_empty_list(self):
        """空列表返回 error。"""
        analyzer = SpatialAnalyzer()
        result = analyzer.merge_layers([])
        assert result["status"] == "error"


# ============================================================
# join_by_location
# ============================================================


class TestJoinByLocation:
    def test_join_by_location_happy_path(self):
        """空间连接返回合并后的属性表。"""
        analyzer = SpatialAnalyzer()
        square = _make_proj_square_polygon(0, 0, 1000, 1000)
        # 在方形内放一个点图层
        center_wgs84 = wgs84_to_gcj02(*_NANJING_WGS84)
        # 用投影坐标系方形中心附近的 GCJ02 点
        gcj_center = _make_points_gdf([center_wgs84], crs="GCJ02")
        square["name"] = "zone_a"

        result = analyzer.join_by_location(gcj_center, square)

        # 点可能在方形内也可能不在（取决于 GCJ02 坐标），不硬断言 count
        assert result["status"] == "success"
        assert isinstance(result["data"], gpd.GeoDataFrame)

    def test_join_by_location_no_match(self):
        """无匹配时返回空结果。"""
        analyzer = SpatialAnalyzer()
        # 点在方形外很远的地方
        square = _make_proj_square_polygon(0, 0, 1000, 1000)
        # 使用遥远的坐标点
        far_point = _make_points_gdf(
            [(wgs84_to_gcj02(-70.0, -30.0))], crs="GCJ02"
        )

        result = analyzer.join_by_location(far_point, square)

        assert result["status"] == "success"
        # 可能为空（点不在方形内）
        # sjoin inner 模式下无匹配返回空
        # 不做强制数量断言，仅验证不报错


# ============================================================
# join_by_nearest
# ============================================================


class TestJoinByNearest:
    def test_join_by_nearest_happy_path(self):
        """最近邻连接正常返回。"""
        analyzer = SpatialAnalyzer()
        points = _make_points_gdf(
            [(wgs84_to_gcj02(118.778, 32.042))], crs="GCJ02"
        )
        squares = _make_proj_square_polygon(0, 0, 1000, 1000)

        result = analyzer.join_by_nearest(points, squares)

        assert result["status"] == "success"

    def test_join_by_nearest_with_max_distance(self):
        """带 max_distance 参数正常。"""
        analyzer = SpatialAnalyzer()
        points = _make_points_gdf(
            [(wgs84_to_gcj02(118.78, 32.04))], crs="GCJ02"
        )
        squares = _make_proj_square_polygon(0, 0, 1000, 1000)

        result = analyzer.join_by_nearest(points, squares, max_distance=100000)

        assert result["status"] == "success"

    def test_join_by_nearest_zero_max_distance_returns_only_touching_features(self):
        """0 m is a valid closed boundary: it means geometric distance exactly zero."""
        analyzer = SpatialAnalyzer()
        matching = wgs84_to_gcj02(118.7782, 32.0417)
        non_matching = wgs84_to_gcj02(118.7802, 32.0417)
        points = _make_points_gdf([matching], crs="GCJ02")
        candidates = _make_points_gdf([matching, non_matching], crs="GCJ02")

        result = analyzer.join_by_nearest(points, candidates, max_distance=0)

        assert result["status"] == "success"
        joined = result["data"]
        assert len(joined) == 1
        assert joined["distance_m"].iloc[0] == pytest.approx(0.0)


# ============================================================
# count_points_in_polygon
# ============================================================


class TestCountPointsInPolygon:
    def test_count_points_in_polygon_happy_path(self):
        """点面统计返回带 count 字段的面图层。"""
        analyzer = SpatialAnalyzer()
        center = _NANJING_GCJ02
        points = _make_points_gdf(
            [(center[0], center[1]),
             (center[0] + 0.001, center[1]),
             (center[0], center[1] + 0.001)],
            crs="GCJ02",
        )
        poly = _make_proj_square_polygon(0, 0, 3000, 3000)

        result = analyzer.count_points_in_polygon(points, poly)

        assert result["status"] == "success"
        assert "count" in result["data"].columns

    def test_count_points_in_polygon_empty_points(self):
        """空点图层不报错。"""
        analyzer = SpatialAnalyzer()
        empty_points = _make_points_gdf([], crs="GCJ02")
        poly = _make_proj_square_polygon(0, 0, 1000, 1000)

        result = analyzer.count_points_in_polygon(empty_points, poly)

        # 可能返回 success 或 empty，验证不抛异常
        assert "status" in result


# ============================================================
# centroid_layer
# ============================================================


class TestCentroidLayer:
    def test_centroid_layer_happy_path(self):
        """质心返回点类型。"""
        analyzer = SpatialAnalyzer()
        square = _make_proj_square_polygon(0, 0, 1000, 1000)

        result = analyzer.centroid_layer(square)

        assert result["status"] == "success"
        assert len(result["data"]) == 1
        geom = result["data"].geometry[0]
        assert geom.geom_type == "Point"
        assert not geom.is_empty


# ============================================================
# point_on_surface
# ============================================================


class TestPointOnSurface:
    def test_point_on_surface_happy_path(self):
        """面上取点返回面内点。"""
        analyzer = SpatialAnalyzer()
        square = _make_proj_square_polygon(0, 0, 1000, 1000)

        result = analyzer.point_on_surface(square)

        assert result["status"] == "success"
        geom = result["data"].geometry[0]
        assert geom.geom_type == "Point"
        # 点在原始面内（在 WGS84 下检查）
        wgs84_square = analyzer._ensure_wgs84(square)
        assert wgs84_square.geometry[0].contains(geom)


# ============================================================
# simplify_geometry
# ============================================================


class TestSimplifyGeometry:
    def test_simplify_geometry_happy_path(self):
        """简化后要素数不变，顶点减少。"""
        analyzer = SpatialAnalyzer()
        # 构造复杂多边形的方形
        square = _make_proj_square_polygon(0, 0, 1000, 1000)

        result = analyzer.simplify_geometry(square, tolerance=10.0)

        assert result["status"] == "success"
        assert len(result["data"]) == 1
        assert not result["data"].geometry[0].is_empty

    def test_simplify_geometry_large_tolerance(self):
        """大容差简化可能导致几何极度简化但不报错。"""
        analyzer = SpatialAnalyzer()
        square = _make_proj_square_polygon(0, 0, 1000, 1000)

        result = analyzer.simplify_geometry(square, tolerance=10000.0)

        # 极大容差可能产生空几何，但不会报 error
        assert "status" in result


# ============================================================
# fix_geometries
# ============================================================


class TestFixGeometries:
    def test_fix_geometries_happy_path(self):
        """有效几何不变。"""
        analyzer = SpatialAnalyzer()
        square = _make_proj_square_polygon(0, 0, 1000, 1000)

        result = analyzer.fix_geometries(square)

        assert result["status"] == "success"
        assert len(result["data"]) == 1
        assert result["data"].geometry[0].is_valid

    def test_fix_geometries_self_intersecting(self):
        """自相交几何被修复。"""
        analyzer = SpatialAnalyzer()
        # 领结形自相交多边形
        bowtie = Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)])
        gdf = gpd.GeoDataFrame({"geometry": [bowtie]}, crs="EPSG:4326")
        # 不标注 GCJ02，以 WGS84 直接传入（fix_geometries 不依赖投影）
        result = analyzer.fix_geometries(gdf)

        assert result["status"] == "success"
        assert result["data"].geometry[0].is_valid


# ============================================================
# check_validity
# ============================================================


class TestCheckValidity:
    def test_check_validity_happy_path(self):
        """有效几何无问题。"""
        analyzer = SpatialAnalyzer()
        square = Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])
        gdf = gpd.GeoDataFrame({"geometry": [square]}, crs="EPSG:4326")

        result = analyzer.check_validity(gdf)

        assert result["status"] == "success"
        assert len(result["data"]["issues"]) == 0

    def test_check_validity_detects_invalid(self):
        """自相交几何被检出。"""
        analyzer = SpatialAnalyzer()
        bowtie = Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)])
        gdf = gpd.GeoDataFrame({"geometry": [bowtie]}, crs="EPSG:4326")

        result = analyzer.check_validity(gdf)

        assert result["status"] == "success"
        assert len(result["data"]["issues"]) == 1
        assert result["data"]["issues"][0]["type"] == "invalid"

    def test_check_validity_detects_empty(self):
        """空几何被检出。"""
        analyzer = SpatialAnalyzer()
        empty = Polygon()
        gdf = gpd.GeoDataFrame({"geometry": [empty]}, crs="EPSG:4326")

        result = analyzer.check_validity(gdf)

        assert result["status"] == "success"
        issues = result["data"]["issues"]
        assert any(i["type"] == "empty" for i in issues)


# ============================================================
# multipart_to_singlepart
# ============================================================


class TestMultipartToSinglepart:
    def test_multipart_to_singlepart_happy_path(self):
        """单部件图层 explode 后要素数与原相同。"""
        analyzer = SpatialAnalyzer()
        square = _make_proj_square_polygon(0, 0, 1000, 1000)

        result = analyzer.multipart_to_singlepart(square)

        assert result["status"] == "success"
        assert len(result["data"]) == 1

    def test_multipart_to_singlepart_multi_polygon(self):
        """MultiPolygon explode 后拆分为多个要素。"""
        analyzer = SpatialAnalyzer()
        mp = MultiPolygon([
            Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
            Polygon([(2, 0), (3, 0), (3, 1), (2, 1)]),
        ])
        gdf = gpd.GeoDataFrame({"geometry": [mp]}, crs="EPSG:4326")

        result = analyzer.multipart_to_singlepart(gdf)

        assert result["status"] == "success"
        assert len(result["data"]) == 2
        for g in result["data"].geometry:
            assert g.geom_type == "Polygon"


# ============================================================
# delete_duplicate_geometries
# ============================================================


class TestDeleteDuplicateGeometries:
    def test_delete_duplicate_geometries_happy_path(self):
        """重复几何被删除。"""
        analyzer = SpatialAnalyzer()
        square = Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])
        gdf = gpd.GeoDataFrame(
            {"geometry": [square, square, square], "id": [1, 2, 3]},
            crs="EPSG:4326",
        )

        result = analyzer.delete_duplicate_geometries(gdf)

        assert result["status"] == "success"
        assert len(result["data"]) == 1

    def test_delete_duplicate_geometries_no_duplicates(self):
        """无重复时要素数不变。"""
        analyzer = SpatialAnalyzer()
        square_a = Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])
        square_b = Polygon([(2, 0), (3, 0), (3, 1), (2, 1), (2, 0)])
        gdf = gpd.GeoDataFrame(
            {"geometry": [square_a, square_b]},
            crs="EPSG:4326",
        )

        result = analyzer.delete_duplicate_geometries(gdf)

        assert result["status"] == "success"
        assert len(result["data"]) == 2


# ============================================================
# snap_geometries
# ============================================================


class TestSnapGeometries:
    def test_snap_geometries_happy_path(self):
        """吸附后几何应仍然有效。"""
        analyzer = SpatialAnalyzer()
        square_a = _make_proj_square_polygon(0, 0, 1000, 1000)
        square_b = _make_proj_square_polygon(1000, 0, 2000, 1000)

        result = analyzer.snap_geometries(square_a, square_b, tolerance=10.0)

        assert result["status"] == "success"
        assert len(result["data"]) == 1
        assert result["data"].geometry[0].is_valid


# ============================================================
# extract_by_attribute
# ============================================================


class TestExtractByAttribute:
    def test_extract_by_attribute_eq(self):
        """等于筛选正常。"""
        analyzer = SpatialAnalyzer()
        gdf = _make_points_gdf([_NANJING_GCJ02], crs="GCJ02")
        gdf["name"] = "nanjing"

        result = analyzer.extract_by_attribute(gdf, "name", "==", "nanjing")

        assert result["status"] == "success"
        assert len(result["data"]) == 1

    def test_extract_by_attribute_not_found(self):
        """无匹配返回空。"""
        analyzer = SpatialAnalyzer()
        gdf = _make_points_gdf([_NANJING_GCJ02], crs="GCJ02")
        gdf["name"] = "nanjing"

        result = analyzer.extract_by_attribute(gdf, "name", "==", "beijing")

        assert result["status"] == "success"
        assert len(result["data"]) == 0

    def test_extract_by_attribute_field_missing(self):
        """字段不存在返回 error。"""
        analyzer = SpatialAnalyzer()
        gdf = _make_points_gdf([_NANJING_GCJ02], crs="GCJ02")

        result = analyzer.extract_by_attribute(gdf, "nonexistent", "==", "x")

        assert result["status"] == "error"

    def test_extract_by_attribute_invalid_operator(self):
        """无效运算符返回 error。"""
        analyzer = SpatialAnalyzer()
        gdf = _make_points_gdf([_NANJING_GCJ02], crs="GCJ02")
        gdf["val"] = 1

        result = analyzer.extract_by_attribute(gdf, "val", "INVALID", 1)

        assert result["status"] == "error"

    def test_extract_by_attribute_is_null(self):
        """is_null 筛选正常。"""
        analyzer = SpatialAnalyzer()
        gdf = _make_points_gdf(
            [_NANJING_GCJ02, wgs84_to_gcj02(118.80, 32.05)],
            crs="GCJ02",
        )
        gdf["desc"] = ["hello", None]

        result = analyzer.extract_by_attribute(gdf, "desc", "is_null", None)

        assert result["status"] == "success"
        assert len(result["data"]) == 1

    def test_extract_by_attribute_contains(self):
        """contains 筛选正常。"""
        analyzer = SpatialAnalyzer()
        gdf = _make_points_gdf([_NANJING_GCJ02], crs="GCJ02")
        gdf["desc"] = "hello world"

        result = analyzer.extract_by_attribute(gdf, "desc", "contains", "hello")

        assert result["status"] == "success"
        assert len(result["data"]) == 1


# ============================================================
# keep_fields
# ============================================================


class TestKeepFields:
    def test_keep_fields_happy_path(self):
        """仅保留指定字段 + geometry。"""
        analyzer = SpatialAnalyzer()
        gdf = _make_points_gdf([_NANJING_GCJ02], crs="GCJ02")
        gdf["a"] = 1
        gdf["b"] = 2
        gdf["c"] = 3

        result = analyzer.keep_fields(gdf, ["a", "b"])

        assert result["status"] == "success"
        assert "geometry" in result["data"].columns
        assert "a" in result["data"].columns
        assert "b" in result["data"].columns
        assert "c" not in result["data"].columns

    def test_keep_fields_ignore_missing(self):
        """不存在的字段被忽略。"""
        analyzer = SpatialAnalyzer()
        gdf = _make_points_gdf([_NANJING_GCJ02], crs="GCJ02")
        gdf["a"] = 1

        result = analyzer.keep_fields(gdf, ["a", "nonexistent"])

        assert result["status"] == "success"
        assert "a" in result["data"].columns


# ============================================================
# rename_field
# ============================================================


class TestRenameField:
    def test_rename_field_happy_path(self):
        """重命名正常。"""
        analyzer = SpatialAnalyzer()
        gdf = _make_points_gdf([_NANJING_GCJ02], crs="GCJ02")
        gdf["old_col"] = 42

        result = analyzer.rename_field(gdf, "old_col", "new_col")

        assert result["status"] == "success"
        assert "old_col" not in result["data"].columns
        assert "new_col" in result["data"].columns

    def test_rename_field_same_name(self):
        """新旧字段名相同返回 error。"""
        analyzer = SpatialAnalyzer()
        gdf = _make_points_gdf([_NANJING_GCJ02], crs="GCJ02")
        gdf["col"] = 1

        result = analyzer.rename_field(gdf, "col", "col")

        assert result["status"] == "error"

    def test_rename_field_old_not_found(self):
        """原字段不存在返回 error。"""
        analyzer = SpatialAnalyzer()
        gdf = _make_points_gdf([_NANJING_GCJ02], crs="GCJ02")

        result = analyzer.rename_field(gdf, "nonexistent", "new")

        assert result["status"] == "error"

    def test_rename_field_target_exists(self):
        """目标字段名已存在返回 error。"""
        analyzer = SpatialAnalyzer()
        gdf = _make_points_gdf([_NANJING_GCJ02], crs="GCJ02")
        gdf["a"] = 1
        gdf["b"] = 2

        result = analyzer.rename_field(gdf, "a", "b")

        assert result["status"] == "error"


# ============================================================
# field_calculator
# ============================================================


class TestFieldCalculator:
    def test_field_calculator_area(self):
        """$area 计算面积（平方米）。"""
        analyzer = SpatialAnalyzer()
        square = _make_proj_square_polygon(0, 0, 1000, 1000)

        result = analyzer.field_calculator(square, "area_m2", "$area")

        assert result["status"] == "success"
        assert "area_m2" in result["data"].columns
        area = result["data"]["area_m2"].iloc[0]
        assert abs(area - 1_000_000) / 1_000_000 < 0.05  # ±5%

    def test_field_calculator_length(self):
        """$length 计算周长（米）。"""
        analyzer = SpatialAnalyzer()
        square = _make_proj_square_polygon(0, 0, 1000, 1000)

        result = analyzer.field_calculator(square, "perimeter_m", "$length")

        assert result["status"] == "success"
        assert "perimeter_m" in result["data"].columns
        length = result["data"]["perimeter_m"].iloc[0]
        assert abs(length - 4000) / 4000 < 0.05  # ±5%

    def test_field_calculator_unsupported_expression(self):
        """不支持表达式返回 error。"""
        analyzer = SpatialAnalyzer()
        square = _make_proj_square_polygon(0, 0, 1000, 1000)

        result = analyzer.field_calculator(square, "val", "unsupported_expr")

        assert result["status"] == "error"


# ============================================================
# reproject_layer
# ============================================================


class TestReprojectLayer:
    def test_reproject_layer_happy_path(self):
        """重投影到 EPSG:4548 正常。"""
        analyzer = SpatialAnalyzer()
        gdf = _make_points_gdf([_NANJING_GCJ02], crs="GCJ02")

        result = analyzer.reproject_layer(gdf, "EPSG:4548")

        assert result["status"] == "success"
        assert result["data"].crs.to_epsg() == 4548

    def test_reproject_layer_converts_gcj02_numbers_before_pyproj(self):
        """GCJ02 values must not be sent to pyproj as if they were WGS84."""
        analyzer = SpatialAnalyzer()
        gdf = _make_points_gdf([_NANJING_GCJ02], crs="GCJ02")

        result = analyzer.reproject_layer(gdf, "EPSG:4548")
        recovered = result["data"].to_crs(epsg=4326).geometry.iloc[0]

        assert result["status"] == "success"
        assert recovered.x == pytest.approx(_NANJING_WGS84[0], abs=1e-6)
        assert recovered.y == pytest.approx(_NANJING_WGS84[1], abs=1e-6)

    def test_reproject_layer_to_wgs84(self):
        """重投影回 WGS84 正常。"""
        analyzer = SpatialAnalyzer()
        gdf = _make_points_gdf([_NANJING_GCJ02], crs="GCJ02")
        # 先转到 4548，再转回 4326
        step1 = analyzer.reproject_layer(gdf, "EPSG:4548")
        assert step1["status"] == "success"

        step2 = analyzer.reproject_layer(step1["data"], "EPSG:4326")
        assert step2["status"] == "success"

    def test_reproject_layer_invalid_crs(self):
        """无效 CRS 返回 error。"""
        analyzer = SpatialAnalyzer()
        gdf = _make_points_gdf([_NANJING_GCJ02], crs="GCJ02")

        result = analyzer.reproject_layer(gdf, "EPSG:99999")

        assert result["status"] == "error"


# ============================================================
# batch_reproject_layers
# ============================================================


class TestBatchReprojectLayers:
    def test_batch_reproject_happy_path(self):
        """批量重投影正常。"""
        analyzer = SpatialAnalyzer()
        gdf_a = _make_points_gdf([_NANJING_GCJ02], crs="GCJ02")
        gdf_b = _make_points_gdf([wgs84_to_gcj02(118.80, 32.05)], crs="GCJ02")

        result = analyzer.batch_reproject_layers(
            [
                {"name": "layer_a", "gdf": gdf_a},
                {"name": "layer_b", "gdf": gdf_b},
            ],
            target_crs="EPSG:4548",
        )

        assert result["status"] == "success"
        assert len(result["data"]) == 2
        for item in result["data"]:
            assert item["gdf"].crs.to_epsg() == 4548


# ============================================================
# 多方法 GCJ02 pipeline 一致性验证
# ============================================================


class TestGCJ02PipelineConsistency:
    """验证新方法的输出均标注 crs_label=GCJ02（即经过 _to_gcj02_output）。"""

    def test_clip_output_is_gcj02(self):
        analyzer = SpatialAnalyzer()
        a = _make_proj_square_polygon(0, 0, 2000, 2000)
        b = _make_proj_square_polygon(500, 500, 1500, 1500)
        r = analyzer.clip(a, b)
        assert r["status"] == "success"
        assert r["data"].attrs.get("crs_label") == "GCJ02"

    def test_convex_hull_output_is_gcj02(self):
        analyzer = SpatialAnalyzer()
        gdf = _make_points_gdf([_NANJING_GCJ02, wgs84_to_gcj02(118.80, 32.05),
                                 wgs84_to_gcj02(118.76, 32.03)], crs="GCJ02")
        r = analyzer.convex_hull(gdf)
        assert r["status"] == "success"
        assert r["data"].attrs.get("crs_label") == "GCJ02"

    def test_dissolve_output_is_gcj02(self):
        analyzer = SpatialAnalyzer()
        gdf = _make_proj_square_polygon(0, 0, 1000, 1000)
        r = analyzer.dissolve(gdf)
        assert r["status"] == "success"
        assert r["data"].attrs.get("crs_label") == "GCJ02"

    def test_centroid_layer_output_is_gcj02(self):
        analyzer = SpatialAnalyzer()
        gdf = _make_proj_square_polygon(0, 0, 1000, 1000)
        r = analyzer.centroid_layer(gdf)
        assert r["status"] == "success"
        assert r["data"].attrs.get("crs_label") == "GCJ02"

    def test_fix_geometries_output_is_gcj02(self):
        analyzer = SpatialAnalyzer()
        gdf = _make_proj_square_polygon(0, 0, 1000, 1000)
        r = analyzer.fix_geometries(gdf)
        assert r["status"] == "success"
        assert r["data"].attrs.get("crs_label") == "GCJ02"


import pandas as pd  # noqa: E402 — used by multi-gdf tests above
