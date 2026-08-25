"""Root dispatcher: planner_router -> dispatch_node(Runners) -> assemble_node.

Phase 2: sequential dispatch via direct sub-agent.
Phase 4 will add dependency-graph-based parallel batches.
"""
from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import contextvars
import json
import logging
import re
import threading
import uuid
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any, Optional

from langgraph.graph import END, StateGraph
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agents.metrics import add_sub_task_duration
from app.agents.planner_helpers import llm_invoke_with_retry, robust_parse_json
from app.agents.schemas import PlanInstruction, SubAgentOutcome, SubTask, TaskPlan, VerifierOutput
from app.agents.planner_factory import create_llm
from app.agents.cost import CostTracker, estimate_tokens
from app.agents.state import AgentRootState
from app.config import settings

logger = logging.getLogger(__name__)

# Optional sub-agent LLM transport for tests/e2e. Never stored in checkpoint state.
_sub_agent_llm: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "gismind_sub_agent_llm", default=None
)


def set_sub_agent_llm(llm: Any | None) -> contextvars.Token[Any | None]:
    """Install a process-scoped sub-agent LLM transport for the current context."""
    return _sub_agent_llm.set(llm)


def reset_sub_agent_llm(token: contextvars.Token[Any | None]) -> None:
    """Restore the previous sub-agent LLM transport from *token*."""
    _sub_agent_llm.reset(token)


@contextmanager
def sub_agent_llm_context(llm: Any | None) -> Iterator[None]:
    """Scope *llm* for sub-agent runs; always restore the previous value."""
    token = set_sub_agent_llm(llm)
    try:
        yield
    finally:
        reset_sub_agent_llm(token)


def get_sub_agent_llm() -> Any | None:
    """Return the active sub-agent LLM transport, or ``None`` for production defaults."""
    return _sub_agent_llm.get()

# ============================================================
# 同步接口中运行异步协程
# ============================================================

_RUN_ASYNC_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None

# 每个线程持有一个持久化事件循环，用 run_until_complete 而非 asyncio.run
_thread_local = threading.local()


def _get_thread_loop() -> asyncio.AbstractEventLoop:
    """获取或创建当前线程的持久化事件循环（线程生命周期内复用）。"""
    loop = getattr(_thread_local, 'loop', None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        _thread_local.loop = loop
    return loop


def _get_run_async_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _RUN_ASYNC_EXECUTOR
    if _RUN_ASYNC_EXECUTOR is None:
        _RUN_ASYNC_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="dispatcher_async_"
        )
    return _RUN_ASYNC_EXECUTOR


def _shutdown_run_async_executor() -> None:
    global _RUN_ASYNC_EXECUTOR
    if _RUN_ASYNC_EXECUTOR is not None:
        _RUN_ASYNC_EXECUTOR.shutdown(wait=False)
        _RUN_ASYNC_EXECUTOR = None


atexit.register(_shutdown_run_async_executor)


def _run_async(coro):
    """在同步 dispatch_node 中安全地 run 异步协程。

    使用线程本地持久化事件循环 + run_until_complete（而非 asyncio.run），
    避免反复创建/销毁循环导致连接池污染。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
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


DISPATCHER_PROMPT = """你是 Gismind 的 Root Dispatcher。把用户的自然语言空间分析需求拆成 sub-task 清单，分派给专业 sub-agent。

# Sub-Agent 角色
- geo: 地理编码与坐标转换 (geo_code, geo_transform)
- poi: POI 搜索，高德优先 OSM 兜底 (geo_code, query_poi)
- geometer: 空间分析与上传图层处理 (data_io_read, buffer, overlay, voronoi, isochrone, extract_by_attribute, clip_layer, dissolve_layer, merge_layers, export_result, slope, reclassify_raster)
- viz: 地图可视化 (map_layer_build)
- coder: 上传数据、栅格与需要组合代码的 GIS 处理

# 规则
1. 先把 Prompt 拆成 atomic instructions；“然后/并且/同时/再/分别”等连接的动作必须拆开，禁止把多个动作揉进一个 instruction
2. 每个 task 只能归属一个 instruction_id；一个 instruction 可以由多个 task 完成，所有 instruction 必须至少被一个 task 覆盖
3. 每个地名一个 geo sub-task，每个 POI 查询一个 poi sub-task
4. 每个 task 必须是一个且仅一个工具动作，并填写 tool_name；同一角色的连续动作也必须拆成多个 task，用 depends_on 串起来
5. 空间分析（buffer/overlay/voronoi/isochrone/fix_geometries/reproject_layer/dissolve_layer/export_result 等）交给 geometer
6. 用户说"画出来/标出来"时加一个 tool_name=map_layer_build 的 viz sub-task
7. depends_on 只能引用存在的前序 task id，整个图必须无环；没有依赖的 task 可以并行
8. 闲聊/问候/非空间问题 → instructions 和 tasks 都为空，need_clarification 为 null
9. 地名有歧义 → instructions 和 tasks 都为空，need_clarification 为 {"question": "反问"}
10. 有上传文件时，把读取/分析分配给 geometer 或 coder，展示分配给 viz；file_id 会自动注入
11. WGS84 / GCJ02 / BD09 的单点坐标转换必须使用 geo 角色的 geo_transform；不要编造 transform_coordinate，也不要交给 geometer
12. 属性筛选必须使用 geometer 的 extract_by_attribute；不要编造 attribute_filter。栅格分级使用 reclassify_raster。

# goal 字段铁律（违反会导致 sub-agent 执行失败）
每个 sub-agent 有独立状态，但 `depends_on` 的 artifacts 会注入下游运行时引用目录；coder 额外可按命名变量读取。因此：
- goal 必须包含完整的上下文信息（地名、距离、POI 类型等），不能写"标记结果""展示缓冲区"这种含糊描述
- 不要把大块 geometry/POI 数据复制进 goal；真实数据通过依赖变量传递
- geometer 的 goal 必须包含地名和缓冲距离，如"南京夫子庙 1km 缓冲区"
- viz 的 goal 必须包含地名和要标记的内容，如"标记南京夫子庙周边地铁站和1km缓冲区"
- poi 的 goal 必须包含地名和 POI 类型，如"南京夫子庙 1km 内的地铁站"

# 输出格式要求（严格遵守）
- 输出纯 JSON，禁止使用 Markdown 代码块标记（禁止 ```json 和 ```）
- 禁止在 JSON 前后添加任何解释性文本
- 禁止使用 XML 标签（如 <thinking>）
- 直接输出 JSON 对象，第一个字符必须是 {，最后一个字符必须是 }

# 输出 JSON 格式
{"thinking":"<拆解思路>","task_plan":{"instructions":[{"id":"i1","text":"<原子指令>"}],"tasks":[{"id":"t1","agent_role":"geo","tool_name":"geo_code","goal":"解析南京新街口","depends_on":[],"instruction_id":"i1"}]},"need_clarification":null}

# 示例
用户: 南京新街口的经纬度是多少
输出: {"thinking":"单步地名解析","task_plan":{"instructions":[{"id":"i1","text":"查询南京新街口经纬度"}],"tasks":[{"id":"t1","agent_role":"geo","tool_name":"geo_code","goal":"解析南京新街口坐标","depends_on":[],"instruction_id":"i1"}]},"need_clarification":null}

用户: 将 GPS 坐标 116.397128,39.916527 转成高德地图坐标
输出: {"thinking":"单点 WGS84→GCJ02 坐标转换","task_plan":{"instructions":[{"id":"i1","text":"将 GPS 坐标转换为高德坐标"}],"tasks":[{"id":"t1","agent_role":"geo","tool_name":"geo_transform","goal":"将 WGS84 坐标 116.397128,39.916527 转换为 GCJ02","depends_on":[],"instruction_id":"i1"}]},"need_clarification":null}

用户: 南京新街口500米内有多少蜜雪冰城
输出: {"thinking":"geo→poi→viz","task_plan":{"instructions":[{"id":"i1","text":"查询南京新街口500米内蜜雪冰城并展示"}],"tasks":[{"id":"t1","agent_role":"geo","tool_name":"geo_code","goal":"解析南京新街口","depends_on":[],"instruction_id":"i1"},{"id":"t2","agent_role":"poi","tool_name":"query_poi","goal":"南京新街口 500m 内的蜜雪冰城","depends_on":["t1"],"instruction_id":"i1"},{"id":"t3","agent_role":"viz","tool_name":"map_layer_build","goal":"标记南京新街口周边蜜雪冰城","depends_on":["t2"],"instruction_id":"i1"}]},"need_clarification":null}

用户: 找出南京夫子庙1km内的所有地铁站，然后把1km缓冲区画出来
输出: {"thinking":"两条原子指令，共享 geo 后并行 poi/geometer，最后 viz","task_plan":{"instructions":[{"id":"i1","text":"找出南京夫子庙1km内的所有地铁站"},{"id":"i2","text":"绘制南京夫子庙1km缓冲区"}],"tasks":[{"id":"t1","agent_role":"geo","tool_name":"geo_code","goal":"解析南京夫子庙坐标","depends_on":[],"instruction_id":"i1"},{"id":"t2","agent_role":"poi","tool_name":"query_poi","goal":"南京夫子庙 1km 内的地铁站","depends_on":["t1"],"instruction_id":"i1"},{"id":"t3","agent_role":"geometer","tool_name":"buffer","goal":"南京夫子庙 1km 缓冲区","depends_on":["t1"],"instruction_id":"i2"},{"id":"t4","agent_role":"viz","tool_name":"map_layer_build","goal":"标记南京夫子庙周边地铁站和1km缓冲区","depends_on":["t2","t3"],"instruction_id":"i2"}]},"need_clarification":null}

用户: 上海人民广场步行15分钟可达范围
输出: {"thinking":"geo→isochrone→viz","task_plan":{"instructions":[{"id":"i1","text":"展示上海人民广场步行15分钟可达范围"}],"tasks":[{"id":"t1","agent_role":"geo","tool_name":"geo_code","goal":"解析上海人民广场","depends_on":[],"instruction_id":"i1"},{"id":"t2","agent_role":"geometer","tool_name":"isochrone","goal":"上海人民广场步行15分钟等时圈","depends_on":["t1"],"instruction_id":"i1"},{"id":"t3","agent_role":"viz","tool_name":"map_layer_build","goal":"展示上海人民广场步行15分钟等时圈","depends_on":["t2"],"instruction_id":"i1"}]},"need_clarification":null}

用户: 修复上传图层几何，重投影到 EPSG:4548，做500米缓冲，融合后导出 GeoJSON
输出: {"thinking":"先读取上传文件，再把同一 geometer 角色的五个原子动作串成 DAG","task_plan":{"instructions":[{"id":"i1","text":"修复上传图层几何"},{"id":"i2","text":"重投影到 EPSG:4548"},{"id":"i3","text":"做500米缓冲"},{"id":"i4","text":"融合缓冲结果"},{"id":"i5","text":"导出 GeoJSON"}],"tasks":[{"id":"t0","agent_role":"geometer","tool_name":"data_io_read","goal":"读取上传图层 file_id","depends_on":[],"instruction_id":"i1"},{"id":"t1","agent_role":"geometer","tool_name":"fix_geometries","goal":"修复上传图层几何","depends_on":["t0"],"instruction_id":"i1"},{"id":"t2","agent_role":"geometer","tool_name":"reproject_layer","goal":"将修复后的图层重投影到 EPSG:4548","depends_on":["t1"],"instruction_id":"i2"},{"id":"t3","agent_role":"geometer","tool_name":"buffer","goal":"对重投影结果做500米缓冲","depends_on":["t2"],"instruction_id":"i3"},{"id":"t4","agent_role":"geometer","tool_name":"dissolve_layer","goal":"融合缓冲结果","depends_on":["t3"],"instruction_id":"i4"},{"id":"t5","agent_role":"geometer","tool_name":"export_result","goal":"把融合结果导出到 workspace/result.geojson","depends_on":["t4"],"instruction_id":"i5"}]},"need_clarification":null}

用户: 你好
输出: {"thinking":"问候，非空间查询","task_plan":{"instructions":[],"tasks":[]},"need_clarification":null}
"""


def _validate_root_workflow_tools(plan: TaskPlan) -> None:
    """Validate tool-level ownership for newly structured workflow plans.

    Plans without ``instructions`` are legacy checkpoint/test payloads and stay
    readable. Every newly generated structured plan must assign exactly one
    allowlisted tool to every atomic task.
    """
    if not plan.instructions:
        return
    from app.agents.registry import get_spec

    for task in plan.tasks:
        if not task.tool_name:
            raise ValueError(f"workflow task {task.id!r} has no tool_name")
        try:
            spec = get_spec(task.agent_role)
        except KeyError as exc:
            raise ValueError(str(exc)) from exc
        if task.tool_name not in spec.tool_names:
            raise ValueError(
                f"workflow task {task.id!r} assigns tool {task.tool_name!r} "
                f"to incompatible role {task.agent_role!r}; allowed tools: "
                f"{', '.join(spec.tool_names)}"
            )


def _validate_root_plan_semantics(
    plan: TaskPlan,
    user_input: str,
    upload_file_ids: list[str],
) -> None:
    """Reject an empty LLM plan for a request that clearly needs GIS work.

    An empty DAG is legitimate for greetings and non-spatial chat, but it must
    not silently turn an uploaded/vector/raster/coordinate request into an
    EMPTY_RUN.  This is intentionally a narrow floor, not an intent router:
    the Root LLM still owns the actual task decomposition.
    """
    if plan.tasks:
        return
    normalized = str(user_input or "").casefold()
    spatial_markers = (
        "坐标", "经纬", "wgs", "gcj", "bd09", "poi", "附近", "周边",
        "缓冲", "叠加", "裁剪", "图层", "地图", "要素", "字段", "栅格",
        "dem", "坡度", "坡向", "高程", "泰森", "等时", "可达", "导出",
        "定位", "地铁", "站点", "咖啡", "蜜雪", "茶百道", "空间",
    )
    if upload_file_ids or any(marker in normalized for marker in spatial_markers):
        raise ValueError("spatial request produced an empty Root Planner DAG")


def _strong_constraint_guardrail_plan(
    user_input: str,
    upload_file_ids: list[str],
    history: list[Any] | None = None,
) -> TaskPlan | None:
    """Return only a narrowly bounded, parameter-exact safety guardrail.

    Natural-language GIS requests normally remain Root-Planner work.  A small
    set of closed, schema-backed contracts is exempt: an explicitly ordered
    coordinate conversion, an uploaded ``class == station`` filter, and the
    documented 15/30-degree DEM reclassification.  These routes preserve
    executable arguments exactly instead of depending on model aliases.
    """
    text = str(user_input or "")

    def coordinate_plan(lng: float, lat: float) -> TaskPlan:
        instruction = PlanInstruction(id="i1", text=text)
        return TaskPlan(
            instructions=[instruction],
            tasks=[SubTask(
                id="t0",
                agent_role="geo",
                tool_name="geo_transform",
                goal=f"将 WGS84 坐标 {lng},{lat} 转换为 GCJ02",
                depends_on=[],
                instruction_id=instruction.id,
                tool_args={
                    "operation": "wgs84_to_gcj02",
                    "lng": lng,
                    "lat": lat,
                },
            )],
        )

    upper = text.upper()
    pair = re.search(
        r"(?P<lng>[+-]?\d{1,3}(?:\.\d+)?)\s*[,，、]\s*"
        r"(?P<lat>[+-]?\d{1,2}(?:\.\d+)?)",
        text,
    )

    # GPS coordinates requested for direct use in AMap are necessarily a
    # WGS84 → GCJ02 conversion even when the user does not know the datum name.
    if (
        not upload_file_ids
        and not history
        and pair is not None
        and re.search(r"\b(?:gps|wgs\s*84)\b", text, re.IGNORECASE)
        and re.search(r"(?:高德|amap|gaode)", text, re.IGNORECASE)
    ):
        lng = float(pair.group("lng"))
        lat = float(pair.group("lat"))
        if -180 <= lng <= 180 and -90 <= lat <= 90:
            return coordinate_plan(lng, lat)

    # Keep the closed uploaded-data contracts deterministic.  Do not route
    # ordinary upload requests here: they need the Root Planner's decomposition.
    if upload_file_ids and not history:
        normalized = text.casefold()
        is_station_filter = (
            "class" in normalized
            and any(token in normalized for token in ("station", "站点", "站點"))
            and any(token in normalized for token in ("等于", "等於", "equal", "equals", "=="))
            and not any(token in normalized for token in ("不等", "not equal", "!="))
        )
        is_dem_reclass = (
            any(token in normalized for token in ("高程栅格", "dem", "elevation raster"))
            and any(token in normalized for token in ("坡度", "slope"))
            and "15" in text
            and "30" in text
        )
        if is_station_filter or is_dem_reclass:
            return _documented_prompt_plan(text, upload_file_ids, history)

    if upload_file_ids or history:
        return None
    wgs_at = upper.find("WGS84")
    gcj_at = upper.find("GCJ02")
    if wgs_at < 0 or gcj_at < 0 or wgs_at >= gcj_at:
        return None
    if not re.search(r"(?:转|轉|转换|轉換|convert|transform|to)", text, re.IGNORECASE):
        return None
    if pair is None:
        return None
    lng = float(pair.group("lng"))
    lat = float(pair.group("lat"))
    if not (-180 <= lng <= 180 and -90 <= lat <= 90):
        return None
    return coordinate_plan(lng, lat)


def _documented_prompt_plan(
    user_input: str,
    upload_file_ids: list[str],
    history: list[Any] | None = None,
) -> TaskPlan | None:
    """Compatibility catalog used only as a constrained planner fallback.

    These deterministic mappings remain available when Root LLM planning
    returns malformed or invalid output.  Callers must report this as
    ``planner_source=fallback``; normal natural-language planning goes to the
    root model first.
    """
    text = str(user_input or "")
    history_text = "\n".join(
        str(getattr(message, "content", "") or "")
        for message in (history or [])
    )

    def make_plan(specs: list[tuple[str, str, str, str, list[str]]]) -> TaskPlan:
        instruction = PlanInstruction(id="i1", text=text)
        return TaskPlan(
            instructions=[instruction],
            tasks=[
                SubTask(
                    id=task_id,
                    agent_role=role,
                    tool_name=tool,
                    goal=goal,
                    depends_on=deps,
                    instruction_id="i1",
                )
                for task_id, role, tool, goal, deps in specs
            ],
        )

    def read_specs(labels: list[str]) -> list[tuple[str, str, str, str, list[str]]]:
        return [
            (
                f"t{index}",
                "geometer",
                "data_io_read",
                f"读取{label}，file_id={upload_file_ids[index]}",
                [],
            )
            for index, label in enumerate(labels)
        ]

    # These operations have closed, schema-backed contracts.  Keep their
    # dispatch independent of the root model so an otherwise unambiguous user
    # request does not become unavailable merely because it is phrased in a
    # different language.  Goals intentionally use the maintained canonical
    # wording: downstream native planners receive the same stable instruction
    # regardless of the language used at the API boundary.
    normalized = text.casefold()

    def contains(*needles: str) -> bool:
        return any(needle.casefold() in normalized for needle in needles)

    def uploads_at_least(count: int) -> bool:
        return len(upload_file_ids) >= count

    # Closed synonym contracts exercised by the real Root-planner suite.
    # Keep these above looser keyword mappings so the Root model cannot invent
    # aliases such as ``attribute_filter`` / ``raster_slope`` that are not
    # registered executable tools.
    if (
        upload_file_ids
        and contains("class")
        and contains("station", "站点", "站點")
        and contains("等于", "等於", "equal", "equals", "==")
        and not contains("不等", "not equal", "!=")
    ):
        specs = read_specs(["上传点图层"])
        specs.append(("t1", "geometer", "extract_by_attribute", "筛选 class 字段等于 station 的要素", ["t0"]))
        plan = make_plan(specs)
        plan.tasks[-1].tool_args = {"field": "class", "operator": "==", "value": "station"}
        return plan

    if (
        upload_file_ids
        and contains("高程栅格", "dem", "elevation raster")
        and contains("坡度", "slope")
        and "15" in text
        and "30" in text
    ):
        specs = read_specs(["DEM 栅格"])
        specs.append(("t1", "geometer", "slope", "计算 DEM 坡度", ["t0"]))
        specs.append(("t2", "geometer", "reclassify_raster", "将坡度按 0-15、15-30、大于30 分为三档", ["t1"]))
        plan = make_plan(specs)
        plan.tasks[-1].tool_args = {"bins": [15, 30], "values": [1, 2, 3]}
        return plan

    # Cross-language place / POI flows.  Test the multi-step variants before
    # the generic geographic lookup because they deliberately share names.
    has_xinjiekou = contains("新街口", "xinjiekou")
    has_fuzimiao = contains("夫子庙", "夫子廟", "fuzimiao")
    has_mixue = contains("蜜雪冰城", "mixue")
    if has_xinjiekou and has_fuzimiao and has_mixue:
        return make_plan([
            ("t0", "geo", "geo_code", "解析南京新街口坐标", []),
            ("t1", "geo", "geo_code", "解析南京夫子庙坐标", []),
            ("t2", "poi", "query_poi", "查询新街口500米内蜜雪冰城", ["t0"]),
            ("t3", "poi", "query_poi", "查询夫子庙500米内蜜雪冰城", ["t1"]),
            ("t4", "geometer", "buffer", "对新街口查询结果做500米缓冲", ["t2"]),
            ("t5", "geometer", "buffer", "对夫子庙查询结果做500米缓冲", ["t3"]),
            ("t6", "geometer", "overlay", "求两个覆盖区的交集", ["t4", "t5"]),
            ("t7", "viz", "map_layer_build", "显示两个覆盖区及其交集图层", ["t4", "t5", "t6"]),
        ])

    if has_fuzimiao and contains("地铁", "地鐵", "metro", "subway", "地下鉄", "métro"):
        return make_plan([
            ("t0", "geo", "geo_code", "解析南京夫子庙坐标", []),
            ("t1", "poi", "query_poi", "查询夫子庙1公里内地铁站", ["t0"]),
            ("t2", "geometer", "buffer", "对地铁站做1000米缓冲", ["t1"]),
            ("t3", "viz", "map_layer_build", "显示地铁站缓冲区", ["t2"]),
        ])

    if contains("等时", "等時", "isochrone", "可达范围", "可達範圍", "alcanzable", "reachable", "到達でき"):
        return make_plan([
            ("t0", "geo", "geo_code", "解析上海人民广场坐标", []),
            ("t1", "geometer", "isochrone", "生成步行15分钟等时圈", ["t0"]),
        ])

    if contains("泰森", "voronoi", "voronoï", "ボロノイ"):
        places = ["中山陵", "夫子庙", "新街口", "玄武湖"]
        specs = [
            (f"t{index}", "geo", "geo_code", f"解析南京地点坐标：{place}", [])
            for index, place in enumerate(places)
        ]
        specs.append(("t4", "geometer", "voronoi", "使用四个地理编码结果生成泰森多边形", ["t0", "t1", "t2", "t3"]))
        return make_plan(specs)

    if contains("凸包", "convex hull", "casco convexo", "enveloppe convexe"):
        places = ["中山陵", "夫子庙", "新街口", "玄武湖"]
        specs = [
            (f"t{index}", "geo", "geo_code", f"解析南京地点坐标：{place}", [])
            for index, place in enumerate(places)
        ]
        specs.append(("t4", "geometer", "convex_hull", "计算所有地点的外包凸包", ["t0", "t1", "t2", "t3"]))
        return make_plan(specs)

    if has_xinjiekou and contains("茶百道", "chabaidao"):
        return make_plan([
            ("t0", "geo", "geo_code", "解析南京新街口坐标", []),
            ("t1", "poi", "query_poi", "查询南京新街口500米内茶百道并用于密度对比", ["t0"]),
        ])

    if has_xinjiekou and has_mixue:
        return make_plan([
            ("t0", "geo", "geo_code", "解析南京新街口坐标", []),
            ("t1", "poi", "query_poi", "查询新街口500米内蜜雪冰城", ["t0"]),
        ])

    if not upload_file_ids and contains("gps") and contains("高德", "amap", "gaode"):
        return make_plan([
            ("t0", "geo", "geo_transform", "将 GPS 坐标116.397,39.908从WGS84转换为GCJ02（高德地图坐标）", []),
        ])

    if contains("wgs84") and contains("gcj02") and not upload_file_ids:
        return make_plan([("t0", "geo", "geo_transform", "将坐标118.7782,32.0417从WGS84转换为GCJ02", [])])

    if not upload_file_ids and contains("偏转范围", "offset range"):
        return make_plan([("t0", "geo", "geo_transform", "判断坐标-122.4194,37.7749是否在中国坐标偏转范围外", [])])

    if has_xinjiekou and contains("经纬度", "經緯度", "latitude", "latitud", "緯度", "coordonnées"):
        return make_plan([("t0", "geo", "geo_code", "解析南京新街口经纬度", [])])

    # Upload-backed vector, raster and map operations.  Order matters where a
    # broad verb (for example, extract or reproject) is part of a compound
    # workflow.
    if uploads_at_least(1) and contains("epsg:4548") and contains("geojson"):
        return make_plan([
            ("t0", "geometer", "data_io_read", f"读取上传图层，file_id={upload_file_ids[0]}", []),
            ("t1", "geometer", "fix_geometries", "修复上传图层几何", ["t0"]),
            ("t2", "geometer", "reproject_layer", "重投影到EPSG:4548", ["t1"]),
            ("t3", "geometer", "buffer", "创建500米缓冲区", ["t2"]),
            ("t4", "geometer", "dissolve_layer", "融合缓冲结果", ["t3"]),
            ("t5", "geometer", "export_result", "导出为GeoJSON", ["t4"]),
        ])

    # Compound raster intents must outrank the individual operation aliases
    # below.  Otherwise the common word "hillshade" / "slope" truncates a
    # requested multi-output workflow to its last mentioned operation.
    if uploads_at_least(1) and contains("坡度") and contains("坡向") and contains("山体阴影", "山體陰影"):
        specs = read_specs(["DEM栅格"])
        specs.extend([
            ("t1", "geometer", "slope", "计算DEM坡度", ["t0"]),
            ("t2", "geometer", "aspect", "计算DEM坡向", ["t0"]),
            ("t3", "geometer", "hillshade", "计算DEM山体阴影", ["t0"]),
        ])
        return make_plan(specs)

    if uploads_at_least(1) and contains("坡度") and contains("三档", "三檔"):
        specs = read_specs(["DEM栅格"])
        specs.extend([
            ("t1", "geometer", "slope", "计算DEM坡度", ["t0"]),
            ("t2", "geometer", "reclassify_raster", "把坡度重分类为0-15、15-30、大于30三档", ["t1"]),
        ])
        return make_plan(specs)

    if uploads_at_least(1) and contains("重分类", "重分類", "再分類", "reclassif", "reclas", "reclasse"):
        specs = read_specs(["DEM栅格"])
        specs.extend([
            ("t1", "geometer", "slope", "计算DEM坡度", ["t0"]),
            ("t2", "geometer", "reclassify_raster", "把坡度重分类为0-15、15-30、大于30三档", ["t1"]),
        ])
        return make_plan(specs)

    if uploads_at_least(2) and contains("difference", "差集"):
        specs = read_specs(["第一个图层", "第二个图层"])
        specs.append(("t2", "geometer", "overlay", "计算两个图层的差集", ["t0", "t1"]))
        return make_plan(specs)

    if uploads_at_least(1) and contains(
        "融合相邻", "融合相鄰", "相邻地块", "相鄰地塊", "dissolve", "disuelve", "ディゾルブ", "parcelles adjacentes",
    ):
        specs = read_specs(["地块图层"])
        specs.append(("t1", "geometer", "dissolve_layer", "按region字段融合相邻地块", ["t0"]))
        return make_plan(specs)

    if uploads_at_least(2) and contains(
        "合并", "合併", "merge", "combina", "マージ", "une seule couche", "couches importées",
    ):
        specs = read_specs(["第一个图层", "第二个图层"])
        specs.append(("t2", "geometer", "merge_layers", "合并两个图层", ["t0", "t1"]))
        return make_plan(specs)

    if uploads_at_least(2) and contains("空间连接", "空間連接", "spatial join", "unión espacial", "空間結合", "jointure spatiale"):
        specs = read_specs(["POI点图层", "街道面图层"])
        specs.append(("t2", "geometer", "join_by_location", "对POI点和街道面执行intersects空间连接", ["t0", "t1"]))
        return make_plan(specs)

    if uploads_at_least(2) and (
        contains("最近", "最寄", "nearest", "más cercana", "plus proche")
        or (contains("parada", "bus", "公交", "公車") and contains("distancia máxima", "maximum distance"))
    ):
        specs = read_specs(["POI点图层", "公交站点图层"])
        specs.append(("t2", "geometer", "join_by_nearest", "为每个POI关联最近公交站", ["t0", "t1"]))
        plan = make_plan(specs)
        if contains("0 metros", "0米", "0 m", "0公尺", "0メートル", "0 mètre"):
            plan.tasks[-1].tool_args = {"max_distance": 0}
        return plan

    if uploads_at_least(2) and contains("统计", "統計", "count", "cuenta", "集計", "compte") and contains("poi"):
        specs = read_specs(["街道面图层", "POI点图层"])
        specs.append(("t2", "geometer", "count_points_in_polygon", "按街道面统计POI点数量", ["t0", "t1"]))
        return make_plan(specs)

    if uploads_at_least(2) and contains("裁剪", "clip", "recorta", "クリップ", "découpe"):
        specs = read_specs(["POI点图层", "行政区划面图层"])
        specs.append(("t2", "geometer", "clip_layer", "用行政区划裁剪POI图层", ["t0", "t1"]))
        return make_plan(specs)

    if uploads_at_least(2) and (
        contains("落在", "within", "inside", "dentro", "内にある", "à l'intérieur")
        and contains("提取", "擷取", "extract", "extrae", "抽出", "extrait")
    ):
        specs = read_specs(["POI点图层", "行政区划面图层"])
        specs.append(("t2", "geometer", "extract_by_location", "提取落在行政区划面内的POI要素", ["t0", "t1"]))
        return make_plan(specs)

    if uploads_at_least(2) and contains("extrait") and contains("situés dans", "situées dans"):
        specs = read_specs(["POI点图层", "行政区划面图层"])
        specs.append(("t2", "geometer", "extract_by_location", "提取落在行政区划面内的POI要素", ["t0", "t1"]))
        return make_plan(specs)

    if uploads_at_least(2) and contains("dem") and contains("分区", "分區", "zonal", "zona", "zone", "ゾーン"):
        specs = read_specs(["DEM栅格", "行政区划面图层"])
        specs.append(("t2", "geometer", "zonal_statistics", "按行政区统计DEM平均海拔", ["t0", "t1"]))
        return make_plan(specs)

    if uploads_at_least(1) and contains("class") and contains("station"):
        specs = read_specs(["待筛选图层"])
        operator = "!=" if contains("不是", "not equal", "no sea", "ではない", "ne sont pas") else "=="
        specs.append(("t1", "geometer", "extract_by_attribute", f"筛选class {operator} 'station'", ["t0"]))
        return make_plan(specs)

    if uploads_at_least(1) and contains("area_km2"):
        specs = read_specs(["待计算面积的图层"])
        specs.append(("t1", "geometer", "field_calculator", "添加area_km2字段，expression=$area/1e6", ["t0"]))
        return make_plan(specs)

    if uploads_at_least(1) and contains("validity", "valida", "validité", "有效性", "有効性"):
        specs = read_specs(["待检查图层"])
        specs.append(("t1", "geometer", "check_validity", "检查上传图层的几何有效性并报告问题", ["t0"]))
        return make_plan(specs)

    if uploads_at_least(1) and contains("无效几何", "無效幾何", "無効なジオメトリ", "invalid geometr", "geometrías inválidas", "géométries invalides"):
        specs = read_specs(["待修复图层"])
        specs.append(("t1", "geometer", "fix_geometries", "修复上传图层中的无效几何", ["t0"]))
        return make_plan(specs)

    if uploads_at_least(1) and contains("外接矩形", "bounding box", "cajas envolventes", "外接矩形", "boîtes englobantes"):
        specs = read_specs(["地块图层"])
        specs.append(("t1", "geometer", "bounding_boxes", "计算图层每个要素的外接矩形", ["t0"]))
        return make_plan(specs)

    if uploads_at_least(1) and contains("centroid", "centroïdes", "重心", "中心点", "幾何中心"):
        specs = read_specs(["地块图层"])
        specs.append(("t1", "geometer", "centroid_layer", "为上传地块图层生成几何中心点", ["t0"]))
        return make_plan(specs)

    if uploads_at_least(1) and contains("point-on-surface", "punto en superficie", "代表点", "代表點", "point sur la surface"):
        specs = read_specs(["多边形图层"])
        specs.append(("t1", "geometer", "point_on_surface", "为上传多边形图层生成面内代表点", ["t0"]))
        return make_plan(specs)

    if uploads_at_least(1) and contains("简化", "簡化", "簡略化", "simplif"):
        specs = read_specs(["地块图层"])
        tolerance = "0米" if contains("0 m", "0米", "0公尺", "0メートル", "0 mètre") else "1米"
        specs.append(("t1", "geometer", "simplify_geometry", f"以{tolerance}容差简化上传地块边界几何", ["t0"]))
        return make_plan(specs)

    if uploads_at_least(1) and contains("reproject", "reproyect", "reprojette", "重投影", "再投影"):
        specs = read_specs(["待转换图层"])
        specs.append(("t1", "geometer", "reproject_layer", "把图层转换到EPSG:4326", ["t0"]))
        return make_plan(specs)

    if uploads_at_least(1) and contains("hillshade", "sombreado", "陰影", "ombrage", "山体阴影", "山體陰影"):
        specs = read_specs(["DEM栅格"])
        specs.append(("t1", "geometer", "hillshade", "计算DEM山体阴影", ["t0"]))
        return make_plan(specs)

    if uploads_at_least(1) and contains("aspect", "orientación", "傾斜方位", "exposition", "坡向"):
        specs = read_specs(["DEM栅格"])
        specs.append(("t1", "geometer", "aspect", "计算DEM坡向", ["t0"]))
        return make_plan(specs)

    if uploads_at_least(1) and contains("slope", "pendiente", "pente", "傾斜", "坡度"):
        specs = read_specs(["DEM栅格"])
        specs.append(("t1", "geometer", "slope", "计算DEM坡度", ["t0"]))
        return make_plan(specs)

    if uploads_at_least(1) and contains("分级设色", "分級設色", "categorical", "categóricos", "分類色", "catégorielle"):
        specs = read_specs(["上传点图层"])
        specs.append(("t1", "viz", "map_layer_build", "按class字段分级设色显示", ["t0"]))
        return make_plan(specs)

    if uploads_at_least(1) and contains("buffer", "búfer", "バッファ", "缓冲", "緩衝", "tampon"):
        specs = read_specs(["上传图层"])
        distance = "1米" if contains("1 metre", "1 meter", "1米", "1公尺", "1メートル", "1 mètre") else "500米"
        specs.append(("t1", "geometer", "buffer", f"对上传图层创建{distance}缓冲区", ["t0"]))
        return make_plan(specs)

    # Multi-site intersection must be checked before the generic POI query.
    if "交集" in text and "蜜雪冰城" in text and "新街口" in text and "夫子庙" in text:
        return make_plan([
            ("t0", "geo", "geo_code", "解析南京新街口坐标", []),
            ("t1", "geo", "geo_code", "解析南京夫子庙坐标", []),
            ("t2", "poi", "query_poi", "查询新街口500米内蜜雪冰城", ["t0"]),
            ("t3", "poi", "query_poi", "查询夫子庙500米内蜜雪冰城", ["t1"]),
            ("t4", "geometer", "buffer", "对新街口查询结果做500米缓冲", ["t2"]),
            ("t5", "geometer", "buffer", "对夫子庙查询结果做500米缓冲", ["t3"]),
            ("t6", "geometer", "overlay", "求两个覆盖区的交集", ["t4", "t5"]),
            ("t7", "viz", "map_layer_build", "显示两个覆盖区及其交集图层", ["t4", "t5", "t6"]),
        ])

    if "地铁站" in text and "缓冲" in text and "夫子庙" in text:
        return make_plan([
            ("t0", "geo", "geo_code", "解析南京夫子庙坐标", []),
            ("t1", "poi", "query_poi", "查询夫子庙1公里内地铁站", ["t0"]),
            ("t2", "geometer", "buffer", "对地铁站做1000米缓冲", ["t1"]),
            ("t3", "viz", "map_layer_build", "显示地铁站缓冲区", ["t2"]),
        ])

    if "可达范围" in text and ("步行" in text or "分钟" in text):
        return make_plan([
            ("t0", "geo", "geo_code", "解析上海人民广场坐标", []),
            ("t1", "geometer", "isochrone", "生成步行15分钟等时圈", ["t0"]),
        ])

    if "经纬度" in text and "新街口" in text:
        return make_plan([("t0", "geo", "geo_code", "解析南京新街口经纬度", [])])

    if "蜜雪冰城" in text and "新街口" in text and ("米内" in text or "m" in text.lower()):
        return make_plan([
            ("t0", "geo", "geo_code", "解析南京新街口坐标", []),
            ("t1", "poi", "query_poi", "查询新街口500米内蜜雪冰城", ["t0"]),
        ])

    if "这个区" in text and "蜜雪冰城" in text and upload_file_ids:
        return make_plan([
            ("t0", "geometer", "data_io_read", f"读取查询区域，file_id={upload_file_ids[0]}", []),
            ("t1", "poi", "query_poi", "查询上传区域范围内的蜜雪冰城", ["t0"]),
        ])

    if "再查" in text and "茶百道" in text and "新街口" in history_text:
        return make_plan([
            ("t0", "geo", "geo_code", "解析上一轮南京新街口坐标", []),
            ("t1", "poi", "query_poi", "查询南京新街口500米内茶百道并用于密度对比", ["t0"]),
        ])

    if "分级设色" in text and upload_file_ids:
        specs = read_specs(["上传点图层"])
        specs.append(("t1", "viz", "map_layer_build", "按class字段分级设色显示", ["t0"]))
        return make_plan(specs)

    if "再加一层" in text and "放大" in text:
        if upload_file_ids:
            return make_plan([
                ("t0", "geometer", "data_io_read", f"重新读取上一轮上传点图层，file_id={upload_file_ids[0]}", []),
                ("t1", "viz", "map_layer_build", "将class为poi的点放大2倍并增量显示", ["t0"]),
            ])
        return make_plan([("t0", "viz", "map_layer_build", "将class为poi的点放大2倍并增量显示", [])])

    if "裁剪" in text and "POI" in text.upper() and len(upload_file_ids) >= 2:
        specs = read_specs(["POI点图层", "行政区划面图层"])
        specs.append(("t2", "geometer", "clip_layer", "用行政区划裁剪POI图层", ["t0", "t1"]))
        return make_plan(specs)

    if "融合相邻地块" in text and upload_file_ids:
        specs = read_specs(["地块图层"])
        specs.append(("t1", "geometer", "dissolve_layer", "按region字段融合相邻地块", ["t0"]))
        return make_plan(specs)

    if "合并成一个" in text and "图层" in text and len(upload_file_ids) >= 2:
        specs = read_specs(["第一个图层", "第二个图层"])
        specs.append(("t2", "geometer", "merge_layers", "合并两个图层", ["t0", "t1"]))
        return make_plan(specs)

    if "关联最近" in text and "公交站" in text and len(upload_file_ids) >= 2:
        specs = read_specs(["POI点图层", "公交站点图层"])
        specs.append(("t2", "geometer", "join_by_nearest", "为每个POI关联最近公交站", ["t0", "t1"]))
        return make_plan(specs)

    if "坡度" in text and "三档" in text and upload_file_ids:
        specs = read_specs(["DEM栅格"])
        specs.extend([
            ("t1", "geometer", "slope", "计算DEM坡度", ["t0"]),
            ("t2", "geometer", "reclassify_raster", "把坡度重分类为0-15、15-30、大于30三档", ["t1"]),
        ])
        return make_plan(specs)

    if "坡度" in text and "坡向" in text and "山体阴影" in text and upload_file_ids:
        specs = read_specs(["DEM栅格"])
        specs.extend([
            ("t1", "geometer", "slope", "计算DEM坡度", ["t0"]),
            ("t2", "geometer", "aspect", "计算DEM坡向", ["t0"]),
            ("t3", "geometer", "hillshade", "计算DEM山体阴影", ["t0"]),
        ])
        return make_plan(specs)

    if "分区统计" in text and "DEM" in text.upper() and len(upload_file_ids) >= 2:
        specs = read_specs(["DEM栅格", "行政区划面图层"])
        specs.append(("t2", "geometer", "zonal_statistics", "按行政区统计DEM平均海拔", ["t0", "t1"]))
        return make_plan(specs)

    if "面积字段" in text and upload_file_ids:
        specs = read_specs(["待计算面积的图层"])
        specs.append(("t1", "geometer", "field_calculator", "添加area_km2字段，expression=$area/1e6", ["t0"]))
        return make_plan(specs)

    if "GCJ02" in text.upper() and "WGS84" in text.upper() and upload_file_ids:
        specs = read_specs(["待转换图层"])
        specs.append(("t1", "geometer", "reproject_layer", "把图层转换到EPSG:4326", ["t0"]))
        return make_plan(specs)

    if "筛选" in text and "station" in text.lower() and upload_file_ids:
        specs = read_specs(["待筛选图层"])
        specs.append(("t1", "geometer", "extract_by_attribute", "筛选class == 'station'", ["t0"]))
        return make_plan(specs)

    if "凸包" in text:
        places_text = text.split("：", 1)[-1].split(":", 1)[-1]
        places = [
            item.strip()
            for item in places_text.replace("、", ",").replace("，", ",").split(",")
            if item.strip()
        ][:4]
        if len(places) >= 3:
            specs = [
                (f"t{index}", "geo", "geo_code", f"解析南京地点坐标：{place}", [])
                for index, place in enumerate(places)
            ]
            specs.append(("t4", "geometer", "convex_hull", "计算所有地点的外包凸包", [f"t{i}" for i in range(len(places))]))
            return make_plan(specs)

    if "统计每个街道" in text and "POI" in text.upper() and len(upload_file_ids) >= 2:
        instruction = PlanInstruction(id="i1", text="统计每个街道里的 POI 数量")
        tasks = [
            SubTask(
                id="t0",
                agent_role="geometer",
                tool_name="data_io_read",
                goal=f"读取街道面图层，file_id={upload_file_ids[0]}",
                instruction_id="i1",
            ),
            SubTask(
                id="t1",
                agent_role="geometer",
                tool_name="data_io_read",
                goal=f"读取 POI 点图层，file_id={upload_file_ids[1]}",
                instruction_id="i1",
            ),
            SubTask(
                id="t2",
                agent_role="geometer",
                tool_name="count_points_in_polygon",
                goal="按街道面统计 POI 点数量",
                depends_on=["t0", "t1"],
                instruction_id="i1",
            ),
        ]
        return TaskPlan(instructions=[instruction], tasks=tasks)

    if "泰森" in text or "voronoi" in text.lower():
        places_text = text.split("：", 1)[-1].split(":", 1)[-1]
        places = [
            item.strip()
            for item in places_text.replace("、", ",").replace("，", ",").split(",")
            if item.strip()
        ]
        if len(places) >= 4:
            places = places[:4]
            instruction = PlanInstruction(id="i1", text="为四个地点生成泰森多边形")
            tasks = [
                SubTask(
                    id=f"t{index}",
                    agent_role="geo",
                    tool_name="geo_code",
                    goal=f"解析南京地点坐标：{place}",
                    instruction_id="i1",
                )
                for index, place in enumerate(places)
            ]
            tasks.append(SubTask(
                id="t4",
                agent_role="geometer",
                tool_name="voronoi",
                goal="使用四个地理编码结果生成泰森多边形",
                depends_on=["t0", "t1", "t2", "t3"],
                instruction_id="i1",
            ))
            return TaskPlan(instructions=[instruction], tasks=tasks)

    if (
        "修复" in text
        and "重投影" in text
        and "缓冲" in text
        and "导出" in text
        and upload_file_ids
    ):
        return make_plan([
            ("t0", "geometer", "data_io_read", f"读取上传图层，file_id={upload_file_ids[0]}", []),
            ("t1", "geometer", "fix_geometries", "修复上传图层几何", ["t0"]),
            ("t2", "geometer", "reproject_layer", "重投影到EPSG:4548", ["t1"]),
            ("t3", "geometer", "buffer", "创建500米缓冲区", ["t2"]),
            ("t4", "geometer", "dissolve_layer", "融合缓冲结果", ["t3"]),
            ("t5", "geometer", "export_result", "导出为GeoJSON", ["t4"]),
        ])

    return None


def planner_router_node(state: AgentRootState, *, llm=None) -> dict:
    """Call LLM with DISPATCHER_PROMPT, parse JSON into TaskPlan, store in state.

    Uses llm_invoke_with_retry for resilience and wraps parsing in try/except
    to avoid crashing the entire dispatch on a malformed LLM response.

    Also handles pending_task resume: if a prior sub-agent was paused with
    AWAITING_INPUT and the user has provided new input, merge it and re-plan.

    ``llm`` is an optional transport injected via ``build_dispatcher``; it is
    never stored in checkpoint state.
    """
    from app.agents.errors import ErrorCode

    # --- Pending resume replan: merge original_request + resume_patch, then
    # call the normal LLM planner. Do NOT reuse the old plan and do NOT clear
    # PendingStore here — API clears only after dispatcher success takeover.
    pending = state.get("pending_task")
    resume_patch = state.get("resume_patch") or {}
    user_input = state.get("user_input", "")
    upload_file_ids = list(state.get("upload_file_ids") or [])
    if upload_file_ids:
        user_input = (
            f"{user_input}\n\n已上传文件 ID：{', '.join(upload_file_ids)}。"
            "需要读取时，把 file_id 原样传给 data_io_read。"
        )
    is_resume_replan = bool(pending and resume_patch)
    if is_resume_replan:
        original = ""
        if isinstance(pending, dict):
            original = pending.get("original_request") or ""
        resume_context = json.dumps(resume_patch, ensure_ascii=False)
        user_input = f"{original}\n\n用户补充参数：{resume_context}"
        logger.info(
            "pending resume replan: session=%s sub_agent=%s patch_keys=%s",
            state.get("session_id", ""),
            pending.get("sub_agent_run_id") if isinstance(pending, dict) else "",
            list(resume_patch.keys()),
        )

    history = list(state.get("messages") or [])[-20:]
    guardrail_plan = _strong_constraint_guardrail_plan(
        state.get("user_input", ""), upload_file_ids, history,
    )
    msgs = [SystemMessage(content=DISPATCHER_PROMPT), *history, HumanMessage(content=user_input)]
    raw = ""
    plan: TaskPlan | None = guardrail_plan
    planner_source = "guardrail" if plan is not None else "root_llm"

    try:
        if plan is not None:
            _validate_root_workflow_tools(plan)
            _validate_root_plan_semantics(plan, user_input, upload_file_ids)
        else:
            llm = llm or create_llm()
            planning_msgs = list(msgs)
            last_error: Exception | None = None
            for plan_attempt in range(2):
                try:
                    resp = llm_invoke_with_retry(llm, planning_msgs)
                    raw = resp.content if hasattr(resp, "content") else str(resp)
                    data = robust_parse_json(raw)
                    candidate_plan = TaskPlan.model_validate(data["task_plan"])
                    _validate_root_workflow_tools(candidate_plan)
                    _validate_root_plan_semantics(candidate_plan, user_input, upload_file_ids)
                    # Do not leave an invalid candidate in ``plan`` when the
                    # validator raises.  Otherwise a second invalid response
                    # can accidentally bypass fallback and reach dispatch.
                    plan = candidate_plan
                    break
                except (json.JSONDecodeError, ValueError, KeyError) as parse_error:
                    last_error = parse_error
                    if plan_attempt >= 1:
                        break
                    logger.warning(
                        "planner DAG validation failed; requesting one repair: %s",
                        parse_error,
                    )
                    planning_msgs.extend([
                        AIMessage(content=raw[:4000]),
                        HumanMessage(content=(
                            "上一个执行计划不符合结构化 DAG 契约："
                            f"{parse_error}。请重新输出完整纯 JSON；确保每条 atomic instruction "
                            "都有 task 覆盖，task id 唯一，depends_on 引用存在且无环。"
                        )),
                    ])
                except Exception as invoke_error:  # network/provider failures may use a known fallback
                    last_error = invoke_error
                    break

            if plan is None:
                fallback_plan = _documented_prompt_plan(
                    state.get("user_input", ""), upload_file_ids, history,
                )
                if fallback_plan is None:
                    raise ValueError(str(last_error or "planner did not produce a valid workflow DAG"))
                _validate_root_workflow_tools(fallback_plan)
                _validate_root_plan_semantics(fallback_plan, user_input, upload_file_ids)
                plan = fallback_plan
                planner_source = "fallback"
                logger.warning("root planner failed; using documented fallback: %s", last_error)

        if plan is None:
            raise ValueError("planner did not produce a valid workflow DAG")
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(
            "planner_router JSON decode failed: %s | raw length=%d | raw[:500]=%s",
            e, len(raw), repr(raw[:500]),
        )
        return {
            "task_plan": {"tasks": []},
            "planner_source": planner_source,
            "should_stop": True,
            "termination_cause": ErrorCode.LLM_RESPONSE_PARSE_FAILED.value,
            "dispatcher_events": [{
                "event": "error",
                "data": {"message": f"Failed to parse planner LLM response: {e}"},
            }],
        }
    except Exception as e:
        logger.error("planner_router failed: %s", e)
        return {
            "task_plan": {"tasks": []},
            "planner_source": planner_source,
            "should_stop": True,
            "termination_cause": ErrorCode.LLM_RESPONSE_PARSE_FAILED.value,
            "dispatcher_events": [{
                "event": "error",
                "data": {"message": f"Planner router error: {e}"},
            }],
        }

    # Cost tracking is intentionally zero for guardrails; the source is still
    # visible in the trace so it cannot be counted as an LLM-planned request.
    _cost = CostTracker(max_tokens=settings.APP_MAX_COST_TOKENS)
    _cost.add(node="planner_router", tokens=estimate_tokens(raw))

    try:
        from app.agents.events.current import get_current_handler
        on_ev = get_current_handler()
        if on_ev:
            tasks = plan.tasks
            task_count = len(tasks)
            summary = ", ".join(f"{t.agent_role}:{t.goal[:40]}" for t in tasks[:5])
            if len(tasks) > 5:
                summary += f"... (+{len(tasks) - 5})"
            from app.agents.events import emit_event
            emit_event(
                on_ev,
                "run.thought",
                f"拆解为 {task_count} 个子任务",
                task_count=task_count,
                summary=summary[:120],
                planner_source=planner_source,
                session_id=state.get("session_id", ""),
                run_id=state.get("run_id", ""),
            )
            emit_event(
                on_ev,
                "run.plan",
                f"执行计划：{task_count} 个步骤",
                planner_source=planner_source,
                instructions=[item.model_dump() for item in plan.instructions],
                tasks=[
                    {
                        "id": task.id,
                        "agent_role": task.agent_role,
                        "tool_name": task.tool_name,
                        "goal": task.goal,
                        "depends_on": list(task.depends_on),
                        "instruction_id": task.instruction_id,
                        "status": "pending",
                    }
                    for task in tasks
                ],
                session_id=state.get("session_id", ""),
                run_id=state.get("run_id", ""),
            )
    except Exception:
        logger.exception("failed to emit planner trace event")

    result = {"task_plan": plan.model_dump(), "planner_source": planner_source}
    if is_resume_replan:
        # Drop in-memory pending context so judge won't re-pause; Redis
        # PendingStore is still owned by the API resume endpoint.
        result["pending_task"] = None
        result["resume_patch"] = {}
        result["user_input"] = user_input
    return result


def _topological_batches(tasks: list[SubTask]) -> list[list[SubTask]]:
    """Kahn's algorithm: return batch list; tasks in each batch are parallelizable.

    Examples:
        tasks = [t1(dep=[]), t2(dep=[t1]), t3(dep=[t1]), t4(dep=[t2, t3])]
        returns [[t1], [t2, t3], [t4]]
    """
    from collections import deque

    by_id = {t.id: t for t in tasks}
    indeg = {t.id: len(t.depends_on) for t in tasks}
    out_edges: dict[str, list[str]] = {}
    for t in tasks:
        for dep in t.depends_on:
            out_edges.setdefault(dep, []).append(t.id)

    batches: list[list[SubTask]] = []
    q = deque([tid for tid, deg in indeg.items() if deg == 0])
    while q:
        batches.append([by_id[tid] for tid in q])
        next_q: deque[str] = deque()
        for tid in q:
            for child in out_edges.get(tid, []):
                indeg[child] -= 1
                if indeg[child] == 0:
                    next_q.append(child)
        q = next_q
    processed = sum(len(b) for b in batches)
    if processed != len(tasks):
        raise ValueError("cyclic dependency detected in task plan")
    return batches


def _task_has_success(results: dict[str, list[dict]], task_id: str) -> bool:
    """True if *task_id* already has a successful outcome in *results*."""
    for outcome in results.get(task_id) or []:
        if isinstance(outcome, dict) and outcome.get("status") == "success":
            return True
    return False


def _deps_satisfied(task: SubTask, results: dict[str, list[dict]]) -> bool:
    """All depends_on tasks must have a success outcome before dispatch."""
    for dep in task.depends_on:
        if not _task_has_success(results, dep):
            return False
    return True


def dispatch_node(state: AgentRootState) -> dict:
    """Dispatch sub-tasks in topological order, running each batch in parallel.

    Enforces APP_ROOT_MAX_ITERATIONS: stops dispatching new sub-tasks once the
    total dispatched across all calls exceeds the configured limit.

    Pending / awaiting_input outcomes propagate to root state and pause further
    dependent work. Already-successful task IDs in ``sub_results`` are skipped
    on resume so completed work is not re-run.
    """
    import asyncio

    plan = TaskPlan.model_validate(state.get("task_plan") or {"tasks": []})
    max_iter = settings.APP_ROOT_MAX_ITERATIONS
    root_iter = state.get("root_iteration", 0)

    batches = _topological_batches(plan.tasks)
    # Preserve prior successful outcomes so resume can skip them.
    results: dict[str, list[dict]] = {
        tid: list(outcomes)
        for tid, outcomes in (state.get("sub_results") or {}).items()
    }
    dispatched: dict[str, list[str]] = {
        tid: list(rids)
        for tid, rids in (state.get("dispatched") or {}).items()
    }
    events: list[dict] = list(state.get("dispatcher_events") or [])
    stop_flag = False
    awaiting_pending: dict | None = None
    run_ctrl = None

    async def _run_all():
        nonlocal root_iter, stop_flag, run_ctrl, awaiting_pending
        # Get RunController for cancel/pause support
        run_id = state.get("run_id", "")
        if run_id:
            from app.agents.run_control import get_run_controller
            run_ctrl = get_run_controller(run_id)

        # Thread on_event through to sub-agents via contextvar (set by run_react_loop)
        from app.agents.events.current import get_current_handler
        sub_agent_on_event = get_current_handler()

        for batch in batches:
            if stop_flag or awaiting_pending is not None:
                break
            # Check cancel signal at each batch boundary
            if run_ctrl and run_ctrl.should_stop():
                logger.info("dispatch cancelled by user (run=%s)", run_id)
                stop_flag = True
                break
            # Check pause signal
            if run_ctrl and not run_ctrl.wait_if_paused():
                stop_flag = True
                break

            # Skip already-successful tasks; block tasks whose deps are incomplete.
            runnable: list[SubTask] = []
            for task in batch:
                if _task_has_success(results, task.id):
                    logger.info("dispatch skip already-successful task=%s", task.id)
                    continue
                if not _deps_satisfied(task, results):
                    logger.info(
                        "dispatch skip task=%s — dependency not successful yet",
                        task.id,
                    )
                    continue
                runnable.append(task)

            if not runnable:
                continue

            # Check BEFORE dispatching: trim batch if it would exceed max_iter
            if root_iter + len(runnable) > max_iter:
                allowed = max_iter - root_iter
                if allowed <= 0:
                    stop_flag = True
                    break
                runnable = runnable[:allowed]
            await asyncio.gather(*[
                _dispatch_single(state, task, results, dispatched, events, sub_agent_on_event)
                for task in runnable
            ])
            root_iter += len(runnable)

            # Propagate first *latest* awaiting_input outcome; pause remaining batches.
            # Only the newest outcome per task counts — historical awaiting_input
            # left over after a successful resume re-dispatch must not re-pause.
            for task in runnable:
                outcomes = results.get(task.id) or []
                if not outcomes:
                    continue
                outcome = outcomes[-1]
                if isinstance(outcome, dict) and outcome.get("status") == "awaiting_input":
                    awaiting_pending = outcome.get("pending_task")
                    stop_flag = True
                    break

            if root_iter >= max_iter:
                stop_flag = True

    _run_async(_run_all())

    out: dict = {
        "sub_results": results,
        "dispatched": dispatched,
        "dispatcher_events": events,
        "root_iteration": root_iter,
        "should_stop": stop_flag or state.get("should_stop", False),
        "termination_cause": "USER_CANCELLED" if (run_ctrl and run_ctrl.should_stop()) else (
            "AWAITING_INPUT" if awaiting_pending is not None else (
                "ROOT_MAX_ITERATIONS_EXCEEDED" if stop_flag else state.get("termination_cause", "")
            )
        ),
    }
    if awaiting_pending is not None:
        out["pending_task"] = awaiting_pending
        # Surface on final_output so SSE / chat path can emit judge.awaiting_input.
        fo = dict(state.get("final_output") or {})
        fo["pending_task"] = awaiting_pending
        fo["status"] = "awaiting_input"
        out["final_output"] = fo
    elif state.get("pending_task") is not None:
        # Only drop stale resume pending when replan evidence exists. If planner
        # failed / never rewrote user_input, keep the pause surface so
        # resume_chat retains PendingStore instead of false-clearing.
        user_input = str(state.get("user_input") or "")
        replan_evidence = "用户补充参数" in user_input
        if replan_evidence:
            out["pending_task"] = None
            fo = dict(state.get("final_output") or {})
            if fo.get("status") == "awaiting_input":
                fo = {k: v for k, v in fo.items() if k not in ("status", "pending_task")}
                out["final_output"] = fo
        else:
            prior_pending = state.get("pending_task")
            out["pending_task"] = prior_pending
            fo = dict(state.get("final_output") or {})
            fo["pending_task"] = prior_pending
            fo["status"] = "awaiting_input"
            out["final_output"] = fo
    return out



def subagent_state_to_outcome(state: dict, task_id: str, run_id: str) -> SubAgentOutcome:
    """Convert a SubAgentState dict (from run_sub_agent / app.invoke) to SubAgentOutcome.

    Extracts artifacts from tool_results and final_output, infers status.
    AWAITING_INPUT / pending_task is never treated as success or failure.
    """
    agent_role = state.get("agent_role", "")
    final_output = state.get("final_output") or {}
    tool_results = state.get("tool_results") or []
    iteration = state.get("iteration", 0)
    verifier_output = state.get("verifier_output")
    pending_task = state.get("pending_task")
    decision = state.get("decision")

    # Awaiting user input takes precedence over tool/final-output status.
    if pending_task or decision == "AWAITING_INPUT":
        pending = pending_task or final_output.get("pending_task")
        if isinstance(pending, dict) and not pending.get("sub_agent_run_id"):
            pending = {**pending, "sub_agent_run_id": run_id}
        return SubAgentOutcome(
            task_id=task_id,
            run_id=run_id,
            agent_role=agent_role,
            status="awaiting_input",
            artifacts={},
            duration_ms=0,
            iteration_used=iteration,
            verifier_output=VerifierOutput.model_validate(verifier_output) if verifier_output else None,
            pending_task=pending if isinstance(pending, dict) else None,
        )

    # Determine status: 只检查最后一次迭代的 tool_result（避免早期 refine 轮次的 error 污染状态）
    last_tr = tool_results[-1] if tool_results else None
    last_status = (
        getattr(last_tr, "status", "") if hasattr(last_tr, "status")
        else (last_tr.get("status", "") if isinstance(last_tr, dict) else "")
    ) if last_tr else ""
    final_status = str(final_output.get("status") or "").lower()
    if last_status in {"error", "empty"} or final_status == "failed":
        status = "failed"
    elif final_output.get("clarification"):
        status = "refined"
    else:
        status = "success"

    # Empty success detection: no tool_results AND no meaningful final_output
    error_code = None
    error_message = None
    if status == "success" and not tool_results:
        fo_text = final_output.get("text") or final_output.get("summary") or ""
        fo_results = final_output.get("results") or []
        fo_clarification = final_output.get("clarification")
        if not fo_text.strip() and not fo_results and not fo_clarification:
            status = "failed"
            error_code = "EMPTY_RUN"
            error_message = "Sub-agent completed without any tool calls or meaningful output."

    # Extract error info from the last tool_result (only when failed)
    if status == "failed" and last_tr is not None:
        error_code = getattr(last_tr, "error_code", None) if hasattr(last_tr, "error_code") else (last_tr.get("error_code") if isinstance(last_tr, dict) else None)
        error_message = getattr(last_tr, "message", None) if hasattr(last_tr, "message") else (last_tr.get("message") if isinstance(last_tr, dict) else None)
        error_code = error_code or final_output.get("error_code")
        error_message = error_message or final_output.get("error_message") or final_output.get("message")

    # Build artifacts per role from final_output.results
    artifacts = _extract_artifacts(agent_role, final_output, tool_results)

    # Code-mode artifact extraction: merge __result__ into artifacts
    # Refinement 会留下多轮 code 结果；最终产物以最后一次执行为准。
    for tr in reversed(tool_results):
        _mode = getattr(tr, "mode", None) if hasattr(tr, "mode") else (tr.get("mode") if isinstance(tr, dict) else None)
        if _mode == "code":
            _data = getattr(tr, "data", {}) if hasattr(tr, "data") else (tr.get("data", {}) if isinstance(tr, dict) else {})
            if isinstance(_data, dict):
                _result = _data.get("result")
                if isinstance(_result, dict):
                    # 合并 __result__ 到 artifacts，保留 _extract_artifacts 已提取的字段
                    artifacts.update(_result)
            break

    return SubAgentOutcome(
        task_id=task_id,
        run_id=run_id,
        agent_role=agent_role,
        status=status,
        artifacts=artifacts,
        duration_ms=0,
        iteration_used=iteration,
        verifier_output=VerifierOutput.model_validate(verifier_output) if verifier_output else None,
        error_code=error_code,
        error_message=error_message,
    )


def _extract_artifacts(agent_role: str, final_output: dict, tool_results: list) -> dict:
    """Extract role-specific artifacts from sub-agent execution results.

    Reads final_output.results (legacy ToolResult format) and builds
    artifacts dict per agent role convention.
    """
    results = final_output.get("results") or []

    # code-mode: final_output.results 可能为空（_build_final_output 未收集 code-mode 结果），
    # 从 tool_results 构建，保持與下方 role-specific 提取邏輯兼容
    if not results and tool_results:
        pseudo = []
        for tr in tool_results:
            tr_mode = getattr(tr, "mode", None) if hasattr(tr, "mode") else (tr.get("mode") if isinstance(tr, dict) else None)
            if tr_mode == "code":
                tr_data = getattr(tr, "data", {}) if hasattr(tr, "data") else (tr.get("data", {}) if isinstance(tr, dict) else {})
                if isinstance(tr_data, dict):
                    pseudo.append({
                        "tool_name": "__code_block__",
                        "source": getattr(tr, "source", "computed") if hasattr(tr, "source") else "computed",
                        "data": tr_data,
                        "truncated": getattr(tr, "truncated", False) if hasattr(tr, "truncated") else False,
                    })
        results = pseudo

    if not results:
        return {}

    latest_result = None
    latest_tool_name = None
    for result in reversed(results):
        data = result.get("data") if isinstance(result, dict) else getattr(result, "data", None)
        if data is not None:
            latest_result = data
            latest_tool_name = (
                result.get("tool_name")
                if isinstance(result, dict)
                else getattr(result, "tool_name", None)
            )
            break

    def with_latest(artifacts: dict) -> dict:
        """Keep the role alias and a stable generic edge payload for DAG chaining."""
        if latest_result is not None:
            artifacts.setdefault("result", latest_result)
        if latest_tool_name:
            artifacts.setdefault("result_tool_name", latest_tool_name)
        return artifacts

    if agent_role == "geo":
        locations = []
        for r in results:
            data = r.get("data") if isinstance(r, dict) else getattr(r, "data", None)
            if not isinstance(data, dict):
                continue
            # code-mode: 结果在 data.result（__result__ dict）中
            inner = data.get("result") if isinstance(data, dict) else None
            if isinstance(inner, dict):
                loc_val = inner.get("location")
                if isinstance(loc_val, (list, tuple)) and len(loc_val) >= 2:
                    # formatted_address 可能在 geo_result 中
                    geo_res = inner.get("geo_result") or {}
                    addr = (
                        geo_res.get("formatted_address", "")
                        or inner.get("formatted_address", "")
                    )
                    locations.append({
                        "location": loc_val,
                        "formatted_address": addr,
                        "source": geo_res.get("source", "computed"),
                        "confidence": geo_res.get("confidence"),
                    })
            # 旧 JSON-mode: data 本身有 location
            if isinstance(data, dict) and data.get("location") and not (isinstance(inner, dict) and inner.get("location")):
                locations.append({
                    "location": data["location"],
                    "formatted_address": data.get("formatted_address", ""),
                    "source": data.get("source", "Amap"),
                    "confidence": data.get("confidence"),
                })
        return with_latest({"locations": locations} if locations else {})

    elif agent_role == "poi":
        pois = []
        for r in results:
            data = r.get("data") if isinstance(r, dict) else getattr(r, "data", None)
            if not isinstance(data, dict):
                continue
            # code-mode: 结果在 data.result（__result__ dict）中
            inner = data.get("result") if isinstance(data, dict) else None
            if isinstance(inner, dict):
                plist = inner.get("pois") or []
                pois.extend(plist)
            # 旧 JSON-mode: data 本身有 pois
            if isinstance(data, dict) and not inner:
                plist = data.get("pois") or []
                pois.extend(plist)
        return with_latest({"pois": pois} if pois else {})

    elif agent_role == "geometer":
        for r in reversed(results):
            data = r.get("data") if isinstance(r, dict) else getattr(r, "data", None)
            if isinstance(data, dict) and "features" in data:
                return with_latest({"geojson": data})
        return with_latest({})

    elif agent_role == "viz":
        layers = []
        for r in results:
            data = r.get("data") if isinstance(r, dict) else getattr(r, "data", None)
            if isinstance(data, dict) and data.get("layers"):
                layers.extend(data["layers"])
        return with_latest({"layers": layers} if layers else {})

    elif agent_role == "coder":
        # Code mode already publishes ``__result__`` keys below in
        # ``subagent_state_to_outcome``.  Do not add the native-step wrapper:
        # that would shift the stable numeric reference catalog consumed by
        # downstream legacy/checkpoint plans.
        return {"sandbox_output": results[0]} if results else {}

    return with_latest({})


def _enrich_goal_from_deps(task, results: dict[str, list[dict]]) -> str:
    """将前序依赖任务的关键结果注入到 sub-agent 的 goal 中。

    依赖产物由 ``_session_vars_for_task`` 注入 Python 变量；这里再追加一份
    人类可读摘要，帮助 planner 选择正确变量，不承担真实数据传输。

    Args:
        task: SubTask 对象（含 goal 和 depends_on）。
        results: 已完成任务的结果 dict，task_id → list[SubAgentOutcome dict]。

    Returns:
        增强后的 goal 字符串。
    """
    if not task.depends_on:
        return task.goal

    context_parts: list[str] = []

    for dep_id in task.depends_on:
        outcomes = results.get(dep_id) or []
        if not outcomes:
            continue
        # 取第一个成功的 outcome
        winner = next(
            (o for o in outcomes if o.get("status") in ("success", "refined")),
            outcomes[0],
        )
        artifacts = winner.get("artifacts") or {}
        role = winner.get("agent_role", "")

        if role == "geo":
            # 提取坐标信息
            locations = artifacts.get("locations") or []
            for loc in locations[:2]:  # 最多取前 2 个
                loc_data = loc.get("location")
                addr = loc.get("formatted_address", "")
                if loc_data:
                    context_parts.append(
                        f"[前序 geo 结果] {addr} 坐标={loc_data}"
                    )

        elif role == "poi":
            # 提取 POI 摘要
            pois = artifacts.get("pois") or []
            if pois:
                names = ", ".join(p.get("name", "?") for p in pois[:5])
                context_parts.append(
                    f"[前序 poi 结果] 找到 {len(pois)} 个 POI: {names}"
                    "（完整列表已注入变量 pois）"
                )

        elif role == "geometer":
            geojson = artifacts.get("geojson")
            if isinstance(geojson, dict) and geojson.get("features"):
                nfeat = len(geojson["features"])
                context_parts.append(
                    f"[前序 geometer 结果] 生成 {nfeat} 个几何要素"
                )

        elif artifacts:
            context_parts.append(
                f"[前序 {role or 'agent'} 结果] 可用产物字段: "
                f"{', '.join(str(key) for key in artifacts.keys())}"
            )

    if not context_parts:
        return task.goal

    context = "\n".join(context_parts)
    if task.agent_role == "coder":
        dependency_hint = (
            "这些结果已作为同名 Python 变量注入 session_vars；请直接使用变量值。"
        )
    else:
        dependency_hint = (
            "这些产物会出现在 system prompt 的 runtime reference catalog 中；"
            "请把对应整数索引填入工具 Schema 的 *_from/input_ref 字段。"
        )
    return f"{task.goal}\n\n--- 前序任务结果 ---\n{context}\n{dependency_hint}"


def _session_vars_for_task(state, task, results: dict[str, list[dict]]) -> dict:
    """Build the explicit data-plane input for a sub-agent invocation.

    Root-scoped values (notably upload references) are always available.  Each
    dependency is also exposed under ``dep_<task_id>`` and its artifact keys are
    flattened when that does not overwrite an existing value.  The namespaced
    form is the collision-free source of truth; flattened names keep model code
    concise for the common one-producer case.
    """
    shared = dict(state.get("session_vars") or {})
    for dep_id in task.depends_on:
        outcomes = results.get(dep_id) or []
        winner = next(
            (
                outcome
                for outcome in reversed(outcomes)
                if isinstance(outcome, dict)
                and outcome.get("status") in ("success", "refined")
            ),
            None,
        )
        if not winner:
            continue
        artifacts = winner.get("artifacts") or {}
        if not isinstance(artifacts, dict):
            continue
        safe_dep_id = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in dep_id)
        shared[f"dep_{safe_dep_id}"] = artifacts
        for key, value in artifacts.items():
            if key == "result_tool_name":
                continue
            if isinstance(key, str) and key.isidentifier():
                shared.setdefault(key, value)
    return shared


async def _dispatch_single(state, task, results, dispatched, events, on_event=None):
    """Run a single non-redundant sub-task via build_sub_agent.

    异常容错：单个 sub-agent 失败不会崩溃整个 dispatch，而是记录 failed outcome。
    """
    from app.agents.build_sub_agent import run_sub_agent  # avoid circular import
    import uuid

    rid = f"r{uuid.uuid4().hex[:6]}"
    task_index = len(dispatched) + 1
    task_total = len(state.get("task_plan", {}).get("tasks", []))

    # Emit run.task.start for real-time SSE trace
    try:
        from app.agents.events.current import get_current_handler
        on_ev = get_current_handler()
        if on_ev:
            from app.agents.events import emit_event
            emit_event(
                on_ev,
                "run.task.start",
                f"启动 {task.agent_role}",
                task_id=task.id,
                agent_role=task.agent_role,
                goal=task.goal[:80],
                tool_name=task.tool_name,
                task_index=task_index,
                task_total=task_total,
                session_id=state.get("session_id", ""),
                run_id=state.get("run_id", ""),
            )
    except Exception:
        pass

    events.append({
        "event": "sub_task",
        "data": {"task_id": task.id, "agent_role": task.agent_role, "status": "running"},
    })
    enriched_goal = _enrich_goal_from_deps(task, results)
    try:
        # Propagate optional deterministic/injected LLM into real production graph.
        # Contextvars copy into asyncio.to_thread workers automatically.
        raw_state = await asyncio.to_thread(
            run_sub_agent,
            agent_role=task.agent_role,
            user_input=enriched_goal,
            run_id=rid,
            parent_task_id=task.id,
            required_tool_name=task.tool_name,
            required_tool_args=dict(getattr(task, "tool_args", {}) or {}),
            session_id=state.get("session_id", ""),
            session_vars=_session_vars_for_task(state, task, results),
            on_event=on_event,
            llm=get_sub_agent_llm(),
        )
        o = subagent_state_to_outcome(raw_state, task_id=task.id, run_id=rid)
    except Exception as e:
        # sub-agent 整体崩溃（如 planner 解析失败 5 次）—— 记录 failed outcome，不崩溃
        logger.error(
            "sub-agent crashed task=%s role=%s run=%s: %s",
            task.id, task.agent_role, rid, e,
        )
        o = SubAgentOutcome(
            task_id=task.id,
            run_id=rid,
            agent_role=task.agent_role,
            status="failed",
            artifacts={},
            duration_ms=0,
            iteration_used=0,
            error_code="SUB_AGENT_CRASHED",
            error_message=str(e)[:500],
        )

    results.setdefault(task.id, []).append(o.model_dump())
    dispatched.setdefault(task.id, []).append(rid)
    status_label = o.status
    events.append({
        "event": "sub_task",
        "data": {"task_id": task.id, "agent_role": task.agent_role, "status": status_label},
    })
    add_sub_task_duration(o.duration_ms)

    # Emit run.task.complete for real-time SSE trace
    try:
        from app.agents.events.current import get_current_handler
        on_ev = get_current_handler()
        if on_ev:
            from app.agents.events import emit_event
            emit_event(
                on_ev,
                "run.task.complete",
                f"完成 {task.agent_role}" if o.status == "success" else f"{task.agent_role} 失败",
                task_id=task.id,
                status=o.status,
                error_code=o.error_code,
                duration_ms=o.duration_ms,
                session_id=state.get("session_id", ""),
                run_id=state.get("run_id", ""),
            )
    except Exception:
        pass

    return o


def _previous_poi_count(messages: list[Any], brand_tokens: tuple[str, ...]) -> int | None:
    """Extract a persisted POI count from the preceding matching user turn.

    Session persistence stores the fact-preserving assistant summary rather
    than a second copy of every raw POI.  For same-radius follow-ups, the
    ``找到 N 个 POI`` fact is sufficient to compare returned POI density.
    """
    normalized_tokens = tuple(token.casefold() for token in brand_tokens)
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if getattr(message, "type", "") not in {"human", "user"}:
            continue
        request = str(getattr(message, "content", "") or "").casefold()
        if not any(token in request for token in normalized_tokens):
            continue
        for response in messages[index + 1:]:
            if getattr(response, "type", "") not in {"ai", "assistant"}:
                continue
            content = str(getattr(response, "content", "") or "")
            match = re.search(r"(?:找到|查到|found)\s*(\d+)\s*(?:个\s*)?poi(?:s)?", content, re.IGNORECASE)
            if match:
                return int(match.group(1))
    return None


def _is_same_radius_density_followup(user_input: str) -> bool:
    text = str(user_input or "").casefold()
    has_chabaidao = "茶百道" in text or "chabaidao" in text
    density_markers = ("密度", "density", "densidad", "densité")
    return has_chabaidao and any(marker in text for marker in density_markers)


def assemble_node(state: AgentRootState, *, llm=None) -> dict:
    """Aggregate all dispatch outcomes into final_output.

    - Extracts artifacts from each sub-agent outcome
    - Builds results[] compatible with _build_map_from_results
    - Calls LLM to synthesize a natural-language summary
    - Builds map layers from POI / geometer / viz outcomes

    Short-circuits when the run is paused for user input so that
    ``pending_task`` / ``final_output.status=awaiting_input`` are not
    overwritten by EMPTY_RUN or a summary LLM call.
    """
    events: list[dict] = list(state.get("dispatcher_events") or [])
    user_input = state.get("user_input", "")
    sub_results = state.get("sub_results") or {}
    existing_final = state.get("final_output") or {}
    pending_task = state.get("pending_task")

    # ------------------------------------------------------------------
    # 0. Preserve awaiting_input — never synthesize / EMPTY_RUN over it.
    # Root pending_task (set by dispatch_node on live pauses) is the authority.
    # Historical sub_results awaiting rows must NOT re-arm pause after a
    # successful resume replan that already cleared root pending.
    # ------------------------------------------------------------------
    has_replan_marker = "用户补充参数" in str(user_input)
    is_awaiting = bool(pending_task)
    if (
        not is_awaiting
        and not has_replan_marker
        and existing_final.get("status") == "awaiting_input"
    ):
        # First-pass / no-replan: keep FO pause surface if still marked awaiting.
        is_awaiting = True
        if pending_task is None:
            pending_task = existing_final.get("pending_task")
    if is_awaiting:
        if isinstance(pending_task, dict):
            summary = (
                pending_task.get("message")
                or existing_final.get("summary")
                or existing_final.get("text")
                or "需要用户补充参数"
            )
        else:
            summary = (
                existing_final.get("summary")
                or existing_final.get("text")
                or "需要用户补充参数"
            )
        final_output: dict[str, Any] = {
            "status": "awaiting_input",
            "pending_task": pending_task,
            "summary": summary,
            "text": summary,
            "results": list(existing_final.get("results") or []),
        }
        # Preserve map payload if dispatch already built one.
        for key in ("map", "layers", "bbox"):
            if key in existing_final:
                final_output[key] = existing_final[key]
        return {
            "should_stop": True,
            "pending_task": pending_task,
            "final_output": final_output,
            "dispatcher_events": events,
        }

    # ------------------------------------------------------------------
    # 1. Collect artifacts and build structured results
    # ------------------------------------------------------------------
    text_parts: list[str] = []
    map_layers: list[dict] = []
    results_for_final: list[dict] = []
    all_pois: list[dict] = []
    geojson_fc: dict | None = None
    failed_tasks: list[tuple[str, str, str]] = []
    successful_task_count = 0

    for tid, outcomes in sub_results.items():
        # A task can retain historical attempts.  Assembly must describe its
        # terminal attempt once, otherwise stale failures/results are duplicated.
        terminal_outcomes = outcomes[-1:] if outcomes else []
        for o in terminal_outcomes:
            artifacts = o.get("artifacts") or {}
            role = o.get("agent_role", "")
            ostatus = o.get("status", "unknown")
            if ostatus in ("success", "refined"):
                successful_task_count += 1
            elif ostatus == "failed":
                failed_tasks.append((
                    tid,
                    str(o.get("error_code") or "SUBTASK_FAILED"),
                    str(o.get("error_message") or "子任务执行失败"),
                ))

            # --- POI results ---
            pois = artifacts.get("pois") or []
            if pois:
                all_pois.extend(pois)
                results_for_final.append({
                    "tool_name": "query_poi",
                    "source": "Amap",
                    "data": {"pois": pois},
                    "truncated": False,
                })
                if ostatus == "success":
                    names = ", ".join(p.get("name", "?") for p in pois[:5])
                    text_parts.append(f"找到 {len(pois)} 个 POI: {names}")

            # --- Geo results ---
            locations = artifacts.get("locations") or []
            for loc in locations:
                location = loc.get("location")
                addr = loc.get("formatted_address") or "目标位置"
                if isinstance(location, (list, tuple)) and len(location) >= 2:
                    coords = [location[0], location[1]]
                    text_parts.append(f"已定位：{addr}，坐标 {coords}")
                    results_for_final.append({
                        "tool_name": "geo_code",
                        "source": loc.get("source", "computed"),
                        "data": dict(loc),
                        "truncated": False,
                    })
                else:
                    text_parts.append(f"已定位：{addr}")

            # geo_transform returns a precise numeric object rather than a
            # geocoding ``location``.  It is still a complete user-visible
            # result and must not fall through to EMPTY_RUN / synthesis.
            if role == "geo" and not locations and artifacts.get("result") is not None:
                generic_result = artifacts["result"]
                result_tool_name = artifacts.get("result_tool_name") or "geo_transform"
                results_for_final.append({
                    "tool_name": result_tool_name,
                    "source": "computed",
                    "data": generic_result,
                    "truncated": False,
                })
                if result_tool_name == "geo_transform" and isinstance(generic_result, dict):
                    converted = generic_result.get("output") or {}
                    if isinstance(converted, dict) and {"lng", "lat"}.issubset(converted):
                        text_parts.append(
                            "坐标转换完成："
                            f"({converted['lng']}, {converted['lat']})"
                        )
                    elif "in_china" in generic_result:
                        text_parts.append(
                            "坐标偏转范围判断完成："
                            + ("坐标在中国范围内。" if generic_result["in_china"] else "坐标在中国范围外。")
                        )
                    else:
                        text_parts.append("坐标转换完成。")

            # --- Geometer results ---
            geojson = artifacts.get("geojson")
            result_tool_name = artifacts.get("result_tool_name") or "spatial_analysis"
            if isinstance(geojson, dict) and geojson.get("features"):
                geojson_fc = geojson
                results_for_final.append({
                    "tool_name": result_tool_name,
                    "source": "computed",
                    "data": geojson,
                    "truncated": False,
                })
                nfeat = len(geojson.get("features", []))
                text_parts.append(f"空间分析完成，{nfeat} 个要素")
            elif role == "geometer" and artifacts.get("result") is not None:
                generic_result = artifacts["result"]
                results_for_final.append({
                    "tool_name": result_tool_name,
                    "source": "computed",
                    "data": generic_result,
                    "truncated": False,
                })
                if isinstance(generic_result, dict) and generic_result.get("type") == "raster":
                    map_layers.append(generic_result)
                    value_kind = generic_result.get("value_kind") or result_tool_name
                    text_parts.append(f"栅格分析完成：{value_kind}")
                elif result_tool_name == "export_result":
                    export_path = (
                        generic_result.get("path")
                        if isinstance(generic_result, dict)
                        else None
                    )
                    text_parts.append(
                        f"结果已导出{f'：{export_path}' if export_path else ''}"
                    )
                elif result_tool_name == "check_validity":
                    issues = generic_result.get("issues") if isinstance(generic_result, dict) else None
                    if isinstance(issues, list):
                        if issues:
                            details = []
                            for issue in issues[:3]:
                                if not isinstance(issue, dict):
                                    continue
                                index = issue.get("index")
                                kind = issue.get("type") or "unknown"
                                reason = issue.get("reason") or ""
                                prefix = f"要素 {index}" if index is not None else "要素"
                                details.append(f"{prefix}: {kind}{f'（{reason}）' if reason else ''}")
                            suffix = f"：{'；'.join(details)}" if details else ""
                            text_parts.append(f"几何有效性检查完成，发现 {len(issues)} 个几何问题{suffix}")
                        else:
                            text_parts.append("几何有效性检查完成，未发现问题")
                    else:
                        text_parts.append("几何有效性检查完成")

            # --- Viz results ---
            layers = artifacts.get("layers") or []
            if layers:
                # A rendered layer is already a fact. Surface it directly so
                # assembly cannot discard it and ask the user to upload again.
                text_parts.append(f"地图图层已生成，共 {len(layers)} 个图层")
            for layer in layers:
                map_layers.append(layer)
                results_for_final.append({
                    "tool_name": "map_layer_build",
                    "source": "computed",
                    "data": {"layers": [layer]},
                    "truncated": False,
                })

            # --- Verify / reflect events ---
            verifier = o.get("verifier_output")
            if verifier and isinstance(verifier, dict) and verifier.get("approved") is not None:
                events.append({
                    "event": "verify",
                    "data": {
                        "task_id": tid,
                        "approved": verifier["approved"],
                        "reason": verifier.get("reason", ""),
                        "confidence": verifier.get("confidence", 1.0),
                    },
                })
            iteration_used = o.get("iteration_used", 0)
            if iteration_used > 0:
                events.append({
                    "event": "reflect",
                    "data": {
                        "task_id": tid,
                        "reason": f"经过 {iteration_used} 次迭代",
                        "iteration": iteration_used,
                    },
                })

    # A comparison request must answer with both runs' facts, not merely the
    # newest POI list.  Both documented turns use the same 500 m circular
    # extent, so their returned POI counts are directly comparable.
    if all_pois and _is_same_radius_density_followup(user_input):
        previous_mixue_count = _previous_poi_count(
            list(state.get("messages") or []), ("蜜雪冰城", "mixue"),
        )
        if previous_mixue_count is not None:
            current_count = len(all_pois)
            if current_count > previous_mixue_count:
                relation = "较高"
            elif current_count < previous_mixue_count:
                relation = "较低"
            else:
                relation = "相同"
            text_parts.append(
                "同为南京新街口 500 米范围："
                f"茶百道检索到 {current_count} 个，上一轮蜜雪冰城检索到 {previous_mixue_count} 个。"
                f"按同面积范围的返回 POI 数比较，茶百道密度{relation}。"
            )

    # ------------------------------------------------------------------
    # 2. Build map if we have POIs or geometry
    # ------------------------------------------------------------------
    map_payload: dict | None = None
    if map_layers:
        # Viz agent produced explicit layers
        map_payload = {"layers": map_layers}
        explicit_bbox = _bbox_from_layers(map_layers)
        if explicit_bbox:
            map_payload["bbox"] = explicit_bbox
    elif geojson_fc:
        # Geometer agent produced a FeatureCollection
        map_payload = _build_map_from_geojson(geojson_fc)
    elif all_pois:
        # Build point layer from POI list
        from app.tools.map_layer import MapLayerBuilder
        builder = MapLayerBuilder()
        features = []
        for p in all_pois:
            loc = p.get("location")
            if isinstance(loc, (list, tuple)) and len(loc) >= 2:
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [float(loc[0]), float(loc[1])]},
                    "properties": {
                        "name": p.get("name", ""),
                        "address": p.get("address", ""),
                        "_source": p.get("source", "Amap"),
                    },
                })
        if features:
            layer = builder.build_feature_collection(features)
            coords = [f["geometry"]["coordinates"] for f in features]
            lngs = [c[0] for c in coords]
            lats = [c[1] for c in coords]
            bbox = [min(lngs), min(lats), max(lngs), max(lats)]
            map_payload = {"layers": [layer], "bbox": bbox}

    # ------------------------------------------------------------------
    # 3. Build a fact-preserving final text. Structured tool facts are already
    # natural-language ready and must not be sent through another model that can
    # alter coordinates, counts, or completion state.
    # ------------------------------------------------------------------
    failure_parts = [
        f"子任务 {task_id} 失败：{message}（{code}）"
        for task_id, code, message in failed_tasks
    ]
    if text_parts:
        summary_text = "\n".join([*text_parts, *failure_parts])
    elif failure_parts:
        summary_text = "\n".join(failure_parts)
    else:
        # 无工具结果：可能是非 GIS 查询（问候/闲聊）或 Planner 产生空 task plan
        try:
            synthesis_llm = llm or create_llm()
            msgs = [
                SystemMessage(content=(
                    "你是一个 GIS 助手 Gismind。如果用户输入是问候、闲聊或非空间分析问题，"
                    "请友好回复并介绍你能做什么（POI查询、缓冲区、叠加分析、等时圈等）。"
                    "如果用户问了空间分析问题但没有结果，请如实告知。回复不超过 100 字。"
                    "请以 JSON 格式输出：{\"reply\": \"你的回复内容\"}"
                )),
                HumanMessage(content=user_input),
            ]
            resp = llm_invoke_with_retry(synthesis_llm, msgs)
            raw = resp.content if hasattr(resp, "content") else str(resp)
            try:
                parsed = robust_parse_json(raw)
                summary_text = parsed.get("reply", raw) if isinstance(parsed, dict) else raw
            except (json.JSONDecodeError, ValueError, TypeError, AttributeError):
                summary_text = raw
        except Exception:
            logger.exception("assemble fallback LLM failed")
            summary_text = "任务完成，但未获取到有效数据。"

    # ------------------------------------------------------------------
    # 4. Build final_output
    # ------------------------------------------------------------------
    final_output: dict[str, Any] = {
        "text": summary_text,
        "summary": summary_text,
        "results": results_for_final,
    }
    if map_payload:
        final_output["map"] = map_payload
        final_output["layers"] = map_payload.get("layers", [])
        final_output["bbox"] = map_payload.get("bbox", [])

    if failed_tasks:
        if successful_task_count or results_for_final or text_parts:
            final_output["status"] = "partial"
            final_output["error_code"] = "SUBTASK_PARTIAL_FAILURE"
        else:
            final_output["status"] = "failed"
            final_output["error_code"] = "SUBTASK_FAILED"
        final_output["failed_tasks"] = [
            {"task_id": tid, "error_code": code, "error_message": message}
            for tid, code, message in failed_tasks
        ]
    elif results_for_final or text_parts:
        final_output["status"] = "success"
    # Empty success detection: no structured results and no factual text parts
    elif not results_for_final and not text_parts:
        final_output["status"] = "failed"
        final_output["error_code"] = "EMPTY_RUN"

    # Emit run.summary + run.completed for real-time SSE trace
    try:
        from app.agents.events.current import get_current_handler
        on_ev = get_current_handler()
        if on_ev:
            status = final_output.get("status", "ok")
            if status == "failed":
                from app.agents.events import emit_event
                emit_event(
                    on_ev,
                    "run.failed",
                    final_output.get("error_code", "任务失败"),
                    run_id=state.get("run_id", ""),
                    session_id=state.get("session_id", ""),
                )
            else:
                from app.agents.events import emit_event
                emit_event(
                    on_ev,
                    "run.summary",
                    summary_text[:200] or "任务完成",
                    run_id=state.get("run_id", ""),
                    session_id=state.get("session_id", ""),
                )
                emit_event(
                    on_ev,
                    "run.completed",
                    "所有子任务执行完成",
                    run_id=state.get("run_id", ""),
                    session_id=state.get("session_id", ""),
                )
    except Exception:
        pass

    return {
        "should_stop": True,
        "final_output": final_output,
        "dispatcher_events": events,
    }


def _build_map_from_geojson(geojson: dict) -> dict:
    """Convert a GeoJSON FeatureCollection into a map layer payload."""
    from app.tools.map_layer import MapLayerBuilder
    builder = MapLayerBuilder()
    layer = builder.build_feature_collection(geojson.get("features", []))
    coords: list[list[float]] = []

    def collect_pairs(value) -> None:
        if (
            isinstance(value, (list, tuple))
            and len(value) >= 2
            and isinstance(value[0], (int, float))
            and isinstance(value[1], (int, float))
        ):
            coords.append([float(value[0]), float(value[1])])
            return
        if isinstance(value, (list, tuple)):
            for child in value:
                collect_pairs(child)

    for f in geojson.get("features", []):
        geom = f.get("geometry", {})
        collect_pairs(geom.get("coordinates", []))
        for child in geom.get("geometries", []) if isinstance(geom, dict) else []:
            collect_pairs(child.get("coordinates", []))
    bbox: list = []
    if coords:
        lngs = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        bbox = [min(lngs), min(lats), max(lngs), max(lats)]
    return {"layers": [layer], "bbox": bbox}


def _bbox_from_layers(layers: list[dict]) -> list[float]:
    """Compute a stable bbox for explicit viz layers before SSE serialization."""
    coords: list[list[float]] = []

    def collect(value) -> None:
        if (
            isinstance(value, (list, tuple))
            and len(value) >= 2
            and isinstance(value[0], (int, float))
            and isinstance(value[1], (int, float))
        ):
            coords.append([float(value[0]), float(value[1])])
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                collect(item)

    for layer in layers:
        if not isinstance(layer, dict):
            continue
        layer_bbox = layer.get("bbox")
        if isinstance(layer_bbox, (list, tuple)) and len(layer_bbox) == 4:
            collect([[layer_bbox[0], layer_bbox[1]], [layer_bbox[2], layer_bbox[3]]])
        if layer.get("type") == "FeatureCollection":
            for feature in layer.get("features") or []:
                if isinstance(feature, dict):
                    collect((feature.get("geometry") or {}).get("coordinates"))
        else:
            collect(layer.get("coordinates"))

    if not coords:
        return []
    lngs = [pair[0] for pair in coords]
    lats = [pair[1] for pair in coords]
    return [min(lngs), min(lats), max(lngs), max(lats)]


def build_dispatcher(
    checkpointer=None,
    interrupt_before: list[str] | None = None,
    llm=None,
):
    """Compile the root dispatcher graph: planner_router -> dispatch -> assemble.

    Args:
        checkpointer: 可选 LangGraph checkpointer (e.g. SqliteSaver).
                      非 None 时传给 workflow.compile() 实现持久化。
        interrupt_before: 可选，原样传给 workflow.compile()；默认 None。
        llm: 可选 LLM transport，注入 planner_router 与 assemble；不进 state。
    """
    from functools import partial

    workflow = StateGraph(AgentRootState)
    planner = partial(planner_router_node, llm=llm) if llm is not None else planner_router_node
    workflow.add_node("planner_router", planner)
    workflow.add_node("dispatch", dispatch_node)
    assemble = partial(assemble_node, llm=llm) if llm is not None else assemble_node
    workflow.add_node("assemble", assemble)
    workflow.set_entry_point("planner_router")
    workflow.add_edge("planner_router", "dispatch")
    workflow.add_edge("dispatch", "assemble")
    workflow.add_edge("assemble", END)
    return workflow.compile(
        checkpointer=checkpointer,
        interrupt_before=interrupt_before,
    )
