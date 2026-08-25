"""MapLayerBuilder 单元测试。

覆盖维度（参考 GIS_Agent_技术文档.md §4.6 + docs/02_data_models.md §4.1）：
1. build_point_layer 结构正确，高德橙色高亮样式
2. build_point_layer OSM 灰色半透明样式
3. build_heatmap_layer 含 weights/radius/gradient
4. build_polygon_layer 结构
5. build_polyline_layer 结构
6. build_feature_collection 从 features list 构建
7. build_feature_collection 支持 Polygon 含孔洞
8. build_chart_config 柱状图配置
9. 坐标保持 GCJ02（不转换）

核心约束：
- 后端只生成图层数据配置（JSON），不生成地图 HTML
- 所有坐标保持 GCJ02（前端高德 JS API 直接渲染）
- 数据源视觉隔离：高德橙色高亮，OSM 灰色半透明
- FeatureCollection 支持孔洞和多维坐标
"""

import pytest

from app.tools.map_layer import MapLayerBuilder


# ============================================================
# 1. build_point_layer 结构正确，高德样式
# ============================================================

def test_build_point_layer_amap_structure():
    """高德数据源：返回 PointLayer 配置，type=point，coordinates 透传，source=Amap。"""
    builder = MapLayerBuilder()
    coords = [[118.7845, 32.0429], [118.7856, 32.0418]]
    layer = builder.build_point_layer(coords, source="Amap")

    assert layer["type"] == "point"
    assert layer["source"] == "Amap"
    assert layer["coordinates"] == coords
    # popup_fields 默认应为列表（可空）
    assert isinstance(layer["popup_fields"], list)


def test_build_point_layer_amap_style():
    """高德样式：highlight_pin + #FF6B35 橙色。"""
    builder = MapLayerBuilder()
    layer = builder.build_point_layer([[118.7845, 32.0429]], source="Amap")
    style = layer["style"]
    assert style["icon"] == "highlight_pin"
    assert style["color"] == "#FF6B35"


def test_build_point_layer_popup_fields_custom():
    """自定义 popup_fields 透传。"""
    builder = MapLayerBuilder()
    layer = builder.build_point_layer(
        [[118.7845, 32.0429]],
        source="Amap",
        popup_fields=["name", "address", "tel"],
    )
    assert layer["popup_fields"] == ["name", "address", "tel"]


# ============================================================
# 2. build_point_layer OSM 样式（灰色半透明）
# ============================================================

def test_build_point_layer_osm_cn_style():
    """OSM_CN 样式：gray_dot + #999999 + opacity 0.6。"""
    builder = MapLayerBuilder()
    layer = builder.build_point_layer([[118.7845, 32.0429]], source="OSM_CN")
    style = layer["style"]
    assert style["icon"] == "gray_dot"
    assert style["color"] == "#999999"
    assert style["opacity"] == 0.6


def test_build_point_layer_osm_global_style():
    """OSM_Global 同样使用灰色半透明样式（数据源视觉隔离）。"""
    builder = MapLayerBuilder()
    layer = builder.build_point_layer([[-122.4194, 37.7749]], source="OSM_Global")
    style = layer["style"]
    assert style["icon"] == "gray_dot"
    assert style["color"] == "#999999"
    assert style["opacity"] == 0.6


def test_build_point_layer_source_field_set():
    """source 字段应被设置为传入值。"""
    builder = MapLayerBuilder()
    for src in ("Amap", "OSM_CN", "OSM_Global", "Upload"):
        layer = builder.build_point_layer([[118.78, 32.04]], source=src)
        assert layer["source"] == src


# ============================================================
# 3. build_heatmap_layer 含 weights/radius/gradient
# ============================================================

def test_build_heatmap_layer_basic():
    """热力图基础结构：type=heatmap，coordinates 透传。"""
    builder = MapLayerBuilder()
    coords = [[118.78, 32.04], [118.79, 32.05], [118.80, 32.06]]
    layer = builder.build_heatmap_layer(coords)

    assert layer["type"] == "heatmap"
    assert layer["coordinates"] == coords


def test_build_heatmap_layer_with_weights():
    """带权重的热力图：weights 透传，长度与 coordinates 一致。"""
    builder = MapLayerBuilder()
    coords = [[118.78, 32.04], [118.79, 32.05]]
    weights = [0.5, 0.9]
    layer = builder.build_heatmap_layer(coords, weights=weights)

    assert layer["weights"] == weights
    assert len(layer["weights"]) == len(layer["coordinates"])


def test_build_heatmap_layer_default_radius():
    """默认 radius=25。"""
    builder = MapLayerBuilder()
    layer = builder.build_heatmap_layer([[118.78, 32.04]])
    assert layer["radius"] == 25


def test_build_heatmap_layer_custom_radius():
    """自定义 radius 透传。"""
    builder = MapLayerBuilder()
    layer = builder.build_heatmap_layer([[118.78, 32.04]], radius=40)
    assert layer["radius"] == 40


def test_build_heatmap_layer_gradient():
    """热力图应包含 gradient 配置（默认渐变）。"""
    builder = MapLayerBuilder()
    layer = builder.build_heatmap_layer([[118.78, 32.04]])
    assert "gradient" in layer
    gradient = layer["gradient"]
    # 渐变应包含至少 3 个断点
    assert len(gradient) >= 3
    # 断点值应为颜色字符串
    for k, v in gradient.items():
        assert isinstance(v, str)


def test_build_heatmap_layer_no_weights_default_none():
    """无 weights 时应为 None（符合 HeatmapLayer 模型默认）。"""
    builder = MapLayerBuilder()
    layer = builder.build_heatmap_layer([[118.78, 32.04]])
    assert layer["weights"] is None


# ============================================================
# 4. build_polygon_layer 结构
# ============================================================

def test_build_polygon_layer_basic():
    """多边形基础结构：type=polygon，coordinates 透传，默认填充样式。"""
    builder = MapLayerBuilder()
    # 单个多边形：外环坐标
    ring = [[118.78, 32.04], [118.79, 32.04], [118.79, 32.05], [118.78, 32.04]]
    coords = [ring]  # 多面列表
    layer = builder.build_polygon_layer(coords)

    assert layer["type"] == "polygon"
    assert layer["coordinates"] == coords
    assert layer["fill_color"] == "#3388ff"
    assert layer["fill_opacity"] == 0.3


def test_build_polygon_layer_custom_style():
    """自定义填充颜色和透明度。"""
    builder = MapLayerBuilder()
    ring = [[118.78, 32.04], [118.79, 32.04], [118.79, 32.05], [118.78, 32.04]]
    layer = builder.build_polygon_layer(
        [ring], fill_color="#FF0000", fill_opacity=0.5
    )
    assert layer["fill_color"] == "#FF0000"
    assert layer["fill_opacity"] == 0.5


# ============================================================
# 5. build_polyline_layer 结构
# ============================================================

def test_build_polyline_layer_basic():
    """线层基础结构：type=polyline，coordinates 透传，默认描边样式。"""
    builder = MapLayerBuilder()
    line = [[118.78, 32.04], [118.79, 32.05], [118.80, 32.06]]
    coords = [line]  # 多线列表
    layer = builder.build_polyline_layer(coords)

    assert layer["type"] == "polyline"
    assert layer["coordinates"] == coords
    assert layer["stroke_color"] == "#FF6B35"
    assert layer["stroke_width"] == 4


def test_build_polyline_layer_custom_style():
    """自定义描边颜色和宽度。"""
    builder = MapLayerBuilder()
    line = [[118.78, 32.04], [118.79, 32.05]]
    layer = builder.build_polyline_layer(
        [line], stroke_color="#00FF00", stroke_width=6
    )
    assert layer["stroke_color"] == "#00FF00"
    assert layer["stroke_width"] == 6


# ============================================================
# 6. build_feature_collection 从 features list 构建
# ============================================================

def test_build_feature_collection_from_features():
    """从 features list 构建 FeatureCollection：type=FeatureCollection，features 透传。"""
    builder = MapLayerBuilder()
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [118.7845, 32.0429]},
            "properties": {"name": "蜜雪冰城"},
        },
        {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [[118.78, 32.04], [118.79, 32.05]],
            },
            "properties": {"name": "路径"},
        },
    ]
    layer = builder.build_feature_collection(features)

    assert layer["type"] == "FeatureCollection"
    assert layer["features"] == features
    assert len(layer["features"]) == 2


def test_build_feature_collection_empty():
    """空 features 列表也应能构建合法 FeatureCollection。"""
    builder = MapLayerBuilder()
    layer = builder.build_feature_collection([])
    assert layer["type"] == "FeatureCollection"
    assert layer["features"] == []


def test_build_feature_collection_with_style():
    """FeatureCollection 支持可选 style（如统一描边/填充）。"""
    builder = MapLayerBuilder()
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [118.78, 32.04]},
            "properties": {},
        },
    ]
    style = {"fill_color": "#FF0000", "stroke_color": "#00FF00"}
    layer = builder.build_feature_collection(features, style=style)
    assert layer["style"] == style


# ============================================================
# 7. build_feature_collection 支持 Polygon 含孔洞
# ============================================================

def test_build_feature_collection_polygon_with_hole():
    """FeatureCollection 支持 Polygon 含孔洞：coordinates[0] 外环，[1] 孔洞。"""
    builder = MapLayerBuilder()
    # 外环 + 一个孔洞
    exterior = [
        [118.78, 32.04],
        [118.80, 32.04],
        [118.80, 32.06],
        [118.78, 32.06],
        [118.78, 32.04],  # 闭合
    ]
    hole = [
        [118.785, 32.045],
        [118.795, 32.045],
        [118.795, 32.055],
        [118.785, 32.055],
        [118.785, 32.045],  # 闭合
    ]
    feature = {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [exterior, hole],  # [外环, 孔洞]
        },
        "properties": {"name": "带孔洞的多边形"},
    }
    layer = builder.build_feature_collection([feature])

    assert layer["type"] == "FeatureCollection"
    geom = layer["features"][0]["geometry"]
    assert geom["type"] == "Polygon"
    # coordinates 应包含两个环：外环 + 孔洞
    assert len(geom["coordinates"]) == 2
    assert geom["coordinates"][0] == exterior
    assert geom["coordinates"][1] == hole


def test_build_feature_collection_polygon_without_hole():
    """无孔洞的 Polygon：coordinates 只含外环。"""
    builder = MapLayerBuilder()
    exterior = [
        [118.78, 32.04],
        [118.80, 32.04],
        [118.80, 32.06],
        [118.78, 32.06],
        [118.78, 32.04],
    ]
    feature = {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [exterior]},
        "properties": {},
    }
    layer = builder.build_feature_collection([feature])
    geom = layer["features"][0]["geometry"]
    assert len(geom["coordinates"]) == 1
    assert geom["coordinates"][0] == exterior


def test_build_feature_collection_mixed_geometry_types():
    """FeatureCollection 可混合 Point/LineString/Polygon。"""
    builder = MapLayerBuilder()
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [118.78, 32.04]},
            "properties": {},
        },
        {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [[118.78, 32.04], [118.79, 32.05]],
            },
            "properties": {},
        },
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[118.78, 32.04], [118.79, 32.04], [118.79, 32.05], [118.78, 32.04]]],
            },
            "properties": {},
        },
    ]
    layer = builder.build_feature_collection(features)
    types = [f["geometry"]["type"] for f in layer["features"]]
    assert types == ["Point", "LineString", "Polygon"]


# ============================================================
# 8. build_chart_config 柱状图配置
# ============================================================

def test_build_chart_config_bar():
    """柱状图配置：type=bar，含 title/x_axis/series。"""
    builder = MapLayerBuilder()
    data = {
        "categories": ["蜜雪冰城", "喜茶", "星巴克"],
        "values": [12, 5, 8],
    }
    config = builder.build_chart_config("bar", data, title="POI 数量统计")

    assert config["title"]["text"] == "POI 数量统计"
    # 应包含 ECharts option 结构
    assert "xAxis" in config or "x_axis" in config
    assert "series" in config
    # 柱状图 series 类型应为 bar
    series = config["series"]
    assert isinstance(series, list)
    assert series[0]["type"] == "bar"
    assert series[0]["data"] == [12, 5, 8]


def test_build_chart_config_bar_categories():
    """柱状图 x 轴类目应透传 data['categories']。"""
    builder = MapLayerBuilder()
    data = {"categories": ["A", "B", "C"], "values": [1, 2, 3]}
    config = builder.build_chart_config("bar", data)
    # 兼容 xAxis 或 x_axis 字段名
    x_axis = config.get("xAxis", config.get("x_axis"))
    assert x_axis["data"] == ["A", "B", "C"]


def test_build_chart_config_line():
    """折线图配置：series 类型为 line。"""
    builder = MapLayerBuilder()
    data = {
        "categories": ["1月", "2月", "3月"],
        "values": [10, 15, 12],
    }
    config = builder.build_chart_config("line", data, title="月度趋势")

    assert config["title"]["text"] == "月度趋势"
    assert config["series"][0]["type"] == "line"
    assert config["series"][0]["data"] == [10, 15, 12]


def test_build_chart_config_pie():
    """饼图配置：series 类型为 pie，data 含 name/value。"""
    builder = MapLayerBuilder()
    data = {
        "items": [
            {"name": "蜜雪冰城", "value": 12},
            {"name": "喜茶", "value": 5},
        ]
    }
    config = builder.build_chart_config("pie", data, title="品牌占比")

    assert config["title"]["text"] == "品牌占比"
    assert config["series"][0]["type"] == "pie"
    assert config["series"][0]["data"] == data["items"]


def test_build_chart_config_default_title():
    """无 title 时默认空字符串。"""
    builder = MapLayerBuilder()
    data = {"categories": ["A"], "values": [1]}
    config = builder.build_chart_config("bar", data)
    assert config["title"]["text"] == ""


# ============================================================
# 9. 坐标保持 GCJ02（不转换）
# ============================================================

def test_point_layer_coordinates_not_transformed():
    """build_point_layer 不对坐标做任何转换（保持 GCJ02）。

    验证：传入的坐标与输出的 coordinates 完全一致（包括小数位）。
    """
    builder = MapLayerBuilder()
    # 南京新街口 GCJ02 坐标
    coords = [[118.78451234, 32.04298765]]
    layer = builder.build_point_layer(coords, source="Amap")
    assert layer["coordinates"] == coords
    # 确保未发生偏转（偏转后坐标会变化）
    assert layer["coordinates"][0][0] == pytest.approx(118.78451234, abs=1e-9)
    assert layer["coordinates"][0][1] == pytest.approx(32.04298765, abs=1e-9)


def test_heatmap_layer_coordinates_not_transformed():
    """build_heatmap_layer 不对坐标做转换。"""
    builder = MapLayerBuilder()
    coords = [[118.7845, 32.0429], [118.7856, 32.0418]]
    layer = builder.build_heatmap_layer(coords)
    assert layer["coordinates"] == coords


def test_polygon_layer_coordinates_not_transformed():
    """build_polygon_layer 不对坐标做转换。"""
    builder = MapLayerBuilder()
    ring = [[118.78, 32.04], [118.79, 32.04], [118.79, 32.05], [118.78, 32.04]]
    layer = builder.build_polygon_layer([ring])
    assert layer["coordinates"] == [ring]


def test_feature_collection_coordinates_not_transformed():
    """build_feature_collection 不对坐标做转换（包括孔洞坐标）。"""
    builder = MapLayerBuilder()
    exterior = [[118.78, 32.04], [118.80, 32.04], [118.80, 32.06], [118.78, 32.04]]
    hole = [[118.785, 32.045], [118.795, 32.045], [118.795, 32.055], [118.785, 32.045]]
    feature = {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [exterior, hole]},
        "properties": {},
    }
    layer = builder.build_feature_collection([feature])
    coords = layer["features"][0]["geometry"]["coordinates"]
    assert coords == [exterior, hole]
    # 外环和孔洞坐标都应保持原值
    assert coords[0] == exterior
    assert coords[1] == hole


def test_point_layer_foreign_coords_not_transformed():
    """国外坐标也不转换（build_point_layer 只做配置生成，不做坐标偏转）。"""
    builder = MapLayerBuilder()
    coords = [[-122.4194, 37.7749]]  # 旧金山
    layer = builder.build_point_layer(coords, source="OSM_Global")
    assert layer["coordinates"] == coords
