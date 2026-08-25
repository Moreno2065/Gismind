"""LLM factories and the executable code-mode prompt contract."""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from app.config import settings


def create_llm() -> ChatOpenAI:
    """Create the JSON-mode LLM used by root planning and synthesis."""
    return ChatOpenAI(
        model=settings.LLM_MODEL,
        api_key=settings.LLM_API_KEY,
        base_url=settings.LLM_BASE_URL,
        temperature=settings.LLM_TEMPERATURE,
        timeout=settings.LLM_TIMEOUT,
        max_tokens=settings.LLM_MAX_TOKENS,
        model_kwargs={"response_format": {"type": "json_object"}},
    )


def create_code_mode_llm() -> ChatOpenAI:
    """Create the unconstrained LLM used to emit Python code."""
    return ChatOpenAI(
        model=settings.LLM_MODEL,
        api_key=settings.LLM_API_KEY,
        base_url=settings.LLM_BASE_URL,
        temperature=settings.LLM_TEMPERATURE,
        timeout=settings.LLM_TIMEOUT,
        max_tokens=settings.LLM_MAX_TOKENS,
    )


def build_code_mode_prompt(
    agent_role: str,
    toolkit_catalog: dict | None = None,
    loaded_skills: dict | None = None,
) -> str:
    """Build a prompt from the current role's real runtime permissions."""
    from app.agents.registry import get_spec, get_tool_spec

    role_spec = get_spec(agent_role)
    tool_specs = [get_tool_spec(name) for name in role_spec.tool_names]
    base_prompt = _build_runtime_contract_prompt(role_spec, tool_specs)
    return inject_toolkit_and_skills(base_prompt, toolkit_catalog, loaded_skills)


def _build_code_mode_prompt(agent_role: str) -> str:
    """Backward-compatible private entry point used by older imports."""
    return build_code_mode_prompt(agent_role)


def _build_runtime_contract_prompt(role_spec, tool_specs: list) -> str:
    tool_specs = [tool for tool in tool_specs if not tool.deprecated]
    signatures = {
        "geo_code": "geo_code(address='南京新街口')",
        "geo_transform": "geo_transform(operation='wgs84_to_gcj02', lng=118.7, lat=32.0)",
        "query_poi": "query_poi(query='咖啡店', location=[118.78, 32.04], radius=1500)",
        "data_io_read": "data_io_read(file_id='file_...')",
        "buffer": "buffer(geometry_from=geometry, radius_m=500)",
        "overlay": "overlay(geometry_a_from=left, geometry_b_from=right, how='intersection')",
        "voronoi": "voronoi(points_from=points)",
        "isochrone": "isochrone(location=[118.78, 32.04], mode='walking', time_min=15)",
        "map_layer_build": "map_layer_build(geometry_from=geometry)",
        "code_executor": "code_executor(code=python_source)",
        "clip_layer": "clip_layer(input_ref=layer, overlay_ref=mask)",
        "dissolve_layer": "dissolve_layer(input_ref=layer, by='field')",
        "merge_layers": "merge_layers(layers_ref=layers)",
        "join_by_location": "join_by_location(input_ref=left, join_ref=right, predicate='intersects')",
        "join_by_nearest": "join_by_nearest(input_ref=left, join_ref=right, max_distance=1000)",
        "count_points_in_polygon": "count_points_in_polygon(points_ref=points, polygons_ref=polygons)",
        "extract_by_location": "extract_by_location(input_ref=layer, mask_ref=mask, predicate='intersects')",
        "centroid_layer": "centroid_layer(input_ref=layer)",
        "point_on_surface": "point_on_surface(input_ref=layer)",
        "simplify_geometry": "simplify_geometry(input_ref=layer, tolerance=10)",
        "fix_geometries": "fix_geometries(input_ref=layer)",
        "check_validity": "check_validity(input_ref=layer)",
        "reproject_layer": "reproject_layer(input_ref=layer, target_crs='EPSG:4548')",
        "convex_hull": "convex_hull(input_ref=layer)",
        "bounding_boxes": "bounding_boxes(input_ref=layer)",
        "extract_by_attribute": "extract_by_attribute(input_ref=layer, expression=\"type == 'park'\")",
        "keep_fields": "keep_fields(input_ref=layer, fields=['name', 'geometry'])",
        "rename_field": "rename_field(input_ref=layer, old_name='old', new_name='new')",
        "field_calculator": "field_calculator(input_ref=layer, field_name='area_m2', expression='$area')",
        "slope": "slope(dem_path=path, degree=True)",
        "aspect": "aspect(dem_path=path, degree=True)",
        "hillshade": "hillshade(dem_path=path, azimuth=315, altitude=45)",
        "reclassify_raster": "reclassify_raster(src_path=path, bins=[15, 30], values=[1, 2, 3])",
        "contour": "contour(dem_path=path, interval=10)",
        "raster_calculator": "raster_calculator(expression='A * 2', bands={'A': path})",
        "zonal_statistics": "zonal_statistics(raster_path=path, vector_path=zones_path, stats=['mean'])",
    }

    lines = [
        f"# {role_spec.role_label or role_spec.agent_role} sub-agent（code mode）",
        "",
        "# 可用函数",
        "只允许调用下面列出的函数；它们已注入运行环境，无需 import：",
    ]
    for tool in tool_specs:
        call = signatures.get(tool.name, f"{tool.name}(...)")
        lines.append(f"- `{call}` — {tool.description}")

    lines.extend([
        "",
        "# 数据流契约",
        "- 所有工具只使用关键字参数，不要写位置参数，也不要使用 await。",
        "- 当前会话变量和依赖子任务产物已经按变量名注入；直接使用这些 Python 值。",
        "- geometry_from、points_from、input_ref 等参数接收直接数据值，不是隐藏的数字索引。",
        "- 不要生成 depends_on；那是旧 JSON 模式的字段。",
        "- 上传文件先调用 data_io_read(file_id='file_...')；file_id 来自 upload_file_ids/upload_0。",
        "- 工具失败会返回含 status/message/error_code 的 dict；先检查 status 再决定是否重试。",
        "",
        "# 输出契约",
        "- 只输出可执行 Python 代码，不要输出 JSON 计划或解释文字。",
        "- 最终必须赋值 __result__ = {...}；地图结果把 layers 放入该 dict。",
        "- 不要 import，不要使用 while 或 try/except。",
    ])

    available = {tool.name for tool in tool_specs}
    if "geo_code" in available:
        lines.extend([
            "", "# 地理编码示例",
            "geo = geo_code(address='南京新街口')",
            "__result__ = {'location': geo.get('location'), 'geo_result': geo}",
        ])
    if "query_poi" in available:
        lines.extend([
            "", "# POI 示例",
            "geo = geo_code(address='南京新街口')",
            "pois = query_poi(query='咖啡店', location=geo['location'], radius=1500)",
            "__result__ = {'pois': pois.get('pois', []), 'location': geo['location']}",
        ])
    if "data_io_read" in available:
        lines.extend([
            "", "# 上传文件示例",
            "uploaded = data_io_read(file_id=upload_file_ids[0])",
            "geometry = uploaded.get('data', {})",
            "__result__ = {'uploaded': uploaded, 'geometry': geometry}",
        ])
    if "map_layer_build" in available:
        lines.extend([
            "", "# 地图图层示例（dependency_geometry 是已注入的依赖产物）",
            "layer_result = map_layer_build(geometry_from=dependency_geometry)",
            "__result__ = {'layers': layer_result.get('layers', [])}",
        ])
    return "\n".join(lines)


def inject_toolkit_and_skills(
    base_prompt: str,
    toolkit_catalog: dict | None = None,
    loaded_skills: dict | None = None,
) -> str:
    """Append active toolkit metadata and loaded skill content."""
    sections: list[str] = []
    if toolkit_catalog:
        lines = ["\n## 可用工具集（ToolKits）"]
        for name, info in toolkit_catalog.items():
            description = info.get("description", "") if isinstance(info, dict) else str(info)
            tools = info.get("tools", []) if isinstance(info, dict) else []
            tool_list = ", ".join(tools) if tools else ""
            suffix = f" (工具: {tool_list})" if tool_list else ""
            lines.append(f"- **{name}**: {description}{suffix}")
        lines.append("\n使用 `select_toolkit(toolkits=['工具集名'])` 激活更多工具集；下一轮代码生效。")
        sections.append("\n".join(lines))
    if loaded_skills:
        lines = ["\n## 已加载技能（Skills）"]
        for name, content in loaded_skills.items():
            lines.append(f"\n### {name}\n{content}")
        sections.append("\n".join(lines))
    return base_prompt if not sections else base_prompt + "\n" + "\n".join(sections)
