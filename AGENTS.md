# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project

Gismind 是基于 Multi-Sub-Agent 架构的空间智能 GIS Agent：用户用自然语言触发坐标转换、POI 查询、空间分析与可视化。后端是 FastAPI + Python，前端是 React 18 + TypeScript + Vite。

设计文档位于 `docs/`：
- `docs/GIS_Agent_技术文档.md` — 主设计文档（v1.7）
- `docs/01_api_spec.md` — API 与 SSE 契约
- `docs/02_data_models.md` — Pydantic / TS 模型
- `docs/03_config_env.md` — `.env` 配置说明
- `docs/04_testing_strategy.md` — 测试策略与黄金用例
- `docs/05_llm_prompts.md` — Dispatcher / Sub-Agent / Verifier Prompts
- `docs/06_security.md` / `docs/07_observability.md`
- `docs/MANUAL_TESTING.md` — 手动回归与准确性对照手册

## Common Commands

### Backend

```bash
cd backend
python -m pytest tests/ -q                           # 全量测试
python -m pytest tests/unit/test_geo_transform.py -v # 单文件测试
python -m pytest tests/unit/test_geo_transform.py::test_wgs84_to_gcj02_golden -v  # 单测试
uvicorn app.main:app --reload                        # 启动开发服务
```

### Frontend

```bash
cd frontend
npm run dev        # 开发服务（默认 5173，代理 /api 到 :8000）
npm run build      # 类型检查 + 构建
npm run typecheck  # TypeScript 类型检查
```

## Architecture

### Multi-Sub-Agent（`app/agents/`）

**Root Dispatcher** (`dispatcher.py`) 是顶层编排器，LangGraph 状态机流程：

`planner_router` → `dispatch_node` → `assemble_node` → END

- `planner_router`：一次生成工具级 `TaskPlan` DAG。Prompt 先拆 `PlanInstruction`，每个 SubTask 指定 `agent_role`、`tool_name`、`instruction_id` 和依赖关系；服务端校验指令覆盖、ID、依赖、环和角色/工具权限。
- `dispatch_node`：拓扑排序后按批次执行；无依赖任务并行，同角色连续动作通过 `depends_on` 顺序执行，失败依赖会阻断下游。
- `assemble_node`：汇总所有 SubAgentOutcome，生成成功/失败终态；不再依赖 Judge 猜测整个 Prompt 是否完成。
- `verifier_node`：独立 LLM 审查 sub-agent 输出，输出 `VerifierOutput`（approved/reason/refinement_hints/confidence）。
- `refine_router`：Verifier 未通过时路由到 refinement 流程。

### Sub-Agent 编译（`build_sub_agent.py`）

每个 sub-agent 是独立的 LangGraph 子图：
- 普通角色（geo/poi/geometer/viz）：Schema Planner → ToolExecutor → Observer → Verifier/refinement → `native_finalize`。只绑定 Root 指定的 `required_tool_name`，每次执行一个原子 JSON Schema Tool Call。
- `native_finalize` 根据最后一个 ToolResult 确定性完成或只重试当前失败步骤；同时接管 awaiting_input 的 PendingStore 持久化。
- coder：Code Planner → Sandbox → Observer → Judge。只有工具 Schema 无法表达的计算才使用 Code Mode。
- `run_sub_agent()` 以隔离的 `SubAgentState` 执行。

### Code-Mode Tools（`app/agents/code_mode/`）

Coder 将 LLM 从"填 JSON"模式翻转成"写 Python 代码"模式。**inline 执行路径已废弃（D2），所有模型生成代码统一走子进程 sandbox 执行**；普通 GIS 步骤不执行模型代码。

- **`HybridExecutor`**（`executor.py`）：主入口。fence/think-tag 预处理 → `ast_guard.inspect(code)` → sandbox 执行。
  - ~~`"inline"` → `ThreadPoolExecutor` 主进程 exec（不阻塞 event loop）~~ — 已废弃
  - `"sandbox"` → `SandboxExecutor`（子进程，可 kill）
  - `ASTBannedNodeError` → ExecutionResult error_code
- **`ast_guard.py`**：AST 静态分析，`required_executor` 路由信号。banned 节点抛 `ASTBannedNodeError`。**D2 后所有代码统一走 sandbox**。
- **`namespace.py`**：干净 namespace 构造（白名单 built-in + 只读模块 + async sync proxy + session_vars 命名冲突防护）。inline 路径已废弃，sandbox stub 保留但不再注入到 inline 路径。
- **`sandbox_runner.py`**：`SandboxExecutor` 包装 `run_in_sandbox`，***tempfile 注入***（避 Windows 32KB 命令行限制）+ UUID sentinel stderr 回捞 `__result__`。
- **`types.py`**：`ExecutionResult` / `InspectionResult` / `ASTBannedNodeError`

工具注册（`TOOL_SPECS`，`registry.py`）当前共有 **62 个规格项**。其中 **60 个**在
`tool_execution._TOOL_REGISTRY` 中有实际 handler；`fetch_from_redis` 与 `parse_zip`
仅保留规格元数据，不应计入当前可执行工具。按规格中的 `executor_type` 统计：

| 执行类型 | 规格数 | 有 handler | 说明 |
|---------|------:|-----------:|------|
| `inline` | 52 | 52 | 工具自身是本地计算 handler；模型生成代码仍统一在 sandbox 子进程中运行 |
| `async` | 8 | 7 | IO/外部服务经同步 proxy 包装；`fetch_from_redis` 当前无 handler |
| `sandbox` | 2 | 1 | `code_executor` 可执行；`parse_zip` 当前无 handler |

5 个 Kernel 语义工具（Toolkit / Skill / Workspace / Clarification）均有 handler。扣除它们后，
当前共有 **55 个有 handler 的非 Kernel 工具**。这是“工具接口数”，不等于产品功能完成数；
一个工具可能承载多个表现能力，部分工具也需要 Toolkit 激活后才会暴露。只有当前角色或
Toolkit 可见且有 handler 的 code-mode 工具才会以 kwargs Python 函数接口暴露给 LLM，
底层由 `_build_code_mode_tool_fns` 路由。

### 可用 Sub-Agent 角色

| 角色 | Prompt 文件 | 职责 |
|------|-----------|------|
| `geo` | `prompts/geo.md` | 地理编码、坐标转换、地名消歧 |
| `poi` | `prompts/poi.md` | POI 查询（高德优先、OSM 兜底） |
| `geometer` | `prompts/geometer.md` | 缓冲区、叠加分析、泰森多边形、等时圈 |
| `viz` | `prompts/viz.md` | 地图图层构建、可视化配置 |
| `coder` | `prompts/coder.md` | 代码解释器沙箱（自定义 Python 空间分析） |
| `verifier` | `prompts/verifier.md` | 审查 sub-agent 输出 |

### Agent State

- `SubAgentState`（`state.py`）：单个 sub-agent 的运行状态 — messages, iteration, tool_results, planner_output, final_output, agent_role, verifier_output 等。
- `AgentRootState`（`state.py`）：顶层编排状态 — task_plan, dispatched, dispatcher_events, root_verifier_output 等。
- 状态持久化：`SqliteSaver`（`checkpointer.py`）管理 LangGraph checkpoint。

### 关键模块

| 文件 | 职责 |
|------|------|
| `app/agents/dispatcher.py` | Root Dispatcher 状态机 + 拓扑分发 |
| `app/agents/build_sub_agent.py` | 普通 Schema 子图 / coder Code-Mode 子图编译 + 运行 |
| `app/agents/tool_execution.py` | 工具注册表 + `run_react_loop` 主入口 + `code_executor_node` + `_build_code_mode_tool_fns` |
| `app/agents/planner_factory.py` | Planner system prompt + few-shot 示例 + LLM 创建 + `build_code_mode_prompt` |
| `app/agents/planner_helpers.py` | `robust_parse_json()` 多策略 JSON 修复解析 |
| `app/agents/judge.py` | coder Judge 决策（CONTINUE/RETRY/FINISH） |
| `app/agents/observer.py` | 工具结果摘要为自然语言 |
| `app/agents/verifier_node.py` | 独立 Verifier LLM 审查（`mode` 字段分支 JSON / code） |
| `app/agents/refine_router.py` | Verifier 不通过时的 refinement 路由 |
| `app/agents/state.py` | SubAgentState / AgentRootState TypedDict（含 `session_vars`） |
| `app/agents/schemas.py` | PlanInstruction / SubTask / TaskPlan / VerifierOutput / SubAgentOutcome |
| `app/agents/checkpointer.py` | SqliteSaver 单例（WAL 模式） |
| `app/agents/context.py` | 上下文构建工具 |
| `app/agents/cost.py` | CostTracker — LLM token 成本追踪 |
| `app/agents/metrics.py` | 指标埋点 |
| `app/agents/errors.py` | 错误定义 |
| `app/agents/registry.py` | Agent 注册 + ToolSpec + TOOL_SPECS（工具注册表 + executor_type 分桶） |
| `app/agents/prompts/*.md` | 各角色 System Prompt（geo/poi/geometer/viz/coder/verifier/dispatcher） |
| `app/agents/code_mode/executor.py` | HybridExecutor — code-mode 主引擎（fence 预处理 → AST → sandbox 执行） |
| `app/agents/code_mode/ast_guard.py` | AST 静态分析 + `ASTBannedNodeError`（D2 后统一走 sandbox） |
| `app/agents/code_mode/namespace.py` | 干净 namespace 构造（async sync proxy + session_vars 保护） |
| `app/agents/code_mode/sandbox_runner.py` | SandboxExecutor（tempfile IPC + UUID sentinel stderr 回捞 + pywin32 Job Object） |
| `app/agents/code_mode/types.py` | ExecutionResult / InspectionResult / ASTBannedNodeError |

### Tool 层（`app/tools/`）

当前工具能力不能再概括成早期的 9 个基础工具。以 `TOOL_SPECS` 和
`_TOOL_REGISTRY` 的交集为准，共有 **60 个可执行 handler**：

| 类别 | 规格数 | 可执行数 | 代表工具 |
|------|------:|---------:|----------|
| 基础 / POI / 核心分析 / 可视化 / 沙箱 | 12 | 10 | `geo_code`、`query_poi`、`buffer`、`overlay`、`voronoi`、`isochrone`、`map_layer_build`、`geo_transform`、`code_executor` |
| Kernel 语义工具 | 5 | 5 | `select_toolkit`、`inspect_workspace`、`suggest_skill`、`load_skill`、`proactive_clarification` |
| Vector 分析 | 9 | 9 | 裁剪、融合、合并、空间连接、最近连接、点计数、位置提取、凸包、包围盒 |
| Vector 变换 | 10 | 10 | 质心、面内点、简化、修复、有效性检查、单部件化、去重、捕捉、重投影、批量重投影 |
| 属性处理 | 4 | 4 | 属性筛选、字段保留、字段改名、字段计算 |
| Raster | 16 | 16 | 重投影、裁剪、栅格计算、分区统计、采样、矢栅互转、坡度、坡向、阴影、等高线、重分类、TRI、TPI、粗糙度 |
| IO | 6 | 6 | 矢量/栅格/CSV 加载、CSV 转点、图层摘要、结果导出 |

> `fetch_from_redis`、`parse_zip` 虽仍在规格表中，但当前没有 handler，不能写成已经接入的正常可执行工具。

- `geo_code.py`：地理编码（地名→坐标），返回多候选含 disambiguated 标记
- `poi_query.py`：高德优先 + OSM 兜底，含 R-Tree 空间去重
- `spatial_analysis.py`：缓冲区、叠加、泰森多边形、等时圈、裁剪、连接、几何修复、属性与投影处理
- `raster_analysis.py`：16 个栅格分析接口，包括地形派生、裁剪、统计、采样、重分类和矢栅互转
- `geo_transform.py`：WGS84 ↔ GCJ02 数学偏转
- `data_io.py`：shp ZIP / GeoJSON / KML / GeoTIFF 上传读取，以及路径型 vector/raster/CSV 加载和结果导出
- `map_layer.py`：点、热力、面、线、FeatureCollection 与图表配置生成
- `policy.py`：工具执行策略
- `sandbox/runner.py`：代码沙箱子进程执行
- `sandbox/sitecustomize_gismind.py`：沙箱内建 import 黑名单 + socket 禁用
- `sandbox/tools.py`：沙箱可用工具白名单

### API 层（`app/api/`）

- `chat.py`：`POST /api/chat` SSE 流式返回。事件类型：`status` / `token` / `react_trace` / `map` / `done` / `error`。新增 `_merge_trace` 将 dispatcher_events 转为统一 react_trace 格式。
- `upload.py`：`POST /api/upload` 文件上传
- `sessions.py`：`GET/POST/PATCH/DELETE /api/sessions` 会话 CRUD
- `memory.py`：`GET/DELETE /api/memory/{session_id}` 空间记忆

### 坐标系约定

- 国内绝对基准是 **GCJ02**（火星坐标）。
- GCJ02 无标准 EPSG，**不能直接用 pyproj 做偏转**。
- 空间计算流程：GCJ02 → WGS84（数学偏转）→ 投影坐标系（EPSG:4548/4549）→ 计算 → WGS84 → GCJ02。
- 国外数据保持 WGS84。

详情见 `docs/GIS_Agent_技术文档.md` §4.2 与 `docs/02_data_models.md` §7。

### 前后端契约

- API：`POST /api/chat`（SSE）、`POST /api/upload`、`GET/POST /api/sessions`、`GET/PATCH/DELETE /api/sessions/{id}`、`GET/DELETE /api/memory/{session_id}`、`GET /api/health`。
- SSE 事件：`status` / `token` / `react_trace` / `map` / `done` / `error`。
- 前端类型：`frontend/src/types/message.ts`，与 `docs/02_data_models.md` §4 对齐。
- 地图渲染：后端返回 GCJ02 图层配置，前端用高德 JS API v2.0 直接渲染。数据源视觉隔离：高德橙色（`#ff7a1a`），OSM 灰色半透明。

### 数据与错误处理

- POI 查询（`app/tools/poi_query.py`）：高德优先，空/超时自动 Fallback 到 OSM；国内 OSM 转 GCJ02，国外保持 WGS84。
- 文件上传：`app/api/upload.py` 校验大小/类型/ZIP 安全；`app/tools/data_io.py` 解析与编码探测。
- 工具异常在 Tool 层吞掉，统一返回 `ToolResult(status="empty"/"error")`，不抛原始异常给 LLM。
- JSON 解析：`planner_helpers.robust_parse_json()` 提供 10 步修复策略（BOM 移除、markdown fence 剥离、控制字符转义等）。

### 会话与持久化

- 会话持久化：`app/utils/session.py` `SessionStore` 把每轮对话写入 Redis（`session:{id}`，TTL 24h）。支持 create / rename / get_meta / list_all / list_messages / save_full_messages。
- 空间记忆：`app/utils/memory.py` 存空间记忆（`memory:{id}`，TTL 30d）。
- Checkpoint：`app/agents/checkpointer.py` SqliteSaver 管理 LangGraph 状态持久化（`checkpoints.db`，WAL 模式）。

## Configuration

复制 `backend/.env.example` 为 `backend/.env`：
- `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`：DeepSeek 配置
- `AMAP_KEY`：高德 web 服务 key
- `AMAP_JS_KEY` / `AMAP_JS_SECURITY_CODE`：高德 JS API key（前端构建需要）
- `APP_CHECKPOINT_DB`：SqliteSaver 数据库路径
- `APP_ROOT_MAX_ITERATIONS`：Root Dispatcher 最大迭代（默认 30）
- `APP_WORKSPACE_DIR`：工作区输出目录，路径白名单根目录（默认 `./workspace`）
- `APP_SANDBOX_ENABLED`：代码沙箱开关（默认 true）
- `APP_INLINE_TIMEOUT_S`：HybridExecutor 超时秒数（默认 30；inline 路径已废弃，仅保留配置占位）

前端：`frontend/.env.local` 设置 `VITE_AMAP_KEY`、`VITE_AMAP_SECURITY_CODE`、`VITE_API_BASE_URL`。

## Current Capability Status

### 数量口径

- `docs/GIS_Agent_技术文档.md` 的设计级产品能力清单当前编号到 **55**，不是旧版的 45。
- `TOOL_SPECS` 有 **62** 个规格项；`_TOOL_REGISTRY` 有 **60** 个对应 handler。
- 60 个 handler 中包含 5 个 Kernel 语义工具，因此有 **55 个非 Kernel 可执行工具**。
- “产品能力数”“工具规格数”“有 handler 的工具数”是三个不同口径，禁止互相替代。
- 仅在 `TOOL_SPECS` 注册不代表可执行；仅在工具类中存在方法也不代表已接入 Root Planner、Sub-Agent 和公开 API。

### 已接入的主要链路

- 地理编码、坐标转换、POI 查询与高德 → OSM 兜底。
- GeoJSON、Shapefile ZIP、KML、GeoTIFF 浏览器上传，Redis `file_id` 持久化与 `data_io_read` 消费。
- 19 个 Vector 分析/变换工具和 4 个属性处理工具。
- 16 个 Raster 工具：重投影、按掩膜/范围裁剪、栅格计算、分区统计、采样、矢栅互转、坡度、坡向、山体阴影、等高线、重分类、TRI、TPI、粗糙度。
- 点、热力、面、线、FeatureCollection、Raster 图层和 ECharts 图表的前端消费与渲染。
- Dispatcher DAG、依赖产物传递、Verifier/refinement、awaiting_input/resume、取消、checkpoint、SSE trace 和 Code Mode sandbox。
- Event、Context Budget、Duplicate Guard 与 Run Control（设计清单 52–55）已经有对应模块并接入运行链路。
- 会话、空间记忆与上传索引使用 Redis；LangGraph checkpoint 使用 SQLite。

**Schema-first + Code-Mode fallback 已实现**：普通角色使用闭合 JSON Schema 单步调用；`coder` 在工具 Schema 无法表达时写 Python。D2 后 inline 路径已废弃，所有模型生成代码统一走子进程 sandbox：
- `HybridExecutor`：fence 预处理 → AST → sandbox 执行
- `ast_guard`：AST 静态分析 + `ASTBannedNodeError`（inline/sandbox 混合调用检测已随 inline 废弃移除）
- `SandboxExecutor`：tempfile IPC（避 Windows 32KB 命令行）+ UUID sentinel stderr 回捞 + pywin32 Job Object 保活
- 沙箱黑名单不可还原化（`sitecustomize_gismind.py`）：import 黑名单 + socket 禁用
- `namespace` + `TOOL_SPECS`：仅当前角色/Toolkit 可见且有 handler 的工具以 kwargs Python 函数暴露；async 工具走 sync proxy，模型生成代码统一在 sandbox 中执行
- `build_code_mode_prompt`：自动从 registry 分桶生成 system prompt

**Preflight / Hooks / Risk 状态**：规则库和 `BEFORE_TOOL_CALL` 已接入主链，preflight issue 可以进入 RiskPolicy 决策；
`AFTER_TOOL_CALL` 风险 hook 虽已注册，但主执行链当前没有发射该 hook，因此执行后结果风险检测尚未贯通。

### 部分实现，不能写成完整能力

| 设计清单项 | 当前状态 |
|------------|----------|
| 4 地理编码/逆编码 | 正向地理编码已接入；逆地理编码不是独立、完整的公开工具 |
| 5 格式互转管道 | 上传支持 ZIP/GeoJSON/JSON/KML/GeoTIFF；内部有 `load_csv`、CSV XY 转点和结果导出，但公开上传不接受 CSV/GPKG，CSV(WKT) 与完整格式互转未完成 |
| 6 高德 POI 查询 | 当前主链路支持关键词/周边查询；设计中的多边形搜索、ID 详情等模式未全部作为独立能力暴露 |
| 17 坡度/坡向/剖面 | `slope`、`aspect` 已实现并有 handler；剖面分析未实现，不能把整项写成“未实现” |
| 18 核密度估计 | `SpatialAnalyzer.kernel_density()` 有实现，但未注册为 `TOOL_SPECS`/handler，Root Planner 当前不可直接规划 |
| 19 拓扑检查与修复 | `check_validity`、`fix_geometries`、去重等已接入；`topology_check()` 方法未作为独立 Agent 工具暴露 |
| 20 属性表智能处理 | 属性筛选、字段保留/改名/计算已接入；Jenks、等距和分位数自动分级没有完整产品链路 |
| 26–27 热力图/专题图 | 热力图配置和基础样式存在；自动 Jenks 分级设色和完整专题图工作流仍不完整 |
| 44–46 缓存、限流、用户隔离 | Redis 会话/上传/空间记忆和外部服务超时兜底已实现；生产级 QPS 限流、认证和多用户安全隔离不在当前单机目标内 |
| 47–51 Hooks / Preflight / Risk / Toolkit / Skill | 基础框架和 handler 已存在；`AFTER_TOOL_CALL` 结果风险检测未进入主链，`inspect_workspace`、`suggest_skill`、`proactive_clarification` 等 Kernel 语义仍是简化或占位实现 |

### 当前未实现的设计能力

| 层 | 缺失能力 | 说明 |
|----|----------|------|
| 数据获取 | 实时交通路况（10）、街景抓取（11） | 没有对应公开工具和完整链路 |
| 空间分析 | 视线/通视（16）、地形剖面（17 的一部分） | 现有 DEM 工具不包含视线与剖面算法 |
| 遥感气象 | DEM 自动获取（21）、遥感影像/NDVI（22）、气象叠加（23）、时序分析（24） | 本地 Raster 计算已实现，但外部数据获取和时序工作流未实现 |
| 可视化交付 | 3D 地形（28）、时序动画（29）、MarkerCluster（30）、PDF/Markdown 报告生成（31） | 尚无完整前后端耦合链路 |
| 工程 | 生产认证、完整限流、多用户隔离、任务队列/worker | 当前定位是单机自用，不是公网多租户服务 |
| 可观测性辅助 | `utils/logging.py`（structlog） | 文档有设计，当前没有该独立实现；不要与已有标准 logging/trace 混淆 |

**上传文件链路**：`app/api/upload.py` 的 `_persist_upload` 写入 Redis `upload:{file_id}`（TTL 1h），`data_io_read` 从 Redis 取 bytes 再解析。
