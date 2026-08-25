"""地图图层构建工具。

实现参考 GIS_Agent_技术文档.md §4.6 + docs/02_data_models.md §4.1。

核心设计：
1. **后端只生成图层数据配置（JSON）**：不生成地图 HTML，前端高德 JS API 直接渲染
2. **所有坐标保持 GCJ02**：本工具不做任何坐标转换（转换由 geo_transform / poi_query
   在数据入库时已完成），这里只负责把坐标数组组装成图层配置
3. **数据源视觉隔离**：高德数据用橙色高亮（#FF6B35, highlight_pin），
   OSM 数据用灰色半透明（#999999, opacity 0.6），让用户在地图上一眼区分来源
4. **FeatureCollection 优先**：标准 GeoJSON，支持 Point/LineString/Polygon
   （含孔洞），前端 AMap.GeoJSON 插件统一解析
5. **ECharts 配置**：柱状图/折线图/饼图，返回标准 ECharts option 结构

图层类型对照 docs/02_data_models.md §4.1 的 MapLayer union：
- PointLayer / HeatmapLayer / PolygonLayer / PolylineLayer / FeatureCollectionLayer
"""

from typing import Any, Optional


class MapLayerBuilder:
    """地图图层配置构建器。

    所有方法返回符合 docs/02_data_models.md §4.1 MapLayer union 的 dict 配置。
    前端拿到这些 JSON 配置后，直接调用高德 JS API 渲染，无需后端生成 HTML。
    """

    # 数据源默认样式（视觉隔离：高德橙色高亮，OSM 灰色半透明）
    _SOURCE_STYLES = {
        "Amap": {"icon": "highlight_pin", "color": "#FF6B35"},
        "OSM_CN": {"icon": "gray_dot", "color": "#999999", "opacity": 0.6},
        "OSM_Global": {"icon": "gray_dot", "color": "#999999", "opacity": 0.6},
        "Upload": {"icon": "default", "color": "#3388ff"},
    }

    # 热力图默认渐变（与高德 Heatmap 插件默认渐变一致）
    _DEFAULT_HEATMAP_GRADIENT = {
        "0.4": "blue",
        "0.65": "lime",
        "1": "red",
    }

    # ------------------------------------------------------------------
    # 点图层
    # ------------------------------------------------------------------

    def build_point_layer(
        self,
        coordinates: list[list[float]],
        source: str = "Amap",
        popup_fields: Optional[list[str]] = None,
    ) -> dict:
        """构建 PointLayer 配置。

        高德用 highlight_pin (#FF6B35 橙色高亮)，OSM 用 gray_dot
        (#999999 灰色, opacity 0.6)，实现数据源视觉隔离。

        Args:
            coordinates: [[lng, lat], ...] GCJ02 坐标数组
            source: 数据源标识，Amap / OSM_CN / OSM_Global / Upload
            popup_fields: 点击 POI 弹窗展示的字段名列表

        Returns:
            PointLayer 配置 dict（符合 docs/02_data_models.md §4.1）
        """
        style = self._SOURCE_STYLES.get(
            source, self._SOURCE_STYLES["Upload"]
        )
        return {
            "type": "point",
            "source": source,
            "coordinates": coordinates,
            "style": dict(style),
            "popup_fields": list(popup_fields) if popup_fields else [],
        }

    # ------------------------------------------------------------------
    # 热力图图层
    # ------------------------------------------------------------------

    def build_heatmap_layer(
        self,
        coordinates: list[list[float]],
        weights: Optional[list[float]] = None,
        radius: int = 25,
    ) -> dict:
        """构建 HeatmapLayer 配置。

        Args:
            coordinates: [[lng, lat], ...] GCJ02 坐标数组
            weights: 可选权重列表，长度应与 coordinates 一致
            radius: 热力图半径（像素），默认 25

        Returns:
            HeatmapLayer 配置 dict，含 weights/radius/gradient
        """
        return {
            "type": "heatmap",
            "coordinates": coordinates,
            "weights": list(weights) if weights is not None else None,
            "radius": radius,
            "gradient": dict(self._DEFAULT_HEATMAP_GRADIENT),
        }

    # ------------------------------------------------------------------
    # 多边形图层
    # ------------------------------------------------------------------

    def build_polygon_layer(
        self,
        coordinates: list[list[list[list[float]]]],
        fill_color: str = "#3388ff",
        fill_opacity: float = 0.3,
    ) -> dict:
        """构建 PolygonLayer 配置（缓冲区/等时圈/泰森多边形/叠加分析结果）。

        Args:
            coordinates: 多面坐标 [[外环[[lng,lat],...], ...], ...] GCJ02
            fill_color: 填充颜色，默认 #3388ff（Leaflet 默认蓝）
            fill_opacity: 填充透明度，默认 0.3

        Returns:
            PolygonLayer 配置 dict
        """
        return {
            "type": "polygon",
            "coordinates": coordinates,
            "fill_color": fill_color,
            "fill_opacity": fill_opacity,
        }

    # ------------------------------------------------------------------
    # 线图层
    # ------------------------------------------------------------------

    def build_polyline_layer(
        self,
        coordinates: list[list[list[float]]],
        stroke_color: str = "#FF6B35",
        stroke_width: int = 4,
    ) -> dict:
        """构建 PolylineLayer 配置（路径规划/轨迹/剖面线）。

        Args:
            coordinates: 多线坐标 [[[lng, lat], ...], ...] GCJ02
            stroke_color: 描边颜色，默认 #FF6B35（与高德点样式一致的橙色）
            stroke_width: 描边宽度，默认 4

        Returns:
            PolylineLayer 配置 dict
        """
        return {
            "type": "polyline",
            "coordinates": coordinates,
            "stroke_color": stroke_color,
            "stroke_width": stroke_width,
        }

    # ------------------------------------------------------------------
    # FeatureCollection（推荐）
    # ------------------------------------------------------------------

    def build_feature_collection(
        self,
        gdf_or_features: Any,
        style: Optional[dict] = None,
    ) -> dict:
        """构建 FeatureCollectionLayer 配置（标准 GeoJSON，推荐使用）。

        支持 Point/LineString/Polygon（含孔洞），坐标保持 GCJ02。
        前端用 AMap.GeoJSON 插件统一解析，避免为每种几何写单独渲染逻辑。

        Args:
            gdf_or_features: GeoDataFrame 或标准 GeoJSON Feature list。
                - 若为 GeoDataFrame，转为 GeoJSON Feature list
                - 若为 list，直接作为 features
            style: 可选的全局样式配置（fill_color/stroke_color 等），
                前端渲染时未在 feature.properties 中覆盖则用此默认值

        Returns:
            FeatureCollectionLayer 配置 dict
        """
        features = self._extract_features(gdf_or_features)
        layer: dict = {
            "type": "FeatureCollection",
            "features": features,
        }
        if style is not None:
            layer["style"] = style
        return layer

    # ------------------------------------------------------------------
    # ECharts 图表配置
    # ------------------------------------------------------------------

    def build_chart_config(
        self,
        chart_type: str,
        data: dict,
        title: str = "",
    ) -> dict:
        """构建 ECharts 配置（柱状图/折线图/饼图）。

        Args:
            chart_type: "bar" | "line" | "pie"
            data: 图表数据
                - bar/line: {"categories": [...], "values": [...]}
                - pie: {"items": [{"name": ..., "value": ...}, ...]}
            title: 图表标题

        Returns:
            ECharts option dict，前端直接 echarts.setOption(config)
        """
        if chart_type == "pie":
            return self._build_pie_config(data, title)
        # bar / line 共用类目轴结构
        return self._build_axis_config(chart_type, data, title)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_features(gdf_or_features: Any) -> list[dict]:
        """从 GeoDataFrame / GeoJSON dict / features list 提取标准 Feature list。

        - list：直接返回（已是 Feature 对象列表）
        - dict（含 "features" 键）：直接取 features（GeoJSON FeatureCollection）
        - GeoDataFrame：调用 to_json() 解析为 FeatureCollection，取 features
        """
        import json

        if isinstance(gdf_or_features, list):
            return gdf_or_features

        # GeoJSON FeatureCollection dict（来自 _gdf_to_dict 的产物）
        if isinstance(gdf_or_features, dict) and "features" in gdf_or_features:
            return gdf_or_features["features"]

        # GeoDataFrame 分支：尝试调用 to_json 转标准 GeoJSON
        try:
            geojson_str = gdf_or_features.to_json()
            geojson = json.loads(geojson_str) if isinstance(geojson_str, str) else geojson_str
            return geojson.get("features", [])
        except (AttributeError, TypeError, ValueError):
            # 不是 GeoDataFrame 或转换失败，降级返回空
            return []

    def _build_axis_config(self, chart_type: str, data: dict, title: str) -> dict:
        """构建类目轴图表配置（bar / line）。"""
        categories = data.get("categories", [])
        values = data.get("values", [])
        return {
            "title": {"text": title},
            "tooltip": {"trigger": "axis"},
            "xAxis": {"type": "category", "data": categories},
            "yAxis": {"type": "value"},
            "series": [
                {
                    "type": chart_type,
                    "data": values,
                }
            ],
        }

    def _build_pie_config(self, data: dict, title: str) -> dict:
        """构建饼图配置。"""
        items = data.get("items", [])
        return {
            "title": {"text": title},
            "tooltip": {"trigger": "item"},
            "series": [
                {
                    "type": "pie",
                    "data": items,
                }
            ],
        }
