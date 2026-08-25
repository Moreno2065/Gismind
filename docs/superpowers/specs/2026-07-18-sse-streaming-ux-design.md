# Gismind SSE 流式输出 UX 设计文档

> 日期：2026-07-18
> 状态：设计待实现（v1.1，经 Reader Testing 自审修订）
> 关联模块：`backend/app/agents/events/*`、`backend/app/api/chat.py`、`backend/app/agents/tool_execution.py`、`backend/app/agents/build_sub_agent.py`、`backend/app/agents/dispatcher.py`、`backend/app/sandbox/*`、`frontend/src/hooks/useSSE.ts`、`frontend/src/components/ThinkingCollapse.tsx`、`frontend/src/components/ChatPanel.tsx`、`frontend/src/types/message.ts`
> 上游计划：`docs/superpowers/plans/2026-07-17-gismind-remediation-plan.md`（本文是其 Task 4.10 事件链接线 + Phase 6 前端的细化设计，并扩展 UX 层）

**编号索引**：本文中的 `B#`（bug 编号）、`D#`（决策编号）、`Task #.#`（任务编号）均引自上游 remediation 计划；其中 **B13** = "chat.py 把 collector.emit 传给 `_run_loop_sync`，但 `run_react_loop` 没有 on_event 参数，事件在节点内拿到 None 静默丢弃"。**D3（Ensemble 直接移除）是已确认决策，执行进度取决于上游计划，本文不假设其已完成。**

## 背景与动机

一次典型查询（定位 → POI → 缓冲 → 图层）端到端约 30 秒，当前用户在 POST /api/chat 流上只能看到一条 `status: thinking` 和每 15s 的心跳——`react_trace` 是整个 run 结束后**事后补发**的（chat.py:421-428），过程完全黑盒。DPSK 的 UX 提案（timeline 渲染、代码/工具事件、沙箱输出回显）方向正确，必须吸收。

但有一个关键事实：**Gismind 的事件基础设施已经存在，只是没接线**。`events/__init__.py` 定义了 23 个 `EVENT_CONTRACTS`（:25-54）和线程安全的 `EventCollector`；`events/stream_adapter.py` 有 session 级 handler 注册表；发射点已存在于 `build_sub_agent.py:205`（code.generation）、`tool_execution.py:2011/2026/2030`（code.execution.start/complete/error）、`judge.py:243`（judge.awaiting_input）、`chat.py:374`（run.session）。问题只有一个：B13（见编号索引）。

因此本设计的第一原则是：**接线现有系统，不新建平行事件体系**。DPSK 提案中的事件类型全部映射到现有契约；仅允许在 `EVENT_CONTRACTS` 注册表内**扩充 key**（段 1.3 新增 2 个 sub-task 生命周期事件），不引入第二套 schema。

## 已敲定的 7 项决策

| # | 决策点 | 选择 | 理由 |
|---|--------|------|------|
| 1 | 事件系统 | 复用 `EVENT_CONTRACTS` + `EventCollector`，只做接线、扩充发射点、注册表内补 key（23 → 25） | 已有契约覆盖 DPSK 提案的几乎全部事件类别；双系统必然漂移 |
| 2 | SSE 通道 | **POST /api/chat 收敛为唯一实时流**；GET /api/chat/{id}/events 标记 deprecated，保留一个版本后删除 | 当前双通道两种方言（POST 有 `event:` 行、GET 只有 `data:` 行）已在漂移 |
| 3 | progress 事件 | **不伪造 total**。进度用两个真实上限：dispatcher 阶段的任务序号（`task_index`/`task_total`，N=TaskPlan 的 subtask 数）+ sub-agent 迭代轮次（`iteration`/`max_iterations`，来自 registry 的 spec） | DPSK 伪代码的 `step: 2, total: 4` 在动态 agent 循环里是编造的；假进度条比没有更糟 |
| 4 | planner 代码"打字机" | **v2 可选**。MVP 在 planner 返回后一次性 emit `code.generation` | planner 是非流式结构化 LLM 调用；打字机需改 LLM client 为流式 + 容忍半截 fence 的增量解析，成本高收益纯视觉 |
| 5 | 沙箱 stdout/stderr 回显 | **MVP 结束后回放**（截断 4KB）；**v2 增量实时**（见段 5 的管道阻塞风险与 sentinel 过滤要求） | 当前 `communicate()` 是批量读取（sandbox/runner.py:105），DPSK 说"几乎免费"不实 |
| 6 | 事件层级 | 所有事件携带 `run_id` / `session_id` / `task_id` / `agent_role` / `iteration`，前端按 task_id 分组渲染 timeline | Gismind 是 multi-sub-agent 架构，DPSK 的线性 fast-path 伪代码不符合现实 |
| 7 | consensus 事件 | 随 Ensemble 移除（remediation D3 执行时）从前端类型与文档中删除 | 决策已确认；若 D3 未执行则前端保留类型但不渲染 |

## 对 DPSK 提案的吸收与修正

| DPSK 提议 | 处置 | 说明 |
|-----------|------|------|
| timeline/折叠卡片渲染 | **吸收** | 扩展现有 `ThinkingCollapse` 为 `TraceTimeline`，display_kind 驱动样式 |
| `thinking` 事件 | **映射** → `run.thought`（debug，默认折叠）+ `code.generation` 附带 planner 推理摘要 | 不新增事件名 |
| `code` 事件 | **映射** → `code.generation`（已有契约，payload 含 code/iteration） | 同上 |
| `tool_call` / `tool_result` | **映射** → `tool.call.start` / `tool.call.complete`；code-mode 下工具调用发生在沙箱**内部**，事件经 RPC 通道回传（段 1.6） | code-mode 的工具调用不是 planner 的直接输出，事件源在 sandbox worker |
| `progress step/total` | **修正** | 见决策 3 |
| 沙箱 stderr 实时流 | **降级为 v2** | 见决策 5 |
| "显示所有细节" | **修正** | display_kind=debug 默认折叠；warning/confirmation 才打断用户 |

## 段 1：后端事件链路

### 1.1 目标链路

```
节点内 emit_event(...)                          ← 发射点（见 1.3 扩充表）
  → contextvar 取当前 run 的 handler             ← 新增 events/current.py（见 1.2）
  → EventCollector.emit（events/__init__.py:95）  ← 已有，线程安全
  → POST /api/chat event_stream 实时桥接          ← chat.py 改造（见 1.4）
  → SSE: event: <contract> \n data: <json>       ← 与前端 useSSE 对齐
```

### 1.2 on_event 传递：contextvar，禁止入 state

新增 `backend/app/agents/events/current.py`：

```python
import contextvars
_current_handler: contextvars.ContextVar = contextvars.ContextVar("gismind_event_handler", default=None)

def set_current_handler(handler): return _current_handler.set(handler)
def reset_current_handler(token): _current_handler.reset(token)
def get_current_handler(): return _current_handler.get()
```

- `run_react_loop`（tool_execution.py:1996 附近）新增 `on_event` 参数，入口处 `set_current_handler(on_event)`，`finally` reset。
- 节点内现有 `state.get("_on_event")`（build_sub_agent.py:198、tool_execution.py:2009、judge.py:241）改为 `get_current_handler() or state.get("_on_event")`（过渡期双读，稳定后删 state 路径）。
- **反模式守卫**：callable 不可 pickle，禁止把 handler 写入 LangGraph state 通道——SqliteSaver checkpoint 会炸。
- **线程可见性约束**：`asyncio.to_thread` 会传播 contextvar，sub-agent 节点链（均在 to_thread worker 线程内同步执行）天然继承；但**发射点不得位于 `_run_async` 的 thread-local loop 线程**（tool_execution.py:52-114，contextvar 不可达）——如未来需要在那里发事件，必须显式把 handler 作为参数穿进去，不得依赖 contextvar。
- `stream_adapter.py` 的 session 级注册表与 contextvar 方案二选一——**保留 contextvar，stream_adapter 标 deprecated**（进程级 dict 有 session 泄漏面，且与 per-run handler 语义重复）。

### 1.3 发射点扩充表（含 2 个新增契约 key）

**新增契约**（加入 `EVENT_CONTRACTS`，display_kind 分别为 progress / workflow_step）：

- `run.task.start`：一个 subtask 开始分发
- `run.task.complete`：一个 subtask 产出 outcome（成功或失败）

| 事件 | 发射位置 | payload（除公共层级字段外） |
|------|---------|------------------------------|
| `run.session` | chat.py:374（已有） | trace_id |
| `run.thought` | dispatcher planner_router 解析出 TaskPlan 后（dispatcher.py:206-234 区域） | `task_count`, `summary`（≤120 字） |
| **`run.task.start`**（新） | dispatcher dispatch_node 每个 subtask 启动时（dispatcher.py:648 附近） | `task_id`, `agent_role`, `goal`（≤80 字）, **`task_index`, `task_total`** |
| **`run.task.complete`**（新） | dispatch_node 收到 outcome 后 | `task_id`, `status`, `error_code?`, `duration_ms` |
| `code.generation` | build_sub_agent.py:205（已有），payload 补 `iteration`, `max_iterations`, `code` | code 截断 2000 字符 |
| `code.execution.start` | tool_execution.py:2011（已有） | `executor_type`（sandbox） |
| `code.execution.stdout` | tool_execution.py 执行完成后（MVP 回放；v2 增量见段 5） | `content` ≤4KB |
| `code.execution.stderr` | 同上 | `content` ≤4KB（sentinel 行已过滤） |
| `code.execution.complete` / `.error` | tool_execution.py:2026/2030（已有） | `duration_ms`, `error_code` |
| `tool.call.start` / `tool.call.complete` | **code-mode：沙箱 RPC 通道回传（段 1.6）**；JSON 路径在 tool proxy（tool_execution.py:1791-1807） | `tool_name`, `status`, `summary`（复用 observer 摘要，≤100 字） |
| `tool.preflight.warning` / `.blocked` | preflight runner（上游 Phase 1 修复注册后生效） | `stage`, `code`, `tool_name`, `message` |
| `judge.decision` | judge.py 决策点 | `decision`, `reason`（≤80 字）, `iteration` |
| `judge.awaiting_input` | judge.py:243（已有） | `pending_task` 全量（含 missing_slots/choices，对齐上游 Task 5.3） |
| `run.paused` / `run.completed` / `run.failed` | run_control / chat.py 收尾 | `reason` |

**反模式守卫**：payload 一律截断（code 2000、stdout/stderr 4096、文本摘要 120、args 复用前端 `formatArgs` 的 40 字符思路在后端先截）。禁止把 GeoJSON/图层数据放进事件——`map` 事件是唯一数据通道。

### 1.4 chat.py 实时桥接（替换事后补发）

先给 `EventCollector` 加一个带超时的公共取数方法（避免 `asyncio.wait_for(anext(...))` 反复取消 async generator 的脆弱模式）：

```python
# events/__init__.py 新增
async def get(self, timeout: float) -> dict | None:
    """Return next event or None on timeout. Thread-affinity: event loop only."""
    try:
        return await asyncio.wait_for(self._queue.get(), timeout)
    except asyncio.TimeoutError:
        return None
```

`event_stream()` 改造（chat.py:362-479）：

```python
loop_task = asyncio.create_task(asyncio.to_thread(...))   # 不变
while not loop_task.done():
    item = await collector.get(timeout=15)
    if item is None:
        yield ": heartbeat\n\n"
    else:
        yield sse_format(item["event"], item)             # 实时转发
# run 结束后 drain 残余事件（while (item := await collector.get(0.2)) is not None: yield ...）
# 随后 map → token → done 不变
collector.stop()
```

- `_merge_trace` 事后补发（:421-428）**降级为持久化用途**：实时 trace 已由事件流覆盖；合并结果存入 session 供刷新回放，不再逐条 yield 到 POST 流（否则同一 trace 出现两次）。
- `collector.clear_dedup()` 在每次 run 开始时调用（修复：dedup 集合跨 run 存活导致第二轮相同 preflight 警告不再上报）。
- collector 泄漏防护：`_COLLECTOR_TTL_S`（chat.py:49 定义未使用）启用为 2h TTL 清扫；`finally` 清理逻辑（:506-510）保留。
- 断连联动（上游 Task 4.9）：`except asyncio.CancelledError` 分支先 `run_ctrl.request_cancel()` 再 `collector.stop()`。该分支现有的 `yield sse_format("error", ...)` 是 best-effort——客户端已断开，实际无人能收到，保留仅为日志语义，不得依赖它做前端收尾。

### 1.5 错误与边界

- 节点内 emit 失败已被 `emit_event` 吞掉记 warning（events/__init__.py:217-218），保持——事件链绝不能影响主链路。
- collector 队列不设上限：stdout 高频事件需发射端自律（v2 增量时按 4KB chunk、≤10 chunk/s 节流）。

### 1.6 沙箱内工具事件回传（code-mode 的 tool.call.* 事件源）

code-mode 下工具调用发生在**沙箱子进程内部**（LLM 代码里的 `buffer(...)` 经 RPC 白名单通道回主进程执行，该通道由上游 Phase 2 建设）。事件设计：

- **发射侧**：主进程的 RPC 执行入口（即未来 `_build_code_mode_tool_fns` 的 RPC client 侧）在发出 RPC 请求前 `emit_event(tool.call.start)`、收到响应后 `emit_event(tool.call.complete)`，payload 含 `tool_name/status/summary/duration_ms`；summary 复用 observer 的自然语言摘要逻辑。
- **线程约束**：补发点位于 code_executor_node 所在的 to_thread worker 线程，contextvar 经 `asyncio.to_thread` 传播可达（满足 1.2 的线程约束）；**不要**试图从子进程直接往 collector 发事件（跨进程不可达，只能是主进程补发）。
- **降级**：RPC 通道建成前（上游 Phase 2 完成前），code-mode 的 timeline 粒度到 `code.execution.*` 为止，`tool.call.*` 仅 JSON 模式可见——前端渲染不得假设 tool.call.* 必然出现。

## 段 2：SSE 契约变更（对前端）

- 事件名 = `EVENT_CONTRACTS` 的 25 个 key + 现有 `status`/`token`/`map`/`done`/`error`。`react_trace`/`sub_task`/`verify`/`reflect`/`consensus` 从流上退役（consensus 随 ensemble 删除；其余被 run.task.*/tool.call.*/judge.decision 取代）。
- 所有契约事件的 data 公共字段：`event`, `event_type`, `display_kind`, `message`, `timestamp`, `run_id`, `session_id`, `task_id?`, `agent_role?`, `iteration?`。
- `docs/01_api_spec.md` §2 与 `docs/02_data_models.md` §4 同步重写（前端 types 已领先文档，本次一并回写）。

## 段 3：前端设计

### 3.1 类型与解析

- `types/message.ts`：`SSEEvent` union 重写为上述契约集；删除 `consensus`；`AwaitingInputEvent` 对齐上游 Task 5.3 的 PendingTask 字段。
- `useSSE.ts:50` known 列表改为从类型层导入的常量数组（单一事实源）；`decodeFrame` 的 JSON.parse 失败与未知事件改为 `console.warn`（当前静默丢帧，token 丢一帧就是丢一段文本）。
- ChatPanel 的 SSE switch 补 `default` 分支；`judge.awaiting_input` 渲染为可操作块（显示 pending_task.message + choices，解除输入框禁用，提交后走 resume 端点）。

### 3.2 TraceTimeline 组件（新）

以 `ThinkingCollapse` 的渲染件（ToolCallLine/ReactTraceLine/code 块）为积木，新组件 `TraceTimeline`：

- **分组**：按 `task_id` 分组成 sub-task 卡片（头部：`agent_role` 图标 + goal 摘要 + 状态点）；root 级事件（run.thought/run.completed）独立成行。
- **行渲染规则（display_kind 驱动）**：
  - `progress` → 时间线行（进行中动画点）
  - `workflow_step` → 完成行（✓ + 摘要）
  - `warning` → 黄色行（preflight/risk/空结果）
  - `confirmation` → 展开为 awaiting_input 操作块
  - `debug` → 默认折叠进"细节"（run.thought/stdout/stderr/judge.decision）
  - `result` → 收束行（run.summary/run.completed）
- **代码块**：`code.generation` 渲染为可折叠 `<pre>`（复用 ThinkingCollapse:137-141 的截断样式）；`executor_type` 角标保留。
- **实时态**：run 进行中 timeline 常驻消息顶部（替换现在的单一 status 条）；run 结束后整体坍缩为 ThinkingCollapse 现状样式（默认收起——"过程感在运行时，复盘时不吵"）。
- **进度展示**：`task i/N` 来自 `run.task.start` 的 task_index/task_total；sub-agent 内部显示 `第 k/m 轮`（code.generation 的 iteration/max_iterations）。

### 3.3 一并修复（本设计的前置依赖，已在上游 Phase 6 列项）

F1 停止按钮状态收尾、F2 session 切换守卫、F3 畸形图层 try/catch、断流未收 done 误判成功（useSSE 记录 terminal 标志，无终端事件走 onError）。

## 段 4：安全与性能

- **XSS**：timeline 所有文本渲染必须转义（`buildPopup` 已有 escapeHtml 先例，trace 渲染一视同仁）；code/stdout 用 `<pre>` 纯文本节点，禁 `dangerouslySetInnerHTML`。
- **数据面**：事件 payload 只放摘要与截断文本；沙箱 stdout/stderr 可能含用户数据片段——截断 4KB 即可，无需额外脱敏（本地单人部署，上游 D1）。
- **checkpoint 卫生**：handler 走 contextvar，不进 state；事件不写 checkpoint（`dispatcher_events` 通道现状保留用于持久化；上游 Task 5.6 的审计报告应改从事件流取数）。
- **速率**：stdout 增量（v2）发射端节流 4KB×10/s；SSE 心跳维持 15s。

## 段 5：v2 增量沙箱输出（可选，明确风险）

`run_in_sandbox` 当前 `communicate()` 批量读（sandbox/runner.py:105）。增量方案：`Popen` 后独立线程持续读 stderr 管道行，sentinel 行（`__GISMIND_STATE_*__`）进结果缓冲、其余行作为 `code.execution.stderr` 事件转发。**两个硬约束**：

1. **必须持续 drain 管道**——Windows 管道缓冲约 4KB，子进程写满即阻塞，比"不实时"更糟。
2. **结果通道与日志通道复用 stderr 的脆弱性**（结果回传走 stderr + 正则，stderr 同时是 traceback 通道）：v2 若 stdout/stderr 分管，需同步把 sentinel 结果通道迁到独立 tempfile 或独立 fd，不得在同一流里继续正则回捞。

验收前不得以"实时输出"名义提交批量回放。

## 段 6：测试与验收

1. **契约同步测试**：后端 `EVENT_CONTRACTS` keys 与前端 known 事件数组做 CI 对拍（脚本比对，防双向漂移）。
2. **wiring 冒烟**：每个发射点一条用例——触发对应节点，断言 collector 收到该事件（B13 的防回归闸）。
3. **e2e**：mock 最底层 LLM 跑一次"POI+buffer"，断言 POST 流事件序列包含 run.session → run.task.start → code.generation → code.execution.start/complete →（RPC 建成后 tool.call.complete）→ map → done，且 react_trace 不再逐条出现。
4. **前端单测**：useSSE 解析器（多行 data/粘包/半帧/AbortError/无 done 断流）；TraceTimeline 按 task_id 分组与 display_kind 样式映射。
5. **手动回归**：MANUAL_TESTING.md 新增"过程可视"用例（30s 任务中用户能看到 ≥5 个阶段行）+ awaiting_input 交互用例。

## 段 7：里程碑

| 里程碑 | 内容 | 依赖 |
|--------|------|------|
| M1 接线 MVP | 段 1.1-1.5 + 前端段 3.1/3.2（不含 v2） | 上游 Phase 1（B1/B10 修好后 preflight 事件才有源） |
| M2 交互完整 | awaiting_input 操作块 + 进度细化 + F1/F2/F3 | 上游 Task 5.3（PendingTask 完整化） |
| M3 v2 增值 | planner 打字机（流式 LLM client）+ 沙箱 stderr 增量（段 5）+ 段 1.6 的 tool.call.* 全量可见 | 上游 Phase 2（sandbox RPC 通道建成） |

## 反模式守卫汇总（评审时逐条核对）

1. 禁止新建第二套事件系统/schema——一切映射到 EVENT_CONTRACTS（仅允许注册表内扩充 key）。
2. 禁止把 callable handler 写入 LangGraph state 或 checkpoint。
3. 禁止伪造 progress total（只允许 task_index/task_total 与 iteration/max_iterations 这两个真实上限）。
4. 禁止在事件 payload 中携带未截断数据（GeoJSON、完整 POI 列表、大段代码）。
5. 禁止 react_trace 双轨长期并存（实时流接入后，POST 上的事后逐条补发必须删除，仅留持久化副本）。
6. 禁止以"实时沙箱输出"名义提交批量回放充数（段 5 验收条款）。
7. 禁止在 `_run_async` 的 thread-local loop 线程内依赖 contextvar 发事件（1.2 线程约束）。
8. 禁止从沙箱子进程直接发事件——tool.call.* 只能由主进程 RPC 侧补发（段 1.6）。
