"""POST /api/chat 端点：SSE 流式返回 React Loop 结果。

实现参考 docs/01_api_spec.md §2：
- 事件类型:status / token / map / done / error
- SSE 标准格式:event: <type>\\ndata: <json>\\n\\n
- trace_id 生成并贯穿（done/error 事件携带）
- 响应头:text/event-stream, Cache-Control: no-cache, X-Accel-Buffering: no

事件流程：
1. status(thinking) ———— 流开始
2. 调 run_react_loop(user_input, session_id, trace_id)
3. final_output.map 存在 → map 事件
4. final_output.text 存在 → token 事件
5. done 事件（含 trace_id）
异常 → error 事件（INTERNAL_ERROR + trace_id + message）

————
认证模型（临时方案）：
  所有端点通过 X-User-Id 请求头识别用户。缺失时默认 "anonymous"。
  此方案仅提供最低限度的跨用户隔离，生产环境必须替换为 JWT / OAuth2。
  会话归属校验：session JSON 内 user_id 与请求头 X-User-Id 不一致时返回 403。
————
"""

import asyncio
import json
import logging
import traceback
import uuid
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.agents.events import EventCollector
from app.agents.tool_execution import run_react_loop
from app.config import settings
from app.models.schemas import ChatRequest
from app.utils.session import SessionStore

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# EventCollector registry for SSE streaming
# ---------------------------------------------------------------------------
_collectors: dict[str, EventCollector] = {}
_COLLECTOR_TTL_S = 600  # 10 minutes; prevents memory leaks


def _register_collector(session_id: str, collector: EventCollector) -> None:
    """Publish the newest run collector for the deprecated GET event stream."""
    _collectors[session_id] = collector


def _unregister_collector(session_id: str, collector: EventCollector) -> None:
    """Remove *collector* only if it is still the session's current run.

    Two POST streams may briefly overlap for one session during stop/switch.
    The older stream's ``finally`` must never delete the newer registration.
    """
    if _collectors.get(session_id) is collector:
        _collectors.pop(session_id, None)


async def _wait_for_collector(
    session_id: str,
    poll_interval: float = 0.1,
    timeout: float = 30.0,
) -> EventCollector | None:
    """Poll ``_collectors`` until a collector appears for *session_id*.

    Returns ``None`` if *timeout* is exceeded.  This covers the race where
    the SSE connection arrives before POST /chat has created the collector.
    """
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        collector = _collectors.get(session_id)
        if collector is not None:
            return collector
        await asyncio.sleep(poll_interval)
    return None


async def _load_history(session_id: str) -> list | None:
    """从 Redis session store 加载多轮对话历史，转为 LangChain 消息列表。

    SessionStore.get_messages() 已返回 list[BaseMessage]（LangChain 对象），
    无需再做 dict→LangChain 二次转换。只截取最近 20 条避免上下文超限。
    """
    store = SessionStore()
    msgs = await store.get_messages(session_id)
    if not msgs:
        return None

    lc_msgs = []
    for m in msgs[-20:]:
        # get_messages 返回的已是 LangChain Message 对象，直接收集
        # 但需确保 AIMessage 的 tool_calls 格式与 LangChain 标准对齐
        if hasattr(m, "tool_calls") and m.tool_calls:
            # save_turn 存的 tool_calls 格式为 {"tool_name": "xxx"}
            # 需转为 LangChain 标准 {"name": "xxx", "args": {}, "id": "..."}
            fixed_tcs = []
            for i, tc in enumerate(m.tool_calls):
                if isinstance(tc, dict) and "name" not in tc:
                    fixed_tcs.append({
                        "name": tc.get("tool_name", ""),
                        "args": tc.get("params", {}),
                        "id": f"hist_{i}",
                    })
                else:
                    fixed_tcs.append(tc)
            # 重建 AIMessage 带上修正后的 tool_calls
            from langchain_core.messages import AIMessage
            m = AIMessage(content=m.content, tool_calls=fixed_tcs)
        lc_msgs.append(m)

    return lc_msgs if lc_msgs else None


async def _await_if_needed(result):
    """若 run_react_loop 返回协程则 await，否则原样返回。"""
    import inspect
    if inspect.isawaitable(result):
        return await result
    return result


def _run_loop_sync(
    user_input: str,
    session_id: str,
    trace_id: str,
    history: list | None,
    upload_file_ids: list[str] | None = None,
    on_event=None,
    run_id: str = "",
    *,
    checkpointer=None,
    dispatcher_llm=None,
    sub_agent_llm=None,
) -> dict:
    """同步包装器：在线程池中运行 run_react_loop（已是 sync 函数）。

    捕获所有异常返回结构化错误 dict，避免线程异常穿透到 SSE 生成器。
    Optional checkpointer / LLM transports come from ``request.app.state`` so
    tests and e2e can inject real resources without patching modules.
    """
    try:
        result = run_react_loop(
            user_input=user_input,
            session_id=session_id,
            trace_id=trace_id,
            history=history,
            upload_file_ids=upload_file_ids,
            run_id=run_id,
            on_event=on_event,
            checkpointer=checkpointer,
            dispatcher_llm=dispatcher_llm,
            sub_agent_llm=sub_agent_llm,
        )
        # 若 run_react_loop 返回协程则在本线程跑完
        import asyncio as _asyncio
        if _asyncio.iscoroutine(result):
            result = _asyncio.run(result)
        return result
    except Exception as e:
        logger.exception("_run_loop_sync failed trace=%s", trace_id)
        return {
            "should_stop": True,
            "iteration": 0,
            "final_output": {
                "status": "failed",
                "error_code": "INTERNAL_ERROR",
                "summary": f"执行失败：{e}",
            },
            "session_id": session_id,
            "trace_id": trace_id,
            "dispatcher_events": [],
            "react_trace": [],
        }


def _get_user_id(request: Request) -> str:
    """从 X-User-Id 请求头提取用户标识，缺失时默认 "anonymous"。

    临时认证方案：生产环境应替换为 JWT / OAuth2 并校验签名。
    """
    return request.headers.get("X-User-Id", "anonymous").strip() or "anonymous"


def sse_format(event: str, data: dict) -> str:
    """格式化 SSE 事件行."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _merge_trace(react_trace: list, dispatcher_events: list) -> list:
    """将 dispatcher_events 转为与 react_trace 统一的格式，合并后返回。

    dispatcher_events 的子任务/审查/投票事件转为 react_trace 的 tool_calls +
    tool_results 结构，使前端 ThinkingCollapse 统一渲染。
    code-mode 步骤的 traceback 在此处做二次截断（3000 字符）。
    """
    if not react_trace and not dispatcher_events:
        return []

    # 按 task_id 将 dispatcher_events 分组
    tasks: dict[str, dict] = {}  # task_id -> {agent_role, status, verified, ...}
    ordered: list[str] = []
    for ev in dispatcher_events:
        data = ev.get("data", {})
        tid = data.get("task_id", "")
        if not tid:
            continue
        if tid not in tasks:
            tasks[tid] = {"agent_role": data.get("agent_role", ""), "status": "running",
                          "verified": None, "consensus": None, "reflection": None}
            ordered.append(tid)
        if ev["event"] == "sub_task":
            tasks[tid]["agent_role"] = data.get("agent_role", tasks[tid]["agent_role"])
            tasks[tid]["status"] = data.get("status", tasks[tid]["status"])
        elif ev["event"] == "verify":
            tasks[tid]["verified"] = data.get("approved")
            tasks[tid]["verify_reason"] = data.get("reason", "")
            tasks[tid]["verify_confidence"] = data.get("confidence")
        elif ev["event"] == "consensus":
            tasks[tid]["consensus"] = data.get("tie_breaker_used")
        elif ev["event"] == "reflect":
            tasks[tid]["reflection"] = data.get("iteration")

    # 构建 unified trace steps
    unified = list(react_trace)  # start with existing react_trace
    for tid in ordered:
        t = tasks[tid]
        role = t["agent_role"]
        status = t["status"]
        status_icon = {"success": "✓", "failed": "✗", "running": "…"}.get(status, "?")
        thinking_parts = [f"{status_icon} {role}"]
        if t["verified"] is not None:
            thinking_parts.append("审查通过" if t["verified"] else "审查未通过")
        if t["consensus"] is not None:
            thinking_parts.append("加时投票" if t["consensus"] else "共识达成")

        unified.append({
            "round": len(unified) + 1,
            "thinking": " ".join(thinking_parts),
            "tool_calls": [{"tool_name": role, "params": {}}],
            "tool_results": [{"tool_name": role, "status": status}],
            "observer_summary": t.get("verify_reason", ""),
        })

    # code-mode traceback 二次截断（3000 字符）
    _TB_LIMIT = 3000
    for step in unified:
        err = step.get("error")
        if isinstance(err, str) and len(err) > _TB_LIMIT:
            step["error"] = err[:_TB_LIMIT] + f"\n... [truncated, {len(err)} chars total]"

    return unified


def _build_map_from_results(results: list) -> dict | None:
    """从 final_output.results 生成地图图层。

    支持：
    - query_poi: POI 点图层（name/address 作为 popup）
    - buffer / overlay / voronoi: 面几何 FeatureCollection 图层
    - map_layer_build: 直接透传预构建的图层
    """
    if not results:
        return None
    from app.tools.map_layer import MapLayerBuilder
    builder = MapLayerBuilder()
    poi_features = []
    geom_features = []
    all_coords = []

    for r in results:
        if not isinstance(r, dict):
            continue
        tn = r.get("tool_name", "")
        data = r.get("data") or {}

        # --- POI 点 ---
        if tn == "query_poi":
            pois = data.get("pois") or []
            if not isinstance(pois, list):
                continue
            for poi in pois:
                loc = poi.get("location")
                if isinstance(loc, (list, tuple)) and len(loc) >= 2:
                    coord = [float(loc[0]), float(loc[1])]
                    all_coords.append(coord)
                    poi_features.append({
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": coord},
                        "properties": {
                            "name": poi.get("name", ""),
                            "address": poi.get("address", ""),
                            "_source": poi.get("source", "Amap"),
                        },
                    })

        # --- 面几何 (buffer / overlay / voronoi) ---
        elif tn in ("buffer", "overlay", "voronoi"):
            features = data.get("features") or []
            if not isinstance(features, list):
                continue
            for f in features:
                if not isinstance(f, dict):
                    continue
                geom = f.get("geometry")
                if isinstance(geom, dict):
                    geom_features.append(f)
                    # 收集坐标算 bbox
                    coords = _collect_coords(geom)
                    all_coords.extend(coords)

        # --- 预构建图层（map_layer_build 输出）---
        elif tn == "map_layer_build":
            layers = data.get("layers") or []
            if isinstance(layers, list) and layers:
                # 直接返回预构建图层(最高优先级)
                fc_layer = None
                for l in layers:
                    ld = l.get("data") if isinstance(l, dict) else None
                    if isinstance(ld, dict) and ld.get("features"):
                        fc_layer = l
                        for f in ld["features"]:
                            g = f.get("geometry") if isinstance(f, dict) else None
                            if isinstance(g, dict):
                                all_coords.extend(_collect_coords(g))
                bbox = _bbox_from_coords(all_coords) if all_coords else []
                return {"layers": layers, "bbox": bbox}

    layers = []
    if poi_features:
        layers.append(builder.build_feature_collection(poi_features))
    if geom_features:
        layers.append(builder.build_feature_collection(geom_features))

    if not layers:
        return None

    bbox = _bbox_from_coords(all_coords) if all_coords else []
    return {"layers": layers, "bbox": bbox}


def _collect_coords(geom: dict) -> list[list[float]]:
    """从 GeoJSON geometry 提取所有坐标点（用于算 bbox）。"""
    coords = []
    gtype = geom.get("type", "")

    def _walk(c):
        if isinstance(c, (int, float)):
            return
        if isinstance(c, list) and len(c) >= 2 and isinstance(c[0], (int, float)):
            coords.append([float(c[0]), float(c[1])])
        elif isinstance(c, list):
            for item in c:
                _walk(item)

    _walk(geom.get("coordinates", []))
    return coords


def _bbox_from_coords(coords: list[list[float]]) -> list:
    """从坐标列表计算 bbox [minLng, minLat, maxLng, maxLat]."""
    if not coords:
        return []
    lngs = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return [min(lngs), min(lats), max(lngs), max(lats)]


@router.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    """SSE 流式返回。事件类型:status/token/map/done/error。

    生成器 yield SSE 格式数据。trace_id 生成并贯穿。
    """
    trace_id = f"trace_{uuid.uuid4().hex[:12]}"
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    user_id = _get_user_id(request)
    logger.info("chat start session=%s trace=%s run=%s user=%s", req.session_id, trace_id, run_id, user_id)

    # Create RunController for cancel/pause support
    from app.agents.run_control import create_run_controller, get_run_controller
    run_ctrl = create_run_controller(run_id)

    # Create EventCollector for SSE streaming (before run starts, so GET /events
    # can find it immediately).
    collector = EventCollector()
    _register_collector(req.session_id, collector)
    logger.debug("chat: collector created for session=%s", req.session_id)

    async def event_stream():
        execution_trace: list[dict] = []

        def remember_event(item: dict) -> None:
            """Keep a bounded, JSON-safe copy for session history replay."""
            if len(execution_trace) >= 250:
                return
            safe = dict(item)
            for key in ("code", "stdout", "stderr", "traceback", "params", "result"):
                value = safe.get(key)
                if value is None:
                    continue
                try:
                    encoded = json.dumps(value, ensure_ascii=False, default=str)
                except Exception:  # noqa: BLE001
                    encoded = str(value)
                if len(encoded) > 2400:
                    safe[key] = encoded[:2400] + "... (truncated)"
            execution_trace.append(safe)

        try:
            yield sse_format("status", {
                "status": "thinking",
                "message": "正在分析你的问题...",
                "run_id": run_id,
            })

            # Mark run as started
            run_ctrl.start()

            # Emit run.session event
            collector.emit("run.session", "会话开始", session_id=req.session_id, trace_id=trace_id)

            # 验证 session 归属
            store = SessionStore()
            try:
                meta = await store.get_meta(req.session_id)
            except Exception as e:
                logger.exception("get_meta failed trace=%s session=%s", trace_id, req.session_id)
                raise
            if meta is not None:
                stored_user = await store.get_user_id(req.session_id)
                if stored_user != "anonymous" and user_id != "anonymous" and stored_user != user_id:
                    yield sse_format("error", {
                        "code": "FORBIDDEN",
                        "message": "无权访问该会话",
                        "trace_id": trace_id,
                    })
                    return

            # 加载多轮对话历史，让 Planner 感知原始任务上下文
            try:
                history_messages = await _load_history(req.session_id)
            except Exception as e:
                logger.exception("_load_history failed trace=%s session=%s", trace_id, req.session_id)
                raise

            # Pull real resource seams from app.state (tests/e2e inject these).
            loop_checkpointer = getattr(request.app.state, "checkpointer", None)
            loop_dispatcher_llm = getattr(request.app.state, "dispatcher_llm", None)
            loop_sub_agent_llm = getattr(request.app.state, "sub_agent_llm", None)

            # Bridge emit_event(dict) → EventCollector.emit(event, message, **payload).
            # Graph nodes call emit_event(handler, event, message, **payload) which
            # builds a dict and invokes handler(item). EventCollector.emit still
            # expects the multi-arg form used by direct callers in this route.
            def _on_graph_event(item: dict) -> None:
                if not isinstance(item, dict):
                    return
                event_name = item.get("event") or item.get("event_type") or "message"
                message = item.get("message") or ""
                payload = {
                    k: v
                    for k, v in item.items()
                    if k not in ("event", "event_type", "message", "display_kind", "timestamp")
                }
                # Keep display_kind/timestamp if present — harmless extras for SSE.
                if "display_kind" in item:
                    payload["display_kind"] = item["display_kind"]
                if "timestamp" in item:
                    payload["timestamp"] = item["timestamp"]
                collector.emit(str(event_name), str(message), **payload)

            # 将 run_react_loop 放到后台任务，同时从 EventCollector 实时转发事件
            loop_task = asyncio.create_task(
                asyncio.to_thread(
                    lambda: _run_loop_sync(
                        req.message, req.session_id, trace_id, history_messages,
                        upload_file_ids=list(req.upload_file_ids or []),
                        on_event=_on_graph_event,
                        run_id=run_id,
                        checkpointer=loop_checkpointer,
                        dispatcher_llm=loop_dispatcher_llm,
                        sub_agent_llm=loop_sub_agent_llm,
                    )
                )
            )

            # Clear dedup set for the new run (prevents cross-run dedup collision)
            collector.clear_dedup()

            # Real-time event bridge: poll collector.get() while run is ongoing,
            # forwarding events as SSE frames immediately. Uses the timeout-enabled
            # get() method instead of consume() to avoid cancelling async generators.
            _HEARTBEAT = 15.0
            _CANCEL_POLL = 0.25
            last_heartbeat = asyncio.get_running_loop().time()
            while not loop_task.done():
                item = await collector.get(timeout=_CANCEL_POLL)
                if run_ctrl.should_stop():
                    collector.stop()
                    loop_task.cancel()  # the worker thread may finish, but its result is detached
                    yield sse_format("error", {
                        "code": "CANCELLED",
                        "message": "请求已取消",
                        "trace_id": trace_id,
                        "run_id": run_id,
                    })
                    return
                if item is None:
                    now = asyncio.get_running_loop().time()
                    if now - last_heartbeat >= _HEARTBEAT:
                        yield ": heartbeat\n\n"
                        last_heartbeat = now
                else:
                    remember_event(item)
                    yield sse_format(item.get("event", "message"), item)

            if run_ctrl.should_stop():
                collector.stop()
                yield sse_format("error", {
                    "code": "CANCELLED",
                    "message": "请求已取消",
                    "trace_id": trace_id,
                    "run_id": run_id,
                })
                return
            result = loop_task.result()

            # Drain any remaining events that arrived after the run completed
            while True:
                item = await collector.get(timeout=0.2)
                if item is None:
                    break
                if run_ctrl.should_stop():
                    collector.stop()
                    yield sse_format("error", {
                        "code": "CANCELLED",
                        "message": "请求已取消",
                        "trace_id": trace_id,
                        "run_id": run_id,
                    })
                    return
                remember_event(item)
                yield sse_format(item.get("event", "message"), item)
            collector.stop()

            if run_ctrl.should_stop():
                yield sse_format("error", {
                    "code": "CANCELLED",
                    "message": "请求已取消",
                    "trace_id": trace_id,
                    "run_id": run_id,
                })
                return

            # Build react_trace for persistence only (NOT yielded to POST stream --
            # real-time events already cover the trace). The merged trace is still
            # saved into the session store for refresh/replay scenarios.
            react_trace = result.get("react_trace") or []
            dispatcher_events = result.get("dispatcher_events") or []
            merged = _merge_trace(react_trace, dispatcher_events)

            final_output = dict(result.get("final_output") or {})
            final_output["execution_trace"] = execution_trace
            if final_output.get("status") in {"failed", "partial"} or result.get("status") == "failed":
                message = (
                    final_output.get("summary")
                    or final_output.get("text")
                    or "任务执行失败"
                )
                try:
                    from app.utils.session import save_turn
                    await save_turn(req.session_id, req.message, final_output)
                except Exception:
                    logger.exception("save failed turn trace=%s", trace_id)
                run_ctrl.mark_failed()
                yield sse_format("error", {
                    "code": final_output.get("error_code", "INTERNAL_ERROR"),
                    "message": message,
                    "terminal_status": final_output.get("status", "failed"),
                    "failed_tasks": final_output.get("failed_tasks") or [],
                    "trace_id": trace_id,
                    "run_id": run_id,
                })
                return

            # map 事件:final_output.layers 或 final_output.map 存在则发送
            map_payload = final_output.get("map")
            if not isinstance(map_payload, dict) and final_output.get("layers"):
                map_payload = {"layers": final_output["layers"],
                               "bbox": final_output.get("bbox", [])}
            # 若无显式 map,从 results 里的 POI 数据自动生成图层
            if not isinstance(map_payload, dict):
                map_payload = _build_map_from_results(final_output.get("results") or [])
            if isinstance(map_payload, dict) and map_payload.get("layers"):
                if run_ctrl.should_stop():
                    yield sse_format("error", {
                        "code": "CANCELLED",
                        "message": "请求已取消",
                        "trace_id": trace_id,
                        "run_id": run_id,
                    })
                    return
                yield sse_format("map", map_payload)
            # token 事件:增量流式发送 final_output.text 或 .summary
            text = final_output.get("text") or final_output.get("summary") or ""
            if text:
                # 按句子/段落增量推送,模拟打字机效果
                _CHUNK_SIZE = 20  # 每次推送 20 字符
                for i in range(0, len(text), _CHUNK_SIZE):
                    if run_ctrl.should_stop():
                        yield sse_format("error", {
                            "code": "CANCELLED",
                            "message": "请求已取消",
                            "trace_id": trace_id,
                            "run_id": run_id,
                        })
                        return
                    yield sse_format("token", {"content": text[i:i + _CHUNK_SIZE]})

            # --- AWAITING_INPUT: emit judge.awaiting_input event ---
            pending_task = None
            # Check final_output for pending_task (judge stores it there on AWAITING_INPUT)
            if isinstance(final_output, dict):
                pending_task = final_output.get("pending_task")
            # Also check sub-agent states propagated up through dispatcher_events
            if not pending_task:
                for ev in (dispatcher_events or []):
                    data = ev.get("data", {})
                    if isinstance(data, dict) and data.get("pending_task"):
                        pending_task = data["pending_task"]
                        break
            if pending_task:
                if run_ctrl.should_stop():
                    yield sse_format("error", {
                        "code": "CANCELLED",
                        "message": "请求已取消",
                        "trace_id": trace_id,
                        "run_id": run_id,
                    })
                    return
                yield sse_format("judge.awaiting_input", {
                    "event": "judge.awaiting_input",
                    "pending_task": pending_task,
                    "issues": pending_task.get("issues") or [],
                    "run_id": run_id,
                    "session_id": req.session_id,
                    "message": pending_task.get("message", "需要更多信息"),
                })

            # 持久化本轮对话到 Redis session store(供 session list 统计 tool_count / message_count / title)
            # 必须在 done 事件之前完成，否则前端刷新时 save_turn 尚未执行（竞态条件）
            if run_ctrl.should_stop():
                yield sse_format("error", {
                    "code": "CANCELLED",
                    "message": "请求已取消",
                    "trace_id": trace_id,
                    "run_id": run_id,
                })
                return
            try:
                from app.utils.session import save_turn
                await save_turn(req.session_id, req.message, final_output)
            except Exception:
                logger.exception("save_turn failed trace=%s", trace_id)

            if run_ctrl.should_stop():
                yield sse_format("error", {
                    "code": "CANCELLED",
                    "message": "请求已取消",
                    "trace_id": trace_id,
                    "run_id": run_id,
                })
                return
            yield sse_format("done", {"trace_id": trace_id, "run_id": run_id})
            run_ctrl.mark_completed()
        except asyncio.CancelledError:
            logger.info("chat stream cancelled by client trace=%s", trace_id)
            run_ctrl.request_cancel()
            collector.stop()
            yield sse_format("error", {
                "code": "CANCELLED",
                "message": "请求已取消",
                "trace_id": trace_id,
                "run_id": run_id,
            })
        except Exception as e:  # noqa: BLE001
            logger.exception("chat stream error trace=%s", trace_id)
            run_ctrl.mark_failed()
            tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            if settings.APP_ENV == "dev":
                # dev 模式：将堆栈尾部附加到 message，便于前端直接显示
                tb_lines = tb.strip().split("\n")
                tb_summary = "\n".join(tb_lines[-15:])
                sanitized = f"{e}\n\n--- Stack Trace ---\n{tb_summary}"
            else:
                sanitized = "Internal server error"
            yield sse_format("error", {
                "code": "INTERNAL_ERROR",
                "message": sanitized,
                "trace_id": trace_id,
            })
        finally:
            # Clean up collector after stream ends (or client disconnects).
            if not collector.queue_has_consumer():
                _unregister_collector(req.session_id, collector)
                logger.debug("chat: collector cleaned up for session=%s", req.session_id)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/api/chat/{session_id}/events")
async def chat_events(session_id: str, request: Request):
    """.. deprecated:: v1.7
       Replaced by real-time event streaming on POST /api/chat.
       Kept for one version for backward compatibility.

    SSE endpoint: consumes events from an existing EventCollector.

    Detects client disconnect via ``request.is_disconnected()`` and calls
    ``collector.stop()`` to unblock the consumer loop.
    """
    collector = _collectors.get(session_id)
    if collector is None:
        collector = await _wait_for_collector(session_id)
    if collector is None:
        return StreamingResponse(
            iter([]), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def event_stream():
        try:
            async for event in collector.consume():
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                # Detect client disconnect early -- unblock consume() via sentinel.
                if await request.is_disconnected():
                    collector.stop()
                    break
        finally:
            collector.mark_no_consumer()
            # If the run has already finished and nobody else is consuming, clean up.
            if not collector.queue_has_consumer():
                _unregister_collector(session_id, collector)
                logger.debug("chat_events: collector cleaned up for session=%s", session_id)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _parse_resume_patch(answer: str, slot_patch_schema: dict | None) -> tuple[dict | None, str | None]:
    """Validate *answer* against ``slot_patch_schema``.

    Returns ``(patch, None)`` on success or ``(None, error_message)`` on failure.
    For ``type == "number"`` slots, extract the first decimal via regex.
    Empty schema keeps the raw answer as ``{"answer": answer}``.
    """
    import re

    schema = slot_patch_schema or {}
    if not schema:
        return ({"answer": answer} if answer is not None else {}), None

    patch: dict[str, object] = {}
    for slot, spec in schema.items():
        if not isinstance(spec, dict):
            continue
        slot_type = spec.get("type")
        if slot_type == "number":
            m = re.search(r"[-+]?\d+(?:\.\d+)?", str(answer) if answer is not None else "")
            if not m:
                return None, f"无法从答复中解析数字 slot={slot}"
            patch[slot] = float(m.group(0))
        else:
            # Non-number slots: pass raw answer through under the slot name.
            patch[slot] = answer
    if not patch:
        return None, "slot_patch_schema 无可用字段"
    return patch, None


def _root_checkpoint_config(session_id: str) -> dict:
    """LangGraph root config: ``thread_id`` only (empty checkpoint_ns).

    Root checkpoints persist under empty namespace. Using ``checkpoint_ns='_root'``
    makes ``get_state`` raise ``ValueError: Subgraph _root not found`` and never
    match real root writes. Sub-agent configs keep their own namespace and must
    not use this helper.
    """
    return {"configurable": {"thread_id": session_id}}


def _resume_replan_consumed(final_state, prior_pending: dict) -> bool:
    """Return True when resume invoke actually consumed pending via replan.

    Strong positive signals:
    - ``user_input`` contains the resume merge marker ``用户补充参数``
    - ``pending_task`` cleared **and** FO is not still same-awaiting without marker
      (bare ``pending is None`` is insufficient if dispatch nulled without replan)

    Stale checkpoint ``task_plan`` alone is NOT enough.

    Same awaiting run id without marker → retain.
    """
    if not isinstance(final_state, dict):
        return False

    pending = final_state.get("pending_task")
    user_input = str(final_state.get("user_input") or "")
    has_marker = "用户补充参数" in user_input
    fo = final_state.get("final_output") or {}
    fo_status = fo.get("status") if isinstance(fo, dict) else None
    fo_pending = fo.get("pending_task") if isinstance(fo, dict) else None
    prior_run = (prior_pending or {}).get("sub_agent_run_id")

    same_awaiting = (
        fo_status == "awaiting_input"
        and isinstance(fo_pending, dict)
        and fo_pending.get("sub_agent_run_id") == prior_run
        and (
            pending is None
            or (
                isinstance(pending, dict)
                and pending.get("sub_agent_run_id") == prior_run
            )
        )
    )
    # Marker is the authoritative replan signal from planner_router.
    if has_marker:
        return True

    if same_awaiting:
        return False

    if pending is None and fo_status != "awaiting_input":
        # Cleared pending and not awaiting — only accept with non-awaiting FO.
        return True

    # Different pending run id (new pause after progress) counts as consumed prior.
    if isinstance(pending, dict) and pending.get("sub_agent_run_id") != prior_run:
        return True
    if (
        isinstance(fo_pending, dict)
        and fo_pending.get("sub_agent_run_id") != prior_run
        and fo_status == "awaiting_input"
    ):
        return True

    if fo_status != "awaiting_input" and pending is None:
        return True
    return False


def _checkpoint_exists(app, config: dict) -> bool:
    """Return True if *app* has a real checkpoint for *config*.

    LangGraph ``get_state`` does **not** return ``None`` for a missing thread —
    it returns ``StateSnapshot(values={}, created_at=None, ...)``. A fake
    ``checkpoint_ns`` can also raise ``ValueError`` (e.g. missing subgraph).
    Treat both as "no checkpoint".
    """
    try:
        state = app.get_state(config)
    except ValueError:
        return False
    except Exception:
        logger.warning("get_state failed for config=%s", config, exc_info=True)
        return False

    if state is None:
        return False

    values = getattr(state, "values", None)
    if values is None and isinstance(state, dict):
        values = state.get("values")
    created_at = getattr(state, "created_at", None)
    if created_at is None and isinstance(state, dict):
        created_at = state.get("created_at")

    # Empty values + no created_at → missing / never-written snapshot.
    if (not values) and created_at is None:
        return False
    return True


@router.post("/api/chat/{session_id}/resume")
async def resume_chat(session_id: str, request: Request):
    """Resume a paused dispatcher run after user answered a ``judge.awaiting_input`` request.

    Body:
        {
          "sub_agent_run_id": "r-abc123",
          "answer": "500米"  # 用户对 missing_slots 的回复
        }

    流程（先校验再 clear）：
    1. 从 Redis ``PendingStore`` 读取挂起的 ``PendingTask``（按 session_id）
    2. 校验 sub_agent_run_id 是否匹配（防误 resume）
    3. 构建 dispatcher，检查 checkpoint 是否存在（无则 ``no_checkpoint``，**不 clear**）
    4. 用 ``slot_patch_schema`` 解析/验证 answer（失败 ``invalid_answer``，**不 clear**）
    5. 将 ``resume_patch`` + ``pending_task`` 写入 state 后 invoke dispatcher
    6. **仅在 dispatcher 成功接管后** ``await store.clear(session_id)``

    返回：
        - ``{"status": "resumed", ...}`` ———— 已交给 dispatcher 并 clear
        - ``{"status": "not_found", ...}`` ———— 没有挂起的 PendingTask
        - ``{"status": "mismatch", ...}`` ———— sub_agent_run_id 不匹配
        - ``{"status": "no_checkpoint", ...}`` ———— 无 checkpoint，pending 保留
        - ``{"status": "invalid_answer", ...}`` ———— patch 解析失败，pending 保留
        - ``{"status": "in_progress", ...}`` ———— 同一 pending 正由另一个 resume 处理
        - ``{"status": "invoke_noop", ...}`` ———— invoke 成功但未 replan，pending 保留
        - ``{"status": "invoke_failed", ...}`` ———— invoke 异常，pending 保留
    """
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}

    user_answer = body.get("answer") or body.get("message") or ""
    sub_agent_run_id = body.get("sub_agent_run_id") or ""

    from app.agents.pending import PendingStore
    from app.agents.checkpointer import get_sqlite_checkpointer
    from app.agents.dispatcher import build_dispatcher

    redis_client = getattr(request.app.state, "redis", None)
    store = PendingStore(redis_client=redis_client)
    pt = await store.load(session_id)
    if pt is None:
        return {
            "status": "not_found",
            "session_id": session_id,
            "message": "没有挂起的待回答任务",
        }

    if sub_agent_run_id and pt.sub_agent_run_id != sub_agent_run_id:
        return {
            "status": "mismatch",
            "session_id": session_id,
            "expected_sub_agent_run_id": pt.sub_agent_run_id,
            "given_sub_agent_run_id": sub_agent_run_id,
        }

    # Build dispatcher + check checkpoint BEFORE clear.
    # Root config: thread_id only (empty namespace). Do not use checkpoint_ns="_root".
    checkpointer = getattr(request.app.state, "checkpointer", None) or get_sqlite_checkpointer()
    dispatcher_llm = getattr(request.app.state, "dispatcher_llm", None)
    dispatcher_app = build_dispatcher(checkpointer=checkpointer, llm=dispatcher_llm)
    config = _root_checkpoint_config(session_id)

    if not _checkpoint_exists(dispatcher_app, config):
        # Keep pending so the user can retry once a checkpoint exists.
        return {
            "status": "no_checkpoint",
            "session_id": session_id,
            "sub_agent_run_id": pt.sub_agent_run_id,
            "answer": user_answer,
            "message": "无可用 checkpoint；请前端重新 POST /api/chat 携带 user_input 触发新规划",
        }

    # Validate answer against slot_patch_schema before takeover.
    resume_patch, err = _parse_resume_patch(user_answer, pt.slot_patch_schema)
    if err is not None:
        return {
            "status": "invalid_answer",
            "session_id": session_id,
            "sub_agent_run_id": pt.sub_agent_run_id,
            "answer": user_answer,
            "message": err,
        }

    # Capture the exact state that produced the awaiting prompt.  It is passed
    # to planner_router as immutable prior context; only an exact task identity
    # may be reused during the subsequent replan.
    try:
        snapshot = dispatcher_app.get_state(config)
    except Exception:
        logger.warning("resume source checkpoint snapshot unavailable", exc_info=True)
        snapshot = None
    snapshot_values = getattr(snapshot, "values", None)
    if snapshot_values is None and isinstance(snapshot, dict):
        snapshot_values = snapshot.get("values")
    if not isinstance(snapshot_values, dict):
        snapshot_values = {}
    snapshot_config = getattr(snapshot, "config", None)
    if snapshot_config is None and isinstance(snapshot, dict):
        snapshot_config = snapshot.get("config")
    configurable = (
        snapshot_config.get("configurable", {})
        if isinstance(snapshot_config, dict)
        else {}
    )
    source_checkpoint_id = str(configurable.get("checkpoint_id") or "")

    # The Redis lease is the concurrency boundary. It is deliberately taken
    # after all pure validation, so malformed requests never block the valid
    # answer and two browser retries cannot invoke the same DAG twice.
    claim_token = await store.claim(session_id, pt.sub_agent_run_id)
    if not claim_token:
        return {
            "status": "in_progress",
            "session_id": session_id,
            "sub_agent_run_id": pt.sub_agent_run_id,
            "message": "该待回答任务正在恢复，请等待当前请求完成",
        }

    async def _release_claim() -> None:
        try:
            await store.release_claim(session_id, claim_token)
        except Exception:
            logger.exception("resume claim release failed session=%s", session_id)

    pending_dict = pt.to_dict()
    # Fresh run_id so a cancelled SSE chat stream cannot cancel this resume via
    # the old RunController (same session_id, different controller key).
    resume_run_id = f"run_{uuid.uuid4().hex[:12]}"
    from app.agents.run_control import create_run_controller

    create_run_controller(resume_run_id)
    resume_values = {
        "user_input": user_answer,
        "pending_task": pending_dict,
        "resume_patch": resume_patch or {},
        "session_id": session_id,
        "run_id": resume_run_id,
        # The active state starts clean, while planner_router receives an
        # immutable prior snapshot and may restore only exact successes.
        "sub_results": {},
        "resume_prior_task_plan": dict(snapshot_values.get("task_plan") or {}),
        "resume_prior_sub_results": dict(snapshot_values.get("sub_results") or {}),
        "resume_provenance": {
            "source_sub_agent_run_id": pt.sub_agent_run_id,
            "source_checkpoint_id": source_checkpoint_id,
            # SqliteSaver checkpoint_id is the immutable checkpoint version.
            # Keep the explicit alias in public provenance so callers do not
            # have to infer version semantics from an implementation name.
            "source_checkpoint_version": source_checkpoint_id,
            "resume_run_id": resume_run_id,
        },
        "final_output": {},
        "should_stop": False,
        "termination_cause": "",
    }

    # Prefer a full re-entry from planner_router with resume payload.
    # After a normal awaiting pause the graph has next=() (ended); invoke(None)
    # is a no-op and must not clear PendingStore. Restarting with resume_values
    # forces planner_router to see pending_task + resume_patch and re-plan.
    # Propagate optional sub-agent LLM transport (tests/e2e) via contextvar.
    from app.agents.dispatcher import sub_agent_llm_context

    sub_agent_llm = getattr(request.app.state, "sub_agent_llm", None)

    def _invoke_resume():
        with sub_agent_llm_context(sub_agent_llm):
            return dispatcher_app.invoke(resume_values, config=config)

    try:
        final_state = await asyncio.to_thread(_invoke_resume)
    except Exception:
        # Fallback: some langgraph versions need update_state then continue.
        logger.warning(
            "resume invoke(resume_values) failed; trying update_state + invoke",
            exc_info=True,
        )
        try:
            if hasattr(dispatcher_app, "update_state"):
                dispatcher_app.update_state(config, resume_values)

            def _invoke_resume_fallback():
                with sub_agent_llm_context(sub_agent_llm):
                    return dispatcher_app.invoke(resume_values, config=config)

            final_state = await asyncio.to_thread(_invoke_resume_fallback)
        except Exception:
            logger.exception("resume invoke failed session=%s", session_id)
            await _release_claim()
            return {
                "status": "invoke_failed",
                "session_id": session_id,
                "sub_agent_run_id": pt.sub_agent_run_id,
                "answer": user_answer,
                "message": "dispatcher resume invoke failed; pending retained",
            }

    if not _resume_replan_consumed(final_state, pending_dict):
        await _release_claim()
        return {
            "status": "invoke_noop",
            "session_id": session_id,
            "sub_agent_run_id": pt.sub_agent_run_id,
            "answer": user_answer,
            "message": "resume invoke did not replan; pending retained",
            "final_output": (
                (final_state or {}).get("final_output")
                if isinstance(final_state, dict)
                else None
            ),
        }

    # Prior pending was consumed. If the replan immediately re-paused with a new
    # PendingTask, replace the store entry instead of bare-clearing (which would
    # drop the row judge just wrote).
    new_pending = None
    if isinstance(final_state, dict):
        new_pending = final_state.get("pending_task")
        if not isinstance(new_pending, dict):
            fo = final_state.get("final_output") or {}
            if isinstance(fo, dict) and fo.get("status") == "awaiting_input":
                new_pending = fo.get("pending_task")

    if isinstance(new_pending, dict) and new_pending.get("sub_agent_run_id"):
        from app.agents.schemas import PendingTask as _PendingTask

        try:
            await store.save(session_id, _PendingTask.from_dict(new_pending))
        except Exception:
            logger.exception(
                "resume re-save pending failed session=%s; clearing old entry",
                session_id,
            )
            await store.clear(session_id)
    else:
        await store.clear(session_id)

    await _release_claim()

    return {
        "status": "resumed",
        "session_id": session_id,
        "sub_agent_run_id": pt.sub_agent_run_id,
        "answer": user_answer,
        "resume_provenance": (
            (final_state or {}).get("resume_provenance", resume_values["resume_provenance"])
            if isinstance(final_state, dict)
            else resume_values["resume_provenance"]
        ),
        "final_output": (final_state or {}).get("final_output") if isinstance(final_state, dict) else None,
    }


@router.post("/api/agent/{session_id}/resume")
async def resume_agent(session_id: str, request: Request):
    """从 SqliteSaver checkpoint 恢复指定 session 的 dispatcher 运行。"""
    from app.agents.checkpointer import get_sqlite_checkpointer
    from app.agents.dispatcher import build_dispatcher

    checkpointer = getattr(request.app.state, "checkpointer", None) or get_sqlite_checkpointer()
    app = build_dispatcher(checkpointer=checkpointer)
    # Root config: thread_id only (empty namespace). Do not use checkpoint_ns="_root".
    config = _root_checkpoint_config(session_id)

    if not _checkpoint_exists(app, config):
        return {"status": "not_found"}

    try:
        state = app.get_state(config)
    except ValueError:
        return {"status": "not_found"}

    values = state.values if hasattr(state, "values") else (state.get("values") or {})
    # Empty snapshot values are treated as missing by _checkpoint_exists; re-check.
    if (not values) and getattr(state, "created_at", None) is None:
        return {"status": "not_found"}

    if values.get("should_stop"):
        return {
            "status": "already_stopped",
            "final_output": values.get("final_output"),
        }

    import asyncio
    final_state = await asyncio.to_thread(app.invoke, None, config=config)
    return {
        "status": "resumed",
        "session_id": session_id,
        "final_output": final_state.get("final_output") if isinstance(final_state, dict) else None,
    }



# ---------------------------------------------------------------------------
# Run Control endpoints: cancel / pause / resume / status
# ---------------------------------------------------------------------------

@router.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    """Cancel a running agent execution."""
    from app.agents.run_control import get_run_controller
    ctrl = get_run_controller(run_id)
    if ctrl is None:
        return {"status": "not_found", "run_id": run_id}
    ctrl.request_cancel()
    return {"status": "cancelled", "run_id": run_id}


@router.post("/api/runs/{run_id}/pause")
async def pause_run(run_id: str):
    """Pause a running agent execution."""
    from app.agents.run_control import get_run_controller
    ctrl = get_run_controller(run_id)
    if ctrl is None:
        return {"status": "not_found", "run_id": run_id}
    ctrl.request_pause()
    return {"status": "paused", "run_id": run_id}


@router.post("/api/runs/{run_id}/resume")
async def resume_run(run_id: str):
    """Resume a paused agent execution."""
    from app.agents.run_control import get_run_controller
    ctrl = get_run_controller(run_id)
    if ctrl is None:
        return {"status": "not_found", "run_id": run_id}
    ctrl.request_resume()
    return {"status": "resumed", "run_id": run_id}


@router.get("/api/runs/{run_id}")
async def get_run_status(run_id: str):
    """Get the status of a run."""
    from app.agents.run_control import get_run_controller
    ctrl = get_run_controller(run_id)
    if ctrl is None:
        return {"status": "not_found", "run_id": run_id}
    return ctrl.to_dict()
