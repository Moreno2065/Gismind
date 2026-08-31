# API 接口规范

> GIS Agent 后端 API 定义。FastAPI 实现，所有响应为 JSON 或 SSE 流。
> 本文档定义前后端契约，数据模型完整定义见 [02_data_models.md](02_data_models.md)。

---

## 1. 端点总览

| Method | Path | 用途 | 响应类型 | 认证 |
|--------|------|------|---------|------|
| POST | `/api/chat` | 主对话端点，SSE 流式返回 | `text/event-stream` | X-User-Id 请求头（临时） |
| POST | `/api/chat/{session_id}/resume` | 提交补充答案并恢复挂起工作流 | JSON | X-User-Id |
| GET | `/api/chat/{session_id}/events` | 兼容事件流端点（deprecated） | `text/event-stream` | X-User-Id |
| POST | `/api/upload` | 文件上传（shp zip/geojson/kml） | JSON | X-User-Id |
| GET | `/api/sessions` | 列出当前用户的所有会话 | JSON | X-User-Id |
| POST | `/api/sessions` | 创建新会话 | JSON | X-User-Id |
| PATCH | `/api/sessions/{id}` | 重命名会话 | JSON | X-User-Id |
| DELETE | `/api/sessions/{id}` | 删除会话及其消息 | JSON | X-User-Id |
| GET | `/api/sessions/{id}/messages` | 获取会话消息列表 | JSON | X-User-Id |
| POST | `/api/agent/{session_id}/resume` | 恢复 Agent 会话 | `text/event-stream` | X-User-Id |
| POST | `/api/runs/{run_id}/cancel` | 取消运行中的任务 | JSON | X-User-Id |
| POST | `/api/runs/{run_id}/pause` | 暂停运行中的任务 | JSON | X-User-Id |
| POST | `/api/runs/{run_id}/resume` | 恢复暂停的任务 | JSON | X-User-Id |
| GET | `/api/runs/{run_id}` | 查询运行详情 | JSON | X-User-Id |
| GET | `/api/memory/{session_id}` | 读取空间记忆 | JSON | 无 |
| DELETE | `/api/memory/{session_id}` | 清除空间记忆 | JSON | 无 |
| GET | `/api/health` | 健康检查 | JSON | 无 |

所有路径以 `/api` 为前缀。前端 `VITE_API_BASE_URL` 默认 `/api`，开发环境通过 Vite proxy 转发到后端 `8000` 端口。

---

## 2. `POST /api/chat` —— 核心对话端点

接收用户自然语言输入，先生成工具级 WorkflowPlan DAG，再由服务端确定性执行，以 SSE 流式返回计划、步骤、工具、文本和地图事件。

### 2.1 请求

```http
POST /api/chat HTTP/1.1
Content-Type: application/json
Accept: text/event-stream

{
  "session_id": "sess_abc123",
  "message": "南京新街口500米内有多少蜜雪冰城",
  "upload_file_ids": ["file_xyz789"]
}
```

**请求体 `ChatRequest`：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `session_id` | string | 是 | 会话标识，前端首次请求时生成 UUID，后续多轮复用 |
| `message` | string | 是 | 用户自然语言输入，去除首尾空白后最多 10000 字符 |
| `upload_file_ids` | string[] | 否 | 已上传文件的 ID 列表（先调 `/api/upload` 拿到） |

`session_id` 用于多轮对话状态保持和空间记忆隔离。前端建议用 `crypto.randomUUID()` 生成，存 localStorage。

### 2.2 响应：SSE 事件流

响应头：

```
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
X-Accel-Buffering: no
```

SSE 事件格式遵循标准：每个事件由 `event:` / `data:` 行组成，空行分隔。`data` 字段始终为 JSON 字符串。

**事件类型总览：**

| event | data 结构 | 说明 |
|-------|----------|------|
| `run.session` | `{session_id, run_id}` | 会话运行开始 |
| `run.thought` | `{summary, task_count}` | Root 规划摘要 |
| `run.plan` | `{planner_source, instructions, tasks}` | 完整工具级 DAG；来源为 `root_llm`、`guardrail` 或 `fallback`；task 含 id/agent_role/tool_name/depends_on/status |
| `run.task.start` | `{task_id, agent_role, tool_name, task_index, task_total}` | 原子步骤开始 |
| `run.task.complete` | `{task_id, status, error_code, duration_ms}` | 原子步骤结束 |
| `tool.call.start` | `{tool_name, tool_call_id, params}` | JSON Schema 工具调用开始 |
| `tool.call.complete` | `{tool_name, tool_call_id, status, result, duration_ms}` | 工具调用结束 |
| `tool.preflight.warning/blocked` | `{tool_name, code, stage, issues}` | 执行前校验警告/阻断 |
| `tool.postflight.warning/empty_result` | `{tool_name, code, warning_message}` | 执行后质量事件 |
| `code.generation` | `{code, role, iteration, max_iterations}` | coder 生成 Python 代码 |
| `code.execution.start/complete/error` | `{executor_type, result, error_code, traceback}` | coder 沙箱执行过程 |
| `judge.awaiting_input` | `{pending_task, issues, run_id, session_id}` | 当前步骤等待用户补参 |
| `run.summary` / `run.completed` | `{message}` | 汇总/正常完成 |
| `run.failed` | `{error_code, terminal_status, failed_tasks, message}` | 工作流失败或部分失败；不会再发 `run.completed` |
| `tool.risk.detected/auto_repair/blocked` | `{risk_code, category, tool_name}` | 风险检测与处置 |
| `status` | `{status, message}` | 状态提示（如"正在搜索备用数据源"） |
| `token` | `{content}` | 文本增量，前端追加到当前 text block |
| `react_trace` | 兼容结构 | 仅用于历史持久化/旧消息回放；新 POST 流以 `run.* / tool.* / code.*` 为准 |
| `map` | `{layers, bbox}` | 地图块，前端 push 一个 MapBlock |
| `error` | `{code, message, trace_id}` | 错误事件，流终止 |
| `done` | `{trace_id, run_id}` | 正常结束事件，流终止 |

**事件示例：**

```
event: status
data: {"status":"thinking","message":"正在分析你的问题..."}

event: token
data: {"content":"我来帮你查询"}

event: token
data: {"content":"南京新街口附近的蜜雪冰城。"}

event: status
data: {"status":"fetching","message":"正在搜索备用数据源..."}

event: map
data: {"layers":[{"type":"FeatureCollection","features":[...]}],"bbox":[118.77,32.04,118.79,32.05]}

event: token
data: {"content":"共找到 12 家蜜雪冰城，其中 3 家来自 OpenStreetMap 补充数据。"}

event: done
data: {"trace_id":"trace_def456"}
```

### 2.3 各事件 payload schema

```python
# 见 02_data_models.md 的 SSEEvent union，此处列出字段说明

# status 事件
{
  "status": "thinking" | "fetching" | "summarizing" | "done" | "error",
  "message": string  // 展示给用户的状态文案
}

# token 事件
{
  "content": string  // 文本增量片段（非完整文本）
}

# run.plan 事件
{
  "planner_source": "root_llm" | "guardrail" | "fallback",
  "instructions": [{"id": string, "text": string}],
  "tasks": [{
    "id": string,
    "agent_role": string,
    "tool_name": string,
    "goal": string,
    "depends_on": string[],
    "instruction_id": string,
    "status": "pending"
  }]
}

# tool.call.start 事件
{
  "tool_name": string,
  "tool_call_id": string,
  "params": object,
  "task_id": string,
  "agent_role": string
}

# tool.call.complete 事件
{
  "tool_name": string,
  "tool_call_id": string,
  "status": "success" | "empty" | "error",
  "result": object,
  "duration_ms": number
}

# map 事件
{
  "layers": MapLayer[],   // 见 02_data_models.md
  "bbox": [minLng, minLat, maxLng, maxLat]  // GCJ02 坐标
}

# error 事件
{
  "code": string,     // 见 §8 错误码表
  "message": string,  // 用户可读的错误消息
  "trace_id": string
}

# done 事件
{
  "trace_id": string,
  "run_id": string
}
```

### 2.4 SSE 连接管理

- **心跳**：后端每 15 秒发送 `: heartbeat\n\n` 注释行（不触发前端 event），防止代理超时断连
- **断线重连**：前端使用原生 `EventSource` 时浏览器自动重连，但 SSE 流不可恢复（对话状态在 Redis）。建议前端不自动重连 chat 流，而是提示"连接中断，请重试"
- **Last-Event-ID**：不支持。chat 是一次性流，不是可恢复的事件日志
- **客户端取消**：前端关闭连接后，后端通过 RunController 在 DAG 批次边界中止后续步骤

### 2.5 Dispatcher 与 SSE 的映射

Multi-Sub-Agent 编排各阶段的输出对应不同 SSE 事件：

```
planner_router(拆任务) → run.thought + run.plan
dispatch_node(派发)    → run.task.start / run.task.complete
普通原子步骤           → tool.call.* → Verifier/refinement → deterministic finalize
coder 回退             → code.generation / code.execution.* → Judge
缺少用户参数           → judge.awaiting_input（名称为兼容契约，不表示普通角色调用 Judge）
assemble_node(汇总)    → run.completed 或 run.failed
API 适配层            → map/token + done；失败只发 error，不再追加 done
```

---

## 3. `POST /api/upload` —— 文件上传

### 3.1 请求

```http
POST /api/upload HTTP/1.1
Content-Type: multipart/form-data

------boundary
Content-Disposition: form-data; name="file"; filename="nanjing_poi.zip"
Content-Type: application/zip

<binary data>
------boundary--
```

### 3.2 响应 `UploadResponse`

```json
{
  "file_id": "file_xyz789",
  "filename": "nanjing_poi.zip",
  "crs": "GCJ02",
  "original_crs": "EPSG:4490",
  "feature_count": 1247,
  "geometry_type": "Point",
  "preview": {
    "bbox": [118.77, 32.04, 118.79, 32.05],
    "sample_features": [...]
  },
  "warnings": ["检测到 3 个自相交几何，已自动修复"]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `file_id` | string | 上传后的引用 ID，传给 `/api/chat` 的 `upload_file_ids` |
| `crs` | string | 统一后的坐标系（国内数据为 `GCJ02`，国外为 `WGS84`） |
| `original_crs` | string | 原始坐标系（识别结果） |
| `feature_count` | int | 要素数量 |
| `geometry_type` | string | Point/LineString/Polygon/Multi* |
| `preview.bbox` | [4]float | 数据范围（坐标系与 `crs` 字段一致） |
| `preview.sample_features` | Feature[] | 前 5 个要素预览 |
| `warnings` | string[] | 警告信息（编码降级、拓扑修复等） |

### 3.3 限制

- **大小**：`UPLOAD_MAX_SIZE`（默认 50MB），超限返回 413
- **类型白名单**：`.zip`（shp 包）/ `.geojson` / `.kml`，其他返回 422
- **ZIP 内容校验**：解压后总大小不超过 500MB、文件数不超过 100，防 ZIP 炸弹（→ 详见 [06_security.md](06_security.md)）
- **存储**：内存解压，解析后转 GeoJSON 存 Redis（`upload:{file_id}`，TTL 1h），不落盘

---

## 4. Sessions API —— 会话管理

### 4.1 `GET /api/sessions` —— 列出会话

```json
{
  "items": [
    {
      "id": "sess_abc123",
      "title": "南京新街口蜜雪冰城查询",
      "created_at": 1720000000000,
      "updated_at": 1720000001000,
      "message_count": 2,
      "tool_count": 2,
      "has_map": true
    }
  ]
}
```

按 `updated_at` 降序返回，默认最多 200 条。支持 `?user_id=` 过滤（通过 `X-User-Id` 请求头自动注入）。

### 4.2 `POST /api/sessions` —— 创建会话

```json
// Response
{"id": "sess_xyz789", "title": "新会话", "created_at": 1720000000000, "updated_at": 1720000000000}
```

### 4.3 `PATCH /api/sessions/{id}` —— 重命名

```json
// Request
{"title": "新标题"}

// Response: 204 No Content
```

### 4.4 `DELETE /api/sessions/{id}` —— 删除会话

```json
// Response: 204 No Content
```

### 4.5 `GET /api/sessions/{id}/messages` —— 获取消息

```json
{
  "messages": [
    {"role": "user", "content": "南京新街口500米内有多少蜜雪冰城", "created_at": "..."},
    {"role": "assistant", "content": "找到 8 家蜜雪冰城...", "tool_calls": [...], "created_at": "..."}
  ]
}
```

**认证模型（临时方案）**：所有 sessions 端点通过 `X-User-Id` 请求头识别用户。缺失时默认 `"anonymous"`。生产环境必须替换为 JWT/OAuth2。

---

## 5. `GET /api/memory/{session_id}` —— 空间记忆读取

```json
{
  "session_id": "sess_abc123",
  "memories": [
    {
      "key": "常用原点",
      "label": "安师老校区",
      "location": [118.78, 32.05],
      "crs": "GCJ02",
      "created_at": "2026-07-09T10:00:00Z"
    }
  ]
}
```

---

## 6. `DELETE /api/memory/{session_id}` —— 清除空间记忆

```json
{
  "deleted": true,
  "session_id": "sess_abc123"
}
```

---

## 7. `GET /api/health` —— 健康检查

```json
{
  "status": "ok",
  "version": "1.6.0",
  "checks": {
    "redis": "ok",
    "celery": "ok",
    "llm": "ok",
    "amap": "ok"
  }
}
```

`status` 为 `ok` 当且仅当所有 checks 通过；任一失败返回 503 + `status: "degraded"`。LLM 和高德的检查为轻量探活（不发真实请求，只验证 key 格式和连通性）。

---

## 8. 统一错误响应

非 SSE 端点（upload/sessions/memory/health）的错误响应统一格式：

```json
{
  "error": {
    "code": "FILE_TOO_LARGE",
    "message": "上传文件超过 50MB 限制",
    "detail": {"max_size_mb": 50, "actual_size_mb": 78},
    "trace_id": "trace_def456"
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | string | 机器可读错误码，见下表 |
| `message` | string | 用户可读消息（中文） |
| `detail` | object | 可选，额外上下文 |
| `trace_id` | string | 贯穿 React Loop 的追踪 ID（→ 详见 [07_observability.md](07_observability.md)） |

**错误码表：**

| code | HTTP | 说明 |
|------|------|------|
| `INVALID_REQUEST` | 400 | 请求参数校验失败 |
| `FILE_TOO_LARGE` | 413 | 上传文件超限 |
| `UNSUPPORTED_FILE_TYPE` | 422 | 不支持的文件类型 |
| `FILE_PARSE_FAILED` | 422 | 文件解析失败（编码/格式） |
| `TASK_NOT_FOUND` | 404 | 异步任务不存在或已过期 |
| `RATE_LIMITED` | 429 | 触发速率限制 |
| `LLM_UNAVAILABLE` | 503 | LLM 服务不可用 |
| `INTERNAL_ERROR` | 500 | 未预期错误 |

**SSE 流内的错误**：不走 HTTP 状态码（SSE 响应头已 200），而是发送 `error` 事件后关闭流。前端监听到 `error` 事件后展示错误气泡。

---

## 9. HTTP 状态码约定

| 码 | 场景 |
|----|------|
| 200 | 成功（含 SSE 流的响应） |
| 400 | 请求体校验失败（Pydantic ValidationError） |
| 404 | 资源不存在（session/memory/run 不存在） |
| 413 | 上传文件超限 |
| 422 | 文件类型不支持 / 文件解析失败 |
| 429 | 速率限制（可选，个人 demo 默认不启用） |
| 500 | 未预期服务端错误 |
| 503 | 健康检查失败 / LLM 不可用 |

---

## 10. CORS 配置

开发环境允许 `http://localhost:*`，生产环境限定域名。详细配置见 [06_security.md](06_security.md) §CORS。

```python
# backend/app/main.py
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,  # dev: ["http://localhost:5173"]
    allow_credentials=False,               # 无认证，不需要 credentials
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
)
```

---

*文档版本：v1.4 | 最后更新：2026-08-31 | 属于 Gismind 补充文档*

*v1.3 变更：补充 run.plan 工具级 DAG 契约、task 的 tool_name/instruction_id、确定性步骤执行与 coder Code-Mode 分界；修正实时事件名为当前 run.*/tool.*/code.* 契约。*
*v1.1 变更：新增 §4 Sessions API（5 个端点）；SSE 事件 `tool_call` → `react_trace`；新增 X-User-Id 临时认证说明；更新 Dispatcher 与 SSE 映射*
*v1.4 变更：补充 `planner_source` 来源枚举、`run.failed` 的终态字段，并明确失败流不再追加 `run.completed`。*
