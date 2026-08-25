"""Tool Execution & Sub-Agent Nodes：工具注册、code-mode 执行引擎与共享 Graph 节点。

提供：
1. _TOOL_REGISTRY：工具注册表（code-mode proxy 从中取 handler）
2. code_executor_node / observer_node / judge_node：可复用 LangGraph 节点
   （被 app.agents.build_sub_agent 作为共享节点引用）
3. run_react_loop：多智能体 Dispatcher 主入口
"""

import asyncio
import ast
import atexit
import concurrent.futures
import json
import logging
import threading
import time
from typing import Any, Callable, Optional

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.planner_factory import create_llm  # noqa: F401 - re-export for unit tests
from app.agents.observer import observe  # noqa: F401
from app.agents.judge import judge  # noqa: F401
from app.agents.errors import ErrorCode
from app.config import settings
from app.agents.context import _ToolContext
from app.models.schemas import (
    PlannerOutput,
    ToolResult,
)
from app.tools.poi_query import POIQuery
from app.tools.geo_code import GeoCoder
from app.tools.spatial_analysis import SpatialAnalyzer
from app.tools.data_io import DataIO
from app.tools.map_layer import MapLayerBuilder

logger = logging.getLogger(__name__)


# ============================================================
# 同步接口中运行异步协程
# ============================================================

_RUN_ASYNC_EXECUTOR: Optional[concurrent.futures.ThreadPoolExecutor] = None
_RUN_ASYNC_EXECUTOR_LOCK = threading.Lock()

# 每个线程持有一个持久化事件循环，用 run_until_complete 而非 asyncio.run，
# 避免反复创建/销毁循环导致异步资源（Redis 连接池等）绑定到已关闭的循环，
# 抛出 "Event loop is closed"。
_thread_local = threading.local()


def _get_thread_loop() -> asyncio.AbstractEventLoop:
    """获取或创建当前线程的持久化事件循环（线程生命周期内复用）。"""
    loop = getattr(_thread_local, 'loop', None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        _thread_local.loop = loop
    return loop


def _get_run_async_executor() -> concurrent.futures.ThreadPoolExecutor:
    """获取模块级共享 ThreadPoolExecutor，避免每次调用都新建。"""
    global _RUN_ASYNC_EXECUTOR
    if _RUN_ASYNC_EXECUTOR is None:
        with _RUN_ASYNC_EXECUTOR_LOCK:
            if _RUN_ASYNC_EXECUTOR is None:
                _RUN_ASYNC_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                    max_workers=4, thread_name_prefix="loop_async_"
                )
    return _RUN_ASYNC_EXECUTOR


def _shutdown_run_async_executor() -> None:
    global _RUN_ASYNC_EXECUTOR
    if _RUN_ASYNC_EXECUTOR is not None:
        _RUN_ASYNC_EXECUTOR.shutdown(wait=False)
        _RUN_ASYNC_EXECUTOR = None


atexit.register(_shutdown_run_async_executor)


def _run_async(coro):
    """在同步上下文中安全地 run 异步协程。

    使用线程本地持久化事件循环 + run_until_complete（而非 asyncio.run），
    确保循环不会被反复创建/销毁，从而避免异步资源连接池污染。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 无运行中的事件循环（如 asyncio.to_thread 的线程池内）
        loop = _get_thread_loop()
        return loop.run_until_complete(coro)

    # 已在事件循环中（兼容路径）— 独立线程
    import threading as _threading

    result_container: list = []
    error_container: list = []

    def _runner():
        try:
            loop = _get_thread_loop()
            result_container.append(loop.run_until_complete(coro))
        except Exception as exc:
            error_container.append(exc)

    t = _threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join()
    if error_container:
        raise error_container[0]
    return result_container[0]


# ============================================================
# POIQuery 实例构造
# ============================================================

def _get_poi_query_instance() -> POIQuery:
    """构造 POIQuery 实例（用 settings 配置）。

    每次调用新建实例，避免跨请求共享可变工具状态。
    """
    return POIQuery(
        amap_key=settings.AMAP_KEY,
        amap_timeout=settings.AMAP_TIMEOUT,
        osm_timeout=settings.OSM_TIMEOUT,
        osm_endpoint=settings.OSM_ENDPOINT,
        osm_backup_endpoints=settings.OSM_BACKUP_ENDPOINTS,
    )




def _resolve_ref(ctx: _ToolContext, field: str) -> Any:
    """Resolve a legacy numeric reference or accept a direct code-mode value.

    JSON tool mode historically used integer indexes.  Code mode executes real
    Python and therefore passes the value held by a variable.  Supporting both
    forms keeps old checkpoints readable without forcing the model to invent an
    index that has no meaning inside the current sandbox execution.
    """
    ref = ctx.params.get(field)
    if isinstance(ref, int) and ref in ctx.results_data:
        return ctx.results_data[ref]
    if ref is not None and not isinstance(ref, int):
        return ref
    return None


def _haversine_m(a: tuple, b: tuple) -> float:
    """两点 Haversine 距离（米）。委托给 tools.geo_transform.haversine_m。"""
    from app.tools.geo_transform import haversine_m
    return haversine_m(a, b)


def _find_anchor_geo_code(ctx: _ToolContext) -> tuple[int, tuple] | None:
    """在 ctx.results_data 里找最近的 geo_code 工具结果（用于一致性校验）。

    反向遍历 ctx.results_data，找到最近一次 tool_name == "geo_code" 且
    status == "success" 的工具结果，提取 location 字段。

    注意：只查 ctx（单次 tools 节点内），不跨 iteration 反查。
    ctx.results_data 是 dict[int, dict] 形态的扁平 dict，
    键是工具调用 i 的位置索引；没有显式的 tool_name 字段，
    所以这里用 location 字段的存在性作为代理（仅 geo_code 在 success 时
    把 location 放在 results_data 顶层）。

    Returns:
        (index, (lng, lat)) 或 None。
    """
    if not ctx.results_data:
        return None
    # 反向遍历：取最近一次的 geo_code 结果
    for idx in sorted(ctx.results_data.keys(), reverse=True):
        ref = ctx.results_data.get(idx)
        if not isinstance(ref, dict):
            continue
        loc = ref.get("location")
        if (
            ref.get("status") == "success"
            and isinstance(loc, (list, tuple))
            and len(loc) == 2
        ):
            try:
                return (idx, (float(loc[0]), float(loc[1])))
            except (TypeError, ValueError):
                continue
    return None


# 坐标系漂移阈值（米）。偏差超过此值视为 LLM 填错坐标 / 引用错前序结果。
LOCATION_DRIFT_THRESHOLD_M = 100.0


def _check_location_drift(
    proposed: tuple, anchor: tuple
) -> tuple[bool, float]:
    """比较 proposed 与 anchor 坐标的偏差（米），返回 (over_threshold, drift_m)。"""
    if not isinstance(proposed, (tuple, list)) or len(proposed) != 2:
        return (False, 0.0)
    if not isinstance(anchor, (tuple, list)) or len(anchor) != 2:
        return (False, 0.0)
    try:
        p = (float(proposed[0]), float(proposed[1]))
        a = (float(anchor[0]), float(anchor[1]))
    except (TypeError, ValueError):
        return (False, 0.0)
    drift = _haversine_m(p, a)
    return (drift > LOCATION_DRIFT_THRESHOLD_M, drift)


def _get_location(ctx: _ToolContext) -> Any:
    """从 params 解析 location：直接值或从前序结果取。"""
    loc = ctx.params.get("location")
    if isinstance(loc, (tuple, list)):
        return tuple(loc)
    if isinstance(loc, str):
        return loc
    ref = _resolve_ref(ctx, "location_from")
    if ref and "location" in ref:
        return tuple(ref["location"])
    return None


def _dict_to_gdf(d: dict | list):
    """把工具结果转成 GeoDataFrame。支持 FeatureCollection / Feature/POI 列表。"""
    import geopandas as gpd
    if not d:
        return None
    if isinstance(d, list):
        first = d[0] if d and isinstance(d[0], dict) else {}
        if first.get("type") == "Feature" or isinstance(first.get("geometry"), dict):
            d = {"type": "FeatureCollection", "features": d}
        elif isinstance(first.get("location"), (list, tuple)):
            d = {"pois": d}
        else:
            return None
    if not isinstance(d, dict):
        return None
    if isinstance(d.get("location"), (list, tuple)):
        return _dict_to_gdf([d])
    # Root Dispatcher injects dependency artifacts as a namespaced wrapper
    # (dep_<task_id>) and also exposes their stable generic ``result`` alias.
    # Accept both wrapper shapes so a model choosing either catalog index gets
    # the same geometry payload.
    for key in ("data", "result", "geojson"):
        nested = d.get(key)
        if isinstance(nested, (dict, list)) and nested is not d:
            resolved = _dict_to_gdf(nested)
            if resolved is not None:
                return resolved
    # GeoJSON FeatureCollection
    if d.get("type") == "FeatureCollection" or "features" in d:
        try:
            # Construct directly from features. Serialising through GDAL first
            # rejects otherwise valid in-memory values such as NaN properties
            # produced by spatial joins/buffers.
            crs: str | None = "EPSG:4326"
            crs_name = (
                (d.get("crs") or {}).get("properties", {}).get("name")
                if isinstance(d.get("crs"), dict)
                else None
            )
            if crs_name:
                import re
                epsg_match = re.search(r"EPSG(?::|::)(\d+)$", str(crs_name), re.IGNORECASE)
                crs = f"EPSG:{epsg_match.group(1)}" if epsg_match else str(crs_name)
            gdf = gpd.GeoDataFrame.from_features(d.get("features") or [], crs=crs)
            # 恢复 _gdf_to_dict 嵌入的坐标系标签
            crs_label = d.get("_crs_label")
            if crs_label:
                gdf.attrs["crs_label"] = crs_label
            return gdf
        except Exception:
            return None
    # POI 列表 {"features": [{"location": [lng, lat], ...}]}
    feats = d.get("features") or d.get("pois") or []
    if feats and isinstance(feats, list) and "location" in (feats[0] if feats else {}):
        from shapely.geometry import Point
        geoms = [Point(f["location"]) for f in feats if f.get("location")]
        if geoms:
            gdf = gpd.GeoDataFrame(feats[:len(geoms)], geometry=geoms, crs="EPSG:4326")
            # 标记实际坐标系：高德/OSM_CN 返回 GCJ02，OSM_Global 返回 WGS84。
            # _ensure_wgs84 依赖此标签决定是否做 GCJ02→WGS84 偏转。
            crs_label = "GCJ02"
            for f in feats:
                fcrs = f.get("crs", "")
                if fcrs == "WGS84":
                    crs_label = "WGS84"
                    break
            gdf.attrs["crs_label"] = crs_label
            return gdf
    return None


def _gdf_to_dict(gdf) -> dict:
    """把 GeoDataFrame 转成 GeoJSON dict。

    crs_label 会嵌入 GeoJSON 的顶层，供 _dict_to_gdf 往返时恢复坐标系标签。
    """
    if gdf is None:
        return None
    try:
        import json
        result = json.loads(gdf.to_json())
        crs_label = gdf.attrs.get("crs_label")
        if crs_label:
            result["_crs_label"] = crs_label
        return result
    except Exception:
        return None


def _analyzer_result_to_tool_result(
    ctx: _ToolContext,
    tool_name: str,
    result: Any,
) -> ToolResult:
    """Normalize extended SpatialAnalyzer ``{status, data}`` contracts.

    Legacy analyzers returned a GeoDataFrame directly while extended methods
    return a status envelope.  Native handlers must not serialize the envelope
    itself as a GeoDataFrame, otherwise they emit ``success`` with ``data=None``.
    """
    if isinstance(result, dict) and "status" in result:
        status = str(result.get("status") or "error")
        payload = result.get("data")
        if payload is not None and not isinstance(payload, (dict, list)):
            payload = _gdf_to_dict(payload)
        return ToolResult(
            tool_call_id=ctx.tool_call_id,
            tool_name=tool_name,
            status=status,
            data=payload,
            message=result.get("message"),
            source="computed" if status == "success" else None,
        )
    return ToolResult(
        tool_call_id=ctx.tool_call_id,
        tool_name=tool_name,
        status="success",
        data=_gdf_to_dict(result),
        source="computed",
    )


def _gdfs_from_ref(value: Any) -> list[Any]:
    """Resolve one or many dependency artifacts into GeoDataFrames."""
    if isinstance(value, (list, tuple)):
        resolved = []
        for item in value:
            gdf = _dict_to_gdf(item)
            if gdf is not None:
                resolved.append(gdf)
        return resolved
    if isinstance(value, dict):
        for key in ("layers", "results", "dependencies"):
            nested = value.get(key)
            if isinstance(nested, (list, tuple)):
                resolved = _gdfs_from_ref(nested)
                if resolved:
                    return resolved
    gdf = _dict_to_gdf(value)
    return [gdf] if gdf is not None else []


def _ordered_dependency_geometries(ctx: _ToolContext) -> list[Any]:
    """Return parseable dependency payloads in the dispatcher DAG order."""
    values: list[Any] = []
    for value in ctx.results_data.values():
        if _dict_to_gdf(value) is not None:
            values.append(value)
    return values


def _resolve_two_geometry_inputs(
    ctx: _ToolContext,
    first: Any,
    second: Any,
) -> tuple[Any, Any]:
    """Use the two declared DAG dependencies when both are available.

    Numeric references are model-generated implementation details. For an
    atomic two-input task the dependency order is already validated by the
    root DAG, so it is the stable authority when the model repeats or swaps an
    index.
    """
    dependencies = _ordered_dependency_geometries(ctx)
    if len(dependencies) >= 2:
        return dependencies[0], dependencies[1]
    return first, second


async def _read_upload_from_redis(file_id: str) -> tuple[bytes, str] | None:
    """Resolve upload metadata and return ``(bytes, original_filename)``.

    New records keep payloads under the local workspace.  ``content_b64`` is
    still accepted so uploads created before the migration remain readable
    until their Redis TTL expires.
    """
    import base64
    from pathlib import Path
    from app.utils.redis import get_redis, make_key

    r = get_redis()
    raw = await r.get(make_key("upload", file_id))
    if raw:
        try:
            payload = json.loads(raw)
            filename = str(payload.get("filename") or f"{file_id}.geojson")
            storage_path = payload.get("storage_path")
            if storage_path:
                upload_root = (Path(settings.APP_WORKSPACE_DIR).resolve() / "uploads")
                resolved = Path(str(storage_path)).resolve()
                if not resolved.is_relative_to(upload_root):
                    logger.warning("upload path escaped workspace file_id=%s path=%s", file_id, resolved)
                    return None
                content = await asyncio.to_thread(resolved.read_bytes)
                return content, filename
            if payload.get("content_b64"):
                return base64.b64decode(payload["content_b64"]), filename
        except (json.JSONDecodeError, KeyError, OSError, ValueError, TypeError):
            logger.warning("upload record unreadable file_id=%s", file_id, exc_info=True)
            return None
    return None


_TOOL_REGISTRY: dict[str, Callable[[_ToolContext], ToolResult]] = {}


def _register_tool(name: str) -> Callable[[Callable[[_ToolContext], ToolResult]], Callable[[_ToolContext], ToolResult]]:
    """工具注册装饰器。"""
    def decorator(fn: Callable[[_ToolContext], ToolResult]) -> Callable[[_ToolContext], ToolResult]:
        _TOOL_REGISTRY[name] = fn
        return fn
    return decorator


@_register_tool("geo_code")
def _handle_geo_code(ctx: _ToolContext) -> ToolResult:
    """调用 GeoCoder.geocode，把完整 dict 透传给 ToolResult.data。

    完整 dict 含 location / formatted_address / source / candidates /
    confidence / disambiguated / principal_rank / cached 等增强字段
    （见 app.tools.geo_code.GeoCoder.geocode 文档），这些都会进入
    ctx.results_data[i]，并在 code mode 作为命名 Python 值返回；下游直接
    使用 result["location"]，数字引用仅保留给旧 JSON 调用兼容。
    """
    # 兼容 LLM 可能使用的不同参数名：address / location / query / name / place_name
    address = (
        ctx.params.get("address")
        or ctx.params.get("location")
        or ctx.params.get("query")
        or ctx.params.get("name")
        or ctx.params.get("place_name")
        or ctx.params.get("place")
        or ctx.params.get("keyword")
        or ctx.params.get("text")
        or ctx.params.get("input")
        or ctx.params.get("q")
        or ""
    )
    # location 字段可能是坐标元组（reverse 路径），此时不走 geocode 正向编码
    if isinstance(address, (list, tuple)) and len(address) == 2:
        # 坐标元组：走 reverse 路径或当作 location 参数传入 geocode
        address = ""
    if not isinstance(address, str):
        address = str(address) if address else ""
    raw = _run_async(ctx.geo_coder.geocode(address))
    status = raw.get("status", "empty")
    return ToolResult(
        tool_call_id=ctx.tool_call_id,
        tool_name="geo_code",
        status=status,
        data=raw,
        message=raw.get("message"),
        error_code=(
            raw.get("error_code")
            or (ErrorCode.GEOCODE_FAILED.value if status == "error" else None)
        ),
        source=raw.get("source", "Amap"),
    )


@_register_tool("query_poi")
def _handle_query_poi(ctx: _ToolContext) -> ToolResult:
    """query_poi 工具处理器。

    location 解析策略（按优先级）：
    1. params.location_from → 旧 JSON 模式兼容引用
    2. params.location 是 tuple/list → code mode 的直接值，并触发可信原点的
       漂移校验：偏差 > 100m 视为 LLM 填错坐标 / 引用错前序结果，返回
       status=error, error_code=LOCATION_DRIFT，要求复用可信 location 变量
    3. params.location 是字符串 → 调 geo_coder.geocode 解析；
       若返回的 disambiguated=True，把 candidates / confidence 一并写入
       ToolResult.data（同时仍保留原 POI 数据结构），让 Observer 看到反问信号
    """
    tool_name = "query_poi"

    # 0. 优先 location_from (point 1)
    ref_from = ctx.params.get("location_from")
    location: Any = None
    referenced_area_radius_m: float | None = None
    if isinstance(ref_from, int) and ref_from in ctx.results_data:
        anchor = ctx.results_data[ref_from]
        loc_val = anchor.get("location") if isinstance(anchor, dict) else None
        if isinstance(loc_val, (list, tuple)) and len(loc_val) == 2:
            location = (float(loc_val[0]), float(loc_val[1]))
        else:
            # Uploaded polygon/line layers are valid search extents. Use a
            # representative point and expand the provider radius to cover the
            # layer bounds rather than forcing the model to invent coordinates.
            anchor_gdf = _dict_to_gdf(anchor)
            if anchor_gdf is not None and not anchor_gdf.empty:
                try:
                    merged = anchor_gdf.geometry.unary_union
                    point = merged.representative_point()
                    location = (float(point.x), float(point.y))
                    minx, miny, maxx, maxy = anchor_gdf.total_bounds
                    referenced_area_radius_m = max(
                        _haversine_m(location, (float(minx), float(miny))),
                        _haversine_m(location, (float(maxx), float(maxy))),
                    )
                except (TypeError, ValueError, AttributeError):
                    location = None

    # 1. location 直接是 tuple/list (point 2)
    if location is None:
        loc_param = ctx.params.get("location")
        if isinstance(loc_param, (tuple, list)) and len(loc_param) == 2:
            try:
                proposed = (float(loc_param[0]), float(loc_param[1]))
            except (TypeError, ValueError):
                proposed = None
            if proposed is not None:
                anchor_pair = _find_anchor_geo_code(ctx)
                if anchor_pair is not None:
                    anchor_idx, anchor_loc = anchor_pair
                    over, drift_m = _check_location_drift(proposed, anchor_loc)
                    if over:
                        return ToolResult(
                            tool_call_id=ctx.tool_call_id,
                            tool_name=tool_name,
                            status="error",
                            message=(
                                f"你填的坐标与可信 geo_code 原点（记录 {anchor_idx}）"
                                f"偏差 {drift_m:.0f}m，超过 100m。"
                                "请直接复用 geo_code 返回的 location 变量。"
                            ),
                            error_code=ErrorCode.LOCATION_DRIFT.value,
                        )
                    # 偏差 ≤ 100m：通过。drift 信息留待 observer 后续接入（无 trace 概念时）
                    # 见 docstring 说明：当前 ToolResult 没有 trace_extra 字段，
                    # 因此不强行放入 data（避免污染 LLM 视图），但记录日志。
                    logger.info(
                        "query_poi drift within threshold: proposed=%s anchor_idx=%d drift_m=%.1f",
                        proposed, anchor_idx, drift_m,
                    )
                # 没找到 anchor（首次无前序）→ 通过，使用 proposed
                location = proposed

    # 2. location 是字符串 (point 3)
    poi_data_extra: dict = {}
    if location is None:
        loc_param = ctx.params.get("location")
        if isinstance(loc_param, str) and loc_param.strip():
            geo_raw = _run_async(ctx.geo_coder.geocode(loc_param))
            if geo_raw.get("status") == "success":
                location = tuple(geo_raw["location"])
                # 消歧反问信号：把 candidates / confidence 暴露给 Observer
                if geo_raw.get("disambiguated"):
                    poi_data_extra = {
                        "disambiguated": True,
                        "confidence": geo_raw.get("confidence"),
                        "candidates": geo_raw.get("candidates"),
                        "principal_rank": geo_raw.get("principal_rank"),
                    }
            else:
                return ToolResult(
                    tool_call_id=ctx.tool_call_id,
                    tool_name=tool_name,
                    status="error",
                    message=f"无法解析地点：{loc_param}",
                    error_code=ErrorCode.GEOCODE_FAILED.value,
                )

    if location is None:
        return ToolResult(
            tool_call_id=ctx.tool_call_id,
            tool_name=tool_name,
            status="error",
            message="query_poi 需要 location 参数",
            error_code=ErrorCode.MISSING_LOCATION.value,
        )

    radius = float(ctx.params.get("radius", 500))
    if referenced_area_radius_m is not None:
        radius = max(radius, referenced_area_radius_m)
    query = ctx.params.get("query", "")
    dedup_threshold_m = ctx.params.get("dedup_threshold_m")
    within_source_dedup = ctx.params.get("within_source_dedup")
    raw = ctx.poi.search_poi_tool(
        query,
        location,
        radius,
        dedup_threshold_m=dedup_threshold_m if isinstance(dedup_threshold_m, (int, float)) else None,
        within_source_dedup=within_source_dedup if isinstance(within_source_dedup, bool) else None,
    )

    # 把 disambiguated 元数据合并进 data（如有），保留原 POI 数据。
    merged_data: Optional[dict] = None
    base_data = raw.get("data")
    # ``POIQuery.search_poi_tool`` may return its provider payload as a JSON
    # string.  Native DAG artifacts must stay structured: otherwise a later
    # map_layer_build receives text at ``data_from`` and incorrectly reports
    # that there are no renderable features.
    if isinstance(base_data, str):
        try:
            parsed_data = json.loads(base_data)
        except json.JSONDecodeError:
            parsed_data = None
        if isinstance(parsed_data, (dict, list)):
            base_data = parsed_data
    if isinstance(base_data, dict):
        merged_data = dict(base_data)
    elif base_data is not None:
        merged_data = {"data": base_data}
    else:
        merged_data = {}
    if poi_data_extra:
        merged_data.update(poi_data_extra)

    return ToolResult(
        tool_call_id=ctx.tool_call_id,
        tool_name=tool_name,
        status=raw.get("status", "empty"),
        data=merged_data if merged_data else raw.get("data"),
        message=raw.get("message"),
        source=raw.get("source"),
    )


@_register_tool("buffer")
def _handle_buffer(ctx: _ToolContext) -> ToolResult:
    radius_m = ctx.params.get("radius_m") or ctx.params.get("radius", 500)
    geom_ref = _resolve_ref(ctx, "geometry_from") or _resolve_ref(ctx, "points_from")
    if not geom_ref:
        return ToolResult(
            tool_call_id=ctx.tool_call_id,
            tool_name="buffer",
            status="empty",
            message="buffer 需要 geometry_from 参数",
        )
    gdf = _dict_to_gdf(geom_ref)
    if gdf is None:
        return ToolResult(
            tool_call_id=ctx.tool_call_id,
            tool_name="buffer",
            status="empty",
            message="无法解析输入几何",
        )
    result_gdf = ctx.analyzer.buffer(gdf, float(radius_m))
    return ToolResult(
        tool_call_id=ctx.tool_call_id,
        tool_name="buffer",
        status="success",
        data=_gdf_to_dict(result_gdf),
        source="computed",
    )


@_register_tool("overlay")
def _handle_overlay(ctx: _ToolContext) -> ToolResult:
    a_ref = _resolve_ref(ctx, "geometry_a_from")
    b_ref = _resolve_ref(ctx, "geometry_b_from")
    a_ref, b_ref = _resolve_two_geometry_inputs(ctx, a_ref, b_ref)
    how = ctx.params.get("how", "intersection")
    if not a_ref or not b_ref:
        return ToolResult(
            tool_call_id=ctx.tool_call_id,
            tool_name="overlay",
            status="empty",
            message="overlay 需要 geometry_a_from 和 geometry_b_from",
        )
    gdf_a = _dict_to_gdf(a_ref)
    gdf_b = _dict_to_gdf(b_ref)
    if gdf_a is None or gdf_b is None:
        return ToolResult(
            tool_call_id=ctx.tool_call_id,
            tool_name="overlay",
            status="empty",
            message="无法解析输入几何",
        )
    result_gdf = ctx.analyzer.overlay(gdf_a, gdf_b, how=how)
    return ToolResult(
        tool_call_id=ctx.tool_call_id,
        tool_name="overlay",
        status="success",
        data=_gdf_to_dict(result_gdf),
        source="computed",
    )


@_register_tool("voronoi")
def _handle_voronoi(ctx: _ToolContext) -> ToolResult:
    points_ref = _resolve_ref(ctx, "points_from")
    if not points_ref:
        return ToolResult(
            tool_call_id=ctx.tool_call_id,
            tool_name="voronoi",
            status="empty",
            message="voronoi 需要 points_from",
        )
    dependency_points = [
        value for value in ctx.results_data.values()
        if isinstance(value, dict) and isinstance(value.get("location"), (list, tuple))
    ]
    gdf = _dict_to_gdf(dependency_points if len(dependency_points) >= 2 else points_ref)
    if gdf is None:
        return ToolResult(
            tool_call_id=ctx.tool_call_id,
            tool_name="voronoi",
            status="empty",
            message="无法解析输入几何",
        )
    result = ctx.analyzer.voronoi(gdf)
    return _analyzer_result_to_tool_result(ctx, "voronoi", result)


@_register_tool("isochrone")
def _handle_isochrone(ctx: _ToolContext) -> ToolResult:
    """isochrone 工具处理器。

    location 解析策略与 _handle_query_poi 一致：
    1. location_from 优先（直接取前序 geo_code 结果的 location）
    2. tuple/list 时与最近 geo_code 锚点做漂移校验（>100m → LOCATION_DRIFT）
    3. 字符串时不再走地理编码（isochrone 不支持地名解析，保留原 error 行为）
    """
    tool_name = "isochrone"

    # 1. 优先 location_from
    ref_from = ctx.params.get("location_from")
    origin: Any = None
    if isinstance(ref_from, int) and ref_from in ctx.results_data:
        anchor = ctx.results_data[ref_from]
        loc_val = anchor.get("location") if isinstance(anchor, dict) else None
        if isinstance(loc_val, (list, tuple)) and len(loc_val) == 2:
            try:
                origin = (float(loc_val[0]), float(loc_val[1]))
            except (TypeError, ValueError):
                origin = None

    # 2. tuple/list 漂移校验
    if origin is None:
        loc_param = ctx.params.get("location")
        if isinstance(loc_param, (tuple, list)) and len(loc_param) == 2:
            try:
                proposed = (float(loc_param[0]), float(loc_param[1]))
            except (TypeError, ValueError):
                proposed = None
            if proposed is not None:
                anchor_pair = _find_anchor_geo_code(ctx)
                if anchor_pair is not None:
                    anchor_idx, anchor_loc = anchor_pair
                    over, drift_m = _check_location_drift(proposed, anchor_loc)
                    if over:
                        return ToolResult(
                            tool_call_id=ctx.tool_call_id,
                            tool_name=tool_name,
                            status="error",
                            message=(
                                f"你填的坐标与前序 geo_code（索引 {anchor_idx}）"
                                f"偏差 {drift_m:.0f}m，超过 100m。"
                                f"请改用 location_from={anchor_idx} 引用前序结果。"
                            ),
                            error_code=ErrorCode.LOCATION_DRIFT.value,
                        )
                    logger.info(
                        "isochrone drift within threshold: proposed=%s anchor_idx=%d drift_m=%.1f",
                        proposed, anchor_idx, drift_m,
                    )
                origin = proposed

    # 3. 字符串不再解析（isochrone 不支持地理编码输入）
    if origin is None:
        loc_param = ctx.params.get("location")
        if isinstance(loc_param, str):
            return ToolResult(
                tool_call_id=ctx.tool_call_id,
                tool_name=tool_name,
                status="error",
                message="isochrone 需要坐标元组，请先用 geo_code 或 location_from",
                error_code=ErrorCode.MISSING_LOCATION.value,
            )
        return ToolResult(
            tool_call_id=ctx.tool_call_id,
            tool_name=tool_name,
            status="empty",
            message="isochrone 需要 location",
        )

    mode = ctx.params.get("mode", "walking")
    time_min = ctx.params.get("time_min", 15)
    result = ctx.analyzer.isochrone(origin, mode, int(time_min))
    if isinstance(result, dict) and result.get("status") == "empty":
        # Stable single-machine fallback: expose the nominal distance envelope
        # when route sampling/provider calls are unavailable. Mark it clearly
        # as approximate so consumers never confuse it with a network isochrone.
        import geopandas as gpd
        from shapely.geometry import Point

        speed_m_per_min = {"walking": 80.0, "driving": 1000.0, "cycling": 250.0}.get(mode, 80.0)
        origin_gdf = gpd.GeoDataFrame(
            {"geometry": [Point(origin)]},
            crs="EPSG:4326",
        )
        origin_gdf.attrs["crs_label"] = "GCJ02"
        fallback_gdf = ctx.analyzer.buffer(origin_gdf, speed_m_per_min * int(time_min))
        fallback_data = _gdf_to_dict(fallback_gdf) or {}
        fallback_data["_approximate"] = True
        fallback_data["_approximation_reason"] = result.get("message", "route sampling unavailable")
        return ToolResult(
            tool_call_id=ctx.tool_call_id,
            tool_name=tool_name,
            status="success",
            data=fallback_data,
            message="路径服务不可用，已生成名义速度近似范围",
            source="computed_approximation",
        )
    if isinstance(result, dict) and result.get("status") == "success":
        payload = result.get("data")
        geometry = payload.get("geometry") if isinstance(payload, dict) else None
        if geometry is not None and hasattr(geometry, "__geo_interface__"):
            import geopandas as gpd

            properties = {key: value for key, value in payload.items() if key != "geometry"}
            output_gdf = gpd.GeoDataFrame([properties], geometry=[geometry], crs="EPSG:4326")
            output_gdf.attrs["crs_label"] = "GCJ02"
            return ToolResult(
                tool_call_id=ctx.tool_call_id,
                tool_name=tool_name,
                status="success",
                data=_gdf_to_dict(output_gdf),
                source="computed",
            )
    return _analyzer_result_to_tool_result(ctx, tool_name, result)


@_register_tool("data_io_read")
def _handle_data_io_read(ctx: _ToolContext) -> ToolResult:
    file_id = ctx.params.get("file_id")
    if not file_id:
        return ToolResult(
            tool_call_id=ctx.tool_call_id,
            tool_name="data_io_read",
            status="empty",
            message="data_io_read 需要 file_id",
        )
    upload_record = _run_async(_read_upload_from_redis(file_id))
    if not upload_record:
        return ToolResult(
            tool_call_id=ctx.tool_call_id,
            tool_name="data_io_read",
            status="empty",
            message="上传文件已过期或不存在",
        )
    try:
        raw_bytes, original_filename = upload_record
        if str(original_filename).lower().endswith((".tif", ".tiff")):
            from pathlib import Path

            safe_id = "".join(
                ch for ch in str(file_id) if ch.isalnum() or ch in ("_", "-")
            )
            if not safe_id or safe_id != str(file_id):
                raise ValueError("invalid raster upload file_id")
            upload_root = Path(settings.APP_WORKSPACE_DIR).resolve() / "uploads"
            raster_dir = upload_root / safe_id
            suffix = Path(str(original_filename)).suffix.lower()
            raster_path = (raster_dir / f"runtime{suffix}").resolve()
            if not raster_path.is_relative_to(upload_root):
                raise ValueError("raster path escaped workspace")
            raster_dir.mkdir(parents=True, exist_ok=True)
            if not raster_path.exists() or raster_path.stat().st_size != len(raw_bytes):
                raster_path.write_bytes(raw_bytes)
            raster_result = ctx.data_io.load_raster(str(raster_path))
            if raster_result.get("status") != "success":
                return ToolResult(
                    tool_call_id=ctx.tool_call_id,
                    tool_name="data_io_read",
                    status="error",
                    error_code=ErrorCode.DATA_PARSE_FAILED.value,
                    message=raster_result.get("message", "GeoTIFF 读取失败"),
                )
            return ToolResult(
                tool_call_id=ctx.tool_call_id,
                tool_name="data_io_read",
                status="success",
                data={
                    "status": "ok",
                    "data": {
                        "raster_path": str(raster_path),
                        "metadata": raster_result.get("data", {}).get("metadata", {}),
                    },
                    "feature_count": 0,
                    "geometry_type": "Raster",
                },
                source="Upload",
            )
        result = ctx.data_io.read_upload(raw_bytes, original_filename)
    except Exception as e:  # noqa: BLE001
        # 解压/解析抛错不再穿透到 loop — 改成友好 error 让 Observer/Judge 走对路径
        logger.exception("data_io_read parse failed: file_id=%s", file_id)
        return ToolResult(
            tool_call_id=ctx.tool_call_id,
            tool_name="data_io_read",
            status="error",
            error_code=ErrorCode.DATA_PARSE_FAILED.value,
            message=f"文件解析失败：{type(e).__name__}: {str(e)[:200]}",
        )
    raw_status = result.get("status")
    # data_io 内部用 "ok" / "error"，loop 用 "success" / "error" — 边界处归一化
    if raw_status == "ok":
        norm_status = "success"
    elif raw_status == "error":
        norm_status = "error"
    else:
        norm_status = raw_status or "success"
    safe_result = dict(result)
    parsed_data = safe_result.get("data")
    if parsed_data is not None and hasattr(parsed_data, "to_json"):
        safe_result["data"] = _gdf_to_dict(parsed_data)
    return ToolResult(
        tool_call_id=ctx.tool_call_id,
        tool_name="data_io_read",
        status=norm_status,
        data=safe_result,
        source="Upload",
    )


@_register_tool("map_layer_build")
def _handle_map_layer_build(ctx: _ToolContext) -> ToolResult:
    def raster_layer(value: Any) -> dict | None:
        """Unwrap a computed raster artifact for the frontend ImageOverlay."""
        if not isinstance(value, dict):
            return None
        if value.get("type") == "raster" and value.get("png_b64") and value.get("bbox"):
            return value
        for key in ("result", "data"):
            nested = value.get(key)
            if nested is not value:
                resolved = raster_layer(nested)
                if resolved is not None:
                    return resolved
        return None

    dependency_layers: list[dict] = []
    for value in ctx.results_data.values():
        raster = raster_layer(value)
        if raster is not None:
            dependency_layers.append(raster)
            continue
        dependency_gdf = _dict_to_gdf(value)
        if dependency_gdf is None or dependency_gdf.empty:
            continue
        dependency_geojson = _gdf_to_dict(dependency_gdf)
        if not dependency_geojson:
            continue
        layer = ctx.layer_builder.build_feature_collection(dependency_geojson)
        if layer.get("features"):
            dependency_layers.append(layer)
    # A visualisation task may have one or many DAG dependencies.  Returning
    # only when there are two made the normal geo→poi→viz chain fall through
    # to a raw POI dict, which MapLayerBuilder quite correctly does not treat
    # as GeoJSON.
    if dependency_layers:
        return ToolResult(
            tool_call_id=ctx.tool_call_id,
            tool_name="map_layer_build",
            status="success",
            data={"layers": dependency_layers},
            source="computed",
        )
    geom_ref = _resolve_ref(ctx, "geometry_from") or _resolve_ref(ctx, "data_from")
    if not geom_ref:
        return ToolResult(
            tool_call_id=ctx.tool_call_id,
            tool_name="map_layer_build",
            status="empty",
            message="map_layer_build 需要 geometry_from",
        )
    if isinstance(geom_ref, dict) and isinstance(geom_ref.get("data"), dict):
        nested = geom_ref["data"]
        if nested.get("type") == "FeatureCollection" or "features" in nested:
            geom_ref = nested
    layer = ctx.layer_builder.build_feature_collection(geom_ref)
    if not layer.get("features"):
        return ToolResult(
            tool_call_id=ctx.tool_call_id,
            tool_name="map_layer_build",
            status="empty",
            message="输入中没有可渲染的 GeoJSON Feature",
        )
    return ToolResult(
        tool_call_id=ctx.tool_call_id,
        tool_name="map_layer_build",
        status="success",
        data={"layers": [layer]},
        source="computed",
    )


@_register_tool("code_executor")
def _handle_code_executor(ctx: _ToolContext) -> ToolResult:
    from app.sandbox.tools import code_executor as _ce
    return _ce(ctx)


@_register_tool("geo_transform")
def _handle_geo_transform(ctx: _ToolContext) -> ToolResult:
    """Coordinates transform handler：WGS84 / GCJ02 / BD09 互转。

    Params:
        operation: "wgs84_to_gcj02" | "gcj02_to_wgs84" | "gcj02_to_bd09"
                   | "bd09_to_gcj02" | "wgs84_to_bd09" | "bd09_to_wgs84"
                   | "haversine" | "out_of_china" | "auto_detect_crs"
        lng: 经度（点转换操作必须）
        lat: 纬度（点转换操作必须）
        p1 / p2: (lng, lat) 元组（haversine 操作必须）
        bbox: (minLng, minLat, maxLng, maxLat) 元组（auto_detect_crs 操作可选）
    """
    from app.tools import geo_transform as gt

    operation = ctx.params.get("operation", "wgs84_to_gcj02")
    try:
        if operation == "haversine":
            p1 = ctx.params.get("p1")
            p2 = ctx.params.get("p2")
            if not p1 or not p2:
                return ToolResult(
                    tool_call_id=ctx.tool_call_id,
                    tool_name="geo_transform",
                    status="error",
                    message="haversine 需要 p1 和 p2 参数",
                    error_code=ErrorCode.INVALID_PARAMS.value,
                )
            distance = gt.haversine_m(tuple(p1), tuple(p2))
            return ToolResult(
                tool_call_id=ctx.tool_call_id,
                tool_name="geo_transform",
                status="success",
                data={"distance_m": round(distance, 2), "operation": "haversine", "p1": p1, "p2": p2},
                source="computed",
            )

        if operation == "out_of_china":
            lng = ctx.params.get("lng")
            lat = ctx.params.get("lat")
            if lng is None or lat is None:
                return ToolResult(
                    tool_call_id=ctx.tool_call_id,
                    tool_name="geo_transform",
                    status="error",
                    message="out_of_china 需要 lng 和 lat",
                    error_code=ErrorCode.INVALID_PARAMS.value,
                )
            result = gt.out_of_china(float(lng), float(lat))
            return ToolResult(
                tool_call_id=ctx.tool_call_id,
                tool_name="geo_transform",
                status="success",
                data={"out_of_china": result, "lng": lng, "lat": lat, "operation": "out_of_china"},
                source="computed",
            )

        if operation == "auto_detect_crs":
            bbox = ctx.params.get("bbox")
            file_path = ctx.params.get("file_path")
            result = gt.auto_detect_crs(bbox, file_path)
            return ToolResult(
                tool_call_id=ctx.tool_call_id,
                tool_name="geo_transform",
                status="success",
                data={"crs": result, "operation": "auto_detect_crs"},
                source="computed",
            )

        # 点对点坐标转换操作
        func_map = {
            "wgs84_to_gcj02": gt.wgs84_to_gcj02,
            "gcj02_to_wgs84": gt.gcj02_to_wgs84,
            "gcj02_to_bd09": gt.gcj02_to_bd09,
            "bd09_to_gcj02": gt.bd09_to_gcj02,
            "wgs84_to_bd09": gt.wgs84_to_bd09,
            "bd09_to_wgs84": gt.bd09_to_wgs84,
        }
        convert_fn = func_map.get(operation)
        if not convert_fn:
            return ToolResult(
                tool_call_id=ctx.tool_call_id,
                tool_name="geo_transform",
                status="error",
                message=(
                    f"不支持的操作：{operation!r}。"
                    f"可选：{', '.join(func_map)}"
                ),
                error_code=ErrorCode.INVALID_PARAMS.value,
            )

        lng = ctx.params.get("lng")
        lat = ctx.params.get("lat")
        if lng is None or lat is None:
            return ToolResult(
                tool_call_id=ctx.tool_call_id,
                tool_name="geo_transform",
                status="error",
                message=f"{operation} 需要 lng 和 lat 参数",
                error_code=ErrorCode.INVALID_PARAMS.value,
            )
        result_lng, result_lat = convert_fn(float(lng), float(lat))
        return ToolResult(
            tool_call_id=ctx.tool_call_id,
            tool_name="geo_transform",
            status="success",
            data={
                "operation": operation,
                "input": {"lng": lng, "lat": lat},
                "output": {"lng": round(result_lng, 6), "lat": round(result_lat, 6)},
            },
            source="computed",
        )

    except Exception as e:
        logger.exception("geo_transform failed: operation=%s", operation)
        return ToolResult(
            tool_call_id=ctx.tool_call_id,
            tool_name="geo_transform",
            status="error",
            message=f"坐标转换失败：{type(e).__name__}: {str(e)[:200]}",
            error_code=ErrorCode.TOOL_EXECUTION_FAILED.value,
        )


# ============================================================
# KERNEL 语义工具处理器
# ============================================================


@_register_tool("select_toolkit")
def _handle_select_toolkit(ctx: _ToolContext) -> ToolResult:
    """激活指定 ToolKit，扩展可见工具集。

    v1 实现：返回可用 toolkit 列表，side-effect（工具可见性变更）由
    ``_build_code_mode_tool_fns`` proxy 层在下一轮生效。

    Params:
        toolkits: list[str] — 要激活的 toolkit 名称列表
    """
    toolkits_param = ctx.params.get("toolkits", [])
    if not toolkits_param and isinstance(ctx.params.get("name"), str):
        toolkits_param = [ctx.params["name"]]
    if not isinstance(toolkits_param, list):
        return ToolResult(
            tool_call_id=ctx.tool_call_id,
            tool_name="select_toolkit",
            status="error",
            message="参数 toolkits 应为列表，如 [\"vector_analysis\"]",
        )

    from app.agents.toolkit.registry import ToolKitRegistry, ToolDisclosureController

    registry = ToolKitRegistry()
    controller = ToolDisclosureController(registry)
    result = controller.select_toolkits({"toolkits": toolkits_param})

    return ToolResult(
        tool_call_id=ctx.tool_call_id,
        tool_name="select_toolkit",
        status="success",
        data={
            "active_toolkits": result["active_toolkits"],
            "tools_added": result["tools_added"],
            "all_toolkits": list(registry.names()),
        },
    )


@_register_tool("inspect_workspace")
def _handle_inspect_workspace(ctx: _ToolContext) -> ToolResult:
    """显示工作区概览：图层、字段、可用工具。

    v1 实现：通过 ``ctx.results_data`` 提供 session_var key 预览；
    完整 workspace inspection 需集成 WorkspaceState（Task 4）。

    Params:
        query_type: str — 查询类型（"layers" | "fields" | "all"，默认 "all"）
    """
    query_type = ctx.params.get("query_type", "all")

    var_preview: dict[str, str] = {}
    if ctx.results_data:
        for idx, val in ctx.results_data.items():
            if isinstance(val, dict):
                var_preview[str(idx)] = (
                    f"dict({len(val)} keys)"
                )
            elif isinstance(val, (list, tuple)):
                var_preview[str(idx)] = f"{type(val).__name__}({len(val)} items)"
            else:
                var_preview[str(idx)] = str(type(val).__name__)

    return ToolResult(
        tool_call_id=ctx.tool_call_id,
        tool_name="inspect_workspace",
        status="success",
        data={
            "query_type": query_type,
            "session_var_count": len(ctx.results_data) if ctx.results_data else 0,
            "session_var_preview": var_preview,
            "note": "v1 — 完整 workspace 信息在集成 WorkspaceState 后可用",
        },
    )


@_register_tool("suggest_skill")
def _handle_suggest_skill(ctx: _ToolContext) -> ToolResult:
    """返回当前可用的 skill 列表。

    v1 占位：不上 LLM 做匹配，仅列出所有注册 skill。
    """
    from app.agents.skill.registry import SkillRegistry

    registry = SkillRegistry()
    names = registry.names()
    catalog = registry.to_catalog()

    return ToolResult(
        tool_call_id=ctx.tool_call_id,
        tool_name="suggest_skill",
        status="success",
        data={
            "skills": list(names),
            "catalog": catalog,
            "note": "使用 load_skill(name) 加载 skill 内容",
        },
    )


@_register_tool("load_skill")
def _handle_load_skill(ctx: _ToolContext) -> ToolResult:
    """加载指定 skill，返回其正文内容。

    v1 不自动注入到 prompt —— 由 ``code_executor_node`` 在下一轮迭代时
    把返回的数据写入 ``state["loaded_skills"][name]``。

    Params:
        name: str — skill 名称（如 "meter_buffer" / "spatial_join"）
    """
    name = ctx.params.get("name", "")
    if not isinstance(name, str) or not name.strip():
        return ToolResult(
            tool_call_id=ctx.tool_call_id,
            tool_name="load_skill",
            status="error",
            message="请指定 skill name，如 load_skill(name=\"meter_buffer\")",
        )

    from app.agents.skill.registry import SkillRegistry

    registry = SkillRegistry()
    meta = registry.get(name)
    if meta is None:
        return ToolResult(
            tool_call_id=ctx.tool_call_id,
            tool_name="load_skill",
            status="error",
            message=f"skill \"{name}\" 不存在。可用：{', '.join(registry.names())}",
        )

    content = registry.read_content(name)
    return ToolResult(
        tool_call_id=ctx.tool_call_id,
        tool_name="load_skill",
        status="success",
        data={
            "name": name,
            "description": meta.description,
            "requires_toolkits": list(meta.requires_toolkits),
            "risk_awareness": list(meta.risk_awareness),
            "content": content,
            "note": "skill 内容已返回，将在下一轮迭代时注入 prompt",
        },
    )


@_register_tool("proactive_clarification")
def _handle_proactive_clarification(ctx: _ToolContext) -> ToolResult:
    """v1 占位：向用户提出澄清问题。

    当前返回可选工具列表，不阻塞执行。
    """
    from app.agents.registry import TOOL_SPECS

    all_tools = sorted(
        (
            {"name": n, "description": s.description}
            for n, s in TOOL_SPECS.items()
            if not s.deprecated
        ),
        key=lambda item: item["name"],
    )
    return ToolResult(
        tool_call_id=ctx.tool_call_id,
        tool_name="proactive_clarification",
        status="success",
        data={
            "slots": [],
            "available_tools": all_tools[:15],
            "message": "v1 占位：当前不主动询问用户。如需澄清，请在代码中直接调用对应工具。",
        },
    )


# ============================================================
# Vector 分析 handlers
# ============================================================


@_register_tool("clip_layer")
def _handle_clip_layer(ctx: _ToolContext) -> ToolResult:
    gdf_ref = _resolve_ref(ctx, "input_ref") or _resolve_ref(ctx, "geometry_from")
    mask_ref = _resolve_ref(ctx, "overlay_ref") or _resolve_ref(ctx, "mask_from")
    gdf_ref, mask_ref = _resolve_two_geometry_inputs(ctx, gdf_ref, mask_ref)
    if not gdf_ref or not mask_ref:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="clip_layer", status="empty", message="需要 input_ref 和 overlay_ref")
    gdf = _dict_to_gdf(gdf_ref)
    mask_gdf = _dict_to_gdf(mask_ref)
    if gdf is None or mask_gdf is None:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="clip_layer", status="empty", message="无法解析输入几何")
    result = ctx.analyzer.clip(gdf, mask_gdf)
    return _analyzer_result_to_tool_result(ctx, "clip_layer", result)


@_register_tool("dissolve_layer")
def _handle_dissolve_layer(ctx: _ToolContext) -> ToolResult:
    geom_ref = _resolve_ref(ctx, "geometry_from") or _resolve_ref(ctx, "input_ref")
    if not geom_ref:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="dissolve_layer", status="empty", message="需要 geometry_from")
    gdf = _dict_to_gdf(geom_ref)
    if gdf is None:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="dissolve_layer", status="empty", message="无法解析输入几何")
    by = ctx.params.get("by")
    result = ctx.analyzer.dissolve(gdf, by=by)
    return _analyzer_result_to_tool_result(ctx, "dissolve_layer", result)


@_register_tool("merge_layers")
def _handle_merge_layers(ctx: _ToolContext) -> ToolResult:
    layers_ref = _resolve_ref(ctx, "layers_from") or _resolve_ref(ctx, "data_from")
    dependency_layers = _ordered_dependency_geometries(ctx)
    if len(dependency_layers) >= 2:
        layers_ref = dependency_layers
    if not layers_ref:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="merge_layers", status="empty", message="需要 layers_from 参数")
    gdfs = _gdfs_from_ref(layers_ref)
    if not gdfs:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="merge_layers", status="empty", message="无法解析输入图层")
    result = ctx.analyzer.merge_layers(gdfs)
    return _analyzer_result_to_tool_result(ctx, "merge_layers", result)


@_register_tool("join_by_location")
def _handle_join_by_location(ctx: _ToolContext) -> ToolResult:
    gdf_ref = _resolve_ref(ctx, "input_ref") or _resolve_ref(ctx, "geometry_from")
    other_ref = _resolve_ref(ctx, "other_ref") or _resolve_ref(ctx, "join_from")
    gdf_ref, other_ref = _resolve_two_geometry_inputs(ctx, gdf_ref, other_ref)
    predicate = ctx.params.get("predicate", "intersects")
    if not gdf_ref or not other_ref:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="join_by_location", status="empty", message="需要 input_ref 和 other_ref")
    gdf = _dict_to_gdf(gdf_ref)
    other = _dict_to_gdf(other_ref)
    if gdf is None or other is None:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="join_by_location", status="empty", message="无法解析输入几何")
    result = ctx.analyzer.join_by_location(gdf, other, predicate=predicate)
    return _analyzer_result_to_tool_result(ctx, "join_by_location", result)


@_register_tool("join_by_nearest")
def _handle_join_by_nearest(ctx: _ToolContext) -> ToolResult:
    gdf_ref = _resolve_ref(ctx, "input_ref") or _resolve_ref(ctx, "geometry_from")
    other_ref = _resolve_ref(ctx, "other_ref") or _resolve_ref(ctx, "join_from")
    gdf_ref, other_ref = _resolve_two_geometry_inputs(ctx, gdf_ref, other_ref)
    max_distance = ctx.params.get("max_distance")
    if not gdf_ref or not other_ref:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="join_by_nearest", status="empty", message="需要 input_ref 和 other_ref")
    gdf = _dict_to_gdf(gdf_ref)
    other = _dict_to_gdf(other_ref)
    if gdf is None or other is None:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="join_by_nearest", status="empty", message="无法解析输入几何")
    result = ctx.analyzer.join_by_nearest(gdf, other, max_distance=max_distance)
    return _analyzer_result_to_tool_result(ctx, "join_by_nearest", result)


@_register_tool("count_points_in_polygon")
def _handle_count_points_in_polygon(ctx: _ToolContext) -> ToolResult:
    poly_ref = _resolve_ref(ctx, "polygons_from") or _resolve_ref(ctx, "geometry_from")
    points_ref = _resolve_ref(ctx, "points_from")
    poly_ref, points_ref = _resolve_two_geometry_inputs(ctx, poly_ref, points_ref)
    if not poly_ref or not points_ref:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="count_points_in_polygon", status="empty", message="需要 polygons_from 和 points_from")
    polys = _dict_to_gdf(poly_ref)
    pts = _dict_to_gdf(points_ref)
    if polys is None or pts is None:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="count_points_in_polygon", status="empty", message="无法解析输入几何")
    result = ctx.analyzer.count_points_in_polygon(pts, polys)
    return _analyzer_result_to_tool_result(ctx, "count_points_in_polygon", result)


@_register_tool("extract_by_location")
def _handle_extract_by_location(ctx: _ToolContext) -> ToolResult:
    gdf_ref = _resolve_ref(ctx, "input_ref") or _resolve_ref(ctx, "geometry_from")
    mask_ref = _resolve_ref(ctx, "mask_ref") or _resolve_ref(ctx, "mask_from")
    gdf_ref, mask_ref = _resolve_two_geometry_inputs(ctx, gdf_ref, mask_ref)
    predicate = ctx.params.get("predicate", "intersects")
    if not gdf_ref or not mask_ref:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="extract_by_location", status="empty", message="需要 input_ref 和 mask_ref")
    gdf = _dict_to_gdf(gdf_ref)
    mask = _dict_to_gdf(mask_ref)
    if gdf is None or mask is None:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="extract_by_location", status="empty", message="无法解析输入几何")
    result = ctx.analyzer.extract_by_location(gdf, mask, predicate=predicate)
    return _analyzer_result_to_tool_result(ctx, "extract_by_location", result)


@_register_tool("convex_hull")
def _handle_convex_hull(ctx: _ToolContext) -> ToolResult:
    geom_ref = _resolve_ref(ctx, "geometry_from") or _resolve_ref(ctx, "input_ref")
    if not geom_ref:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="convex_hull", status="empty", message="需要 geometry_from")
    dependency_points = [
        value for value in ctx.results_data.values()
        if isinstance(value, dict) and isinstance(value.get("location"), (list, tuple))
    ]
    gdf = _dict_to_gdf(dependency_points if len(dependency_points) >= 3 else geom_ref)
    if gdf is None:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="convex_hull", status="empty", message="无法解析输入几何")
    result = ctx.analyzer.convex_hull(gdf)
    return _analyzer_result_to_tool_result(ctx, "convex_hull", result)


@_register_tool("bounding_boxes")
def _handle_bounding_boxes(ctx: _ToolContext) -> ToolResult:
    geom_ref = _resolve_ref(ctx, "geometry_from") or _resolve_ref(ctx, "input_ref")
    if not geom_ref:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="bounding_boxes", status="empty", message="需要 geometry_from")
    gdf = _dict_to_gdf(geom_ref)
    if gdf is None:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="bounding_boxes", status="empty", message="无法解析输入几何")
    result = ctx.analyzer.bounding_boxes(gdf)
    return _analyzer_result_to_tool_result(ctx, "bounding_boxes", result)


# ============================================================
# Vector 变换 handlers
# ============================================================


@_register_tool("centroid_layer")
def _handle_centroid_layer(ctx: _ToolContext) -> ToolResult:
    geom_ref = _resolve_ref(ctx, "geometry_from") or _resolve_ref(ctx, "input_ref")
    if not geom_ref:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="centroid_layer", status="empty", message="需要 geometry_from")
    gdf = _dict_to_gdf(geom_ref)
    if gdf is None:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="centroid_layer", status="empty", message="无法解析输入几何")
    result = ctx.analyzer.centroid_layer(gdf)
    return _analyzer_result_to_tool_result(ctx, "centroid_layer", result)


@_register_tool("point_on_surface")
def _handle_point_on_surface(ctx: _ToolContext) -> ToolResult:
    geom_ref = _resolve_ref(ctx, "geometry_from") or _resolve_ref(ctx, "input_ref")
    if not geom_ref:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="point_on_surface", status="empty", message="需要 geometry_from")
    gdf = _dict_to_gdf(geom_ref)
    if gdf is None:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="point_on_surface", status="empty", message="无法解析输入几何")
    result = ctx.analyzer.point_on_surface(gdf)
    return _analyzer_result_to_tool_result(ctx, "point_on_surface", result)


@_register_tool("simplify_geometry")
def _handle_simplify_geometry(ctx: _ToolContext) -> ToolResult:
    geom_ref = _resolve_ref(ctx, "geometry_from") or _resolve_ref(ctx, "input_ref")
    tolerance = ctx.params.get("tolerance", 1.0)
    if not geom_ref:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="simplify_geometry", status="empty", message="需要 geometry_from")
    gdf = _dict_to_gdf(geom_ref)
    if gdf is None:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="simplify_geometry", status="empty", message="无法解析输入几何")
    result = ctx.analyzer.simplify_geometry(gdf, tolerance=float(tolerance))
    return _analyzer_result_to_tool_result(ctx, "simplify_geometry", result)


@_register_tool("fix_geometries")
def _handle_fix_geometries(ctx: _ToolContext) -> ToolResult:
    geom_ref = _resolve_ref(ctx, "geometry_from") or _resolve_ref(ctx, "input_ref")
    if not geom_ref:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="fix_geometries", status="empty", message="需要 geometry_from")
    gdf = _dict_to_gdf(geom_ref)
    if gdf is None:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="fix_geometries", status="empty", message="无法解析输入几何")
    result = ctx.analyzer.fix_geometries(gdf)
    return _analyzer_result_to_tool_result(ctx, "fix_geometries", result)


@_register_tool("check_validity")
def _handle_check_validity(ctx: _ToolContext) -> ToolResult:
    geom_ref = _resolve_ref(ctx, "geometry_from") or _resolve_ref(ctx, "input_ref")
    if not geom_ref:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="check_validity", status="empty", message="需要 geometry_from")
    gdf = _dict_to_gdf(geom_ref)
    if gdf is None:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="check_validity", status="empty", message="无法解析输入几何")
    result = ctx.analyzer.check_validity(gdf)
    if isinstance(result, dict):
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="check_validity", status=result.get("status", "success"), data=result.get("data"), message=result.get("message"))
    return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="check_validity", status="success", data=result, source="computed")


@_register_tool("multipart_to_singlepart")
def _handle_multipart_to_singlepart(ctx: _ToolContext) -> ToolResult:
    geom_ref = _resolve_ref(ctx, "geometry_from") or _resolve_ref(ctx, "input_ref")
    if not geom_ref:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="multipart_to_singlepart", status="empty", message="需要 geometry_from")
    gdf = _dict_to_gdf(geom_ref)
    if gdf is None:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="multipart_to_singlepart", status="empty", message="无法解析输入几何")
    result = ctx.analyzer.multipart_to_singlepart(gdf)
    return _analyzer_result_to_tool_result(ctx, "multipart_to_singlepart", result)


@_register_tool("delete_duplicate_geometries")
def _handle_delete_duplicate_geometries(ctx: _ToolContext) -> ToolResult:
    geom_ref = _resolve_ref(ctx, "geometry_from") or _resolve_ref(ctx, "input_ref")
    if not geom_ref:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="delete_duplicate_geometries", status="empty", message="需要 geometry_from")
    gdf = _dict_to_gdf(geom_ref)
    if gdf is None:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="delete_duplicate_geometries", status="empty", message="无法解析输入几何")
    result = ctx.analyzer.delete_duplicate_geometries(gdf)
    return _analyzer_result_to_tool_result(ctx, "delete_duplicate_geometries", result)


@_register_tool("snap_geometries")
def _handle_snap_geometries(ctx: _ToolContext) -> ToolResult:
    geom_ref = _resolve_ref(ctx, "geometry_from") or _resolve_ref(ctx, "input_ref")
    snap_ref = _resolve_ref(ctx, "snap_to") or _resolve_ref(ctx, "target_ref")
    tolerance = ctx.params.get("tolerance", 1.0)
    if not geom_ref or not snap_ref:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="snap_geometries", status="empty", message="需要 geometry_from 和 snap_to")
    gdf = _dict_to_gdf(geom_ref)
    target = _dict_to_gdf(snap_ref)
    if gdf is None or target is None:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="snap_geometries", status="empty", message="无法解析输入几何")
    result = ctx.analyzer.snap_geometries(gdf, target, tolerance=float(tolerance))
    return _analyzer_result_to_tool_result(ctx, "snap_geometries", result)


@_register_tool("reproject_layer")
def _handle_reproject_layer(ctx: _ToolContext) -> ToolResult:
    geom_ref = _resolve_ref(ctx, "geometry_from") or _resolve_ref(ctx, "input_ref")
    target_crs = ctx.params.get("target_crs", "EPSG:4326")
    if not geom_ref:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="reproject_layer", status="empty", message="需要 geometry_from")
    gdf = _dict_to_gdf(geom_ref)
    if gdf is None:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="reproject_layer", status="empty", message="无法解析输入几何")
    result = ctx.analyzer.reproject_layer(gdf, target_crs=str(target_crs))
    return _analyzer_result_to_tool_result(ctx, "reproject_layer", result)


@_register_tool("batch_reproject_layers")
def _handle_batch_reproject_layers(ctx: _ToolContext) -> ToolResult:
    layers_ref = _resolve_ref(ctx, "layers_ref") or _resolve_ref(ctx, "data_from")
    target_crs = ctx.params.get("target_crs", "EPSG:4326")
    if not layers_ref:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="batch_reproject_layers", status="empty", message="需要 layers_ref 参数")
    if not isinstance(layers_ref, dict):
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="batch_reproject_layers", status="empty", message="layers_ref 需要是 dict 格式")
    layers_dict = {}
    for k, v in layers_ref.items():
        gdf = _dict_to_gdf(v)
        if gdf is not None:
            layers_dict[k] = gdf
    if not layers_dict:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="batch_reproject_layers", status="empty", message="无法解析任何图层")
    result = ctx.analyzer.batch_reproject(layers_dict, target_crs=str(target_crs))
    result_dict = {k: _gdf_to_dict(v) for k, v in result.items()}
    return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="batch_reproject_layers", status="success", data=result_dict, source="computed")


# ============================================================
# 属性 handlers
# ============================================================


@_register_tool("extract_by_attribute")
def _handle_extract_by_attribute(ctx: _ToolContext) -> ToolResult:
    geom_ref = _resolve_ref(ctx, "geometry_from") or _resolve_ref(ctx, "input_ref")
    expression = ctx.params.get("expression") or ctx.params.get("where", "")
    field = ctx.params.get("field")
    operator = ctx.params.get("operator")
    value = ctx.params.get("value")
    if not geom_ref:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="extract_by_attribute", status="empty", message="需要 geometry_from")
    if not expression and not (field and operator):
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="extract_by_attribute", status="empty", message="需要 expression 参数")
    gdf = _dict_to_gdf(geom_ref)
    if gdf is None:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="extract_by_attribute", status="empty", message="无法解析输入几何")
    if expression and not (field and operator):
        import re

        match = re.match(
            r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(==|!=|>=|<=|>|<|contains|is_null)\s*(.*?)\s*$",
            str(expression),
        )
        if not match:
            return ToolResult(
                tool_call_id=ctx.tool_call_id,
                tool_name="extract_by_attribute",
                status="error",
                message="expression 仅支持 field operator value，例如 class == 'station'",
            )
        field, operator, raw_value = match.groups()
        if operator == "is_null":
            value = None
        else:
            try:
                value = ast.literal_eval(raw_value)
            except (SyntaxError, ValueError):
                value = raw_value.strip().strip("'\"")
    result = ctx.analyzer.extract_by_attribute(gdf, str(field), str(operator), value)
    return _analyzer_result_to_tool_result(ctx, "extract_by_attribute", result)


@_register_tool("keep_fields")
def _handle_keep_fields(ctx: _ToolContext) -> ToolResult:
    geom_ref = _resolve_ref(ctx, "geometry_from") or _resolve_ref(ctx, "input_ref")
    fields = ctx.params.get("fields", [])
    if not geom_ref:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="keep_fields", status="empty", message="需要 geometry_from")
    if not fields:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="keep_fields", status="empty", message="需要 fields 参数")
    gdf = _dict_to_gdf(geom_ref)
    if gdf is None:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="keep_fields", status="empty", message="无法解析输入几何")
    result = ctx.analyzer.keep_fields(gdf, list(fields))
    return _analyzer_result_to_tool_result(ctx, "keep_fields", result)


@_register_tool("rename_field")
def _handle_rename_field(ctx: _ToolContext) -> ToolResult:
    geom_ref = _resolve_ref(ctx, "geometry_from") or _resolve_ref(ctx, "input_ref")
    old_name = ctx.params.get("old_name") or ctx.params.get("field", "")
    new_name = ctx.params.get("new_name", "")
    if not geom_ref:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="rename_field", status="empty", message="需要 geometry_from")
    if not old_name or not new_name:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="rename_field", status="empty", message="需要 old_name 和 new_name")
    gdf = _dict_to_gdf(geom_ref)
    if gdf is None:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="rename_field", status="empty", message="无法解析输入几何")
    result = ctx.analyzer.rename_field(gdf, str(old_name), str(new_name))
    return _analyzer_result_to_tool_result(ctx, "rename_field", result)


@_register_tool("field_calculator")
def _handle_field_calculator(ctx: _ToolContext) -> ToolResult:
    geom_ref = _resolve_ref(ctx, "geometry_from") or _resolve_ref(ctx, "input_ref")
    field_name = ctx.params.get("field") or ctx.params.get("field_name", "")
    expression = ctx.params.get("expression", "")
    if not geom_ref:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="field_calculator", status="empty", message="需要 geometry_from")
    if not field_name or not expression:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="field_calculator", status="empty", message="需要 field 和 expression")
    gdf = _dict_to_gdf(geom_ref)
    if gdf is None:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="field_calculator", status="empty", message="无法解析输入几何")
    result = ctx.analyzer.field_calculator(gdf, str(field_name), str(expression))
    return _analyzer_result_to_tool_result(ctx, "field_calculator", result)


# ============================================================
# Raster handlers
# ============================================================

def _resolve_raster_path(value: Any) -> str | None:
    """Unwrap uploaded/derived raster artifacts to their concrete file path."""
    if isinstance(value, (_Path, str)):
        return str(value)
    if isinstance(value, dict):
        for key in ("raster_path", "dst_path", "src_path", "path"):
            candidate = value.get(key)
            if isinstance(candidate, (_Path, str)) and str(candidate):
                return str(candidate)
        for key in ("data", "result", "raster"):
            nested = value.get(key)
            if nested is not None and nested is not value:
                resolved = _resolve_raster_path(nested)
                if resolved:
                    return resolved
    return None


def _resolve_vector_input(value: Any) -> Any:
    """Return a vector path or an in-memory GeoDataFrame from artifact wrappers."""
    if isinstance(value, (_Path, str)):
        return str(value)
    gdf = _dict_to_gdf(value) if isinstance(value, (dict, list)) else None
    return gdf


@_register_tool("reproject_raster")
def _handle_reproject_raster(ctx: _ToolContext) -> ToolResult:
    src_path = ctx.params.get("src_path")
    dst_crs = ctx.params.get("dst_crs", "EPSG:4326")
    dst_path = ctx.params.get("dst_path")
    if not src_path:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="reproject_raster", status="error", message="需要 src_path")
    result = ctx.raster_analyzer.reproject_raster(src_path, dst_crs=str(dst_crs), dst_path=dst_path)
    return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="reproject_raster", status=result.get("status", "error"), data=result.get("data"), message=result.get("message"))


@_register_tool("clip_raster_by_mask")
def _handle_clip_raster_by_mask(ctx: _ToolContext) -> ToolResult:
    src_path = ctx.params.get("src_path")
    mask_path = ctx.params.get("mask_path")
    dst_path = ctx.params.get("dst_path")
    if not src_path or not mask_path:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="clip_raster_by_mask", status="error", message="需要 src_path 和 mask_path")
    result = ctx.raster_analyzer.clip_raster_by_mask(src_path, mask_path, dst_path=dst_path)
    return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="clip_raster_by_mask", status=result.get("status", "error"), data=result.get("data"), message=result.get("message"))


@_register_tool("clip_raster_by_extent")
def _handle_clip_raster_by_extent(ctx: _ToolContext) -> ToolResult:
    src_path = ctx.params.get("src_path")
    extent = ctx.params.get("extent")
    dst_path = ctx.params.get("dst_path")
    if not src_path or not extent:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="clip_raster_by_extent", status="error", message="需要 src_path 和 extent")
    result = ctx.raster_analyzer.clip_raster_by_extent(src_path, extent, dst_path=dst_path)
    return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="clip_raster_by_extent", status=result.get("status", "error"), data=result.get("data"), message=result.get("message"))


@_register_tool("raster_calculator")
def _handle_raster_calculator(ctx: _ToolContext) -> ToolResult:
    expression = ctx.params.get("expression")
    bands = ctx.params.get("bands", {})
    dst_path = ctx.params.get("dst_path")
    if not expression:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="raster_calculator", status="error", message="需要 expression")
    result = ctx.raster_analyzer.raster_calculator(expression, bands=bands, dst_path=dst_path)
    return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="raster_calculator", status=result.get("status", "error"), data=result.get("data"), message=result.get("message"))


@_register_tool("zonal_statistics")
def _handle_zonal_statistics(ctx: _ToolContext) -> ToolResult:
    raster_value = _resolve_ref(ctx, "raster_from") or ctx.params.get("raster_path")
    vector_value = _resolve_ref(ctx, "vector_from") or ctx.params.get("vector_path")
    if len(ctx.results_data) >= 2:
        ordered_values = list(ctx.results_data.values())
        raster_value, vector_value = ordered_values[0], ordered_values[1]
    raster_path = _resolve_raster_path(raster_value)
    vector_path = _resolve_vector_input(vector_value)
    stats = ctx.params.get("stats", ["mean", "min", "max"])
    if not raster_path or vector_path is None:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="zonal_statistics", status="error", message="需要 raster_path 和 vector_path")
    result = ctx.raster_analyzer.zonal_statistics(raster_path, vector_path, stats=stats)
    data = result.get("data")
    if (
        result.get("status") == "success"
        and isinstance(data, list)
        and data
        and isinstance(data[0], dict)
        and data[0].get("type") == "Feature"
    ):
        data = {"type": "FeatureCollection", "features": data}
    return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="zonal_statistics", status=result.get("status", "error"), data=data, message=result.get("message"))


@_register_tool("raster_sampling")
def _handle_raster_sampling(ctx: _ToolContext) -> ToolResult:
    raster_path = ctx.params.get("raster_path")
    points = ctx.params.get("points")
    if not raster_path or not points:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="raster_sampling", status="error", message="需要 raster_path 和 points")
    result = ctx.raster_analyzer.raster_sampling(raster_path, points)
    return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="raster_sampling", status=result.get("status", "error"), data=result.get("data"), message=result.get("message"))


@_register_tool("rasterize_vector")
def _handle_rasterize_vector(ctx: _ToolContext) -> ToolResult:
    vector_path = ctx.params.get("vector_path")
    attribute = ctx.params.get("attribute")
    dst_path = ctx.params.get("dst_path")
    pixel_size = ctx.params.get("pixel_size", 30.0)
    if not vector_path:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="rasterize_vector", status="error", message="需要 vector_path")
    result = ctx.raster_analyzer.rasterize_vector(vector_path, attribute=attribute, dst_path=dst_path, pixel_size=float(pixel_size))
    return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="rasterize_vector", status=result.get("status", "error"), data=result.get("data"), message=result.get("message"))


@_register_tool("polygonize_raster")
def _handle_polygonize_raster(ctx: _ToolContext) -> ToolResult:
    src_path = ctx.params.get("src_path")
    band = ctx.params.get("band", 1)
    dst_path = ctx.params.get("dst_path")
    if not src_path:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="polygonize_raster", status="error", message="需要 src_path")
    result = ctx.raster_analyzer.polygonize_raster(src_path, band=int(band), dst_path=dst_path)
    return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="polygonize_raster", status=result.get("status", "error"), data=result.get("data"), message=result.get("message"))


@_register_tool("slope")
def _handle_slope(ctx: _ToolContext) -> ToolResult:
    dem_path = _resolve_raster_path(_resolve_ref(ctx, "dem_from") or ctx.params.get("dem_path"))
    degree = ctx.params.get("degree", True)
    dst_path = ctx.params.get("dst_path")
    if not dem_path:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="slope", status="error", message="需要 dem_path")
    result = ctx.raster_analyzer.slope(dem_path, dst_path=dst_path, degree=degree)
    return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="slope", status=result.get("status", "error"), data=result.get("data"), message=result.get("message"))


@_register_tool("aspect")
def _handle_aspect(ctx: _ToolContext) -> ToolResult:
    dem_path = _resolve_raster_path(_resolve_ref(ctx, "dem_from") or ctx.params.get("dem_path"))
    dst_path = ctx.params.get("dst_path")
    if not dem_path:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="aspect", status="error", message="需要 dem_path")
    result = ctx.raster_analyzer.aspect(dem_path, dst_path=dst_path)
    return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="aspect", status=result.get("status", "error"), data=result.get("data"), message=result.get("message"))


@_register_tool("hillshade")
def _handle_hillshade(ctx: _ToolContext) -> ToolResult:
    dem_path = _resolve_raster_path(_resolve_ref(ctx, "dem_from") or ctx.params.get("dem_path"))
    azimuth = ctx.params.get("azimuth", 315.0)
    altitude = ctx.params.get("altitude", 45.0)
    dst_path = ctx.params.get("dst_path")
    if not dem_path:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="hillshade", status="error", message="需要 dem_path")
    result = ctx.raster_analyzer.hillshade(dem_path, azimuth=float(azimuth), altitude=float(altitude), dst_path=dst_path)
    return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="hillshade", status=result.get("status", "error"), data=result.get("data"), message=result.get("message"))


@_register_tool("contour")
def _handle_contour(ctx: _ToolContext) -> ToolResult:
    dem_path = _resolve_raster_path(ctx.params.get("dem_path"))
    interval = ctx.params.get("interval", 10.0)
    dst_path = ctx.params.get("dst_path")
    if not dem_path:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="contour", status="error", message="需要 dem_path")
    result = ctx.raster_analyzer.contour(dem_path, interval=float(interval), dst_path=dst_path)
    return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="contour", status=result.get("status", "error"), data=result.get("data"), message=result.get("message"))


@_register_tool("reclassify_raster")
def _handle_reclassify_raster(ctx: _ToolContext) -> ToolResult:
    src_path = _resolve_raster_path(_resolve_ref(ctx, "src_from") or ctx.params.get("src_path"))
    bins = ctx.params.get("bins", [])
    values = ctx.params.get("values", [])
    dst_path = ctx.params.get("dst_path")
    if not src_path:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="reclassify_raster", status="error", message="需要 src_path")
    result = ctx.raster_analyzer.reclassify_raster(
        src_path,
        bins=bins,
        values=values,
        dst_path=dst_path,
    )
    return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="reclassify_raster", status=result.get("status", "error"), data=result.get("data"), message=result.get("message"))


@_register_tool("terrain_ruggedness_index")
def _handle_terrain_ruggedness_index(ctx: _ToolContext) -> ToolResult:
    dem_path = ctx.params.get("dem_path")
    dst_path = ctx.params.get("dst_path")
    if not dem_path:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="terrain_ruggedness_index", status="error", message="需要 dem_path")
    result = ctx.raster_analyzer.terrain_ruggedness_index(dem_path, dst_path=dst_path)
    return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="terrain_ruggedness_index", status=result.get("status", "error"), data=result.get("data"), message=result.get("message"))


@_register_tool("topographic_position_index")
def _handle_topographic_position_index(ctx: _ToolContext) -> ToolResult:
    dem_path = ctx.params.get("dem_path")
    radius = ctx.params.get("radius", 100.0)
    dst_path = ctx.params.get("dst_path")
    if not dem_path:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="topographic_position_index", status="error", message="需要 dem_path")
    result = ctx.raster_analyzer.topographic_position_index(dem_path, radius=float(radius), dst_path=dst_path)
    return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="topographic_position_index", status=result.get("status", "error"), data=result.get("data"), message=result.get("message"))


@_register_tool("roughness")
def _handle_roughness(ctx: _ToolContext) -> ToolResult:
    dem_path = ctx.params.get("dem_path")
    dst_path = ctx.params.get("dst_path")
    if not dem_path:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="roughness", status="error", message="需要 dem_path")
    result = ctx.raster_analyzer.roughness(dem_path, dst_path=dst_path)
    return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="roughness", status=result.get("status", "error"), data=result.get("data"), message=result.get("message"))


# ============================================================
# IO handlers
# ============================================================

import os as _os
from pathlib import Path as _Path


def _validate_io_path(file_path: str) -> str | None:
    """校验文件路径在允许的目录内。

    Returns:
        校验失败时返回错误消息，成功返回 None。
    """
    if not file_path:
        return "需要 file_path"
    from app.config import settings as _s
    ws_root = _Path(_s.APP_WORKSPACE_DIR).resolve()
    ws_root.mkdir(parents=True, exist_ok=True)
    upload_root = _Path("./uploads").resolve()
    upload_root.mkdir(parents=True, exist_ok=True)
    resolved = _Path(file_path).resolve()
    try:
        if resolved.is_relative_to(ws_root) or resolved.is_relative_to(upload_root):
            return None
    except (ValueError, OSError):
        pass
    return f"路径不在允许的目录内: {file_path}"


@_register_tool("load_vector")
def _handle_load_vector(ctx: _ToolContext) -> ToolResult:
    file_path = ctx.params.get("file_path") or ctx.params.get("path", "")
    if err := _validate_io_path(file_path):
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="load_vector", status="error", message=err)
    result = ctx.data_io.load_vector(file_path)
    return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="load_vector", status=result.get("status", "error"), data=result.get("data"), message=result.get("message"))


@_register_tool("load_raster")
def _handle_load_raster(ctx: _ToolContext) -> ToolResult:
    file_path = ctx.params.get("file_path") or ctx.params.get("path", "")
    if err := _validate_io_path(file_path):
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="load_raster", status="error", message=err)
    result = ctx.data_io.load_raster(file_path)
    return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="load_raster", status=result.get("status", "error"), data=result.get("data"), message=result.get("message"))


@_register_tool("load_csv")
def _handle_load_csv(ctx: _ToolContext) -> ToolResult:
    file_path = ctx.params.get("file_path") or ctx.params.get("path", "")
    if err := _validate_io_path(file_path):
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="load_csv", status="error", message=err)
    result = ctx.data_io.load_csv(file_path)
    return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="load_csv", status=result.get("status", "error"), data=result.get("data"), message=result.get("message"))


@_register_tool("csv_to_points")
def _handle_csv_to_points(ctx: _ToolContext) -> ToolResult:
    data_ref = _resolve_ref(ctx, "data_from") or _resolve_ref(ctx, "input_ref")
    if not data_ref:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="csv_to_points", status="empty", message="需要 data_from 参数")
    x_col = ctx.params.get("x_col") or ctx.params.get("lon_col", "lon")
    y_col = ctx.params.get("y_col") or ctx.params.get("lat_col", "lat")
    crs = ctx.params.get("crs", "EPSG:4326")
    result = ctx.data_io.csv_to_points(data_ref, x_field=str(x_col), y_field=str(y_col), crs=str(crs))
    if isinstance(result, dict):
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="csv_to_points", status=result.get("status", "success"), data=result.get("data"), message=result.get("message"))
    return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="csv_to_points", status="success", data=_gdf_to_dict(result), source="computed")


@_register_tool("summarize_layer")
def _handle_summarize_layer(ctx: _ToolContext) -> ToolResult:
    geom_ref = _resolve_ref(ctx, "geometry_from") or _resolve_ref(ctx, "input_ref") or _resolve_ref(ctx, "data_from")
    if not geom_ref:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="summarize_layer", status="empty", message="需要 geometry_from")
    gdf = _dict_to_gdf(geom_ref)
    if gdf is None:
        result = ctx.data_io.summarize_layer(geom_ref)
    else:
        result = ctx.data_io.summarize_layer(gdf)
    return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="summarize_layer", status=result.get("status", "success"), data=result.get("data"), message=result.get("message"))


@_register_tool("export_result")
def _handle_export_result(ctx: _ToolContext) -> ToolResult:
    data_ref = _resolve_ref(ctx, "data_from") or _resolve_ref(ctx, "input_ref")
    fmt = ctx.params.get("format", "geojson")
    requested_path = str(ctx.params.get("output_path") or ctx.params.get("path") or "")
    if not data_ref:
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="export_result", status="error", message="需要 data_from")
    extension_map = {
        "geojson": ".geojson",
        "json": ".geojson",
        "gpkg": ".gpkg",
        "shp": ".shp",
        "kml": ".kml",
    }
    workspace_root = _Path(settings.APP_WORKSPACE_DIR).resolve()
    export_root = workspace_root / "exports"
    export_root.mkdir(parents=True, exist_ok=True)
    requested = _Path(requested_path) if requested_path else _Path()
    requested_resolved = requested.resolve() if requested_path else None
    if (
        requested_resolved is not None
        and requested.is_absolute()
        and requested_resolved.is_relative_to(workspace_root)
    ):
        output_path = requested_resolved
    else:
        filename = requested.name if requested.name not in ("", ".") else "result"
        suffix = extension_map.get(str(fmt).lower(), ".geojson")
        if not _Path(filename).suffix:
            filename += suffix
        output_path = export_root / filename
    if err := _validate_io_path(str(output_path)):
        return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="export_result", status="error", message=err)
    driver_map = {
        "geojson": "GeoJSON",
        "json": "GeoJSON",
        "gpkg": "GPKG",
        "shp": "ESRI Shapefile",
        "kml": "KML",
    }
    result = ctx.data_io.export_result(
        data_ref,
        path=str(output_path),
        driver=driver_map.get(str(fmt).lower(), str(fmt)),
    )
    return ToolResult(tool_call_id=ctx.tool_call_id, tool_name="export_result", status=result.get("status", "error"), data=result.get("data"), message=result.get("message"))


# ============================================================
# 可复用 Graph 节点（供子 Agent 使用）
# ============================================================


def _pending_from_preflight_error(error: Any, state: dict) -> dict | None:
    """Translate a user-resolvable preflight block into a resume payload.

    Native schema steps bypass the legacy Judge, so leaving a ``PreflightError``
    as a generic tool failure makes the advertised awaiting-input flow
    unreachable.  Only repair kinds that require an explicit user decision are
    paused; automatic repair and terminal validation failures retain the normal
    tool-error path.
    """
    issues = list(getattr(error, "issues", None) or [])
    issue_dicts = [
        issue.to_dict() if hasattr(issue, "to_dict") else dict(issue)
        for issue in issues
        if hasattr(issue, "to_dict") or isinstance(issue, dict)
    ]
    pause_issues = [
        issue for issue in issue_dicts
        if isinstance(issue.get("repair"), dict)
        and issue["repair"].get("kind") in {"ask_user", "confirm_action", "confirm_overwrite"}
    ]
    if not pause_issues:
        return None

    message = "; ".join(
        str(issue.get("message") or "需要用户补充信息")
        for issue in pause_issues
    )
    return {
        "sub_agent_run_id": str(state.get("run_id") or ""),
        "original_request": str(state.get("user_input") or ""),
        "missing_slots": [],
        # Empty schema deliberately preserves the raw answer under ``answer``
        # for the Root Planner's resume replan path.
        "slot_patch_schema": {},
        "message": message,
        "issues": pause_issues,
    }


def native_tool_executor_node(state: dict) -> dict:
    """Execute exactly one validated model-native tool call."""
    from app.agents.duplicate_guard import DuplicateActionGuard
    from app.agents.native_tool_mode import (
        ToolArgumentValidationError,
        native_reference_data,
        validate_tool_arguments,
    )
    from app.agents.registry import TOOL_SPECS, get_semantic_action, get_spec
    from app.agents.workspace.state import WorkspaceState
    from app.agents.preflight.runner import run_with_preflight
    from app.agents.preflight.validation import PreflightError
    from app.tools.raster_analysis import RasterAnalyzer

    planner_output = state.get("planner_output")
    calls = list(getattr(planner_output, "tool_calls", None) or [])
    if len(calls) != 1:
        return {"tool_results": list(state.get("tool_results") or [])}

    call = calls[0]
    tool_name = str(getattr(call, "name", "") or "")
    call_id = str(getattr(call, "id", "") or f"native_{state.get('iteration', 0)}")
    raw_args = getattr(call, "args", {}) or {}
    spec = get_spec(state.get("agent_role", ""))
    from app.agents.events.current import get_current_handler
    from app.agents.events import emit_event
    on_event = get_current_handler()
    started_at = time.perf_counter()
    emit_event(
        on_event,
        "tool.call.start",
        f"开始执行 {tool_name}",
        tool_name=tool_name,
        tool_call_id=call_id,
        params=dict(raw_args),
        task_id=state.get("parent_task_id") or "",
        agent_role=state.get("agent_role") or "",
    )

    tr: ToolResult
    pending_task: dict | None = None
    if tool_name not in spec.tool_names:
        tr = ToolResult(
            tool_call_id=call_id,
            tool_name=tool_name or "__invalid_tool__",
            mode="json",
            status="error",
            error_code="TOOL_NOT_ALLOWED",
            message=f"{tool_name!r} is not allowed for role {spec.agent_role!r}",
        )
    else:
        try:
            args = validate_tool_arguments(tool_name, raw_args)
        except (ToolArgumentValidationError, KeyError) as exc:
            tr = ToolResult(
                tool_call_id=call_id,
                tool_name=tool_name,
                mode="json",
                status="error",
                error_code="INVALID_TOOL_ARGUMENTS",
                message=str(exc),
            )
        else:
            guard = DuplicateActionGuard()
            for entry in state.get("duplicate_actions") or []:
                if isinstance(entry, dict):
                    guard.record(entry.get("tool", ""), entry.get("params", {}))
            recent = list(state.get("tool_results") or [])
            retry_after_failure = bool(
                recent
                and getattr(recent[-1], "status", None) in {"error", "empty"}
            )
            if not retry_after_failure and guard.is_duplicate(tool_name, args):
                tr = ToolResult(
                    tool_call_id=call_id,
                    tool_name=tool_name,
                    mode="json",
                    status="error",
                    error_code="DUPLICATE_ACTION",
                    message=guard.suggestion(tool_name),
                )
            else:
                handler = _TOOL_REGISTRY.get(tool_name)
                if handler is None:
                    tr = ToolResult(
                        tool_call_id=call_id,
                        tool_name=tool_name,
                        mode="json",
                        status="error",
                        error_code="TOOL_NOT_REGISTERED",
                        message=f"No handler registered for {tool_name!r}",
                    )
                else:
                    instances = {
                        "poi": _get_poi_query_instance(),
                        "geo_coder": GeoCoder(),
                        "analyzer": SpatialAnalyzer(
                            amap_key=settings.AMAP_KEY,
                            amap_timeout=settings.AMAP_TIMEOUT,
                        ),
                        "data_io": DataIO(),
                        "layer_builder": MapLayerBuilder(),
                        "raster_analyzer": RasterAnalyzer(),
                    }
                    ctx = _ToolContext(
                        tool_call_id=call_id,
                        tool_name=tool_name,
                        iteration=state.get("iteration", 0),
                        params=args,
                        results_data=native_reference_data(state),
                        instances=instances,
                    )
                    try:
                        tool_spec = TOOL_SPECS[tool_name]
                        tr = run_with_preflight(
                            tool_name=tool_name,
                            semantic_action=get_semantic_action(tool_spec),
                            fn=handler,
                            args=(ctx,),
                            kwargs=args,
                            workspace=WorkspaceState(state.get("session_vars") or {}),
                        )
                        if not isinstance(tr, ToolResult):
                            tr = ToolResult(
                                tool_call_id=call_id,
                                tool_name=tool_name,
                                status="success",
                                data=tr,
                            )
                        tr.mode = "json"
                    except PreflightError as exc:
                        pending_task = _pending_from_preflight_error(exc, state)
                        tr = ToolResult(
                            tool_call_id=call_id,
                            tool_name=tool_name,
                            mode="json",
                            status="error",
                            error_code="PREFLIGHT_BLOCKED",
                            message=str(exc),
                        )
                        issue_payload = [
                            issue.to_dict() if hasattr(issue, "to_dict") else issue
                            for issue in (getattr(exc, "issues", None) or [])
                        ]
                        emit_event(
                            on_event,
                            "tool.preflight.blocked",
                            pending_task.get("message", str(exc)) if pending_task else str(exc),
                            tool_name=tool_name,
                            issues=issue_payload,
                            task_id=state.get("parent_task_id") or "",
                            agent_role=state.get("agent_role") or "",
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("native tool execution failed tool=%s", tool_name)
                        tr = ToolResult(
                            tool_call_id=call_id,
                            tool_name=tool_name,
                            mode="json",
                            status="error",
                            error_code="TOOL_EXECUTION_ERROR",
                            message=f"{type(exc).__name__}: {exc}",
                        )

    existing = list(state.get("tool_results") or [])
    measured_duration_ms = max(0, int((time.perf_counter() - started_at) * 1000))
    if tr.duration_ms <= 0:
        tr.duration_ms = measured_duration_ms
    existing.append(tr)
    result_preview: Any
    if tr.status == "success":
        result_preview = tr.data
    else:
        result_preview = {
            "message": tr.message or "",
            "error_code": tr.error_code,
        }
    try:
        preview_json = json.dumps(result_preview, ensure_ascii=False, default=str)
        if len(preview_json) > 2000:
            result_preview = preview_json[:2000] + "... (truncated)"
    except Exception:  # noqa: BLE001 - tracing must never break tool execution
        result_preview = str(result_preview)[:2000]
    emit_event(
        on_event,
        "tool.call.complete",
        f"{tool_name} 执行完成",
        tool_name=tool_name,
        tool_call_id=call_id,
        status=tr.status,
        error_code=tr.error_code,
        duration_ms=tr.duration_ms,
        result=result_preview,
        task_id=state.get("parent_task_id") or "",
        agent_role=state.get("agent_role") or "",
    )
    duplicate_actions = list(state.get("duplicate_actions") or [])
    duplicate_actions.append({"tool": tool_name, "params": dict(raw_args)})
    tool_message = json.dumps(tr.model_dump(exclude_none=True), ensure_ascii=False, default=str)
    if len(tool_message) > 4000:
        tool_message = tool_message[:4000] + "... (truncated)"
    result = {
        "tool_results": existing,
        "duplicate_actions": duplicate_actions,
        "messages": [ToolMessage(content=tool_message, tool_call_id=call_id)],
    }
    if pending_task is not None:
        result["pending_task"] = pending_task
    return result


def native_step_finalize_node(state: dict) -> dict:
    """Deterministically finish or retry the current atomic workflow step.

    Ordinary roles no longer ask Judge whether the whole user request is done.
    Root Dispatcher owns the multi-step DAG; this node only evaluates the last
    ToolResult for the current step.
    """
    pending = state.get("pending_task")
    if pending:
        # Native JSON steps bypass the legacy Judge, which used to own pending
        # persistence.  Persist here so the production /resume endpoint can
        # find the pause after the graph has returned.
        try:
            session_id = state.get("session_id", "")
            if session_id:
                from app.agents.pending import PendingStore, PendingTask

                pending_record = PendingTask(
                    sub_agent_run_id=(
                        pending.get("sub_agent_run_id") or state.get("run_id", "")
                    ),
                    original_request=(
                        pending.get("original_request") or state.get("user_input", "")
                    ),
                    missing_slots=pending.get("missing_slots") or [],
                    candidates=pending.get("candidates") or [],
                    slot_patch_schema=pending.get("slot_patch_schema") or {},
                    choices=pending.get("choices") or [],
                    correction_history=pending.get("correction_history") or [],
                    message=pending.get("message", ""),
                    issues=pending.get("issues") or [],
                )
                PendingStore().save_sync(session_id, pending_record)
        except Exception:
            logger.exception("native AWAITING_INPUT: failed to persist PendingStore")

        try:
            from app.agents.events.current import get_current_handler
            from app.agents.events import emit_event

            emit_event(
                get_current_handler(),
                "judge.awaiting_input",
                pending.get("message", "需要用户提供更多信息"),
                pending_task=pending,
                issues=pending.get("issues") or [],
                run_id=state.get("run_id", ""),
                session_id=state.get("session_id", ""),
            )
        except Exception:
            logger.exception("native AWAITING_INPUT: failed to emit event")

        return {
            "should_stop": True,
            "decision": "AWAITING_INPUT",
            "pending_task": pending,
        }

    tool_results = list(state.get("tool_results") or [])
    last = tool_results[-1] if tool_results else None
    status = (
        getattr(last, "status", "")
        if hasattr(last, "status")
        else last.get("status", "") if isinstance(last, dict)
        else ""
    )
    tool_name = (
        state.get("required_tool_name")
        or (getattr(last, "tool_name", "") if hasattr(last, "tool_name") else "")
        or "current step"
    )

    if status == "success":
        from app.agents.judge import _build_final_output

        final_output = _build_final_output(dict(state), f"{tool_name} 执行成功")
        final_output["status"] = "success"
        return {
            "should_stop": True,
            "decision": "FINISH",
            "final_output": final_output,
        }

    error_code = (
        getattr(last, "error_code", None)
        if hasattr(last, "error_code")
        else last.get("error_code") if isinstance(last, dict)
        else "EMPTY_STEP"
    )
    message = (
        getattr(last, "message", None)
        if hasattr(last, "message")
        else last.get("message") if isinstance(last, dict)
        else "No ToolResult was produced."
    )
    iteration = int(state.get("iteration", 0))
    max_iterations = int(state.get("max_iterations", 1))
    if iteration >= max_iterations:
        return {
            "should_stop": True,
            "decision": "FINISH",
            "termination_cause": "NATIVE_STEP_RETRY_EXHAUSTED",
            "final_output": {
                "status": "failed",
                "summary": f"{tool_name} 执行失败：{message or error_code}",
                "error_code": error_code or "NATIVE_STEP_FAILED",
                "results": [],
            },
        }

    return {
        "should_stop": False,
        "messages": [HumanMessage(content=(
            f"只修订当前失败步骤 {tool_name}，不要添加或重做其他 DAG 步骤。\n"
            f"错误码：{error_code or 'UNKNOWN'}\n"
            f"失败原因：{message or '工具返回空结果'}\n"
            "重新填写该工具的 JSON Schema 参数。"
        ))],
    }


def observer_node(state: dict, *, llm=None) -> dict:
    """Observer 节点：对当前轮次所有 ToolResult 生成摘要消息。

    使用 planner_output.tool_calls 数量来确定本轮新增的结果数，
    只摘要本轮新产生的 tool_results，避免重复处理历史结果。
    """
    tool_results = state.get("tool_results") or []
    if not tool_results:
        return {"messages": []}

    planner_output: Optional[PlannerOutput] = state.get("planner_output")
    # code-mode and native tool mode both produce one ToolResult per round.
    num_current = 1 if (
        planner_output
        and (
            getattr(planner_output, "code", None)
            or getattr(planner_output, "tool_calls", None)
        )
    ) else 0
    # 只取本轮新增的结果（tool_results 尾部 num_current 条）
    current_round_results = tool_results[-num_current:] if num_current > 0 else tool_results

    summaries: list[str] = []
    for tr in current_round_results:
        summaries.append(observe(tr, llm=llm))

    if not summaries:
        return {"messages": []}

    combined = "\n".join(summaries)
    return {"messages": [HumanMessage(content=combined)]}


def judge_node(state: dict, *, llm=None) -> dict:
    """Judge 节点：判断 CONTINUE/RETRY/FINISH/AWAITING_INPUT。

    返回的 delta 含 should_stop / final_output（FINISH 时）/ messages（RETRY 时）/
    decision + pending_task（AWAITING_INPUT 时）。

    pending_task 优先于迭代上限强制终止：挂起等待用户输入时不得被
    max-iteration shortcut 改写成 FINISH final_output。

    无 pending 时，迭代上限强制终止在节点层做，不依赖 judge() 内部实现，
    避免 judge 异常/缺省返回导致死循环。
    """
    iteration = state.get("iteration", 0)

    # pending_task 优先：必须先走 judge() 的 AWAITING_INPUT 路径，
    # 即使已达 max_iterations 也不得 force-finish。
    if state.get("pending_task"):
        result = judge(dict(state), llm=llm)
        if result.get("decision") == "AWAITING_INPUT":
            return {
                "should_stop": True,
                "decision": "AWAITING_INPUT",
                "reason": result.get("reason", ""),
                "pending_task": result.get("pending_task") or state.get("pending_task"),
            }

    # 节点层强制 FINISH：达到最大迭代上限直接终止
    max_iter = state.get("max_iterations", 6)
    if iteration >= max_iter:
        from app.agents.judge import extract_partial_result
        logger.info("judge_node force FINISH at iteration=%d", iteration)
        fo = extract_partial_result(dict(state))
        return {
            "should_stop": True,
            "final_output": fo,
            "termination_cause": fo.get("termination_cause", "达到最大迭代上限"),
        }

    result = judge(dict(state), llm=llm)
    # AWAITING_INPUT：透传 decision / pending_task，不构造 final_output
    if result.get("decision") == "AWAITING_INPUT":
        return {
            "should_stop": True,
            "decision": "AWAITING_INPUT",
            "reason": result.get("reason", ""),
            "pending_task": result.get("pending_task") or state.get("pending_task"),
        }

    delta = {
        "should_stop": result.get("should_stop", False),
    }
    if result.get("decision") is not None:
        delta["decision"] = result["decision"]
    if result.get("final_output") is not None:
        delta["final_output"] = result["final_output"]
    elif result.get("should_stop"):
        # judge 判定 FINISH 但未提供 final_output 时，从状态构造兜底输出
        from app.agents.judge import _build_final_output
        delta["final_output"] = _build_final_output(dict(state), result.get("reason", ""))
    if result.get("messages"):
        delta["messages"] = result["messages"]
    # 传递 termination_cause（达到上限时）
    fo = result.get("final_output") or {}
    if fo.get("termination_cause"):
        delta["termination_cause"] = fo["termination_cause"]
    return delta


# ============================================================
# _build_code_mode_tool_fns — 为 sub-agent 所有工具构造 code-mode 函数
# ============================================================

def _build_code_mode_tool_fns(
    spec,
    session_vars: dict | None = None,
    enabled_toolkits: tuple[str, ...] = ("data_io",),
) -> dict:
    """为 spec.tool_names 下所有工具构建 code-mode 可调用的函数映射。

    让 LLM 可以像调普通 Python 函数一样调所有工具（thick/thin/sandbox），
    底层路由/异步/HTTP 调用由框架处理。

    - IO 工具（geo_code/query_poi/data_io_read）：sync proxy，内部处理 async
    - 计算工具（buffer/overlay/...）：sandbox 经 host RPC 调注册 handler
    - 兼容工具（code_executor）：同样经 host RPC；所有 LLM 代码统一在子进程

    Args:
        spec: SubAgentSpec — 当前 sub-agent 的规格。
        session_vars: 跨 step 持久化的变量 dict（供 tool proxy 引用前序结果）。
        enabled_toolkits: 当前 sub-agent 启用的 toolkit 名称元组，
                          用于按 ToolDisclosureController.visible_tools 过滤可见工具。
                          默认 ("data_io",) 保持向后兼容。

    Returns:
        {tool_name: callable} dict，供 LLM 代码直接调用。
    """
    from app.agents.context import _ToolContext
    from app.agents.code_mode.namespace import _make_not_in_sandbox_stub
    from app.agents.registry import TOOL_SPECS
    from app.agents.toolkit.registry import (
        ALWAYS_VISIBLE_TOOLS,
        ToolDisclosureController,
        ToolKitRegistry,
    )
    from app.tools.poi_query import POIQuery
    from app.tools.geo_code import GeoCoder
    from app.tools.spatial_analysis import SpatialAnalyzer
    from app.tools.data_io import DataIO
    from app.tools.map_layer import MapLayerBuilder
    from app.tools.raster_analysis import RasterAnalyzer

    # shallow copy: instances 的值对象共享引用，handler 不应修改它们的状态
    instances = {
        "poi": POIQuery(
            amap_key=settings.AMAP_KEY,
            amap_timeout=settings.AMAP_TIMEOUT,
            osm_timeout=settings.OSM_TIMEOUT,
            osm_endpoint=settings.OSM_ENDPOINT,
            osm_backup_endpoints=settings.OSM_BACKUP_ENDPOINTS,
        ),
        "geo_coder": GeoCoder(),
        "analyzer": SpatialAnalyzer(
            amap_key=settings.AMAP_KEY,
            amap_timeout=settings.AMAP_TIMEOUT,
        ),
        "data_io": DataIO(),
        "layer_builder": MapLayerBuilder(),
        "raster_analyzer": RasterAnalyzer(),
    }

    # 计算当前 sub-agent 可见的工具集合
    try:
        registry = ToolKitRegistry()
        controller = ToolDisclosureController(registry)
        # 用 enabled_toolkits 覆盖默认激活（v1 简化：调用方传入完整启用集合）
        controller.select_toolkits({"toolkits": list(enabled_toolkits)})
        visible = set(controller.visible_tools(list(spec.tool_names)))
        # 角色基础工具始终可用；显式选择的 toolkit 在下一轮扩展工具集。
        visible.update(spec.tool_names)
        visible.update(registry.active_tools(list(enabled_toolkits)))
        visible.update(ALWAYS_VISIBLE_TOOLS)
    except Exception:
        visible = set(spec.tool_names)

    tool_fns: dict = {}

    for name in visible:
        handler = _TOOL_REGISTRY.get(name)
        if not handler:
            continue
        spec_ = TOOL_SPECS.get(name)

        # D2: LLM 代码始终在 sandbox 子进程跑；所有 registry 工具（含 code_executor
        # 等 executor_type=sandbox）通过 host RPC 回调真实 handler，不再注入
        # NotInSandboxError stub（那会让 coder 的 code_executor 不可达）。
        is_async = bool(spec_ and spec_.executor_type == "async")

        def _make_fn(n, h, is_async_fn, insts_copy, session_vars_ref):
            def fn(*args, **kwargs):
                # 显式阻止位置参数（handler 签名是 ctx 不是业务参数名）
                # 但为关键工具做兜底翻译：geo_code("地名") → geo_code(address="地名")
                if args:
                    if len(args) == 1 and isinstance(args[0], str):
                        if n == "geo_code":
                            kwargs.setdefault("address", args[0])
                        elif n == "query_poi":
                            kwargs.setdefault("query", args[0])
                        else:
                            raise TypeError(
                                f"{n}() does not accept positional arguments. "
                                f"Use keyword arguments only, e.g. {n}(address='...')."
                            )
                    else:
                        raise TypeError(
                            f"{n}() does not accept positional arguments. "
                            f"Use keyword arguments only, e.g. {n}(address='...')."
                        )
                # results_data 从 session_vars 构建，供 location_from/geometry_from 等引用
                results_data = {}
                if session_vars_ref:
                    for idx, val in enumerate(session_vars_ref.values()):
                        results_data[idx] = val
                ctx = _ToolContext(
                    tool_call_id=f"code_{n}",
                    tool_name=n,
                    iteration=0,
                    params=kwargs,
                    results_data=results_data,
                    instances=insts_copy,
                )

                # --- BEFORE_TOOL_CALL hook pipeline ---
                try:
                    from app.agents.hooks import get_pipeline, HookPoint, BeforeToolContext

                    before_ctx = BeforeToolContext(
                        tool_name=n,
                        params=kwargs,
                        code="",
                        state=session_vars_ref or {},
                    )
                    pipeline = get_pipeline()
                    before_ctx = pipeline.emit(HookPoint.BEFORE_TOOL_CALL, before_ctx)
                    # 处理 validation_issues：blocking 的抛出 PreflightError
                    if before_ctx.validation_issues:
                        from app.agents.preflight.validation import PreflightError
                        blocking = [i for i in before_ctx.validation_issues if i.severity == "error"]
                        if blocking:
                            msg = "; ".join(i.message for i in blocking)
                            raise PreflightError(msg, issues=blocking)
                except ImportError:
                    pass  # hook 系统不可用，静默跳过
                except PreflightError:
                    raise  # blocking issue，向上传播
                except Exception:
                    logger.warning("BEFORE_TOOL_CALL hook failed", exc_info=True)

                # --- Preflight wrapper: 在工具执行前做规则检查 ---
                try:
                    from app.agents.preflight.runner import run_with_preflight
                    from app.agents.registry import get_semantic_action, TOOL_SPECS as _TS
                    from app.agents.workspace.state import WorkspaceState

                    ws = WorkspaceState(session_vars_ref or {})
                    _spec = _TS.get(n)
                    _sa = get_semantic_action(_spec) if _spec else n
                    result = run_with_preflight(
                        tool_name=n, semantic_action=_sa,
                        fn=h, args=(ctx,), kwargs=kwargs,
                        workspace=ws,
                    )
                except ImportError:
                    # preflight 模块不可用时回退到直接调用
                    result = h(ctx)

                # KERNEL 工具的选择需要跨 code block 生效。host RPC 与调用者
                # 共享这份 dict，因此把选择写入 session_vars，下一轮重建
                # namespace/prompt 时即可读取，不再只是返回一段说明文字。
                if isinstance(result, ToolResult) and result.status == "success" and session_vars_ref is not None:
                    if n == "select_toolkit" and isinstance(result.data, dict):
                        session_vars_ref["__enabled_toolkits__"] = list(
                            result.data.get("active_toolkits") or []
                        )
                    elif n == "load_skill" and isinstance(result.data, dict):
                        skill_name = result.data.get("name")
                        content = result.data.get("content")
                        if skill_name and isinstance(content, str):
                            loaded = dict(session_vars_ref.get("__loaded_skills__") or {})
                            loaded[str(skill_name)] = content
                            session_vars_ref["__loaded_skills__"] = loaded

                # 成功时给模型纯数据；失败/空结果必须保留状态和原因，不能把
                # data=None 解包成 None，否则 code-mode 会把真实失败误判成成功。
                if isinstance(result, ToolResult):
                    if result.status == "success":
                        return result.data
                    failure = {
                        "status": result.status,
                        "message": result.message,
                    }
                    if result.error_code:
                        failure["error_code"] = result.error_code
                    if result.data is not None:
                        failure["data"] = result.data
                    return failure
                if hasattr(result, 'data'):
                    return result.data
                if hasattr(result, 'result') and not isinstance(result, ToolResult):
                    return result.result
                return result
            fn.__name__ = n
            return fn

        tool_fns[name] = _make_fn(name, handler, is_async, dict(instances), session_vars)

    return tool_fns


# ============================================================
# code_executor_node — code-mode 的 "tools" 节点
# ============================================================

_SHARED_EXECUTOR = None
_SHARED_EXECUTOR_LOCK = threading.Lock()


def _get_shared_executor():
    """获取模块级共享 HybridExecutor。"""
    global _SHARED_EXECUTOR
    if _SHARED_EXECUTOR is None:
        with _SHARED_EXECUTOR_LOCK:
            if _SHARED_EXECUTOR is None:
                from app.agents.code_mode.executor import HybridExecutor
                _SHARED_EXECUTOR = HybridExecutor()
    return _SHARED_EXECUTOR

def code_executor_node(state: dict) -> dict:
    """Code-mode 工具执行节点：执行 planner_output.code（Python 代码）。

    流程：
    1. 从 state 取 session_vars、planner_output.code
    2. DuplicateActionGuard 检测重复动作
    3. HybridExecutor.execute(code, session_vars, known_tools)
    4. execution_to_tool_result() 映射回 ToolResult
    5. 更新 session_vars（__result__ 更新）
    6. 构造 ToolMessage 并返回 delta
    """
    planner_output = state.get("planner_output")
    if not planner_output or not getattr(planner_output, "code", None):
        return {"tool_results": []}

    from app.agents.code_mode.executor import HybridExecutor, execution_to_tool_result
    from app.agents.registry import TOOL_SPECS
    from app.agents.duplicate_guard import DuplicateActionGuard, extract_tool_calls_from_code

    code = planner_output.code
    iteration = state.get("iteration", 1)

    # --- Duplicate action guard ---
    dup_actions = list(state.get("duplicate_actions") or [])
    guard = DuplicateActionGuard()
    # Restore history from state
    for entry in dup_actions:
        if isinstance(entry, dict):
            guard.record(entry.get("tool", ""), entry.get("params", {}))

    # Extract tool calls from code for duplicate detection
    extracted_calls = extract_tool_calls_from_code(code)
    duplicate_detected = False
    dup_tool_name = ""

    # Only block duplicates if the most recent execution was successful
    # (failed executions should be retryable)
    allow_retry = False
    _recent_results = state.get("tool_results") or []
    if _recent_results:
        last_tr = _recent_results[-1]
        _status = (
            last_tr.status if hasattr(last_tr, "status")
            else last_tr.get("status", "") if isinstance(last_tr, dict)
            else ""
        )
        if _status in ("error", "empty"):
            allow_retry = True

    if not allow_retry:
        for tool_name, params in extracted_calls:
            if guard.is_duplicate(tool_name, params):
                duplicate_detected = True
                dup_tool_name = tool_name
                break

    if duplicate_detected:
        # Don't execute duplicate code; inject a hint message instead
        suggestion = guard.suggestion(dup_tool_name)
        tr = ToolResult(
            tool_call_id=f"dup_{iteration}",
            tool_name="__duplicate_guard__",
            status="error",
            message=suggestion,
            error_code="DUPLICATE_ACTION",
        )
        existing = list(state.get("tool_results") or [])
        existing.append(tr)
        return {
            "tool_results": existing,
            "messages": [HumanMessage(content=suggestion)],
        }

    # 获取当前 sub-agent 的已知工具
    agent_role = state.get("agent_role", "")
    from app.agents.registry import get_spec
    try:
        spec = get_spec(agent_role)
        known_tools = {}
        for name in spec.tool_names:
            if name in TOOL_SPECS:
                known_tools[name] = TOOL_SPECS[name].executor_type
    except KeyError:
        known_tools = {}
        spec = None

    executor = _get_shared_executor()
    # enabled_toolkits：可由 state 注入；默认 ("data_io",) 保持向后兼容。
    state_session_vars = state.get("session_vars") or {}
    enabled_toolkits = tuple(
        state.get("enabled_toolkits")
        or state_session_vars.get("__enabled_toolkits__")
        or ("data_io",)
    )
    tool_fns = (
        _build_code_mode_tool_fns(
            spec,
            session_vars=state.get("session_vars"),
            enabled_toolkits=enabled_toolkits,
        )
        if spec and spec.tool_names
        else {}
    )
    for tool_name in tool_fns:
        if tool_name in TOOL_SPECS:
            known_tools[tool_name] = TOOL_SPECS[tool_name].executor_type

    # --- Event emission: code.execution.start ---
    from app.agents.events.current import get_current_handler
    from app.agents.events import emit_event
    on_event = get_current_handler()
    trace_task_id = state.get("parent_task_id") or ""
    emit_event(on_event, "code.execution.start", "开始执行代码",
               executor_type="sandbox", role=agent_role,
               task_id=trace_task_id, agent_role=agent_role)

    exec_result = executor.execute(
        code=code,
        session_vars=state.get("session_vars", {}),
        known_tools=known_tools,
        tool_fns=tool_fns,
        on_event=on_event,
    )

    tr = execution_to_tool_result(exec_result, iteration=iteration)

    # --- Event emission: code.execution.complete / error ---
    if exec_result.stdout:
        emit_event(on_event, "code.execution.stdout", "代码标准输出",
                   stdout=exec_result.stdout[:2400], task_id=trace_task_id,
                   agent_role=agent_role)
    if exec_result.stderr:
        emit_event(on_event, "code.execution.stderr", "代码标准错误",
                   stderr=exec_result.stderr[:2400], task_id=trace_task_id,
                   agent_role=agent_role)
    if exec_result.success:
        result_preview = exec_result.result
        try:
            result_json = json.dumps(result_preview, ensure_ascii=False, default=str)
            if len(result_json) > 2400:
                result_preview = result_json[:2400] + "... (truncated)"
        except Exception:  # noqa: BLE001
            result_preview = str(result_preview)[:2400]
        emit_event(on_event, "code.execution.complete", "代码执行完成",
                   result_keys=list(exec_result.result.keys()) if isinstance(exec_result.result, dict) else [],
                   result=result_preview, duration_ms=exec_result.duration_ms,
                   task_id=trace_task_id, agent_role=agent_role)
    else:
        tb_snippet = (exec_result.traceback or "")[-500:]
        emit_event(on_event, "code.execution.error", "代码执行失败",
                   error=tb_snippet, traceback=tb_snippet,
                   error_code=exec_result.error_code,
                   duration_ms=exec_result.duration_ms,
                   task_id=trace_task_id, agent_role=agent_role)

    # 更新 session_vars（__result__ dict update）
    # 过滤不可序列化对象：每个 value 走 json.dumps 探测（不带 default=str，
    # 非 JSON-safe 对象直接拒绝入库，让 LLM 收到明确提示而非静默吞掉）。
    updated_session_vars = dict(state.get("session_vars", {}))
    if isinstance(exec_result.result, dict):
        for k, v in exec_result.result.items():
            try:
                json.dumps(v)
                updated_session_vars[k] = v
            except (TypeError, ValueError, OverflowError):
                logger.warning(
                    "session_vars[%s] (type=%s) 非 JSON 可序列化，已跳过",
                    k, type(v).__name__,
                )

    # --- Record actions in duplicate guard and persist to state ---
    for tool_name, params in extracted_calls:
        guard.record(tool_name, params)
    new_dup_actions = [
        {"tool": name, "params": params}
        for name, params in extracted_calls
    ]
    updated_dup_actions = dup_actions + new_dup_actions

    # tool_results 增加
    existing = list(state.get("tool_results") or [])
    existing.append(tr)

    return {
        "tool_results": existing,
        "session_vars": updated_session_vars,
        "enabled_toolkits": list(updated_session_vars.get("__enabled_toolkits__") or enabled_toolkits),
        "loaded_skills": dict(updated_session_vars.get("__loaded_skills__") or state.get("loaded_skills") or {}),
        "duplicate_actions": updated_dup_actions,
        "messages": [],  # observer 后续处理
    }


# ============================================================
# 多智能体 Dispatcher 主入口
# ============================================================

def _carry_forward_upload_file_ids(
    app: Any,
    config: dict,
    incoming_file_ids: list[str] | None,
) -> list[str]:
    """Keep the active upload set across turns in the same checkpoint thread."""
    incoming = list(incoming_file_ids or [])
    if incoming:
        return incoming
    try:
        snapshot = app.get_state(config)
        values = getattr(snapshot, "values", None) or {}
        prior = values.get("upload_file_ids") or []
        return [str(file_id) for file_id in prior if file_id]
    except Exception:
        logger.debug("unable to read prior upload ids from checkpoint", exc_info=True)
        return []


def _checkpoint_turn_history(app: Any, config: dict) -> list[Any]:
    """Recover the preceding user/assistant turn from the root checkpoint."""
    try:
        snapshot = app.get_state(config)
        values = getattr(snapshot, "values", None) or {}
        user_text = str(values.get("user_input") or "").strip()
        final_output = values.get("final_output") or {}
        assistant_text = str(
            final_output.get("summary") or final_output.get("text") or ""
        ).strip()
        messages: list[Any] = []
        if user_text:
            messages.append(HumanMessage(content=user_text))
        if assistant_text:
            messages.append(AIMessage(content=assistant_text))
        return messages
    except Exception:
        logger.debug("unable to read prior semantic turn from checkpoint", exc_info=True)
        return []


def run_react_loop(
    user_input: str,
    session_id: str,
    trace_id: str,
    history: Optional[list] = None,
    upload_file_ids: Optional[list[str]] = None,
    run_id: str = "",
    on_event: Any = None,
    *,
    checkpointer: Any = None,
    dispatcher_llm: Any = None,
    sub_agent_llm: Any = None,
) -> dict:
    """主入口：运行多智能体 Dispatcher，替代旧 React Loop 状态机。

    通过 build_dispatcher → checkpointer → CostTracker 执行空间智能任务。
    Redis 会话历史通过 ``history`` 注入 Root Planner；LangGraph checkpointer
    负责运行状态与恢复，两者职责不同。

    Args:
        user_input: 用户自然语言输入
        session_id: 会话 ID（兼作 checkpointer thread_id）
        trace_id: 追踪 ID（日志关联）
        history: 最近的 LangChain 对话消息，注入 Root Planner。
        run_id: Run 控制器 ID，用于暂停/取消。
        on_event: 可选 EventHandler callable，用于 SSE 实时事件发射。
                  通过 contextvar 传递给所有下游节点。
        checkpointer: optional LangGraph checkpointer (defaults to process singleton).
        dispatcher_llm: optional root planner transport (tests/e2e injection).
        sub_agent_llm: optional sub-agent transport, stored on a contextvar for
            ``dispatch_node`` → ``run_sub_agent`` (tests/e2e injection).

    Returns:
        dict: 含 should_stop / iteration / final_output / dispatcher_events / react_trace。
    """
    from app.agents.checkpointer import get_sqlite_checkpointer
    from app.agents.cost import CostTracker
    from app.agents.dispatcher import build_dispatcher, sub_agent_llm_context
    from app.agents.state import new_root_state
    from app.agents.events.current import set_current_handler, reset_current_handler

    # Wire event handler through contextvar so all downstream nodes can emit
    # events without going through LangGraph state (which can't serialise callables).
    _ctx_token = set_current_handler(on_event)

    checkpointer = checkpointer or get_sqlite_checkpointer()
    cost = CostTracker(max_tokens=settings.APP_MAX_COST_TOKENS)

    app = build_dispatcher(checkpointer=checkpointer, llm=dispatcher_llm)

    # Root checkpoints persist under empty namespace (thread_id only).
    # Do not set checkpoint_ns="_root" — that makes get_state raise
    # ValueError("Subgraph _root not found") and resume cannot find state.
    # Sub-agent runs keep their own checkpoint_ns in build_sub_agent.
    config = {
        "configurable": {
            "thread_id": session_id,
        }
    }
    effective_upload_file_ids = _carry_forward_upload_file_ids(
        app,
        config,
        list(upload_file_ids or []),
    )
    effective_history = list(history or [])
    if not effective_history:
        effective_history = _checkpoint_turn_history(app, config)
    initial_state = new_root_state(
        user_input,
        trace_id=trace_id,
        session_id=session_id,
        run_id=run_id,
        upload_file_ids=effective_upload_file_ids,
    )
    if effective_history:
        initial_state["messages"] = effective_history

    try:
        with sub_agent_llm_context(sub_agent_llm):
            try:
                final_state = app.invoke(initial_state, config=config)
            except Exception as e:
                logger.exception(
                    "run_react_loop failed session=%s trace=%s",
                    session_id, trace_id,
                )
                return {
                    "should_stop": True,
                    "iteration": 0,
                    "final_output": {
                        "status": "failed",
                        "error_code": "INTERNAL_ERROR",
                        "summary": f"Dispatcher 执行失败：{e}",
                    },
                    "session_id": session_id,
                    "trace_id": trace_id,
                    "dispatcher_events": [],
                    "react_trace": [],
                }

        logger.info(
            "run_react_loop done session=%s trace=%s cost_total=%d",
            session_id, trace_id, cost.total,
        )

        # --- AFTER_RUN hook: trigger session memory extraction etc. ---
        try:
            from app.agents.hooks import get_pipeline, HookPoint, AfterRunContext
            pipeline = get_pipeline()
            after_ctx = AfterRunContext(
                result=final_state,
                session_id=session_id,
                user_input=user_input,
            )
            pipeline.emit(HookPoint.AFTER_RUN, after_ctx)
        except Exception:
            logger.warning("AFTER_RUN hook failed", exc_info=True)

    finally:
        reset_current_handler(_ctx_token)

    return {
        "should_stop": final_state.get("should_stop", True),
        "iteration": final_state.get("iteration", 0),
        "final_output": final_state.get("final_output", {}),
        "session_id": session_id,
        "trace_id": trace_id,
        "dispatcher_events": final_state.get("dispatcher_events", []),
        "react_trace": _build_react_trace(final_state),
    }


# ============================================================
# React 思考链构建
# ============================================================

def _build_react_trace(final_state: dict) -> list[dict]:
    """从最终状态提取 ReAct 思考链，供前端展示。

    返回按时间排序的步骤列表，每步含：
    - round: 轮次
    - thinking: Planner 思考内容
    - tool_calls: [{tool_name, params}]
    - observer_summary: Observer 摘要（执行后）
    """
    messages = final_state.get("messages") or []
    tool_results = final_state.get("tool_results") or []
    trace: list[dict] = []
    round_idx = 0
    tr_idx = 0

    for m in messages:
        mtype = getattr(m, "type", "")
        content = getattr(m, "content", "")

        # AIMessage from planner: contains thinking + tool_calls
        if mtype == "ai" and hasattr(m, "tool_calls") and m.tool_calls:
            round_idx += 1
            # 兼容 LangChain ToolCall 的两种形态：dict-like 或 attribute-based
            safe_tcs = []
            for tc in m.tool_calls:
                name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
                args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
                safe_tcs.append({"tool_name": name or "", "params": args or {}})
            step: dict = {
                "round": round_idx,
                "thinking": content if isinstance(content, str) else "",
                "tool_calls": safe_tcs,
            }
            trace.append(step)

        # HumanMessage from observer: summary of tool execution
        elif mtype == "human" and isinstance(content, str) and content.strip():
            text = content.strip()
            # 跳过 JSON、RETRY 上下文、用户原始输入
            if text.startswith("{") or "Judge 判定 RETRY" in text:
                continue
            if trace:
                trace[-1]["observer_summary"] = text

    # 附加 tool_result 状态信息
    for tr in tool_results:
        if tr_idx < len(trace):
            tr_mode = getattr(tr, "mode", None) if hasattr(tr, "mode") else (tr.get("mode") if isinstance(tr, dict) else None)
            status = tr.status if hasattr(tr, "status") else tr.get("status", "")
            if tr_mode == "code":
                # code-mode 步骤：展示 Python 代码 + stdout + 执行结果
                tr_data = getattr(tr, "data", {}) if hasattr(tr, "data") else (tr.get("data", {}) if isinstance(tr, dict) else {})
                # 将 __code_block__ 映射为友好名称
                if trace[tr_idx].get("tool_calls"):
                    for tc in trace[tr_idx]["tool_calls"]:
                        if tc.get("tool_name") == "__code_block__":
                            tc["tool_name"] = "Python 代码执行"
                trace[tr_idx].update({
                    "code": tr_data.get("code", ""),
                    "stdout": tr_data.get("stdout", ""),
                    "result": tr_data.get("result"),
                    "executor_type": tr_data.get("executor_type", "?"),
                    "error": tr_data.get("traceback") if not tr_data.get("success", True) else None,
                })
            else:
                # JSON 模式：标准 tool_name + status
                if not trace[tr_idx].get("tool_results"):
                    trace[tr_idx]["tool_results"] = []
                trace[tr_idx]["tool_results"].append({
                    "tool_name": tr.tool_name if hasattr(tr, "tool_name") else tr.get("tool_name", ""),
                    "status": status,
                })
            tr_idx += 1

    return trace


# ============================================================
# 模块级重导出（observe / judge / create_llm / POIQuery）
# ============================================================

# plan / observe / judge / POIQuery 已在顶部导入并重导出，
# 供同包其它模块与 unit 测试直接引用本模块符号。
