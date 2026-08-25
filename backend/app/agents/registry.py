"""Sub-Agent 注册表 + ToolSpec 注册表。

- SubAgentSpec: 每个 sub-agent 的元数据（agent_role / system_prompt_path / tool_names / ...）
- REGISTRY: 全局 sub-agent 注册表（agent_role -> SubAgentSpec）
- ToolSpec: 单个工具的元数据（name / executor_type / is_async / description）
- TOOL_SPECS: 全局工具注册表（tool_name -> ToolSpec）
- SubAgentSpec.inline_tools / async_tools / sandbox_tools: 按 ToolSpec 分桶的工具名
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolSpec:
    """单工具的元数据。

    Attributes:
        name: 工具名（如 "geo_code"）。
        executor_type: 执行路径（"inline" | "async" | "sandbox"）。
        is_async: 工具原始实现是否为 async def（executor_type="async" 必须 is_async=True）。
        description: 工具说明（LLM prompt 用）。
        deprecated: 是否已废弃（保留向后兼容，但 LLM 不应再调用）。
    """
    name: str
    executor_type: str = "inline"  # "inline" | "async" | "sandbox"
    is_async: bool = False
    description: str = ""
    deprecated: bool = False
    semantic_action: str = ""  # preflight 规则匹配用的语义动作别名，默认与 name 相同


# ============================================================
# 全局工具注册表（单点真相）
# ============================================================

TOOL_SPECS: dict[str, ToolSpec] = {
    # async 类（IO 工具，复用 tool_execution._run_async）
    "geo_code": ToolSpec(
        name="geo_code",
        executor_type="async",
        is_async=True,
        description="地理编码（地名→坐标）/ 逆编码（坐标→地址）",
    ),
    "query_poi": ToolSpec(
        name="query_poi",
        executor_type="async",
        is_async=True,
        description="POI 查询（高德优先，OSM 兜底）",
    ),
    "fetch_from_redis": ToolSpec(
        name="fetch_from_redis",
        executor_type="async",
        is_async=True,
        description="旧版上传读取接口（仅保留兼容，不向模型暴露）",
        deprecated=True,
    ),
    "data_io_read": ToolSpec(
        name="data_io_read",
        executor_type="async",
        is_async=True,
        description="按 file_id 读取并解析本机工作区中的上传文件",
    ),

    # inline 类（纯本地计算，主进程 exec 零开销）
    "buffer": ToolSpec(
        name="buffer",
        executor_type="inline",
        is_async=False,
        description="缓冲区分析",
    ),
    "overlay": ToolSpec(
        name="overlay",
        executor_type="inline",
        is_async=False,
        description="叠加分析（交集/并集/差集）",
    ),
    "voronoi": ToolSpec(
        name="voronoi",
        executor_type="inline",
        is_async=False,
        description="泰森多边形",
    ),
    "isochrone": ToolSpec(
        name="isochrone",
        executor_type="inline",
        is_async=False,
        description="等时圈（驾车/步行/骑行 N 分钟可达范围）",
    ),
    "map_layer_build": ToolSpec(
        name="map_layer_build",
        executor_type="inline",
        is_async=False,
        description="生成地图图层配置",
    ),

    # sandbox 类（不可信输入/可能死循环，子进程隔离）
    "parse_zip": ToolSpec(
        name="parse_zip",
        executor_type="sandbox",
        is_async=False,
        description="旧版 ZIP 解析接口（仅保留兼容，不向模型暴露）",
        deprecated=True,
    ),
    "code_executor": ToolSpec(
        name="code_executor",
        executor_type="sandbox",
        is_async=False,
        description="在受限 Python 沙箱里运行代码（兼容旧接口）",
    ),

    # 坐标转换工具（纯数学计算，inline）
    "geo_transform": ToolSpec(
        name="geo_transform",
        executor_type="inline",
        is_async=False,
        description="WGS84/GCJ02/BD09 互转（纯数学偏转，无 IO）",
    ),

    # ============================================================
    # KERNEL 语义工具（ToolKit / Skill / Clarification）
    # ============================================================

    "select_toolkit": ToolSpec(
        name="select_toolkit",
        executor_type="inline",
        is_async=False,
        description="激活指定 ToolKit（data_io / vector_analysis / vector_overlay），扩展可见工具集。",
    ),
    "inspect_workspace": ToolSpec(
        name="inspect_workspace",
        executor_type="inline",
        is_async=False,
        description="显示当前工作区所有图层的 CRS / 字段 / 几何类型。",
    ),
    "suggest_skill": ToolSpec(
        name="suggest_skill",
        executor_type="inline",
        is_async=False,
        description="（v1 占位）基于当前任务推荐 skill；暂返回可用 skill 列表。",
    ),
    "load_skill": ToolSpec(
        name="load_skill",
        executor_type="inline",
        is_async=False,
        description="加载一份 GIS best-practice skill（meter_buffer / spatial_join），内容注入后续 prompt。",
    ),
    "proactive_clarification": ToolSpec(
        name="proactive_clarification",
        executor_type="inline",
        is_async=False,
        description="（v1 占位）向用户提出澄清问题；暂返回可用工具列表。",
    ),

    # ============================================================
    # Vector 分析
    # ============================================================
    "clip_layer": ToolSpec(
        name="clip_layer",
        executor_type="inline",
        is_async=False,
        description="矢量裁剪 — clip_layer(input_ref, overlay_ref)，用叠加图层裁剪输入图层",
    ),
    "dissolve_layer": ToolSpec(
        name="dissolve_layer",
        executor_type="inline",
        is_async=False,
        description="融合 — dissolve_layer(input_ref, by)，按字段融合相邻面",
    ),
    "merge_layers": ToolSpec(
        name="merge_layers",
        executor_type="inline",
        is_async=False,
        description="合并多个图层",
    ),
    "join_by_location": ToolSpec(
        name="join_by_location",
        executor_type="inline",
        is_async=False,
        description="按位置空间连接 — join_by_location(input_ref, join_ref, predicate)，空间谓词连接两个图层",
    ),
    "join_by_nearest": ToolSpec(
        name="join_by_nearest",
        executor_type="inline",
        is_async=False,
        description="最近邻空间连接",
    ),
    "count_points_in_polygon": ToolSpec(
        name="count_points_in_polygon",
        executor_type="inline",
        is_async=False,
        description="计算面内点数 — count_points_in_polygon(points_ref, polygons_ref)，统计每个面内的点数",
    ),
    "extract_by_location": ToolSpec(
        name="extract_by_location",
        executor_type="inline",
        is_async=False,
        description="按位置筛选 — extract_by_location(input_ref, mask_ref, predicate)，空间谓词过滤",
    ),
    "convex_hull": ToolSpec(
        name="convex_hull",
        executor_type="inline",
        is_async=False,
        description="凸包 — convex_hull(input_ref)，计算最小凸多边形",
    ),
    "bounding_boxes": ToolSpec(
        name="bounding_boxes",
        executor_type="inline",
        is_async=False,
        description="外包矩形",
    ),

    # ============================================================
    # Vector 变换
    # ============================================================
    "centroid_layer": ToolSpec(
        name="centroid_layer",
        executor_type="inline",
        is_async=False,
        description="计算质心 — centroid_layer(input_ref)，返回面的几何中心点",
    ),
    "point_on_surface": ToolSpec(
        name="point_on_surface",
        executor_type="inline",
        is_async=False,
        description="面内点",
    ),
    "simplify_geometry": ToolSpec(
        name="simplify_geometry",
        executor_type="inline",
        is_async=False,
        description="简化几何 — simplify_geometry(input_ref, tolerance)，Douglas-Peucker 简化",
    ),
    "fix_geometries": ToolSpec(
        name="fix_geometries",
        executor_type="inline",
        is_async=False,
        description="修复无效几何 — fix_geometries(input_ref)，自动修复自交/环方向等问题",
    ),
    "check_validity": ToolSpec(
        name="check_validity",
        executor_type="inline",
        is_async=False,
        description="检查几何有效性",
    ),
    "multipart_to_singlepart": ToolSpec(
        name="multipart_to_singlepart",
        executor_type="inline",
        is_async=False,
        description="多部件拆分",
    ),
    "delete_duplicate_geometries": ToolSpec(
        name="delete_duplicate_geometries",
        executor_type="inline",
        is_async=False,
        description="删除重复几何",
    ),
    "snap_geometries": ToolSpec(
        name="snap_geometries",
        executor_type="inline",
        is_async=False,
        description="几何吸附",
    ),
    "reproject_layer": ToolSpec(
        name="reproject_layer",
        executor_type="inline",
        is_async=False,
        description="重投影图层 — reproject_layer(input_ref, target_crs)，转换坐标参考系",
    ),
    "batch_reproject_layers": ToolSpec(
        name="batch_reproject_layers",
        executor_type="inline",
        is_async=False,
        description="批量重投影",
    ),

    # ============================================================
    # 属性
    # ============================================================
    "extract_by_attribute": ToolSpec(
        name="extract_by_attribute",
        executor_type="inline",
        is_async=False,
        description="按属性筛选 — extract_by_attribute(input_ref, field, operator, value)，SQL 风格属性过滤",
    ),
    "keep_fields": ToolSpec(
        name="keep_fields",
        executor_type="inline",
        is_async=False,
        description="保留字段",
    ),
    "rename_field": ToolSpec(
        name="rename_field",
        executor_type="inline",
        is_async=False,
        description="重命名字段",
    ),
    "field_calculator": ToolSpec(
        name="field_calculator",
        executor_type="inline",
        is_async=False,
        description="字段计算 — field_calculator(input_ref, field_name, expression, field_type)，计算并添加新字段",
    ),

    # ============================================================
    # Raster
    # ============================================================
    "reproject_raster": ToolSpec(
        name="reproject_raster",
        executor_type="inline",
        is_async=False,
        description="重投影栅格",
    ),
    "clip_raster_by_mask": ToolSpec(
        name="clip_raster_by_mask",
        executor_type="inline",
        is_async=False,
        description="按掩膜裁剪栅格",
    ),
    "clip_raster_by_extent": ToolSpec(
        name="clip_raster_by_extent",
        executor_type="inline",
        is_async=False,
        description="按范围裁剪栅格",
    ),
    "raster_calculator": ToolSpec(
        name="raster_calculator",
        executor_type="inline",
        is_async=False,
        description="栅格计算器",
    ),
    "zonal_statistics": ToolSpec(
        name="zonal_statistics",
        executor_type="inline",
        is_async=False,
        description="分区统计 — zonal_statistics(raster_ref, zones_ref, stats)，按区域统计栅格值",
    ),
    "raster_sampling": ToolSpec(
        name="raster_sampling",
        executor_type="inline",
        is_async=False,
        description="栅格采样",
    ),
    "rasterize_vector": ToolSpec(
        name="rasterize_vector",
        executor_type="inline",
        is_async=False,
        description="矢量转栅格",
    ),
    "polygonize_raster": ToolSpec(
        name="polygonize_raster",
        executor_type="inline",
        is_async=False,
        description="栅格转矢量",
    ),
    "slope": ToolSpec(
        name="slope",
        executor_type="inline",
        is_async=False,
        description="坡度分析 — slope(input_ref)，从 DEM 计算坡度(度)",
    ),
    "aspect": ToolSpec(
        name="aspect",
        executor_type="inline",
        is_async=False,
        description="坡向分析 — aspect(input_ref)，从 DEM 计算坡向",
    ),
    "hillshade": ToolSpec(
        name="hillshade",
        executor_type="inline",
        is_async=False,
        description="山体阴影 — hillshade(input_ref, azimuth, altitude)，基于 DEM 生成山体阴影",
    ),
    "contour": ToolSpec(
        name="contour",
        executor_type="inline",
        is_async=False,
        description="等高线",
    ),
    "reclassify_raster": ToolSpec(
        name="reclassify_raster",
        executor_type="inline",
        is_async=False,
        description="栅格重分类 — reclassify_raster(input_ref, rules)，按规则重新赋值栅格",
    ),
    "terrain_ruggedness_index": ToolSpec(
        name="terrain_ruggedness_index",
        executor_type="inline",
        is_async=False,
        description="地形崎岖指数",
    ),
    "topographic_position_index": ToolSpec(
        name="topographic_position_index",
        executor_type="inline",
        is_async=False,
        description="地形位置指数",
    ),
    "roughness": ToolSpec(
        name="roughness",
        executor_type="inline",
        is_async=False,
        description="地表粗糙度",
    ),

    # ============================================================
    # IO
    # ============================================================
    "load_vector": ToolSpec(
        name="load_vector",
        executor_type="async",
        is_async=True,
        description="加载矢量数据",
    ),
    "load_raster": ToolSpec(
        name="load_raster",
        executor_type="async",
        is_async=True,
        description="加载栅格数据",
    ),
    "load_csv": ToolSpec(
        name="load_csv",
        executor_type="async",
        is_async=True,
        description="加载CSV数据",
    ),
    "csv_to_points": ToolSpec(
        name="csv_to_points",
        executor_type="inline",
        is_async=False,
        description="CSV转点图层",
    ),
    "summarize_layer": ToolSpec(
        name="summarize_layer",
        executor_type="inline",
        is_async=False,
        description="图层摘要",
    ),
    "export_result": ToolSpec(
        name="export_result",
        executor_type="async",
        is_async=True,
        description="导出结果",
    ),
}


def get_semantic_action(spec: ToolSpec) -> str:
    """返回工具的语义动作名，用于 preflight 规则匹配。"""
    return spec.semantic_action or spec.name


def get_tool_spec(name: str) -> ToolSpec:
    """查工具 spec，未登记返回 inline 占位 + warning（向后兼容）。"""
    if name not in TOOL_SPECS:
        warnings.warn(
            f"Tool {name!r} not in TOOL_SPECS; defaulting to executor_type='sandbox' "
            f"for safety. Please register it in app.agents.registry.TOOL_SPECS.",
            stacklevel=2,
        )
        return ToolSpec(name=name, executor_type="sandbox")
    return TOOL_SPECS[name]


@dataclass(frozen=True)
class SubAgentSpec:
    agent_role: str
    system_prompt_path: str
    tool_names: list[str]
    max_iterations: int = 6
    verifier_required: bool = True
    role_label: str = ""
    execution_mode: str = "tool_call"  # normal roles use JSON Schema; coder keeps Python code

    @property
    def inline_tools(self) -> list[str]:
        """只返回 inline 类工具名。"""
        return [n for n in self.tool_names if get_tool_spec(n).executor_type == "inline"]

    @property
    def async_tools(self) -> list[str]:
        """只返回 async 类工具名。"""
        return [n for n in self.tool_names if get_tool_spec(n).executor_type == "async"]

    @property
    def sandbox_tools(self) -> list[str]:
        """只返回 sandbox 类工具名。"""
        return [n for n in self.tool_names if get_tool_spec(n).executor_type == "sandbox"]


REGISTRY: dict[str, SubAgentSpec] = {
    "geo": SubAgentSpec(
        agent_role="geo",
        system_prompt_path="app/agents/prompts/geo.md",
        tool_names=["geo_code", "geo_transform"],
        max_iterations=4,
        role_label="地名解析",
    ),
    "poi": SubAgentSpec(
        agent_role="poi",
        system_prompt_path="app/agents/prompts/poi.md",
        tool_names=["geo_code", "query_poi"],
        max_iterations=6,
        role_label="POI 查询",
    ),
    "geometer": SubAgentSpec(
        agent_role="geometer",
        system_prompt_path="app/agents/prompts/geometer.md",
        tool_names=[
            "data_io_read", "buffer", "overlay", "voronoi", "isochrone", "geo_code",
            "clip_layer", "dissolve_layer", "merge_layers",
            "join_by_location", "join_by_nearest", "count_points_in_polygon",
            "extract_by_location", "centroid_layer", "point_on_surface",
            "simplify_geometry", "fix_geometries", "check_validity",
            "reproject_layer", "convex_hull", "bounding_boxes", "export_result",
            "extract_by_attribute", "field_calculator",
            "slope", "aspect", "hillshade", "zonal_statistics", "reclassify_raster",
        ],
        max_iterations=6,
        role_label="空间分析",
    ),
    "viz": SubAgentSpec(
        agent_role="viz",
        system_prompt_path="app/agents/prompts/viz.md",
        tool_names=["data_io_read", "map_layer_build"],
        max_iterations=3,
        role_label="可视化",
    ),
    "coder": SubAgentSpec(
        agent_role="coder",
        system_prompt_path="app/agents/prompts/coder.md",
        tool_names=[
            "data_io_read", "code_executor",
            "extract_by_attribute", "keep_fields", "rename_field",
            "field_calculator", "slope", "aspect", "hillshade",
            "contour", "raster_calculator", "zonal_statistics",
            "reclassify_raster",
        ],
        max_iterations=3,
        verifier_required=False,
        role_label="代码执行",
        execution_mode="code",
    ),
    "verifier": SubAgentSpec(
        agent_role="verifier",
        system_prompt_path="app/agents/prompts/verifier.md",
        tool_names=[],
        max_iterations=2,
        role_label="对抗审查",
    ),
}


def get_spec(role: str) -> SubAgentSpec:
    if role not in REGISTRY:
        raise KeyError(f"Unknown agent_role={role!r}; available: {list(REGISTRY)}")
    return REGISTRY[role]


def list_roles() -> list[str]:
    return list(REGISTRY.keys())
