# 可观测性

> 日志、指标、追踪三支柱。React Loop 多轮迭代，没追踪很难 debug。
> 配置见 [03_config_env.md](03_config_env.md)，安全脱敏见 [06_security.md](06_security.md)。

---

## 1. 三支柱

```
┌─────────────────────────────────────────────┐
│  日志（Logs）    结构化 JSON，含 trace_id      │
│                谁/什么时候/发生了什么          │
├─────────────────────────────────────────────┤
│  指标（Metrics）  聚合数值，按工具/状态分桶      │
│                 耗时/成功率/缓存命中率          │
├─────────────────────────────────────────────┤
│  追踪（Traces）   trace_id 贯穿 React Loop      │
│                 一个请求的全部迭代链路          │
└─────────────────────────────────────────────┘
```

个人 demo 场景，不部署独立监控系统（Prometheus/ Grafana），日志输出到 stdout/stderr，指标通过日志聚合统计。

---

## 2. 结构化日志

### 2.1 日志格式

**生产环境**：JSON 格式，便于聚合查询：

```json
{
  "timestamp": "2026-07-10T08:05:30.123Z",
  "level": "INFO",
  "trace_id": "trace_def456",
  "session_id": "sess_abc123",
  "event": "tool_call",
  "tool": "query_poi",
  "duration_ms": 234,
  "source": "Amap",
  "status": "success",
  "message": "POI 查询完成，找到 12 个结果"
}
```

**开发环境**：console 格式，人类可读：

```
2026-07-10 08:05:30 INFO  [trace_def456] tool_call query_poi 234ms source=Amap → 找到 12 个结果
```

通过 `APP_LOG_FORMAT` 切换（`json` / `console`）。

### 2.2 日志级别约定

| 级别 | 使用场景 |
|------|---------|
| `DEBUG` | 详细调试信息（工具参数、LLM 原始响应），生产默认关闭 |
| `INFO` | 正常业务流转（请求开始/结束、工具调用、Loop 迭代） |
| `WARN` | 可恢复的异常（高德超时走 OSM、截断、编码降级） |
| `ERROR` | 不可恢复的错误（LLM 不可用、Redis 断连、文件解析失败） |

**原则**：ERROR 必须有人处理（即使只是告警）。可自动恢复的异常用 WARN，不要滥用 ERROR。

### 2.3 SSE 事件清单（23 种）

所有 SSE 事件由 `EventCollector` 统一管理（`backend/app/agents/events/__init__.py`），每个事件包含 `event`、`event_type`、`display_kind`、`message`、`timestamp` 标准字段。

**Run 级事件**

| event | 何时发出 | display_kind | 级别 |
|-------|---------|-------------|------|
| `run.session` | 会话开始，携带 session_id 元信息 | progress | INFO |
| `run.thought` | LLM 内部思考过程（CoT） | debug | DEBUG |
| `run.plan` | Root 输出完整工具级 DAG（instructions/tasks/tool_name/depends_on） | workflow_step | INFO |
| `run.summary` | Run 完成后的结果摘要 | result | INFO |
| `run.completed` | Run 正常结束 | result | INFO |
| `run.failed` | Run 异常终止 | result | ERROR |
| `run.paused` | 用户暂停 Run（awaiting_input） | progress | INFO |

**Code 执行事件**

| event | 何时发出 | display_kind | 级别 |
|-------|---------|-------------|------|
| `code.generation` | LLM 生成代码块 | workflow_step | INFO |
| `code.execution.start` | 沙箱开始执行代码 | progress | INFO |
| `code.execution.stdout` | 沙箱 stdout 输出 | debug | DEBUG |
| `code.execution.stderr` | 沙箱 stderr 输出 | debug | DEBUG |
| `code.execution.complete` | 沙箱执行成功完成 | workflow_step | INFO |
| `code.execution.error` | 沙箱执行报错（含 SANDBOX_TIMEOUT / SANDBOX_OOM） | warning | WARN |

**Tool 调用事件**

| event | 何时发出 | display_kind | 级别 |
|-------|---------|-------------|------|
| `tool.call.start` | 工具调用开始 | progress | INFO |
| `tool.preflight.warning` | preflight 校验发现问题（非致命） | warning | WARN |
| `tool.preflight.blocked` | preflight 校验拦截调用 | warning | WARN |
| `tool.call.complete` | 工具调用正常完成 | workflow_step | INFO |
| `tool.postflight.warning` | postflight 校验发现问题 | warning | WARN |
| `tool.postflight.empty_result` | 工具返回空结果 | warning | WARN |

**Risk 事件**

| event | 何时发出 | display_kind | 级别 |
|-------|---------|-------------|------|
| `tool.risk.detected` | GIS 风险检测命中（CRS/FIELD/GEOMETRY 等） | warning | WARN |
| `tool.risk.auto_repair` | 风险自动修复执行 | progress | INFO |
| `tool.risk.blocked` | 风险等级超过策略阈值，阻止执行 | warning | WARN |

**完成判定 / 挂起事件**

| event | 何时发出 | display_kind | 级别 |
|-------|---------|-------------|------|
| `judge.decision` | coder Judge 做出 stop/continue 判定 | debug | DEBUG |
| `judge.awaiting_input` | 当前步骤需要用户确认或输入；事件名为兼容契约，普通角色由 native finalizer 发出 | confirmation | INFO |

前端 `TraceTimeline` 以 `run.plan` 建立待执行步骤列表，再用 `run.task.start/complete` 更新状态，并把 `tool.call.*`、`code.execution.*`、preflight/risk 事件归入对应步骤。`task_index/task_total` 来自真实 TaskPlan，不生成虚假百分比。

### 2.4 structlog 配置

```python
# backend/app/utils/logging.py
import structlog
import logging
from app.config import settings
from app.utils.log_sanitizer import sanitize

def setup_logging():
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _sanitize_processor,           # 脱敏
            _json_or_console_processor,    # 根据 APP_LOG_FORMAT 选择
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.APP_LOG_LEVEL)
        ),
    )

def _sanitize_processor(logger, method_name, event_dict):
    """脱敏处理"""
    for key, value in event_dict.items():
        if isinstance(value, str):
            event_dict[key] = sanitize(value)
    return event_dict

def _json_or_console_processor(logger, method_name, event_dict):
    if settings.APP_LOG_FORMAT == "json":
        return structlog.processors.JSONRenderer()(logger, method_name, event_dict)
    return structlog.dev.ConsoleRenderer()(logger, method_name, event_dict)
```

### 2.5 使用示例

```python
from app.utils.logging import setup_logging
import structlog

setup_logging()
logger = structlog.get_logger()

# 自动携带 trace_id（见 §3）
logger.info("tool_call", tool="query_poi", duration_ms=234, source="Amap", status="success")
logger.warning("fallback_triggered", from_source="Amap", to_source="OSM", reason="timeout")
```

---

## 3. trace_id 贯穿 React Loop

### 3.1 生成时机

每个 `/api/chat` 请求进入时生成 `trace_id`，贯穿整个 React Loop 的所有迭代：

```python
# backend/app/api/chat.py
import uuid
from structlog.contextvars import bind_contextvars, clear_contextvars

@router.post("/api/chat")
async def chat(request: ChatRequest):
    trace_id = f"trace_{uuid.uuid4().hex[:12]}"

    # 绑定到日志上下文，后续所有日志自动携带
    bind_contextvars(trace_id=trace_id, session_id=request.session_id)

    try:
        logger.info("request_start", path="/api/chat", message=request.message[:100])
        result = await run_react_loop(request, trace_id)
        return result
    finally:
        clear_contextvars()
```

### 3.2 传递方式

| 传递路径 | 方式 |
|---------|------|
| HTTP 请求 → React Loop | `trace_id` 作为参数传入 `AgentRootState` |
| React Loop → 工具调用 | `structlog.contextvars` 自动传递（同进程） |
| 后端 → 前端 | SSE 事件携带 `trace_id` |
| 前端 → 后端（错误上报） | 前端在错误日志中带上 `trace_id` |

```python
# AgentRootState / SubAgentState 携带 trace_id（见 02_data_models.md）
class AgentRootState(TypedDict):
    ...
    trace_id: str
    session_id: str

```

### 3.3 SSE 事件携带 trace_id

```python
# error 事件和 done 事件都带 trace_id
yield sse_event("error", {
    "code": "LLM_UNAVAILABLE",
    "message": "LLM 服务暂时不可用，请稍后重试",
    "trace_id": trace_id,
})

yield sse_event("done", {"trace_id": trace_id})
```

前端收到错误时，可将 `trace_id` 展示给用户，便于排查：

```typescript
// 前端错误展示
<p>请求出错，追踪 ID: {trace_id}（可提供给开发者排查）</p>
```

---

## 4. 指标埋点

### 4.1 指标清单

| 指标 | 类型 | 标签 | 说明 |
|------|------|------|------|
| `react_loop_iterations` | histogram | session_id | 每次对话的迭代轮次分布 |
| `sub_agent_calls_total` | counter | agent_role, status | Sub-Agent 调用次数 |
| ⚠️ `ensemble_votes_total` | counter | task_id, result | ~~Ensemble 投票结果~~（已废弃，v1.2 移除 Ensemble 机制） |
| `verifier_checks_total` | counter | agent_role, approved | Verifier 审查次数 |
| `tool_call_duration_ms` | histogram | tool, source | 工具调用耗时 |
| `tool_call_total` | counter | tool, status, source | 工具调用次数 |
| `preflight_checks_total` | counter | tool, rule | Preflight 校验触发次数 |
| `preflight_blocked_total` | counter | tool, rule, code | Preflight 拦截次数 |
| `sandbox_executions_total` | counter | error_code | 代码沙箱执行次数 |
| `sandbox_duration_ms` | histogram | - | 沙箱执行耗时 |
| `risk_detected_total` | counter | risk_type | GIS 风险检测命中次数（CRS/FIELD/GEOMETRY 等） |
| `risk_auto_repaired_total` | counter | risk_type | 风险自动修复成功次数 |
| `risk_blocked_total` | counter | risk_type | 风险等级超策略阈值被阻止执行次数 |
| `llm_tokens_total` | counter | model, type(prompt/completion) | LLM token 消耗 (CostTracker) |
| `llm_call_duration_ms` | histogram | model | LLM 调用耗时 |
| `external_api_duration_ms` | histogram | service(amap/osm) | 外部 API 耗时 |
| `external_api_total` | counter | service, status | 外部 API 调用次数 |
| `fallback_total` | counter | from, to, reason | Fallback 触发次数 |
| `cache_hits_total` | counter | cache_type | 缓存命中次数 |
| `cache_misses_total` | counter | cache_type | 缓存未命中次数 |
| `sse_connections_active` | gauge | - | 当前活跃 SSE 连接数 |

### 4.2 个人 demo 的简化实现

不部署 Prometheus，指标通过结构化日志聚合：

```python
# backend/app/utils/metrics.py
from app.utils.logging import setup_logging
import structlog

logger = structlog.get_logger()

def record_tool_call(tool: str, duration_ms: int, status: str, source: str = None):
    """记录工具调用指标（通过日志聚合统计）"""
    logger.info("tool_call",
        tool=tool,
        duration_ms=duration_ms,
        status=status,
        source=source,
    )

def record_llm_call(model: str, prompt_tokens: int, completion_tokens: int, duration_ms: int):
    logger.info("llm_call",
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        duration_ms=duration_ms,
    )

def record_fallback(from_source: str, to_source: str, reason: str):
    logger.warning("fallback_triggered",
        from_source=from_source,
        to_source=to_source,
        reason=reason,
    )
```

日志查询示例（jq 聚合）：

```bash
# 统计各工具平均耗时
grep '"event":"tool_call"' app.log | \
  jq -s 'group_by(.tool) | map({tool: .[0].tool, avg_ms: (map(.duration_ms) | add / length)})'

# 统计 Fallback 触发次数
grep '"event":"fallback_triggered"' app.log | jq -s 'length'
```

### 4.3 生产环境升级路径

若后续需要正式监控系统：

1. **Prometheus + Grafana**：后端接入 `prometheus-client`，指标从日志改为 Prometheus 格式
2. **Loki**：日志聚合，支持 LogQL 查询
3. **Jaeger**：分布式追踪，但本项目单服务，structlog 的 trace_id 足够

---

## 5. 前端可观测性

### 5.1 SSE 错误上报

```typescript
// frontend/src/hooks/useSSE.ts
function handleSSEError(event: { code: string; message: string; trace_id: string }) {
  // 展示给用户
  setError({ ...event });

  // 上报到后端（可选）
  if (import.meta.env.VITE_SENTRY_DSN) {
    Sentry.captureMessage(`SSE error: ${event.code}`, {
      tags: { trace_id: event.trace_id },
      extra: event,
    });
  }

  // 本地日志
  console.error('[SSE error]', event);
}
```

### 5.2 地图实例数监控

防止懒加载地图实例泄漏（原文档 §4.8.5）：

```typescript
// frontend/src/components/LazyMapView.tsx
const mapInstanceCount = useRef(0);

useEffect(() => {
  if (isVisible && !mapInstance.current) {
    mapInstance.current = new AMap.Map(...);
    mapInstanceCount.current++;
    console.debug(`[Map] instance created, total active: ${mapInstanceCount.current}`);
  }

  return () => {
    if (mapInstance.current) {
      mapInstance.current.destroy();
      mapInstance.current = null;
      mapInstanceCount.current--;
    }
  };
}, [isVisible]);
```

在开发环境定期检查：

```typescript
// frontend/src/utils/devChecks.ts
if (import.meta.env.DEV) {
  setInterval(() => {
    const containers = document.querySelectorAll('[data-testid="map-container"]');
    if (containers.length > 10) {
      console.warn(`[Memory] ${containers.length} map containers in DOM, possible leak`);
    }
  }, 30000);
}
```

### 5.3 前端性能

使用 `web-vitals` 库采集核心指标：

```typescript
// frontend/src/main.tsx
import { onLCP, onFCP, onCLS } from 'web-vitals';

onLCP(console.log);
onFCP(console.log);
onCLS(console.log);
```

---

## 6. 健康检查

### 6.1 端点

`GET /api/health`（见 [01_api_spec.md](01_api_spec.md) §7）：

```python
# backend/app/api/health.py
@router.get("/api/health")
async def health():
    checks = {}

    # Redis
    try:
        get_redis().ping()
        checks["redis"] = "ok"
    except Exception:
        checks["redis"] = "error"

    # LLM（仅校验 key 格式，不发真实请求）
    checks["llm"] = "ok" if settings.LLM_API_KEY else "error"

    # 高德（仅校验 key 格式）
    checks["amap"] = "ok" if settings.AMAP_KEY else "error"

    all_ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={
            "status": "ok" if all_ok else "degraded",
            "version": "1.6.0",
            "checks": checks,
        },
    )
```

### 6.2 Docker 健康检查

```dockerfile
# Dockerfile
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -f http://localhost:8000/api/health || exit 1
```

---

## 7. 告警建议（个人 demo 可选）

个人 demo 不部署告警系统。若部署到公网，建议：

| 场景 | 阈值 | 告警方式 |
|------|------|---------|
| 健康检查失败 | 连续 3 次 | Docker 重启 / 邮件 |
| ERROR 日志激增 | 每分钟 > 10 条 | 邮件 / Webhook |
| LLM 不可用 | 连续 3 次请求失败 | 邮件 |
| 高德配额耗尽 | 429 频率 > 10% | 邮件 |
| 内存使用 | > 80% | Docker 重启 |

---

## 8. 调试技巧

### 8.1 按 trace_id 过滤日志

```bash
grep "trace_def456" app.log | jq '.'
```

可看到一次完整请求的全部日志：request_start → loop_iteration → tool_call → llm_call → done。

### 8.2 React Loop 迭代追踪

```python
# 每轮迭代记录完整状态快照（DEBUG 级别）
logger.debug("loop_iteration",
    iteration=state["iteration"],
    tool_results_count=len(state["tool_results"]),
    should_stop=state["should_stop"],
    messages_count=len(state["messages"]),
)
```

### 8.3 工具结果快照

工具返回的大 GeoJSON 不进日志（会爆），只记录摘要：

```python
logger.info("tool_call",
    tool="query_poi",
    status="success",
    result_count=len(result.get("data", {}).get("features", [])),
    result_size_bytes=len(str(result)),
    truncated=result.get("truncated", False),
)
```

---

*文档版本：v1.3 | 最后更新：2026-08-09 | 属于 Gismind 补充文档*

*v1.3 变更：新增 run.plan 工具级 DAG 事件及前端 Timeline 映射；明确 judge.awaiting_input 是兼容事件名，普通角色由确定性 finalizer 发出。*

*v1.2 变更：SSE 事件清单更新为 23 种实际事件；移除 Celery 相关指标与追踪；新增 preflight / risk 相关指标；标记 Ensemble 指标为已废弃；AgentState → AgentRootState*
*v1.1 变更：AgentState → AgentRootState / SubAgentState；新增 sub_agent_calls / ensemble_votes / verifier_checks / sandbox_executions 指标*
