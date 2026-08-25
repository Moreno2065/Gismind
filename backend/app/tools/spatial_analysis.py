"""空间分析引擎。

实现参考 GIS_Agent_技术文档.md §4.4 + docs/02_data_models.md §3.2。

核心设计原则（与 docs/02_data_models.md §7 一致）：
1. **GCJ02 不能直接进 pyproj**：无标准 EPSG，pyproj 会把 GCJ02 当 WGS84 处理导致整体偏移。
   所有 GCJ02 输入必须先用 `geo_transform.gcj02_to_wgs84`（数学偏转）转 WGS84，再投影计算。
2. **空间计算在投影坐标系下进行**：通过 `_resolve_projected_crs` 动态选择投影坐标系
   （中国境内用 CGCS2000 3度带，境外用 UTM），避免使用固定 EPSG 导致的远离中央经线时的系统变形。
   地理坐标系下"500m"是角度单位，缓冲结果严重变形。
3. **出口统一回 GCJ02**：供前端高德 JS API 套合国内底图。
4. **错误边界**：
   - voronoi 点数 < 4 -> error
   - voronoi 所有点共线 -> error（scipy.Voronoi 会抛 QhullError）
   - isochrone 有效采样点 < 3 -> empty（海边/路网稀疏）
   - isochrone 路径规划服务不可用 -> empty

返回值约定（dict，与 ToolResult 模型对齐）：
- 成功：{"status": "success", "data": ...}
- 空结果：{"status": "empty", "message": "..."}
- 错误：{"status": "error", "message": "..."}
"""

import logging
import math
from dataclasses import dataclass
from typing import Any, Literal, Optional

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import Point, Polygon, MultiPolygon, mapping, box
from shapely.ops import unary_union, polygonize
from scipy.spatial import Voronoi

from app.tools.geo_transform import wgs84_to_gcj02, gcj02_to_wgs84

# shapely.ops.transform passes arrays (x, y, z) in shapely >= 2.0,
# but passes scalar coordinates (x, y) in shapely < 2.0.
# Our transform helper functions expect array inputs.
_SHAPELY_GE_2 = int(shapely.__version__.split(".")[0]) >= 2

logger = logging.getLogger(__name__)


@dataclass
class GeoLayer:
    """带 CRS 语义的轻量 GeoDataFrame 包装。

    直接传 gpd.GeoDataFrame 时无法 self-document 坐标系类型（WGS84 / GCJ02 /
    PROJECTED），GeoLayer 显式声明 crs_label，让 _ensure_wgs84 无需依赖
    attrs 探测即可获知坐标系。

    Attributes:
        gdf: GeoDataFrame 数据
        crs_label: "WGS84" | "GCJ02" | "PROJECTED"，默认 WGS84
    """

    gdf: Any
    crs_label: Literal["WGS84", "GCJ02", "PROJECTED"] = "WGS84"


# WGS84 地理坐标系 EPSG
_WGS84_EPSG = 4326
# GCJ02 自定义 CRS 标识（无标准 EPSG，geopandas 接受任意字符串作为 CRS 名）
_GCJ02_CRS = "GCJ02"


def _resolve_projected_crs(gdf_wgs84):
    """确定适合 GeoDataFrame（WGS84）的动态投影坐标系。

    中国境内（73.5°E ~ 135.5°E）使用 CGCS2000 3度带，
    境外使用 WGS84 UTM（北半球 / 南半球自动判断）。

    Args:
        gdf_wgs84: crs 为 EPSG:4326 的 GeoDataFrame

    Returns:
        EPSG 整数编码，例如 4548（CGCS2000 CM 117°E）或 32630（UTM 30N）
    """
    centroid = gdf_wgs84.union_all().centroid
    lng = centroid.x

    # 中国经度范围：约 73.5°E 到 135.5°E
    if 73.5 <= lng <= 135.5:
        # CGCS2000 3度带：epsg = 4534 + round((round(lng/3)*3 - 75)/3)
        # 4534=CM75E, 4535=CM78E, ..., 4548=CM117E, ..., 4554=CM135E
        cm = round(lng / 3) * 3
        epsg = 4534 + round((cm - 75) / 3)
        return int(epsg)
    else:
        # UTM zone
        zone = int((lng + 180) / 6) + 1
        lat = centroid.y
        if lat >= 0:
            return 32600 + zone  # WGS84 UTM North
        else:
            return 32700 + zone  # WGS84 UTM South

# 等时圈径向采样方向数（8 方向，覆盖完整方位）
_ISOCHRONE_DIRECTIONS = 8
# 等时圈有效采样点下限（< 3 无法生成多边形）
_ISOCHRONE_MIN_VALID = 3


class SpatialAnalyzer:
    """空间分析引擎。

    所有计算方法遵循统一管线：
        GCJ02 输入 -> _ensure_wgs84 -> 投影坐标系计算 -> _to_gcj02_output -> GCJ02 输出

    对外暴露 buffer / overlay / voronoi / isochrone / topology_check / kernel_density /
    clip / extract_by_location / convex_hull / bounding_boxes / dissolve / merge_layers /
    join_by_location / join_by_nearest / count_points_in_polygon / centroid_layer /
    point_on_surface / simplify_geometry / fix_geometries / check_validity /
    multipart_to_singlepart / delete_duplicate_geometries / snap_geometries /
    extract_by_attribute / keep_fields / rename_field / field_calculator /
    reproject_layer / batch_reproject_layers。
    """

    def __init__(self, amap_key: str = "", amap_timeout: int = 3):
        """初始化空间分析器。

        Args:
            amap_key: 高德 Web 服务 API key（isochrone 路径规划用，可空，空则 isochrone 无法调用真实路径规划）
            amap_timeout: 高德 API 超时秒数
        """
        self.amap_key = amap_key
        self.amap_timeout = amap_timeout

    # ------------------------------------------------------------------
    # 坐标系入口/出口校验
    # ------------------------------------------------------------------

    def _ensure_wgs84(self, gdf: "gpd.GeoDataFrame | GeoLayer") -> gpd.GeoDataFrame:
        """强制入口校验：GCJ02 -> WGS84。

        GCJ02 无标准 EPSG，pyproj 不接受 "GCJ02" 字符串作为 CRS。约定通过
        `GeoDataFrame.attrs["crs_label"] == "GCJ02"` 标注（crs 字段仍为
        EPSG:4326，但坐标值是 GCJ02 偏转后的）。

        本方法按以下优先级解析坐标系标注：
        1. GeoLayer 包装：直接读取 .crs_label（显式声明，推荐方式）
        2. GeoDataFrame.attrs["crs_label"]：兼容旧的 attrs 标注方式
        3. 无标注：假定 WGS84，发出 warning

        若 crs_label 为 GCJ02，逐点应用 `gcj02_to_wgs84` 数学偏转
        （不能用 pyproj，pyproj 不认识 GCJ02）。

        Args:
            gdf: 输入 GeoDataFrame 或 GeoLayer 包装

        Returns:
            crs 为 EPSG:4326 的 GeoDataFrame，attrs 中 crs_label 已清除
        """
        # 1. GeoLayer 封装：显式 crs_label
        if isinstance(gdf, GeoLayer):
            crs_label = gdf.crs_label.upper()
            gdf = gdf.gdf
        else:
            # 2. GeoDataFrame.attrs 标注（兼容旧方式）
            crs_label = gdf.attrs.get("crs_label", "").upper() if gdf.attrs else ""

        if crs_label == "GCJ02":
            return self._gcj02_gdf_to_wgs84(gdf)

        # 3. A real projected CRS is authoritative even when attrs has no
        # semantic label (for example an explicit reproject_layer output).
        crs_epsg = gdf.crs.to_epsg() if getattr(gdf, "crs", None) else None
        if crs_epsg and crs_epsg != _WGS84_EPSG:
            result = gdf.to_crs(epsg=_WGS84_EPSG)
            result.attrs = {
                key: value
                for key, value in (gdf.attrs or {}).items()
                if key != "crs_label"
            }
            return result

        # 4. 无标注且坐标系为 EPSG:4326 时默认 WGS84
        if not crs_label:
            logger.warning(
                "_ensure_wgs84: 输入未标注 crs_label，假定为 WGS84。"
                "建议显式传入 GeoLayer(gdf, crs_label=...) 以消除歧义。"
            )
            return gdf

        if crs_label == "WGS84":
            return gdf

        # Unknown semantic label on an unprojected/unknown CRS.
        logger.warning(
            "_ensure_wgs84: 未识别的 crs_label=%r，假定为投影坐标系，直接返回。",
            crs_label,
        )
        return gdf

    def _to_gcj02_output(self, gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        """出口：WGS84 -> GCJ02，供前端高德 JS API 使用。"""
        return self._wgs84_gdf_to_gcj02(gdf)

    def _gcj02_gdf_to_wgs84(self, gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        """GeoDataFrame 整体 GCJ02 -> WGS84（逐点数学偏转）。

        输入 gdf.attrs["crs_label"]=="GCJ02"，crs 字段为 EPSG:4326（坐标值是 GCJ02）。
        输出 crs 为 EPSG:4326（坐标值是 WGS84），attrs 中 crs_label 清除。
        """
        # NOTE: shapely.ops.transform passes arrays (x, y) per geometry in shapely >= 2.0,
        # but scalar (x, y) per coordinate in shapely < 2.0. This code handles both paths.
        from shapely.ops import transform

        gdf = gdf.copy()

        def _transform_geom(geom):
            def _fn(x, y, z=None):
                if _SHAPELY_GE_2:
                    # shapely >= 2.0: x, y are coordinate arrays for the entire geometry
                    new_coords = []
                    for xi, yi in zip(x, y):
                        nx, ny = gcj02_to_wgs84(float(xi), float(yi))
                        new_coords.append((nx, ny))
                    nx_arr = np.array([c[0] for c in new_coords])
                    ny_arr = np.array([c[1] for c in new_coords])
                    if z is not None:
                        return (nx_arr, ny_arr, z)
                    return (nx_arr, ny_arr)
                else:
                    # shapely < 2.0: x, y are scalar values per coordinate
                    nx, ny = gcj02_to_wgs84(float(x), float(y))
                    if z is not None:
                        return (nx, ny, z)
                    return (nx, ny)

            return transform(_fn, geom)

        new_geoms = [_transform_geom(g) for g in gdf.geometry]
        result = gpd.GeoDataFrame({"geometry": new_geoms}, crs=f"EPSG:{_WGS84_EPSG}")
        # 保留原属性列
        for col in gdf.columns:
            if col != "geometry":
                result[col] = gdf[col].values
        # WGS84 输出清除 crs_label
        result.attrs = {k: v for k, v in (gdf.attrs or {}).items() if k != "crs_label"}
        return result

    def _wgs84_gdf_to_gcj02(self, gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        """GeoDataFrame 整体 WGS84 -> GCJ02（逐点数学偏转）。

        输入 crs 为 EPSG:4326（或其他投影，先转 4326）。
        输出 crs 字段为 EPSG:4326（坐标值是 GCJ02），attrs["crs_label"]="GCJ02"。
        """
        # NOTE: See _SHAPELY_GE_2 comment in _gcj02_gdf_to_wgs84 above.
        from shapely.ops import transform

        gdf = gdf.copy()
        # 若输入不是 EPSG:4326，先转 WGS84 地理坐标
        crs_str = gdf.crs.to_string().upper() if gdf.crs else ""
        if "EPSG:4326" not in crs_str:
            gdf = gdf.to_crs(epsg=_WGS84_EPSG)

        def _transform_geom(geom):
            def _fn(x, y, z=None):
                if _SHAPELY_GE_2:
                    # shapely >= 2.0: x, y are coordinate arrays
                    new_coords = []
                    for xi, yi in zip(x, y):
                        nx, ny = wgs84_to_gcj02(float(xi), float(yi))
                        new_coords.append((nx, ny))
                    nx_arr = np.array([c[0] for c in new_coords])
                    ny_arr = np.array([c[1] for c in new_coords])
                    if z is not None:
                        return (nx_arr, ny_arr, z)
                    return (nx_arr, ny_arr)
                else:
                    # shapely < 2.0: x, y are scalar values
                    nx, ny = wgs84_to_gcj02(float(x), float(y))
                    if z is not None:
                        return (nx, ny, z)
                    return (nx, ny)

            return transform(_fn, geom)

        new_geoms = [_transform_geom(g) for g in gdf.geometry]
        result = gpd.GeoDataFrame({"geometry": new_geoms}, crs=f"EPSG:{_WGS84_EPSG}")
        for col in gdf.columns:
            if col != "geometry":
                result[col] = gdf[col].values
        # GCJ02 输出标注 crs_label
        result.attrs = dict(gdf.attrs or {})
        result.attrs["crs_label"] = "GCJ02"
        return result

    # ------------------------------------------------------------------
    # 缓冲区分析
    # ------------------------------------------------------------------

    def buffer(self, points_gdf: gpd.GeoDataFrame, radius_m: float) -> gpd.GeoDataFrame:
        """等距缓冲：GCJ02 -> WGS84 -> 投影计算 -> 结果回 GCJ02。

        Args:
            points_gdf: 点 GeoDataFrame（GCJ02 或 WGS84）
            radius_m: 缓冲距离，米

        Returns:
            GeoDataFrame，含缓冲 Polygon，crs 为 GCJ02
        """
        wgs84_gdf = self._ensure_wgs84(points_gdf)
        epsg = _resolve_projected_crs(wgs84_gdf)
        projected = wgs84_gdf.to_crs(epsg=epsg)
        buffered = projected.buffer(radius_m)
        # 包装回 GeoDataFrame（保留属性）
        proj_result = gpd.GeoDataFrame(
            {**{c: projected[c].values for c in projected.columns if c != "geometry"},
             "geometry": buffered},
            crs=f"EPSG:{epsg}",
        )
        wgs84_result = proj_result.to_crs(epsg=_WGS84_EPSG)
        return self._to_gcj02_output(wgs84_result)

    # ------------------------------------------------------------------
    # 叠加分析
    # ------------------------------------------------------------------

    def overlay(
        self,
        gdf_a: gpd.GeoDataFrame,
        gdf_b: gpd.GeoDataFrame,
        how: str = "intersection",
    ) -> gpd.GeoDataFrame:
        """叠加分析：GCJ02 -> WGS84 -> 投影计算 -> 结果回 GCJ02。

        Args:
            gdf_a: 输入 A（面/线/点）
            gdf_b: 输入 B
            how: intersection / union / difference / symmetric_difference

        Returns:
            GeoDataFrame，叠加结果，crs 为 GCJ02
        """
        wgs84_a = self._ensure_wgs84(gdf_a)
        wgs84_b = self._ensure_wgs84(gdf_b)
        epsg = _resolve_projected_crs(wgs84_a)
        proj_a = wgs84_a.to_crs(epsg=epsg)
        proj_b = wgs84_b.to_crs(epsg=epsg)
        result = gpd.overlay(proj_a, proj_b, how=how)
        wgs84_result = result.to_crs(epsg=_WGS84_EPSG)
        return self._to_gcj02_output(wgs84_result)

    # ------------------------------------------------------------------
    # 泰森多边形
    # ------------------------------------------------------------------

    def voronoi(
        self,
        points_gdf: gpd.GeoDataFrame,
        boundary: Optional[Polygon] = None,
    ) -> dict:
        """泰森多边形：GCJ02 -> WGS84 -> 投影计算 -> 结果回 GCJ02。

        边界条件：
        - 点数 < 4：返回 error（scipy.Voronoi 需至少 4 个点形成 3D 凸包）
        - 所有点共线：返回 error（QhullError）

        Args:
            points_gdf: 点 GeoDataFrame（GCJ02 或 WGS84）
            boundary: 可选裁剪边界（Polygon，坐标系与输入一致）

        Returns:
            {"status": "success", "data": GeoDataFrame} 或
            {"status": "error", "message": str}
        """
        # 步骤 1：点数校验（共线校验需在投影坐标下做，GCJ02 偏转会使同纬度点
        # 产生微小纬度差异，原始坐标判共线不可靠）
        coords_orig = np.array([(p.x, p.y) for p in points_gdf.geometry])
        if len(coords_orig) < 4:
            return {"status": "error", "message": "点数过少(需至少4个), 无法生成泰森多边形"}

        try:
            # 步骤 2：GCJ02 -> WGS84 -> 投影
            wgs84_gdf = self._ensure_wgs84(points_gdf)
            epsg = _resolve_projected_crs(wgs84_gdf)
            projected = wgs84_gdf.to_crs(epsg=epsg)
            coords = np.array([(p.x, p.y) for p in projected.geometry])

            # 步骤 3：投影坐标下判共线（数值精度稳定）
            if self._is_collinear(coords):
                return {"status": "error", "message": "所有点共线, 无法生成泰森多边形"}

            vor = Voronoi(coords)

            # 步骤 4：从 voronoi 图组装每个点的"势力范围"多边形
            polygons = self._build_voronoi_polygons(vor, len(coords))

            # 步骤 5：boundary 裁剪
            # 无穷远顶点会延伸到百万米级，GCJ02 数学偏转在大跨度上非线性，
            # 转回地理坐标会自相交。必须用边界裁剪到合理范围。
            if boundary is not None:
                # 显式 boundary：坐标系与输入一致，同步 attrs 后转 WGS84 投影裁剪
                boundary_gdf = gpd.GeoDataFrame(
                    {"geometry": [boundary]}, crs=f"EPSG:{_WGS84_EPSG}"
                )
                if points_gdf.attrs and points_gdf.attrs.get("crs_label"):
                    boundary_gdf.attrs = dict(points_gdf.attrs)
                boundary_wgs84 = self._ensure_wgs84(boundary_gdf)
                boundary_proj = boundary_wgs84.to_crs(epsg=epsg)
                boundary_proj_geom = boundary_proj.geometry[0]
            else:
                # 自动 boundary：输入点投影坐标的 bbox，外扩 50km 保证覆盖
                min_x, min_y = coords.min(axis=0)
                max_x, max_y = coords.max(axis=0)
                margin = 50_000.0  # 50km
                boundary_proj_geom = Polygon([
                    (min_x - margin, min_y - margin),
                    (max_x + margin, min_y - margin),
                    (max_x + margin, max_y + margin),
                    (min_x - margin, max_y + margin),
                ])

            polygons = [p.intersection(boundary_proj_geom) for p in polygons]

            # 步骤 6：修复无效几何（自相交/重复点）并过滤空几何
            fixed = []
            for p in polygons:
                if p.is_empty:
                    continue
                if not p.is_valid:
                    p = p.buffer(0)
                if not p.is_empty:
                    fixed.append(p)
            polygons = fixed

            if not polygons:
                return {"status": "error", "message": "泰森多边形计算失败: 所有结果为空"}

            # 步骤 6：投影结果 -> WGS84 -> GCJ02
            result_proj = gpd.GeoDataFrame(
                {"geometry": polygons},
                crs=f"EPSG:{epsg}",
            )
            result_wgs84 = result_proj.to_crs(epsg=_WGS84_EPSG)
            result_gcj02 = self._to_gcj02_output(result_wgs84)
            return {"status": "success", "data": result_gcj02}

        except Exception as e:
            logger.exception("voronoi 计算失败")
            return {"status": "error", "message": f"泰森多边形计算失败: {str(e)}"}

    def _is_collinear(self, coords: np.ndarray) -> bool:
        """判断所有点是否共线。

        用 PCA 思想：若所有点在两个主方向上的方差，第二个方向方差接近 0，则共线。
        投影后同纬度的点因 GCJ02 偏转和投影变形会有微小垂直偏移（约百米量级），
        但与主方向方差（千万平米级）相比仍可视为共线。

        阈值 1e-6：次方向方差 < 主方向的 0.0001%。
        """
        if len(coords) < 3:
            return False  # 点数 < 3 不判共线（voronoi 已单独处理点数 < 4）

        centered = coords - coords.mean(axis=0)
        cov = np.cov(centered.T)
        if np.isscalar(cov):
            return True
        eigenvalues = np.linalg.eigvalsh(cov)
        max_ev = max(eigenvalues.max(), 1e-20)
        min_ev = eigenvalues.min()
        return min_ev / max_ev < 1e-6

    def _build_voronoi_polygons(self, vor: Voronoi, n_points: int) -> list:
        """从 scipy Voronoi 结果组装每个输入点对应的多边形。

        对每个输入点 p_i，其对应 Voronoi 区域 = 所有以 p_i 为端点的 ridge 对应的
        Voronoi 顶点构成的多边形（按角度排序）。
        """
        # 中心点（用于构造无穷远方向辅助点）
        center = vor.points.mean(axis=0)

        polygons = []
        for i in range(n_points):
            region_idx = vor.point_region[i]
            region = vor.regions[region_idx]
            if not region or -1 in region:
                # 区域有无穷远顶点，用 ridge 法构造有界近似
                poly = self._build_unbounded_region(vor, i, center)
            else:
                verts = vor.vertices[region]
                poly = Polygon(verts) if len(verts) >= 3 else Polygon()
            polygons.append(poly)
        return polygons

    def _build_unbounded_region(self, vor: Voronoi, point_idx: int, center: np.ndarray) -> Polygon:
        """构造含无穷远顶点的 Voronoi 区域的有界近似。

        策略：对该点所有 ridge，收集有限顶点；对无穷 ridge，沿 ridge 法向延伸固定距离
        作为虚拟顶点。所有顶点去重后按角度排序构成多边形。
        """
        ridge_points = vor.ridge_points
        ridge_vertices = vor.ridge_vertices

        finite_verts = []
        for rp, rv in zip(ridge_points, ridge_vertices):
            if point_idx not in rp:
                continue
            if -1 not in rv:
                # 有限 ridge：添加两个端点（可能与其他 ridge 共享，后面去重）
                for v_idx in rv:
                    finite_verts.append(tuple(vor.vertices[v_idx]))
            else:
                # 无穷 ridge：取有限端点 + 沿法向延伸
                finite_idx = [v for v in rv if v != -1]
                if finite_idx:
                    base = tuple(vor.vertices[finite_idx[0]])
                    finite_verts.append(base)
                    # ridge 法向 = 两点连线的垂线方向
                    other = rp[0] if rp[1] == point_idx else rp[1]
                    tangent = vor.points[other] - vor.points[point_idx]
                    normal = np.array([-tangent[1], tangent[0]])
                    norm_len = np.linalg.norm(normal)
                    if norm_len > 0:
                        normal = normal / norm_len
                        direction = np.array(base) - center
                        if np.dot(direction, normal) < 0:
                            normal = -normal
                        extended = tuple(np.array(base) + normal * 1e6)
                        finite_verts.append(extended)

        # 去重（同一点可能被多个 ridge 共享）
        unique_verts = list(dict.fromkeys(finite_verts))

        if len(unique_verts) < 3:
            return Polygon()

        # 按角度排序构成凸多边形
        arr = np.array(unique_verts)
        centroid = arr.mean(axis=0)
        angles = np.arctan2(arr[:, 1] - centroid[1], arr[:, 0] - centroid[0])
        order = np.argsort(angles)
        sorted_verts = arr[order]
        return Polygon(sorted_verts)

    # ------------------------------------------------------------------
    # 等时圈
    # ------------------------------------------------------------------

    def isochrone(self, origin: tuple, mode: str, time_min: int) -> dict:
        """等时圈：GCJ02 原点 -> WGS84 -> 8 方向采样 -> 结果回 GCJ02。

        采用 8 方向径向采样，每个方向通过路径规划求可达距离，再用凸包生成多边形。
        海边/路网稀疏区域无效采样会被过滤，有效点 < 3 返回 empty。

        NOTE: 几何计算（采样点构造、凸包）在 WGS84 下进行。高德路径规划 API
        期望 GCJ02 输入，_route_reachable_distance 内部会将 WGS84 转为 GCJ02 后再
        调用 API，其他层无需关心坐标系差异。

        Args:
            origin: (lng, lat) GCJ02 坐标
            mode: driving / walking / riding
            time_min: 可达时间，分钟

        Returns:
            {"status": "success", "data": {"geometry": Polygon}} 或
            {"status": "empty", "message": str}
        """
        try:
            import requests  # noqa: F811  # 用于异常类型捕获

            # 步骤 1：GCJ02 原点 -> WGS84（几何计算层统一用 WGS84）
            origin_wgs84 = gcj02_to_wgs84(origin[0], origin[1])

            # 步骤 2：8 方向径向采样（几何在 WGS84 下进行）
            sample_points = self._adaptive_radial_sampling(origin_wgs84, mode, time_min)

            # 步骤 3：过滤无效采样（海边/江边朝向水面的采样）
            valid_points = self._filter_invalid_samples(sample_points, origin_wgs84)

            if len(valid_points) < _ISOCHRONE_MIN_VALID:
                return {"status": "empty", "message": "该区域路网稀疏, 无法生成有效等时圈"}

            # 步骤 4：凸包生成多边形 -> WGS84 -> GCJ02
            from shapely.geometry import MultiPoint

            mp = MultiPoint(valid_points + [Point(origin_wgs84)])
            hull = mp.convex_hull
            if hull.is_empty or hull.geom_type not in ("Polygon", "MultiPolygon"):
                return {"status": "empty", "message": "无法生成有效等时圈多边形"}

            # 转 GeoDataFrame 走标准出口管线
            wgs84_gdf = gpd.GeoDataFrame({"geometry": [hull]}, crs=f"EPSG:{_WGS84_EPSG}")
            gcj02_gdf = self._to_gcj02_output(wgs84_gdf)
            return {"status": "success", "data": {"geometry": gcj02_gdf.geometry[0]}}

        except requests.exceptions.RequestException:
            logger.warning("isochrone 路径规划服务不可用", exc_info=True)
            return {"status": "empty", "message": "路径规划服务暂不可用"}

    def _adaptive_radial_sampling(
        self, origin_wgs84: tuple, mode: str, time_min: int
    ) -> list:
        """8 方向径向采样 + 距离剧变象限二分细化。

        几何计算在 WGS84 下进行。路径规划 API 调用时，
        _route_reachable_distance 内部会将 WGS84 转为 GCJ02 后再调高德 API。

        Args:
            origin_wgs84: (lng, lat) WGS84
            mode: driving/walking/riding
            time_min: 分钟

        Returns:
            采样点列表（Point, WGS84）
        """
        # 8 方向角度（0=正东，逆时针）
        angles = [i * 2 * math.pi / _ISOCHRONE_DIRECTIONS for i in range(_ISOCHRONE_DIRECTIONS)]

        # 估算可达距离上限（用于二分）
        # 驾车 60km/h × 时间，步行 5km/h，骑行 15km/h
        speed_m_per_min = {"driving": 1000.0, "walking": 80.0, "riding": 250.0}.get(mode, 250.0)
        max_distance_m = speed_m_per_min * time_min

        samples = []
        for angle in angles:
            # 调用路径规划求该方向可达距离
            distance = self._route_reachable_distance(origin_wgs84, angle, mode, time_min, max_distance_m)
            if distance <= 0:
                continue
            # 把距离转成经纬度偏移（近似：1° ≈ 111km，按方向分解）
            # 注意：纬度方向 1° ≈ 111km，经度方向 1° ≈ 111km × cos(lat)
            lat_rad = math.radians(origin_wgs84[1])
            dlng_per_m = 1.0 / (111320.0 * math.cos(lat_rad))
            dlat_per_m = 1.0 / 111320.0
            dx = distance * math.cos(angle) * dlng_per_m
            dy = distance * math.sin(angle) * dlat_per_m
            samples.append(Point(origin_wgs84[0] + dx, origin_wgs84[1] + dy))
        return samples

    def _route_reachable_distance(
        self,
        origin_wgs84: tuple,
        angle: float,
        mode: str,
        time_min: int,
        max_distance_m: float,
    ) -> float:
        """调用高德路径规划求指定方向上 time_min 可达的最远距离。

        策略：在指定方向上取 max_distance_m 处的候选终点，调用高德路径规划
        得到实际路径距离，反推时间约束下的可达距离。

        高德路径规划 API 期望 GCJ02 坐标输入。本方法接收 WGS84 坐标
        （与上层几何计算统一），内部转换为 GCJ02 后再调用高德 API。
        转换误差 ~1m，对路径规划无实际影响。

        本方法是可 mock 的钩子，测试时用 patch 替换。真实实现调用高德 API。

        Args:
            origin_wgs84: 起点 (lng, lat) WGS84
            angle: 方向弧度
            mode: driving/walking/riding
            time_min: 可达时间分钟
            max_distance_m: 距离上限（用于构造候选终点）

        Returns:
            可达距离（米），0 表示无路网可达
        """
        if not self.amap_key:
            # 无 key 时无法调用真实 API，返回 0（上层会判 empty）
            return 0.0

        # 构造候选终点（WGS84 下）
        lat_rad = math.radians(origin_wgs84[1])
        dlng_per_m = 1.0 / (111320.0 * math.cos(lat_rad))
        dlat_per_m = 1.0 / 111320.0
        dx = max_distance_m * math.cos(angle) * dlng_per_m
        dy = max_distance_m * math.sin(angle) * dlat_per_m
        dest_wgs84 = (origin_wgs84[0] + dx, origin_wgs84[1] + dy)

        # WGS84 -> GCJ02 转换（高德 API 要求 GCJ02 输入）
        origin_gcj = wgs84_to_gcj02(origin_wgs84[0], origin_wgs84[1])
        dest_gcj = wgs84_to_gcj02(dest_wgs84[0], dest_wgs84[1])

        # 调用高德路径规划
        try:
            import requests
        except ImportError:
            return 0.0

        url = "https://restapi.amap.com/v3/direction"
        mode_url = {"driving": "/driving", "walking": "/walking", "riding": "/bicycling"}.get(
            mode, "/driving"
        )
        params = {
            "key": self.amap_key,
            "origin": f"{origin_gcj[0]},{origin_gcj[1]}",
            "destination": f"{dest_gcj[0]},{dest_gcj[1]}",
        }
        try:
            resp = requests.get(url + mode_url, params=params, timeout=self.amap_timeout)
            data = resp.json()
            if data.get("status") != "1":
                return 0.0
            # 解析路径距离
            paths = data.get("route", {}).get("paths", [])
            if not paths:
                return 0.0
            path = paths[0]
            duration_s = float(path.get("duration", 0))
            distance_m = float(path.get("distance", 0))
            # 若实际耗时 <= time_min，该方向可达 max_distance_m
            if duration_s <= time_min * 60:
                return max_distance_m
            # 否则按时间比例反推
            ratio = (time_min * 60) / duration_s if duration_s > 0 else 0
            return distance_m * ratio
        except requests.exceptions.RequestException:
            raise
        except Exception as e:
            logger.warning("路径规划失败: %s", e)
            return 0.0

    def _filter_invalid_samples(
        self, samples: list, origin_wgs84: tuple
    ) -> list:
        """过滤无效采样点。

        无效场景：
        - 采样点距离原点过近（< 原点距离的 1/10，可能是海边/江边朝水面方向无路网）
        - 采样点坐标无效（NaN）

        Args:
            samples: 采样点列表（Point, WGS84）
            origin_wgs84: 原点 (lng, lat) WGS84

        Returns:
            有效采样点列表
        """
        if not samples:
            return []

        # 计算所有采样点到原点的距离
        from app.tools.geo_transform import haversine_m

        distances = []
        for p in samples:
            if p.is_empty or math.isnan(p.x) or math.isnan(p.y):
                distances.append(0.0)
            else:
                d = haversine_m(origin_wgs84, (p.x, p.y))
                distances.append(d)

        if not distances:
            return []

        max_d = max(distances)
        if max_d <= 0:
            return []

        # 过滤：距离 < 最大距离的 10% 视为无效（路网不通方向）
        threshold = max_d * 0.1
        return [p for p, d in zip(samples, distances) if d >= threshold]

    # ------------------------------------------------------------------
    # 拓扑检查与修复
    # ------------------------------------------------------------------

    def topology_check(self, gdf: gpd.GeoDataFrame) -> dict:
        """拓扑检查与修复。

        检查项：
        - 几何是否 valid（自相交、坐标方向等）
        - 几何是否 empty
        修复方式：buffer(0)（shapely 经典修复，能处理自相交、裂缝）

        Args:
            gdf: 输入 GeoDataFrame

        Returns:
            {"status": "success", "data": {"issues": [...], "fixed_geometries": [...]}}
        """
        issues = []
        fixed_geoms = []
        for idx, geom in enumerate(gdf.geometry):
            issue = {"index": idx}
            if geom.is_empty:
                issue["type"] = "empty"
                issues.append(issue)
                fixed_geoms.append(geom)
                continue
            if not geom.is_valid:
                fixed = geom.buffer(0)
                issue["type"] = "invalid"
                issue["fix"] = fixed
                issues.append(issue)
                fixed_geoms.append(fixed)
            else:
                fixed_geoms.append(geom)

        return {
            "status": "success",
            "data": {
                "issues": issues,
                "fixed_geometries": fixed_geoms,
            },
        }

    # ------------------------------------------------------------------
    # 核密度估计
    # ------------------------------------------------------------------

    def kernel_density(
        self,
        points_gdf: gpd.GeoDataFrame,
        bandwidth: Optional[float] = None,
    ) -> dict:
        """核密度估计：在投影坐标系下计算每个点的局部密度。

        Args:
            points_gdf: 点 GeoDataFrame（GCJ02 或 WGS84）
            bandwidth: 核函数带宽（米），None 时自动估算（默认点到最近邻居的中位数）

        Returns:
            {"status": "success", "data": {"densities": [float, ...], "bandwidth": float}}
        """
        from sklearn.neighbors import KernelDensity

        wgs84_gdf = self._ensure_wgs84(points_gdf)
        projected = wgs84_gdf.to_crs(epsg=_resolve_projected_crs(wgs84_gdf))
        coords = np.array([(p.x, p.y) for p in projected.geometry])

        if len(coords) == 0:
            return {"status": "empty", "message": "无点数据"}

        # 自动带宽：最近邻居距离中位数
        if bandwidth is None:
            from scipy.spatial import cKDTree

            tree = cKDTree(coords)
            # 每个点到最近邻居（不含自己）的距离
            dists, _ = tree.query(coords, k=2)
            nearest = dists[:, 1]
            median_d = np.median(nearest)
            bandwidth = max(median_d, 1.0)  # 至少 1m

        # sklearn KernelDensity 用高斯核
        kde = KernelDensity(bandwidth=bandwidth, metric="euclidean", kernel="gaussian")
        kde.fit(coords)
        # 每个点的对数密度 -> 密度
        log_dens = kde.score_samples(coords)
        densities = np.exp(log_dens).tolist()

        return {
            "status": "success",
            "data": {
                "densities": densities,
                "bandwidth": float(bandwidth),
            },
        }


    # ------------------------------------------------------------------
    # 叠加分析类
    # ------------------------------------------------------------------

    def clip(
        self,
        gdf: gpd.GeoDataFrame,
        mask_gdf: gpd.GeoDataFrame,
    ) -> dict:
        """裁剪：用 mask 多边形裁剪输入图层。

        遵循 GCJ02 pipeline：GCJ02 -> WGS84 -> 投影计算 -> WGS84 -> GCJ02。

        Args:
            gdf: 输入 GeoDataFrame（GCJ02 或 WGS84）
            mask_gdf: 裁剪面 GeoDataFrame（GCJ02 或 WGS84）

        Returns:
            {"status": "success", "data": GeoDataFrame} 或 {"status": "error", "message": str}
        """
        try:
            wgs84_gdf = self._ensure_wgs84(gdf)
            wgs84_mask = self._ensure_wgs84(mask_gdf)
            epsg = _resolve_projected_crs(wgs84_gdf)
            proj_gdf = wgs84_gdf.to_crs(epsg=epsg)
            proj_mask = wgs84_mask.to_crs(epsg=epsg)
            clipped = gpd.clip(proj_gdf, proj_mask)
            wgs84_result = clipped.to_crs(epsg=_WGS84_EPSG)
            result = self._to_gcj02_output(wgs84_result)
            return {"status": "success", "data": result}
        except Exception as e:
            logger.exception("clip 计算失败")
            return {"status": "error", "message": f"裁剪失败: {str(e)}"}

    def extract_by_location(
        self,
        gdf: gpd.GeoDataFrame,
        mask_gdf: gpd.GeoDataFrame,
        predicate: str = "intersects",
    ) -> dict:
        """按空间位置筛选：保留与 mask 满足空间关系的要素，不移除几何。

        使用 spatial join 筛选（不执行 overlay 运算），保留输入图层的原始几何。

        Args:
            gdf: 输入 GeoDataFrame
            mask_gdf: 空间条件面 GeoDataFrame
            predicate: 空间关系，如 intersects / contains / within / touches / crosses

        Returns:
            {"status": "success", "data": GeoDataFrame} 或 {"status": "error", "message": str}
        """
        try:
            wgs84_gdf = self._ensure_wgs84(gdf)
            wgs84_mask = self._ensure_wgs84(mask_gdf)
            # spatial join 筛选匹配行，保留左侧几何
            joined = gpd.sjoin(wgs84_gdf, wgs84_mask, how="inner", predicate=predicate)
            # 移除 sjoin 添加的 index_right 列，保留原始字段
            drop_cols = [c for c in joined.columns if c.endswith("_right") or c == "index_right"]
            result_gdf = joined.drop(columns=drop_cols, errors="ignore")
            result_gdf = gpd.GeoDataFrame(result_gdf, crs=wgs84_gdf.crs)
            result = self._to_gcj02_output(result_gdf)
            return {"status": "success", "data": result}
        except Exception as e:
            logger.exception("extract_by_location 计算失败")
            return {"status": "error", "message": f"空间筛选失败: {str(e)}"}

    def convex_hull(self, gdf: gpd.GeoDataFrame) -> dict:
        """计算凸包：返回所有要素几何的凸包多边形。

        Args:
            gdf: 输入 GeoDataFrame

        Returns:
            {"status": "success", "data": GeoDataFrame} 或 {"status": "error", "message": str}
        """
        try:
            wgs84_gdf = self._ensure_wgs84(gdf)
            hull = wgs84_gdf.union_all().convex_hull
            result_gdf = gpd.GeoDataFrame({"geometry": [hull]}, crs=wgs84_gdf.crs)
            result = self._to_gcj02_output(result_gdf)
            return {"status": "success", "data": result}
        except Exception as e:
            logger.exception("convex_hull 计算失败")
            return {"status": "error", "message": f"凸包计算失败: {str(e)}"}

    def bounding_boxes(self, gdf: gpd.GeoDataFrame) -> dict:
        """计算外包矩形（envelope）：每个要素的最小外包矩形。

        Args:
            gdf: 输入 GeoDataFrame

        Returns:
            {"status": "success", "data": GeoDataFrame} 或 {"status": "error", "message": str}
        """
        try:
            wgs84_gdf = self._ensure_wgs84(gdf)
            envelopes = wgs84_gdf.envelope
            result_gdf = gpd.GeoDataFrame(
                {
                    **{c: wgs84_gdf[c].values for c in wgs84_gdf.columns if c != "geometry"},
                    "geometry": envelopes,
                },
                crs=wgs84_gdf.crs,
            )
            result = self._to_gcj02_output(result_gdf)
            return {"status": "success", "data": result}
        except Exception as e:
            logger.exception("bounding_boxes 计算失败")
            return {"status": "error", "message": f"外包矩形计算失败: {str(e)}"}

    # ------------------------------------------------------------------
    # 融合/合并
    # ------------------------------------------------------------------

    def dissolve(
        self,
        gdf: gpd.GeoDataFrame,
        by: Optional[str] = None,
    ) -> dict:
        """融合相邻/重叠面：按指定字段或全局合并几何。

        Args:
            gdf: 输入 GeoDataFrame
            by: 融合字段名，None 则全局融合

        Returns:
            {"status": "success", "data": GeoDataFrame} 或 {"status": "error", "message": str}
        """
        try:
            wgs84_gdf = self._ensure_wgs84(gdf)
            proj_gdf = wgs84_gdf.to_crs(epsg=_resolve_projected_crs(wgs84_gdf))
            dissolved = proj_gdf.dissolve(by=by)
            wgs84_result = dissolved.to_crs(epsg=_WGS84_EPSG)
            result = self._to_gcj02_output(wgs84_result)
            return {"status": "success", "data": result}
        except Exception as e:
            logger.exception("dissolve 计算失败")
            return {"status": "error", "message": f"融合失败: {str(e)}"}

    def merge_layers(self, gdfs: list[gpd.GeoDataFrame]) -> dict:
        """合并多个图层：将多个 GeoDataFrame 拼接为一个。

        所有输入必须具有相同的 CRS（或 GCJ02 标注），合并后统一 CRS。

        Args:
            gdfs: GeoDataFrame 列表

        Returns:
            {"status": "success", "data": GeoDataFrame} 或 {"status": "error", "message": str}
        """
        if not gdfs:
            return {"status": "error", "message": "图层列表为空"}
        try:
            wgs84_list = [self._ensure_wgs84(gdf) for gdf in gdfs]
            merged = gpd.GeoDataFrame(
                pd.concat(wgs84_list, ignore_index=True),
                crs=wgs84_list[0].crs,
            )
            result = self._to_gcj02_output(merged)
            return {"status": "success", "data": result}
        except Exception as e:
            logger.exception("merge_layers 计算失败")
            return {"status": "error", "message": f"图层合并失败: {str(e)}"}

    # ------------------------------------------------------------------
    # 空间连接
    # ------------------------------------------------------------------

    def join_by_location(
        self,
        gdf_a: gpd.GeoDataFrame,
        gdf_b: gpd.GeoDataFrame,
        predicate: str = "intersects",
    ) -> dict:
        """空间连接：按空间关系将两个图层的属性合并。

        Args:
            gdf_a: 目标图层（保留其几何）
            gdf_b: 连接图层（属性附加到目标图层）
            predicate: 空间关系，如 intersects / contains / within

        Returns:
            {"status": "success", "data": GeoDataFrame} 或 {"status": "error", "message": str}
        """
        try:
            wgs84_a = self._ensure_wgs84(gdf_a)
            wgs84_b = self._ensure_wgs84(gdf_b)
            joined = gpd.sjoin(wgs84_a, wgs84_b, how="inner", predicate=predicate)
            result = self._to_gcj02_output(joined)
            return {"status": "success", "data": result}
        except Exception as e:
            logger.exception("join_by_location 计算失败")
            return {"status": "error", "message": f"空间连接失败: {str(e)}"}

    def join_by_nearest(
        self,
        gdf_a: gpd.GeoDataFrame,
        gdf_b: gpd.GeoDataFrame,
        max_distance: Optional[float] = None,
        k: int = 1,
    ) -> dict:
        """最近邻连接：将 B 中最近的 k 个要素属性附加到 A。

        在投影坐标系下计算距离（米），max_distance 单位为米。

        Args:
            gdf_a: 目标图层
            gdf_b: 连接图层
            max_distance: 最大搜索距离（米），None 无限制
            k: 每个 A 要素最多匹配 B 要素数

        Returns:
            {"status": "success", "data": GeoDataFrame} 或 {"status": "error", "message": str}
        """
        try:
            wgs84_a = self._ensure_wgs84(gdf_a)
            wgs84_b = self._ensure_wgs84(gdf_b)
            epsg = _resolve_projected_crs(wgs84_a)
            proj_a = wgs84_a.to_crs(epsg=epsg)
            proj_b = wgs84_b.to_crs(epsg=epsg)
            if max_distance is not None and max_distance < 0:
                raise ValueError("max_distance must be greater than or equal to 0")
            if max_distance == 0:
                # Shapely's nearest query rejects zero, but it is a meaningful
                # user constraint: only geometries that touch/intersect have a
                # distance of exactly zero.  A spatial inner join implements
                # that closed boundary without silently widening the radius.
                joined = gpd.sjoin(proj_a, proj_b, how="inner", predicate="intersects")
                joined["distance_m"] = 0.0
            else:
                kwargs = {}
                if max_distance is not None:
                    kwargs["max_distance"] = max_distance
                joined = gpd.sjoin_nearest(
                    proj_a, proj_b, how="inner", distance_col="distance_m", **kwargs,
                )
            wgs84_result = joined.to_crs(epsg=_WGS84_EPSG)
            result = self._to_gcj02_output(wgs84_result)
            return {"status": "success", "data": result}
        except Exception as e:
            logger.exception("join_by_nearest 计算失败")
            return {"status": "error", "message": f"最近邻连接失败: {str(e)}"}

    def count_points_in_polygon(
        self,
        points_gdf: gpd.GeoDataFrame,
        polygons_gdf: gpd.GeoDataFrame,
    ) -> dict:
        """点面统计：统计每个面内包含的点数。

        通过 sjoin + groupby size 实现。结果多边形 gdf 新增 count 字段。

        Args:
            points_gdf: 点 GeoDataFrame
            polygons_gdf: 面 GeoDataFrame（需有唯一标识字段如 name/id）

        Returns:
            {"status": "success", "data": GeoDataFrame} 或 {"status": "error", "message": str}
        """
        try:
            wgs84_points = self._ensure_wgs84(points_gdf)
            wgs84_polygons = self._ensure_wgs84(polygons_gdf)
            # 给多边形加一个临时索引用于 groupby
            wgs84_polygons = wgs84_polygons.copy()
            wgs84_polygons["_poly_idx"] = range(len(wgs84_polygons))
            joined = gpd.sjoin(wgs84_points, wgs84_polygons, how="inner", predicate="intersects")
            counts = joined.groupby("_poly_idx").size()
            wgs84_polygons["count"] = wgs84_polygons["_poly_idx"].map(counts).fillna(0).astype(int)
            result_gdf = wgs84_polygons.drop(columns=["_poly_idx"], errors="ignore")
            result = self._to_gcj02_output(result_gdf)
            return {"status": "success", "data": result}
        except Exception as e:
            logger.exception("count_points_in_polygon 计算失败")
            return {"status": "error", "message": f"点面统计失败: {str(e)}"}

    # ------------------------------------------------------------------
    # 几何变换
    # ------------------------------------------------------------------

    def centroid_layer(self, gdf: gpd.GeoDataFrame) -> dict:
        """计算质心：每个要素的几何中心点。

        Args:
            gdf: 输入 GeoDataFrame

        Returns:
            {"status": "success", "data": GeoDataFrame} 或 {"status": "error", "message": str}
        """
        try:
            wgs84_gdf = self._ensure_wgs84(gdf)
            centroids = wgs84_gdf.centroid
            result_gdf = gpd.GeoDataFrame(
                {
                    **{c: wgs84_gdf[c].values for c in wgs84_gdf.columns if c != "geometry"},
                    "geometry": centroids,
                },
                crs=wgs84_gdf.crs,
            )
            result = self._to_gcj02_output(result_gdf)
            return {"status": "success", "data": result}
        except Exception as e:
            logger.exception("centroid_layer 计算失败")
            return {"status": "error", "message": f"质心计算失败: {str(e)}"}

    def point_on_surface(self, gdf: gpd.GeoDataFrame) -> dict:
        """面上取点：每个要素内部的一个代表点（保证在面内）。

        Args:
            gdf: 输入 GeoDataFrame

        Returns:
            {"status": "success", "data": GeoDataFrame} 或 {"status": "error", "message": str}
        """
        try:
            wgs84_gdf = self._ensure_wgs84(gdf)
            rep_points = wgs84_gdf.representative_point()
            result_gdf = gpd.GeoDataFrame(
                {
                    **{c: wgs84_gdf[c].values for c in wgs84_gdf.columns if c != "geometry"},
                    "geometry": rep_points,
                },
                crs=wgs84_gdf.crs,
            )
            result = self._to_gcj02_output(result_gdf)
            return {"status": "success", "data": result}
        except Exception as e:
            logger.exception("point_on_surface 计算失败")
            return {"status": "error", "message": f"面上取点失败: {str(e)}"}

    def simplify_geometry(
        self,
        gdf: gpd.GeoDataFrame,
        tolerance: float,
    ) -> dict:
        """简化几何：使用 Douglas-Peucker 算法简化，tolerance 单位为米。

        在投影坐标系下计算以保证 tolerance 为米制单位。

        Args:
            gdf: 输入 GeoDataFrame
            tolerance: 简化容差（米）

        Returns:
            {"status": "success", "data": GeoDataFrame} 或 {"status": "error", "message": str}
        """
        try:
            wgs84_gdf = self._ensure_wgs84(gdf)
            proj_gdf = wgs84_gdf.to_crs(epsg=_resolve_projected_crs(wgs84_gdf))
            simplified = proj_gdf.simplify(tolerance)
            # 用简化后的几何替换
            result_proj = gpd.GeoDataFrame(
                {
                    **{c: proj_gdf[c].values for c in proj_gdf.columns if c != "geometry"},
                    "geometry": simplified,
                },
                crs=proj_gdf.crs,
            )
            wgs84_result = result_proj.to_crs(epsg=_WGS84_EPSG)
            result = self._to_gcj02_output(wgs84_result)
            return {"status": "success", "data": result}
        except Exception as e:
            logger.exception("simplify_geometry 计算失败")
            return {"status": "error", "message": f"几何简化失败: {str(e)}"}

    def fix_geometries(self, gdf: gpd.GeoDataFrame) -> dict:
        """修复无效几何：使用 buffer(0) 修复自相交、重复点等问题。

        Args:
            gdf: 输入 GeoDataFrame

        Returns:
            {"status": "success", "data": GeoDataFrame} 或 {"status": "error", "message": str}
        """
        try:
            wgs84_gdf = self._ensure_wgs84(gdf)
            fixed_geoms = []
            for g in wgs84_gdf.geometry:
                if g.is_empty:
                    fixed_geoms.append(g)
                elif not g.is_valid:
                    fixed = g.buffer(0)
                    fixed_geoms.append(fixed)
                else:
                    fixed_geoms.append(g)
            result_gdf = gpd.GeoDataFrame(
                {
                    **{c: wgs84_gdf[c].values for c in wgs84_gdf.columns if c != "geometry"},
                    "geometry": fixed_geoms,
                },
                crs=wgs84_gdf.crs,
            )
            result = self._to_gcj02_output(result_gdf)
            return {"status": "success", "data": result}
        except Exception as e:
            logger.exception("fix_geometries 计算失败")
            return {"status": "error", "message": f"几何修复失败: {str(e)}"}

    def check_validity(self, gdf: gpd.GeoDataFrame) -> dict:
        """检查几何有效性：返回每个要素的问题列表。

        Args:
            gdf: 输入 GeoDataFrame

        Returns:
            {"status": "success", "data": {"issues": [{"index": int, "type": str, "reason": str}, ...]}}
        """
        try:
            wgs84_gdf = self._ensure_wgs84(gdf)
            issues = []
            for idx, geom in enumerate(wgs84_gdf.geometry):
                if geom.is_empty:
                    issues.append({"index": idx, "type": "empty", "reason": "几何为空"})
                elif not geom.is_valid:
                    reason = shapely.is_valid_reason(geom) if hasattr(shapely, "is_valid_reason") else "几何无效"
                    issues.append({"index": idx, "type": "invalid", "reason": str(reason)})
            return {"status": "success", "data": {"issues": issues}}
        except Exception as e:
            logger.exception("check_validity 计算失败")
            return {"status": "error", "message": f"有效性检查失败: {str(e)}"}

    def multipart_to_singlepart(self, gdf: gpd.GeoDataFrame) -> dict:
        """多部件转单部件：将 MultiPolygon/MultiLineString 拆分为单独要素。

        Args:
            gdf: 输入 GeoDataFrame

        Returns:
            {"status": "success", "data": GeoDataFrame} 或 {"status": "error", "message": str}
        """
        try:
            wgs84_gdf = self._ensure_wgs84(gdf)
            exploded = wgs84_gdf.explode(index_parts=True)
            # reset_index 清理 explode 产生的多级索引
            result_gdf = exploded.reset_index(drop=True)
            result_gdf = gpd.GeoDataFrame(result_gdf, crs=wgs84_gdf.crs)
            result = self._to_gcj02_output(result_gdf)
            return {"status": "success", "data": result}
        except Exception as e:
            logger.exception("multipart_to_singlepart 计算失败")
            return {"status": "error", "message": f"多部件拆分失败: {str(e)}"}

    def delete_duplicate_geometries(self, gdf: gpd.GeoDataFrame) -> dict:
        """删除重复几何：移除完全相同的几何要素（按 WKB 比较）。

        Args:
            gdf: 输入 GeoDataFrame

        Returns:
            {"status": "success", "data": GeoDataFrame} 或 {"status": "error", "message": str}
        """
        try:
            wgs84_gdf = self._ensure_wgs84(gdf)
            # 用 WKB 字节串判断几何相等（shapely 的 == 有时对 NaN 坐标不敏感）
            wkb_col = wgs84_gdf.geometry.apply(lambda g: g.wkb)
            deduped = wgs84_gdf.loc[~wkb_col.duplicated()].copy()
            result = self._to_gcj02_output(deduped)
            return {"status": "success", "data": result}
        except Exception as e:
            logger.exception("delete_duplicate_geometries 计算失败")
            return {"status": "error", "message": f"删除重复几何失败: {str(e)}"}

    def snap_geometries(
        self,
        gdf: gpd.GeoDataFrame,
        reference_gdf: gpd.GeoDataFrame,
        tolerance: float,
    ) -> dict:
        """吸附几何：将 gdf 的顶点吸附到 reference_gdf 的最近顶点。

        tolerance 单位为米，在投影坐标系下计算。

        Args:
            gdf: 待吸附图层
            reference_gdf: 参考图层
            tolerance: 吸附容差（米）

        Returns:
            {"status": "success", "data": GeoDataFrame} 或 {"status": "error", "message": str}
        """
        try:
            wgs84_gdf = self._ensure_wgs84(gdf)
            wgs84_ref = self._ensure_wgs84(reference_gdf)
            from shapely.ops import snap as shapely_snap

            proj_gdf = wgs84_gdf.to_crs(epsg=_resolve_projected_crs(wgs84_gdf))
            proj_ref = wgs84_ref.to_crs(epsg=_resolve_projected_crs(wgs84_gdf))
            # 对每个要素分别 snap 到参考的所有要素的 union
            ref_union = unary_union(proj_ref.geometry.tolist())
            snapped_geoms = [shapely_snap(g, ref_union, tolerance) for g in proj_gdf.geometry]
            result_proj = gpd.GeoDataFrame(
                {
                    **{c: proj_gdf[c].values for c in proj_gdf.columns if c != "geometry"},
                    "geometry": snapped_geoms,
                },
                crs=proj_gdf.crs,
            )
            wgs84_result = result_proj.to_crs(epsg=_WGS84_EPSG)
            result = self._to_gcj02_output(wgs84_result)
            return {"status": "success", "data": result}
        except Exception as e:
            logger.exception("snap_geometries 计算失败")
            return {"status": "error", "message": f"几何吸附失败: {str(e)}"}

    # ------------------------------------------------------------------
    # 属性操作
    # ------------------------------------------------------------------

    def extract_by_attribute(
        self,
        gdf: gpd.GeoDataFrame,
        field: str,
        operator: str,
        value: Any,
    ) -> dict:
        """按属性筛选：保留满足属性条件的要素。

        支持的 operator：==, !=, >, >=, <, <=, contains, is_null.

        Args:
            gdf: 输入 GeoDataFrame
            field: 字段名
            operator: 比较运算符
            value: 比较值（is_null 时忽略）

        Returns:
            {"status": "success", "data": GeoDataFrame} 或 {"status": "error", "message": str}
        """
        try:
            wgs84_gdf = self._ensure_wgs84(gdf)
            if field not in wgs84_gdf.columns:
                return {"status": "error", "message": f"字段 {field} 不存在"}

            col = wgs84_gdf[field]
            if operator == "==":
                mask = col == value
            elif operator == "!=":
                mask = col != value
            elif operator == ">":
                mask = col > value
            elif operator == ">=":
                mask = col >= value
            elif operator == "<":
                mask = col < value
            elif operator == "<=":
                mask = col <= value
            elif operator == "contains":
                mask = col.astype(str).str.contains(str(value), na=False)
            elif operator == "is_null":
                mask = col.isnull()
            else:
                return {"status": "error", "message": f"不支持的运算符: {operator}"}

            result_gdf = wgs84_gdf[mask].copy()
            result = self._to_gcj02_output(result_gdf)
            return {"status": "success", "data": result}
        except Exception as e:
            logger.exception("extract_by_attribute 计算失败")
            return {"status": "error", "message": f"属性筛选失败: {str(e)}"}

    def keep_fields(
        self,
        gdf: gpd.GeoDataFrame,
        fields: list[str],
    ) -> dict:
        """保留字段：仅保留 geometry 和指定字段。

        Args:
            gdf: 输入 GeoDataFrame
            fields: 要保留的字段名列表

        Returns:
            {"status": "success", "data": GeoDataFrame} 或 {"status": "error", "message": str}
        """
        try:
            wgs84_gdf = self._ensure_wgs84(gdf)
            keep_cols = ["geometry"] + [f for f in fields if f in wgs84_gdf.columns and f != "geometry"]
            result_gdf = wgs84_gdf[keep_cols].copy()
            result = self._to_gcj02_output(result_gdf)
            return {"status": "success", "data": result}
        except Exception as e:
            logger.exception("keep_fields 计算失败")
            return {"status": "error", "message": f"保留字段失败: {str(e)}"}

    def rename_field(
        self,
        gdf: gpd.GeoDataFrame,
        old_name: str,
        new_name: str,
    ) -> dict:
        """重命名字段：修改字段名，新旧不同才执行。

        Args:
            gdf: 输入 GeoDataFrame
            old_name: 原字段名
            new_name: 新字段名

        Returns:
            {"status": "success", "data": GeoDataFrame} 或 {"status": "error", "message": str}
        """
        try:
            wgs84_gdf = self._ensure_wgs84(gdf)
            if old_name == new_name:
                return {"status": "error", "message": "新旧字段名相同，无需重命名"}
            if old_name not in wgs84_gdf.columns:
                return {"status": "error", "message": f"字段 {old_name} 不存在"}
            if new_name in wgs84_gdf.columns and new_name != old_name:
                return {"status": "error", "message": f"目标字段名 {new_name} 已存在"}
            result_gdf = wgs84_gdf.rename(columns={old_name: new_name})
            result = self._to_gcj02_output(result_gdf)
            return {"status": "success", "data": result}
        except Exception as e:
            logger.exception("rename_field 计算失败")
            return {"status": "error", "message": f"字段重命名失败: {str(e)}"}

    def field_calculator(
        self,
        gdf: gpd.GeoDataFrame,
        field_name: str,
        expression: str,
        field_type: str = "float",
    ) -> dict:
        """字段计算器：新增或更新字段，支持 $area（平方米）和 $length（米）。

        Args:
            gdf: 输入 GeoDataFrame
            field_name: 新字段名
            expression: 计算表达式。支持 $area / $length 宏
            field_type: 字段类型，当前仅支持 float

        Returns:
            {"status": "success", "data": GeoDataFrame} 或 {"status": "error", "message": str}
        """
        try:
            wgs84_gdf = self._ensure_wgs84(gdf)
            # 投影到米制坐标系计算面积/长度
            proj_gdf = wgs84_gdf.to_crs(epsg=_resolve_projected_crs(wgs84_gdf))

            expr_lower = expression.lower().replace(" ", "")
            if expr_lower.startswith("$area"):
                values = proj_gdf.geometry.area.values
                macro = "$area"
            elif expr_lower.startswith("$length"):
                values = proj_gdf.geometry.length.values
                macro = "$length"
            else:
                return {"status": "error", "message": "仅支持 $area 和 $length 表达式"}

            suffix = expr_lower[len(macro):]
            if suffix:
                try:
                    if suffix.startswith("/"):
                        divisor = float(suffix[1:])
                        if divisor == 0:
                            return {"status": "error", "message": "字段计算除数不能为 0"}
                        values = values / divisor
                    elif suffix.startswith("*"):
                        values = values * float(suffix[1:])
                    else:
                        return {"status": "error", "message": "仅支持 $area/$length 后接常数乘除"}
                except ValueError:
                    return {"status": "error", "message": "字段计算常数无效"}

            wgs84_gdf = wgs84_gdf.copy()
            wgs84_gdf[field_name] = values.astype(float)
            result = self._to_gcj02_output(wgs84_gdf)
            return {"status": "success", "data": result}
        except Exception as e:
            logger.exception("field_calculator 计算失败")
            return {"status": "error", "message": f"字段计算失败: {str(e)}"}

    def reproject_layer(
        self,
        gdf: gpd.GeoDataFrame,
        target_crs: str,
    ) -> dict:
        """重投影图层：先恢复真实 WGS84 数值，再投影到目标 CRS。

        Args:
            gdf: 输入 GeoDataFrame
            target_crs: 目标 CRS 字符串（如 "EPSG:4548" 或 "EPSG:32650"）

        Returns:
            {"status": "success", "data": GeoDataFrame} 或 {"status": "error", "message": str}
        """
        try:
            # GCJ02 has no EPSG code. Its compatibility EPSG:4326 label must
            # never be passed directly to pyproj, otherwise every coordinate
            # is projected from an already-shifted value.
            wgs84_gdf = self._ensure_wgs84(gdf)
            result_gdf = wgs84_gdf.to_crs(target_crs)
            # A reprojected output has real coordinates in target_crs; it is
            # not a GCJ02 display layer, including the EPSG:4326 target case.
            result_gdf.attrs = {
                key: value
                for key, value in (wgs84_gdf.attrs or {}).items()
                if key != "crs_label"
            }
            return {"status": "success", "data": result_gdf}
        except Exception as e:
            logger.exception("reproject_layer 计算失败")
            return {"status": "error", "message": f"重投影失败: {str(e)}"}

    def batch_reproject_layers(
        self,
        layer_dicts: list[dict],
        target_crs: str,
    ) -> dict:
        """批量重投影：对多个图层逐一执行 reproject_layer。

        Args:
            layer_dicts: [{"name": "layer_a", "gdf": GeoDataFrame}, ...]
            target_crs: 目标 CRS 字符串

        Returns:
            {"status": "success", "data": [{"name": ..., "gdf": GeoDataFrame}, ...]}
        """
        results = []
        for item in layer_dicts:
            try:
                name = item.get("name", "unnamed")
                gdf = item["gdf"]
                r = self.reproject_layer(gdf, target_crs)
                if r["status"] == "error":
                    return r
                results.append({"name": name, "gdf": r["data"]})
            except Exception as e:
                return {"status": "error", "message": f"图层 {name} 重投影失败: {str(e)}"}
        return {"status": "success", "data": results}
