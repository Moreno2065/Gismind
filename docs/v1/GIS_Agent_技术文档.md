# GIS Agent 项目技术文档

> 基于 LangGraph StateGraph Multi-Sub-Agent 架构的空间智能 Agent，支持自然语言驱动的坐标转换、POI 查询、空间分析与可视化输出。

> 当前项目定位为单机自用。核心前后端链路与 GIS 工具已完成本地自动化验证；真实 LLM 和外部服务仍可能波动。运行范围、测试证据和已知限制见 [Gismind 单机运行状态](LOCAL_SINGLE_MACHINE_STATUS.md)。

---

## 1. 项目概述

本项目构建一个具备自然语言理解能力的 GIS Agent，用户可以通过日常语言（如"南京新街口 500 米内有多少蜜雪冰城"）触发复杂的空间分析任务。Agent 基于 **LangGraph StateGraph** 编排架构，Root Dispatcher 采用 `planner_router → dispatch → assemble` 三节点图。模型一次生成经过 Pydantic 校验的工具级 `TaskPlan` DAG；服务端负责拓扑调度、依赖传递、失败阻断与完成判断。

核心设计原则：
- **自然语言即接口**：用户无需了解 GIS 术语
- **多源数据融合**：高德 API + OpenStreetMap 双源互补
- **结构化多步 DAG**：一个 Prompt 中的多条原子指令一次规划，每个 Task 对应一个明确的 `tool_name`
- **确定性调度**：无依赖任务并行、同角色连续动作按 `depends_on` 顺序执行，完成状态不由 LLM 猜测
- **Schema 优先、Code 兜底**：普通 GIS 步骤使用闭合 JSON Schema；仅 `coder` 处理工具 Schema 无法表达的计算
- **现成算法优先**：核心空间计算依赖成熟开源库，专注业务胶水层

---

## 2. 系统架构

```
+-------------------------------------------------------------+
|                        用户层                                |
|  自然语言输入 / 文件上传 / 地图交互（单页对话流）              |
+-------------------------------------------------------------+
                              |
+-------------------------------------------------------------+
|                     前端层 (React + SSE)                     |
|  +------------+  +------------+  +------------+              |
|  | ChatPanel  |  | MessageBubble | | LazyMapView |          |
|  | 聊天面板    |  | 混合渲染气泡   | | 懒加载地图   |          |
|  +------------+  +------------+  +------------+              |
|  SSE 流式接收: token(文本) | map(地图) | status(状态)       |
+-------------------------------------------------------------+
                              |
+-------------------------------------------------------------+
|        LangGraph StateGraph Multi-Sub-Agent 编排层            |
|                                                              |
|  +-------------------------------------------------------+  |
|  | Root Dispatcher (dispatcher.py)                       |  |
|  |  ┌──────────────┐   ┌──────────┐   ┌──────────────┐  |  |
|  |  │planner_router│ → │dispatch  │ → │  assemble    │  |  |
|  |  │  意图拆解     │   │拓扑批次派发│   │汇总+终态输出  │  |  |
|  |  └──────────────┘   └──────────┘   └──────────────┘  |  |
|  |                            │                │         |  |
|  |                     依赖产物变量注入          ↓         |  |
|  |                                       FINISH / FAILED  |  |
|  +-------------------------------------------------------+  |
|                              |                               |
|      Tool-level TaskPlan DAG → 按依赖拓扑派发 Sub-Agent      |
|                              |                               |
|  +----------+ +----------+ +----------+ +----------+        |
|  | geo      | | poi      | | geometer | | coder    |        |
|  | 地理编码  | | POI 查询  | | 空间分析  | | 代码沙箱  |        |
|  +----------+ +----------+ +----------+ +----------+        |
|  +----------+ +----------+                                  |
|  | viz      | | verifier |  普通角色: Schema Planner →       |
|  | 图层构建  | | 结果审查  |  ToolExec → Observer → Finalize |
|  +----------+ +----------+  coder: Code → Sandbox → Judge   |
|                              |                               |
|  +-------------------------------------------------------+  |
|  |  横切系统 (贯穿整个编排生命周期)                         |  |
|  |  hooks pipeline | preflight rules | risk system      |  |
|  |  context_budget | duplicate_guard  | run_control     |  |
|  +-------------------------------------------------------+  |
+-------------------------------------------------------------+
                              |
+-------------------------------------------------------------+
|                      工具服务层                              |
|  +----------+ +----------+ +----------+ +----------+       |
|  | 坐标转换  | | POI 查询  | | 空间分析  | | 数据 I/O  |       |
|  +----------+ +----------+ +----------+ +----------+       |
|  +----------+ +----------+ +----------+                   |
|  | 图层构建  | | 代码沙箱  | | 遥感处理  |                   |
|  +----------+ +----------+ +----------+                   |
|                                                              |
|  toolkit (55+ tools) | skill system | events (23 types)     |
+-------------------------------------------------------------+
                              |
+-------------------------------------------------------------+
|                      数据接入层                              |
|  高德 API  /  OSM Overpass  /  DEM(SRTM)  /  气象 API       |
+-------------------------------------------------------------+
```

### 2.1 项目目录结构

```
gis-agent/
├── backend/
│   ├── app/
│   │   ├── main.py                  # FastAPI 入口
│   │   ├── agents/
│   │   │   ├── dispatcher.py        # Root Dispatcher — planner_router → dispatch → assemble 三节点图
│   │   │   ├── build_sub_agent.py   # Sub-Agent 图编译 + 运行
│   │   │   ├── tool_execution.py    # 工具注册表 + 执行调度
│   │   │   ├── planner_factory.py   # Planner prompt + few-shot + LLM
│   │   │   ├── planner_helpers.py   # robust_parse_json 多策略修复
│   │   │   ├── judge.py             # coder Code-Mode 的 Judge 决策
│   │   │   ├── observer.py          # 工具结果摘要
│   │   │   ├── verifier_node.py     # 独立 Verifier LLM 审查
│   │   │   ├── refine_router.py     # Refinement 路由
│   │   │   ├── state.py             # SubAgentState / AgentRootState
│   │   │   ├── schemas.py           # SubTask / TaskPlan / VerifierOutput
│   │   │   ├── checkpointer.py      # SqliteSaver 单例 (WAL)
│   │   │   ├── context.py           # 上下文构建
│   │   │   ├── cost.py              # CostTracker — Token 成本追踪
│   │   │   ├── code_mode/           # Code-Mode 执行（统一走 sandbox）
│   │   │   │   ├── executor.py      # SandboxExecutor — fence 预处理 → AST → sandbox
│   │   │   │   ├── ast_guard.py     # AST 静态分析 + 安全校验
│   │   │   │   ├── namespace.py     # build_namespace — 白名单 built-in
│   │   │   │   ├── sandbox_runner.py # 子进程隔离 IPC + sentinel stderr 回捞
│   │   │   │   └── types.py         # ExecutionResult / InspectionResult
│   │   │   ├── hooks/               # Hooks 流水线（事件生命周期拦截）
│   │   │   ├── preflight/           # Preflight 规则（执行前准入检查）
│   │   │   ├── risks/               # 风险评分系统（risk score + 降级策略）
│   │   │   └── workspace/           # 工作空间管理（session 隔离 + 文件生命周期）
│   │   ├── tools/
│   │   │   ├── geo_transform.py     # 坐标转换（GCJ02 基准）
│   │   │   ├── geo_code.py          # 地理编码（多候选 + 消歧）
│   │   │   ├── poi_query.py         # 高德 + OSM POI（内部 Fallback）
│   │   │   ├── spatial_analysis.py  # 空间分析（结果回 GCJ02）
│   │   │   ├── data_io.py           # shp ZIP / geojson / kml 上传处理
│   │   │   ├── map_layer.py         # 图层数据配置生成（供前端高德 JS API）
│   │   │   └── policy.py            # 工具执行策略
│   │   ├── sandbox/
│   │   │   ├── runner.py            # 子进程代码沙箱
│   │   │   ├── tools.py             # 沙箱可用工具白名单
│   │   │   └── sitecustomize_gismind.py # import 黑名单 + socket 禁用
│   │   ├── toolkit/                 # 工具集系统 — 工具注册/发现/分组/参数校验
│   │   ├── skill/                   # Skill 系统 — 可组合的技能能力
│   │   ├── events/                  # 事件系统 — 23 种事件类型的发射与消费
│   │   ├── api/
│   │   │   ├── chat.py              # POST /api/chat SSE 端点
│   │   │   ├── upload.py            # POST /api/upload 文件上传
│   │   │   ├── sessions.py          # /api/sessions CRUD
│   │   │   └── memory.py            # /api/memory 空间记忆
│   │   ├── models/
│   │   │   └── schemas.py           # Pydantic 模型（ChatRequest/ToolResult/SessionMeta 等）
│   │   ├── utils/
│   │   │   ├── session.py           # SessionStore — Redis 会话持久化
│   │   │   ├── memory.py            # 空间记忆 Redis 持久化
│   │   │   └── redis.py             # Redis 连接管理
│   │   ├── session_memory.py        # 会话级空间记忆管理
│   │   ├── run_control.py           # 运行控制（迭代上限、超时、强制终止）
│   │   ├── errors.py                # 统一错误类型定义 + 注册表
│   │   ├── metrics.py               # 指标埋点（耗时、Token、工具调用次数）
│   │   ├── context_budget.py        # 上下文预算管理（截断策略 + 窗口管理）
│   │   ├── duplicate_guard.py       # 重复调用守卫（幂等检测 + 缓存命中）
│   │   └── pending.py               # 异步任务挂起管理（长时间任务状态追踪）
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   └── src/
│       ├── components/
│       │   ├── ChatPanel.tsx            # 聊天面板（消息列表+输入框）
│       │   ├── MessageBubble.tsx        # 混合渲染气泡（Markdown+Map+Chart）
│       │   ├── LazyMapView.tsx          # 懒加载地图（IntersectionObserver）
│       │   ├── FullscreenMap.tsx        # 全屏地图弹窗
│       │   ├── ChartView.tsx            # 图表渲染（ECharts）
│       │   └── ThinkingCollapse.tsx     # 折叠思考过程（推理链）
│       ├── hooks/
│       │   ├── useSSE.ts                # SSE 流式数据接收
│       │   └── useScrollAnchor.ts       # 滚动锚定控制
│       ├── types/
│       │   └── message.ts               # MessageBlock 类型定义
│       └── App.tsx
└── docker-compose.yml
```

---

## 3. 功能模块清单

### 3.1 基础层（数据与坐标）

| # | 功能 | 说明 |
|---|------|------|
| 1 | 坐标系转换 | WGS84 <-> GCJ02（火星坐标）<-> BD09 <-> CGCS2000 3度带/6度带 |
| 2 | 坐标系自动识别 | 上传 shp 无 `.prj` 时，根据 bbox 范围启发式推断 |
| 3 | 批量投影转换 | 一句话转高斯投影、UTM、Web Mercator 等 |
| 4 | 地理编码/逆编码 | "南京新街口" -> 经纬度；坐标 -> 地址 |
| 5 | 格式互转管道 | shp <-> GeoJSON <-> KML <-> GeoPackage <-> CSV(带WKT) |

### 3.2 数据获取层（多源 POI）

| # | 功能 | 说明 |
|---|------|------|
| 6 | 高德 POI 查询 | 周边搜索、关键词搜索、多边形搜索、ID 查询详情 |
| 7 | OSM Overpass 查询 | 开源补漏，支持复杂 QL 查询 |
| 8 | **双源融合去重** | 高德为主 + OSM 补漏，基于 **R-Tree/GeoHash 空间索引** 的去重（非暴力两两比较） |
| 9 | POI 分类映射 | 高德分类码 <-> OSM Tag 的自动映射 |
| 10 | 实时交通路况 | 高德路况 API，做时空分析 |
| 11 | 街景图像抓取 | 百度/谷歌街景 API，接多模态模型分析场景 |

#### 降级策略（Fallback）

系统采用**高德优先、OSM 兜底**的数据获取策略。该逻辑封装在底层 Tool 内部，对 Agent 透明：

1. 优先调用高德 API，返回 GCJ02 坐标数据
2. 高德返回空时，自动提取当前空间范围（BBox）调用 OSM Overpass API
3. 通过 `is_china_bbox()` 判断区域：
   - **国内**：OSM 的 WGS84 数据转 GCJ02（适配高德底图）
   - **国外**：OSM 数据保持 WGS84（高德海外底图自动切换为无偏移 WGS84）
4. 设置 **3 秒硬超时**，超时直接返回"未找到"，前端通过 SSE 提示"正在搜索备用数据源"

**为什么不让 LLM 调度 Fallback？** 增加一轮完整循环（思考->调用高德->观察为空->思考->调用 OSM）会浪费数秒延迟和 Token 成本。Tool 内部硬代码实现 Fallback 是工程最优解。

### 3.3 空间分析层（核心计算）

| # | 功能 | 说明 |
|---|------|------|
| 12 | 缓冲区分析 | 点/线/面缓冲，支持不同半径 |
| 13 | 叠加分析 | 点-in-多边形、多边形交集/并集/差集 |
| 14 | 泰森多边形 | Voronoi 划分服务范围，支持边界裁剪 |
| 15 | 等时圈/可达性 | 骑车/驾车/步行 N 分钟可达范围，接高德路径规划 |
| 16 | 视线/通视分析 | 接 DEM 高程数据，判断 A 点能否看到 B 点 |
| 17 | 坡度/坡向/剖面 | 从 DEM 提取地形参数 |
| 18 | 核密度估计 | POI 分布密度计算，输出栅格或等值线 |
| 19 | 拓扑检查与修复 | 自相交、重复节点、空几何、裂缝自动修复 |
| 20 | 属性表智能处理 | 自然断点法（Jenks）、等距分级、分位数分级 |

### 3.4 遥感与气象层（进阶数据）

| # | 功能 | 说明 |
|---|------|------|
| 21 | DEM 高程数据获取 | NASA SRTM、ASTER GDEM 自动下载 |
| 22 | 遥感影像处理 | Sentinel-2 / Landsat，NDVI 植被指数计算 |
| 23 | 气象数据叠加 | OpenWeatherMap 降水/温度格点，插值到 POI |
| 24 | 时序分析 | 多期影像/POI 数据对比，变化检测 |

### 3.5 可视化输出层（交付物）

| # | 功能 | 说明 |
|---|------|------|
| 25 | 动态交互地图 | 高德 JS API 前端渲染，返回可交互地图 |
| 26 | 热力图 | POI 密度热力，支持加权 |
| 27 | 专题图（分级设色） | 自动配色 + Jenks 分级，输出静态/动态图 |
| 28 | 3D 地形场景 | DEM + 卫星影像叠加（规划中） |
| 29 | 时序动画 | 时间轴滑块 / GIF，展示变化过程 |
| 30 | 地图标注与聚类 | MarkerCluster，大数据量不卡 |
| 31 | 报告自动生成 | 文字 + 图表 + 地图，一键导出 PDF/Markdown |

### 3.6 Agent 智能层（Multi-Sub-Agent）

| # | 功能 | 说明 |
|---|------|------|
| 32 | 自然语言意图拆解 | "南京新街口500米内蜜雪冰城" -> 结构化 ToolCall 链 |
| 33 | 地理实体消歧 | "新街口"在南京还是合肥？主动反问确认 |
| 34 | 隐含参数推断 | "附近"自动推断 500m，"最近"自动排序 |
| 35 | 空间记忆 | 记住用户常用原点（如"安师"），下次直接复用 |
| 36 | 工具链自动编排 | 选址 -> POI -> 缓冲 -> 叠加 -> 输出，自动串 |
| 37 | 失败回退策略 | 高德限流 -> 切 OSM；OSM 缺失 -> 提示上传 |
| 38 | 代码解释器沙箱 | 复杂分析 Agent 自写 Python，子进程隔离执行 |
| 39 | 多轮对话状态保持 | "刚才查的蜜雪冰城，再查下茶百道对比" |
| 40 | 结果摘要与解释 | 把原始 GeoJSON 翻译成自然语言报告 |
| 41 | Multi-Sub-Agent 并行 | Planner 拆解 TaskPlan → Dispatcher 按角色并行派发 |
| 42 | Verifier 审查 | 独立 LLM 审查 sub-agent 输出，不通过触发 refinement |

### 3.7 工程与基础设施层

| # | 功能 | 说明 |
|---|------|------|
| 43 | 文件上传处理 | 支持 shp(zip)、geojson、kml 上传解析 |
| 44 | 缓存机制 | 同一区域 POI 结果缓存，避免重复调用 |
| 45 | API 限流与重试 | 高德 QPS 管理，自动 sleep + 重试 |
| 46 | 用户会话管理 | 多用户隔离，各自空间记忆独立 |
| 47 | Hooks Pipeline | 事件生命周期拦截，可插拔的中间件链 |
| 48 | Preflight 规则 | 执行前准入检查，拦截高风险操作 |
| 49 | Risk 评分系统 | 操作风险评分 + 自动降级策略 |
| 50 | Toolkit 系统 | ~55 个工具的注册/发现/分组/参数校验 |
| 51 | Skill 系统 | 可组合的技能能力，支持动态加载 |
| 52 | Event 系统 | 23 种事件类型，贯穿编排全生命周期的可观测性 |
| 53 | Context Budget | 上下文预算管理，自动截断 + 窗口滑动 |
| 54 | Duplicate Guard | 幂等检测，避免重复调用浪费 Token |
| 55 | Run Control | 迭代上限、超时控制、强制终止 |

---

## 4. 技术方案详解

### 4.1 自然语言理解与意图拆解

**核心思路**：不是简单 prompt，而是结构化约束输出。利用 LLM 的结构化生成能力，将自然语言映射为强类型的 `SpatialIntent` + `ToolCall` 链。

```python
from pydantic import BaseModel, Field
from typing import List, Literal, Optional

class SpatialIntent(BaseModel):
    action: Literal[
        "convert", "query_poi", "buffer", "overlay", 
        "voronoi", "isochrone", "visualize", "data_clean"
    ]
    location: str = Field(description="地名或坐标，如'南京新街口'")
    radius: Optional[float] = Field(default=None, description="缓冲距离，米")
    poi_type: Optional[str] = Field(default=None, description="POI类型，如'蜜雪冰城'")
    data_source: Literal["amap", "osm", "upload"] = "amap"
    output_format: Literal["geojson", "shp", "html_map", "chart"] = "html_map"
    raw_query: str

class ToolCall(BaseModel):
    tool_name: str
    params: dict
    depends_on: List[int] = Field(default_factory=list)
```

**System Prompt 设计要点**：
1. 识别空间意图（action）
2. 提取地理实体（location, poi_type）
3. 推断隐含参数（如"附近"默认半径 500m）
4. 地名歧义时，先调用 `geo_code` 确认
5. **用户输入的国内坐标（经纬度）默认视为 GCJ02**，直接用于高德生态，不做 WGS84 转换
6. 输出严格的 ToolCall 链，不假设用户懂技术细节

#### 当前实现：Root 规划优先级与闭合契约

正常的自然语言请求由 Root Dispatcher 调用 LLM 生成 `TaskPlan`；服务端先校验 instruction 覆盖、DAG、角色/工具白名单和依赖关系，结构化输出无效时只请求一次修复，随后才使用受限的文档化 fallback。

以下输入具有不依赖模型猜测的、参数闭合的语义，直接走 `planner_source="guardrail"`：

1. 明确的 `WGS84 → GCJ02` 坐标转换；
2. 明示 GPS 坐标并要求“高德 / AMap / Gaode 可直接使用”的转换；
3. 已上传图层上的 `class == station` 属性筛选；
4. 已上传 DEM 的“先求坡度，再按 15° / 30° 三档重分类”。

这些路径把 `operation/lng/lat`、`field/operator/value` 或 `bins/values` 写入 `SubTask.tool_args`，再原样传给唯一允许的工具。它们的目的不是替代自然语言规划，而是阻止模型生成未注册的别名（如 `attribute_filter`、`raster_slope`）或丢失边界参数。其余请求仍保留 `planner_source="root_llm"`；LLM 规划不可用时的确定性目录兜底标为 `planner_source="fallback"`。

对于“沿用刚才的位置，换一个品牌并比较数量/密度”的同半径多轮请求，当前 assemble 阶段会从持久化的上一轮摘要中提取可验证的 POI 数量，并同时输出上一轮、当前轮和数量比较，不能只报告当前轮结果。

**示例流转**：
```
用户："南京新街口500米内有多少蜜雪冰城"
-> action: query_poi, location: "南京新街口", radius: 500, poi_type: "蜜雪冰城"
-> 下一步 action: buffer（以查询结果为中心做缓冲区）
-> 最后 action: visualize（生成热力图）
```

#### geo_code 多候选消歧策略（增量）

`geo_code` 工具的返回结构从单点 (lng, lat) 升级为：

```json
{
  "status": "success",
  "location": [118.785, 32.041],
  "formatted_address": "江苏省南京市鼓楼区新街口",
  "candidates": [
    {"rank": 0, "location": [118.785, 32.041], "location_type": "POI",
     "distance_to_principal": 0, ...},
    {"rank": 1, "location": [118.79, 32.045], "location_type": "地铁站", ...},
    {"rank": 2, "location": [118.81, 32.03], "location_type": "地名", ...}
  ],
  "confidence": 0.92,
  "disambiguated": true,    // top-2 confidence 差 < 0.15 → LLM 主动反问
  "principal_rank": 0,
  "cached": false
}
```

策略要点：
1. 默认 top-3（`DEFAULT_TOP_N=3`）
2. `confidence` 启发式：`location_type` 基础置信度 × 多结果折扣，落到 0.3–1.0
3. `disambiguated=True` 仅在 top-2 candidate 的 confidence 差 < 0.15，**避免**对所有同名都反问
4. 缓存 key 不变，但 schema 升级：旧缓存值（无 `candidates` 字段）在反序列化时走 fallback 重写
5. 历史上下文已选了某 rank 时，由 `principal_rank` 参数指定：从 `results_data[主点]` 取

### 4.2 以 GCJ02 为核心的坐标统一与投影计算

**核心原则**：以 **GCJ02（火星坐标）** 为绝对基准坐标系。所有国内数据必须统一对齐至高德 GCJ02，最终可视化输出也必须为 GCJ02 才能套合高德底图。

- **高德 API 返回数据**：原样使用（已是 GCJ02）
- **用户输入的国内坐标**（经纬度）：默认视为 GCJ02
- **OSM 数据**：WGS84 -> GCJ02（国内场景）；国外场景保持 WGS84（高德海外底图自动切换为 WGS84，转了反而偏）
- **空间计算**（缓冲区、叠加）：需在 **CGCS2000 3度带投影坐标系** 下进行，计算结果转回 GCJ02 供前端渲染
- **shp 上传数据**：先识别原坐标系，国内数据统一转 GCJ02

```python
import pyproj
from shapely.geometry import Point
from shapely.ops import transform

# 坐标系处理核心原则：
# 1. GCJ02 没有标准 EPSG 编码，不能直接用 pyproj 做偏转计算
# 2. 正确流程：WGS84 <-> GCJ02 用数学偏转算法，投影计算用 pyproj
# 3. 所有 pyproj 投影计算必须在 WGS84 或 CGCS2000 上进行，不能直接用 GCJ02

CRS_MAP = {
    "wgs84": "EPSG:4326",
    "cgcs2000": "EPSG:4490",   # CGCS2000 国家大地坐标系，用于投影计算
    "cgcs2000_3d_118": "EPSG:4548",  # 3度带，中央经线118°
    "cgcs2000_3d_120": "EPSG:4549",  # 3度带，中央经线120°
}

def wgs84_to_gcj02(lng: float, lat: float) -> tuple:
    # WGS84 -> GCJ02 火星坐标偏转
    # 必须使用数学偏转算法，不能用 pyproj（GCJ02 无标准 EPSG）
    # 推荐：coordTransform_py 或自研高精度算法
    # 警告：evil_transform 等公开近似算法在芜湖等城市有 10-50m 偏差
    pass

def gcj02_to_wgs84(lng: float, lat: float) -> tuple:
    # GCJ02 -> WGS84 逆偏转
    # 用于：OSM 数据入库前统一转 WGS84，再做空间计算
    pass

def is_china_bbox(bbox: tuple) -> bool:
    # 通过经纬度范围判断是否为国内场景
    # 国内：经度 73-135，纬度 3-54
    # 国外：保持 WGS84，不转 GCJ02
    pass

def auto_detect_crs(file_path: str) -> str:
    # 自动识别坐标系
    # 1. 读取 .prj 文件
    # 2. 无 .prj 时，根据坐标范围启发式推断：
    #    - 经度 73-135, 纬度 3-54 -> 中国范围
    #    - 数值 > 180 -> 可能是高斯投影，反算中央经线
    pass

def batch_transform(geojson: dict, target_crs: str) -> dict:
    # GeoJSON 批量投影转换
    # 正确流程：
    # 1. 若源数据是 GCJ02：先 gcj02_to_wgs84() 转成 WGS84
    # 2. 用 pyproj 在 WGS84/CGCS2000 上做投影计算
    # 3. 若目标需要 GCJ02：再用 wgs84_to_gcj02() 转回
    # 4. 国外数据：全程保持 WGS84，不转 GCJ02
    # 错误做法：直接把 GCJ02 当 EPSG:4490 丢给 pyproj，会产生几十到几百米偏差

    # 步骤 1：GCJ02 先转 WGS84（如果源是 GCJ02）
    if is_gcj02(geojson):
        geojson = gcj02_geojson_to_wgs84(geojson)

    # 步骤 2：pyproj 做投影计算
    project = pyproj.Transformer.from_crs(
        pyproj.CRS(geojson["crs"]), 
        pyproj.CRS(target_crs), 
        always_xy=True
    ).transform

    # 步骤 3：若目标需要 GCJ02，转回
    if target_crs == "gcj02":
        transformed = wgs84_geojson_to_gcj02(transformed)

    return transformed_geojson
```

**关键细节**：
- **国内绝对基准是 GCJ02**，不是 WGS84。所有国内数据（含 OSM、用户上传 shp）必须对齐 GCJ02 才能套合高德底图
- **高德海外底图自动切换为 WGS84**（无偏移），国外 OSM 数据千万不要转 GCJ02，转了反而偏
- **shp 上传处理流程**：ZIP 解压 -> 读取 .prj 识别坐标系 -> 转 CGCS2000 投影做空间计算 -> **最终结果统一转 GCJ02** 返回前端
- **上传 CRS 标记不可丢失**：`DataIO` 把国内上传结果转为 GCJ02 后写入 `GeoDataFrame.attrs["crs_label"] = "GCJ02"`。空间工具以此标记先做 GCJ02→WGS84 数学逆偏转，再进入 pyproj；不能把已偏转的数值再当 WGS84 偏转一次。
- 缓冲区分析必须在**投影坐标系**（如 CGCS2000 3度带）下执行，否则 500m 在地理坐标下是角度单位，结果严重变形
- 自动识别时，若 bbox 经度 > 180，说明是平面坐标，需根据范围反推中央经线
- **高精度 WGS84->GCJ02 转换**：不要用 evil_transform 近似算法，建议使用 pyproj 配合七参数/格网改正

### 4.3 POI 查询（多源融合）

采用**双源策略**：高德为主（全量、结构化），OSM 补漏（开源、无配额）。

**当前故障转移语义**：高德返回有效非空结果时直接结束；高德空结果、超时、连接错误或异常时才计算查询 bbox 并请求 Overpass。Overpass 按 `OSM_ENDPOINT` 后接 `OSM_BACKUP_ENDPOINTS`（逗号分隔）顺序尝试；仅当端点请求/HTTP/JSON 失败时切下一个端点，合法的空响应仍代表“未找到”，不会额外扇出请求。国内 OSM 结果在工具内转成 GCJ02，国外保持 WGS84。所有外部异常被工具层收敛为可读的空结果/失败信息，普通 sub-agent 只重试当前原子步骤，不重做前序成功步骤。

> 下面代码块用于说明算法结构；当前真实实现以 `app/tools/poi_query.py` 为准。高德返回的是 GCJ02，不会先转换为 WGS84。

```python
import requests
from typing import List, Dict

class POIQuery:
    def __init__(self, amap_key: str):
        self.amap_key = amap_key
        self.osm_endpoint = "https://overpass-api.de/api/interpreter"

    def query_amap(self, keyword: str, location: str, radius: int) -> List[Dict]:
        # 高德周边搜索
        # 1. 地理编码 location -> gcj02 坐标
        # 2. 调用 https://restapi.amap.com/v3/place/around
        # 3. 结果转 WGS84
        # 4. 返回 GeoJSON FeatureCollection
        pass

    def query_osm(self, tag: str, bbox: tuple) -> List[Dict]:
        # OSM Overpass QL 查询
        query = f"""
        [out:json][timeout:25];
        (
          node["name"~"{tag}"]({bbox[1]},{bbox[0]},{bbox[3]},{bbox[2]});
          way["name"~"{tag}"]({bbox[1]},{bbox[0]},{bbox[3]},{bbox[2]});
        );
        out body;
        >;
        out skel qt;
        """
        pass

    def search_poi_tool(self, query: str, location: str, radius: int) -> dict:
        # 统一的 POI 搜索工具，对 Agent 屏蔽底层数据源差异
        # Fallback 逻辑封装在 Tool 内部，减少编排轮次
        # 核心原则：TimeoutError / ConnectionError 对 LLM 来说就是 Empty Result

        # 1. 优先调用高德（GCJ02，200ms 内响应）
        try:
            amap_results = self.query_amap(query, location, radius)
            if amap_results and len(amap_results) > 0:
                return self._format_results(amap_results, source="Amap")
        except (TimeoutError, ConnectionError):
            # 高德超时/断连 -> 对 LLM 来说就是"没数据"，继续走 OSM 兜底
            pass

        # 2. 高德返回空或异常，触发 OSM 兜底（3秒硬超时）
        try:
            bbox = self._radius_to_bbox(location, radius)
            osm_results = self.query_osm(query, bbox, timeout=3)

            if osm_results:
                if self._is_china_bbox(bbox):
                    # 国内：WGS84 -> GCJ02（适配高德底图）
                    osm_results = [self._wgs84_to_gcj02(p) for p in osm_results]
                    return self._format_results(osm_results, source="OSM_CN")
                else:
                    # 国外：保持 WGS84（高德海外底图为无偏移 WGS84）
                    return self._format_results(osm_results, source="OSM_Global")
        except (TimeoutError, ConnectionError):
            # OSM 也超时/断连 -> 对 LLM 来说同样是"没数据"
            pass

        # 3. 统一返回 Empty Result，LLM 无需区分"查不到"还是"查超时"
        return {"status": "empty", "message": "未找到相关 POI"}

    def _deduplicate(self, results_a, results_b, threshold=50):
        # 基于 R-Tree 空间索引的去重，非暴力两两比较
        # 致命前提：results_a 和 results_b 必须处于同一坐标系！
        # 高德是 GCJ02，OSM 是 WGS84，必须先统一坐标系再比较
        # 建议：统一转 WGS84 做去重，或统一转 GCJ02 做去重

        # 1. 统一坐标系（示例：统一转 WGS84）
        # results_a_gcj = [gcj02_to_wgs84(p) for p in results_a]
        # results_b_wgs = results_b  # OSM 已是 WGS84

        # 2. R-Tree 空间索引去重
        from rtree import index
        idx = index.Index()
        # 建索引 + 查询近邻
        pass
```

**关键细节**：
- **Fallback 封装在 Tool 内部**：不要让 LLM 决定"高德查不到再查 OSM"，避免额外编排轮次
- **TimeoutError / ConnectionError 降级为 Empty Result**：对 LLM 来说，"查不到"和"查超时"是一回事——就是没有数据。Tool 内部 catch 后统一返回 `{"status": "empty"}`，LLM 无需区分
- 高德 POI 分类体系（`010000` 美食等）与 OSM Tag（`name="Mixue Ice Cream & Tea"`）需做模糊映射
- **国内数据统一为 GCJ02**，国外数据保持 WGS84。通过 `is_china_bbox()` 动态判断
- 高德周边搜索默认 20 条/页，最多 75 页（1,500 条）。全市级查询需多边形搜索 + 网格切分
- **OSM 3 秒硬超时**：Overpass API 响应慢，超时直接返回，前端 SSE 推送状态缓解焦虑
- **前端视觉隔离**：高德数据用高亮精美图标，OSM 兜底数据用灰色半透明图标，Popup 标注数据来源

### 4.4 空间分析引擎

```python
import geopandas as gpd
from shapely.geometry import Point, Polygon
from scipy.spatial import Voronoi
import numpy as np

class SpatialAnalyzer:
    # 空间分析引擎 - 所有计算必须在 WGS84/CGCS2000 投影坐标系下进行
    # 严禁直接用 GCJ02 做投影计算(pyproj 不认识 GCJ02, 会当 WGS84 处理, 导致整体偏移)

    def _ensure_wgs84(self, gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        # 强制入口校验: 如果传入的是 GCJ02, 先转 WGS84 再做计算
        crs = gdf.crs.to_string() if gdf.crs else ""
        if "gcj02" in crs.lower() or crs == "":
            # GCJ02 -> WGS84(数学偏转算法, 不能用 pyproj)
            gdf = gcj02_gdf_to_wgs84(gdf)
        return gdf

    def _to_gcj02_output(self, gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        # 出口: 计算结果 WGS84 -> GCJ02, 供前端高德 JS API 使用
        return wgs84_gdf_to_gcj02(gdf)

    def buffer(self, points_gdf: gpd.GeoDataFrame, radius_m: float):
        # 等距缓冲: GCJ02 -> WGS84 -> 投影计算 -> 结果回 GCJ02
        # 步骤 1: 强制转 WGS84(不能用 pyproj 直接处理 GCJ02)
        wgs84_gdf = self._ensure_wgs84(points_gdf)
        # 步骤 2: WGS84 -> CGCS2000 投影坐标系做计算
        projected = wgs84_gdf.to_crs(epsg=4548)
        buffered = projected.buffer(radius_m)
        # 步骤 3: 投影结果 -> WGS84 -> GCJ02
        wgs84_result = buffered.to_crs(epsg=4326)
        return self._to_gcj02_output(wgs84_result)

    def voronoi(self, points_gdf: gpd.GeoDataFrame, boundary: Polygon = None):
        # 泰森多边形: GCJ02 -> WGS84 -> 投影计算 -> 结果回 GCJ02
        # 边界条件: 点数 < 4 或共线时 scipy.Voronoi 直接崩溃
        coords = np.array([(p.x, p.y) for p in points_gdf.geometry])

        if len(coords) < 4:
            return {"status": "error", "message": "点数过少(需至少4个), 无法生成泰森多边形"}
        if self._is_collinear(coords):
            return {"status": "error", "message": "所有点共线, 无法生成泰森多边形"}

        try:
            # 步骤 1: GCJ02 -> WGS84
            wgs84_gdf = self._ensure_wgs84(points_gdf)
            # 步骤 2: WGS84 -> 投影坐标系计算 Voronoi
            projected = wgs84_gdf.to_crs(epsg=4548)
            coords = np.array([(p.x, p.y) for p in projected.geometry])
            vor = Voronoi(coords)
            # 步骤 3: 结果 -> WGS84 -> GCJ02
            # ... 边界裁剪逻辑 ...
            return self._to_gcj02_output(result_gdf)
        except Exception as e:
            return {"status": "error", "message": f"泰森多边形计算失败: {str(e)}"}

    def overlay(self, gdf_a: gpd.GeoDataFrame, gdf_b: gpd.GeoDataFrame, 
                how: str = "intersection"):
        # 叠加分析: GCJ02 -> WGS84 -> 投影计算 -> 结果回 GCJ02
        # 步骤 1: 强制转 WGS84
        wgs84_a = self._ensure_wgs84(gdf_a)
        wgs84_b = self._ensure_wgs84(gdf_b)
        # 步骤 2: 投影坐标系做 overlay
        proj_a = wgs84_a.to_crs(epsg=4548)
        proj_b = wgs84_b.to_crs(epsg=4548)
        result = gpd.overlay(proj_a, proj_b, how=how)
        # 步骤 3: 回 WGS84 -> GCJ02
        wgs84_result = result.to_crs(epsg=4326)
        return self._to_gcj02_output(wgs84_result)

    def isochrone(self, origin: tuple, mode: str, time_min: int):
        # 等时圈: GCJ02 原点 -> WGS84 -> 自适应稀疏采样 -> 结果回 GCJ02
        # 关键: 不能 360 度盲目采样(36 次路径规划, QPS 5 需 7 秒, 超 3 秒超时)
        # 采用: 8 方向粗采样 + 距离剧变象限二分细化, 控制在 10 次以内
        try:
            # 步骤 1: GCJ02 原点 -> WGS84(高德路径规划 API 需要 WGS84)
            origin_wgs84 = gcj02_to_wgs84(origin[0], origin[1])
            # 步骤 2: 自适应稀疏采样(8 方向 + 局部二分细化)
            sample_points = self._adaptive_radial_sampling(origin_wgs84, mode, time_min)
            # 步骤 3: 过滤无效采样点(海边/江边朝向水面的采样)
            valid_points = self._filter_invalid_samples(sample_points)
            if len(valid_points) < 3:
                return {"status": "empty", "message": "该区域路网稀疏, 无法生成有效等时圈"}
            # 步骤 4: 生成凸包或 alpha shape -> WGS84 -> GCJ02
            pass
        except (TimeoutError, ConnectionError):
            return {"status": "empty", "message": "路径规划服务暂不可用", "data": []}

    def topology_check(self, gdf: gpd.GeoDataFrame):
        # 拓扑检查与修复
        issues = []
        for idx, geom in enumerate(gdf.geometry):
            if not geom.is_valid:
                issues.append({
                    "index": idx, 
                    "type": "invalid", 
                    "fix": geom.buffer(0)  # 自动修复
                })
            if geom.is_empty:
                issues.append({"index": idx, "type": "empty"})
        return issues
```

### 4.5 数据 I/O（矢量数据处理）

#### 单机上传读写与生命周期

`POST /api/upload` 以 1 MiB 分块读取内容，先校验扩展名、总大小、ZIP 文件数/解压大小/压缩比以及 ZipSlip 路径；随后由 `DataIO.read_upload()` 统一解析 shp ZIP、GeoJSON、KML 和 GeoTIFF 预览。原始 payload **不**写入 Redis：它以原子替换方式保存在 `APP_WORKSPACE_DIR/uploads/{file_id}/original.{ext}`；Redis 的 `upload:{file_id}` 只保存 `filename/storage_path/size_bytes/created_at` 及 TTL 索引。

读取工具通过该元数据定位本机 payload。上传时会清理同一 uploads 根目录中超过 `UPLOAD_TTL_S` 的直接子目录，因此单机开发必须让 `APP_WORKSPACE_DIR` 指向可持久、受限的本地路径；不要把它配置为临时系统目录或通过文件名拼接到根目录外。

```python
import geopandas as gpd
from io import BytesIO

class DataIO:
    # 常见 GIS 中文编码候选列表，Linux 服务器默认 UTF-8，但用户 shp 常为 GBK
    COMMON_CN_ENCODINGS = ['utf-8', 'gbk', 'gb2312', 'gb18030', 'big5']

    def _detect_dbf_encoding(self, zip_ref, dbf_path: str) -> str:
        # 通过读取 .dbf 文件的部分二进制流来探测编码
        try:
            with zip_ref.open(dbf_path) as dbf_file:
                raw_data = dbf_file.read(4096)
                result = chardet.detect(raw_data)
                confidence = result.get('confidence', 0)
                encoding = result.get('encoding')
                if confidence > 0.7 and encoding:
                    if encoding.lower() in ['gb2312', 'gb18030']:
                        return 'gbk'
                    return encoding
        except Exception:
            pass
        return None

    def read_upload(self, file_bytes: bytes, filename: str):
        # 支持 shp ZIP 包（含 .shp .shx .dbf .prj .cpg）、geojson、kml
        # 核心：所有上传数据最终统一为 GCJ02
        # 关键：Linux 服务器读取含中文的 shp 时，必须探测编码，否则 UnicodeDecodeError
        if filename.endswith(".zip"):
            # shp 必须以 ZIP 包上传（包含 .shp .shx .dbf .prj，可选 .cpg）
            import io
            zip_buffer = io.BytesIO(file_bytes)
            detected_encoding = None

            # 1. 预扫描 ZIP 包，寻找 .cpg 编码声明文件
            with zipfile.ZipFile(zip_buffer, 'r') as zip_ref:
                cpg_files = [f for f in zip_ref.namelist() if f.lower().endswith('.cpg')]
                if cpg_files:
                    try:
                        with zip_ref.open(cpg_files[0]) as cpg_file:
                            detected_encoding = cpg_file.read().decode('ascii').strip()
                    except:
                        pass

                # 2. 无 .cpg 时，用 chardet 探测 .dbf 文件头
                if not detected_encoding:
                    dbf_files = [f for f in zip_ref.namelist() if f.lower().endswith('.dbf')]
                    if dbf_files:
                        detected_encoding = self._detect_dbf_encoding(zip_ref, dbf_files[0])

            # 3. 构建编码尝试列表（探测到的优先，再轮询常见中文编码）
            encodings_to_try = []
            if detected_encoding:
                encodings_to_try.append(detected_encoding)
            for enc in self.COMMON_CN_ENCODINGS:
                if enc not in encodings_to_try:
                    encodings_to_try.append(enc)

            # 4. 多重降级尝试读取（内存级解压，不落盘）
            last_error = None
            for enc in encodings_to_try:
                try:
                    zip_buffer.seek(0)  # 重置指针
                    gdf = gpd.read_file(zip_buffer, encoding=enc)
                    break
                except UnicodeDecodeError as e:
                    last_error = e
                    continue
                except Exception as e:
                    last_error = e
                    continue
            else:
                # 所有编码均失败 -> 对 LLM 降级为友好错误，不抛底层异常
                return {
                    "status": "error",
                    "message": f"Shapefile 编码解析失败，尝试过: {encodings_to_try}。建议另存为 UTF-8 后重新上传。"
                }

        elif filename.endswith(".geojson"):
            try:
                gdf = gpd.read_file(BytesIO(file_bytes))
            except UnicodeDecodeError:
                # GeoJSON 偶尔也是 GBK，降级尝试
                decoded_str = file_bytes.decode('gbk', errors='ignore')
                gdf = gpd.read_file(BytesIO(decoded_str.encode('utf-8')))

        crs = gdf.crs.to_string() if gdf.crs else "Unknown"

        # 国内数据统一转 GCJ02（适配高德底图）
        if crs != "Unknown" and self._is_china_data(gdf):
            gdf = self._to_gcj02(gdf)

        return {"data": gdf, "crs": "GCJ02", "count": len(gdf)}

    def export(self, gdf: gpd.GeoDataFrame, fmt: str = "geojson"):
        # 导出：前端下载时保持 GCJ02（与高德生态一致）
        if fmt == "geojson":
            return gdf.to_json()
        elif fmt == "shp":
            # 写到内存 zip（含 .shp .shx .dbf .prj .cpg），返回 bytes
            pass
```

空间工具入口通过 `_ensure_wgs84()` 消费 `crs_label`，再投影计算；`reproject_layer` 也必须先标准化到 WGS84，不能对 GCJ02 数值直接 `to_crs()`。`join_by_nearest(max_distance=0)` 是一个特殊精确语义：它使用几何 `intersects`，仅保留实际相交/重合的要素并固定 `distance_m=0`；负距离被拒绝，不能把 0 传给底层 `sjoin_nearest`。

### 4.6 可视化输出（高德 JS API 前端渲染）

**架构调整**：后端不再生成静态地图 HTML，而是返回 **GCJ02 坐标数据 + 图层配置**，前端通过**高德 JavaScript API** 直接渲染。这样所有数据天然对齐 GCJ02 底图，无需坐标转换。

```python
# backend/app/tools/map_layer.py
# 后端只负责：生成图层数据配置（JSON），不生成地图

class MapLayerBuilder:
    def build_point_layer(self, gdf: gpd.GeoDataFrame, source: str = "Amap"):
        # 返回 GCJ02 坐标数组 + 样式配置
        return {
            "type": "point",
            "source": source,
            "coordinates": [[p.x, p.y] for p in gdf.geometry],  # GCJ02
            "style": {
                "Amap": {"icon": "highlight_pin", "color": "#FF6B35"},
                "OSM_CN": {"icon": "gray_dot", "color": "#999999", "opacity": 0.6}
            },
            "popup_fields": ["name", "address", "tel"]
        }

    def build_heatmap_layer(self, gdf: gpd.GeoDataFrame, weight_field=None):
        return {
            "type": "heatmap",
            "coordinates": [[p.x, p.y] for p in gdf.geometry],  # GCJ02
            "weights": gdf[weight_field].tolist() if weight_field else None,
            "radius": 25,
            "gradient": {"0.4": "blue", "0.65": "lime", "1": "red"}
        }

    def build_polygon_layer(self, gdf: gpd.GeoDataFrame, value_field: str = None):
        # 缓冲区、等时圈、泰森多边形、叠加分析结果等
        # 所有坐标已是 GCJ02，前端高德 JS API 直接渲染
        return {
            "type": "polygon",
            "coordinates": [list(geom.exterior.coords) for geom in gdf.geometry],  # GCJ02
            "fill_color": "#3388ff",
            "fill_opacity": 0.3,
            "stroke_color": "#3388ff",
            "stroke_width": 2
        }

    def build_polyline_layer(self, gdf: gpd.GeoDataFrame):
        # 路径规划、轨迹、剖面线等
        return {
            "type": "polyline",
            "coordinates": [list(geom.coords) for geom in gdf.geometry],  # GCJ02
            "stroke_color": "#FF6B35",
            "stroke_width": 4
        }
```

```typescript
// frontend/src/components/LazyMapView.tsx
// 前端直接调用高德 JS API 渲染

import { useEffect, useRef } from 'react';

interface MapLayer {
  type: 'point' | 'heatmap' | 'polygon';
  coordinates: number[][];
  style?: Record<string, any>;
}

export function MapView({ layers, center }: { layers: MapLayer[]; center: [number, number] }) {
  const mapRef = useRef<AMap.Map | null>(null);

  useEffect(() => {
    // 初始化高德地图（自动适配 GCJ02）
    const map = new AMap.Map('container', {
      zoom: 12,
      center: center,
      viewMode: '2D'
    });
    mapRef.current = map;

    // 根据图层类型渲染
    layers.forEach(layer => {
      if (layer.type === 'point') {
        layer.coordinates.forEach((coord, i) => {
          const marker = new AMap.Marker({
            position: coord,  // GCJ02 直接可用
            icon: layer.style?.icon || 'default',
            offset: new AMap.Pixel(-13, -30)
          });
          marker.setMap(map);
        });
      } else if (layer.type === 'heatmap') {
        const heatmap = new AMap.Heatmap(map, {
          radius: layer.style?.radius || 25,
          gradient: layer.style?.gradient
        });
        heatmap.setDataSet({
          data: layer.coordinates.map((c, i) => ({
            lng: c[0], lat: c[1],
            count: layer.style?.weights?.[i] || 1
          }))
        });
      } else if (layer.type === 'FeatureCollection') {
        // 标准 GeoJSON 解析, 支持孔洞和多维坐标
        layer.features.forEach((feature: any) => {
          const geojson = feature.geometry;
          if (geojson.type === 'Polygon') {
            // Polygon: coordinates[0] 是外环, [1+] 是孔洞
            const paths = geojson.coordinates.map((ring: number[][]) => 
              ring.map((coord: number[]) => [coord[0], coord[1]]) // 只取 [lng, lat], 丢弃 Z
            );
            const polygon = new AMap.Polygon({
              path: paths,  // GCJ02 直接可用
              fillColor: layer.style?.fill_color || '#3388ff',
              fillOpacity: layer.style?.fill_opacity || 0.3,
              strokeColor: layer.style?.stroke_color || '#3388ff',
              strokeWeight: layer.style?.stroke_width || 2
            });
            polygon.setMap(map);
          } else if (geojson.type === 'LineString') {
            const path = geojson.coordinates.map((coord: number[]) => [coord[0], coord[1]]);
            const polyline = new AMap.Polyline({
              path: path,
              strokeColor: layer.style?.stroke_color || '#FF6B35',
              strokeWeight: layer.style?.stroke_width || 4
            });
            polyline.setMap(map);
          }
        });
      }
    });

    return () => map.destroy();
  }, [layers, center]);

  return <div id="container" style={{ width: '100%', height: '100%' }} />;
}
```

**为什么不用 Folium？**
- Folium 基于 Leaflet，默认使用 WGS84，国内数据会有偏移
- 高德 JS API 原生支持 GCJ02，数据直接渲染，零转换
- 前端交互更丰富（拖拽、缩放、点击 Popup 等事件直接绑定）
- 热力图等高级可视化直接调用高德插件

### 4.7 Multi-Sub-Agent 编排

> **当前架构**：Root Dispatcher 基于 LangGraph StateGraph，采用 `planner_router → dispatch → assemble` 三节点图。Planner 一次生成工具级 `TaskPlan` DAG；每个 `SubTask` 只允许一个 `tool_name`，执行器按依赖关系顺序或并行派发。

#### 4.7.1 两层状态机

**Root 层（`dispatcher.py`）— LangGraph StateGraph 三节点图**：

```
planner_router → dispatch_node → assemble_node → END
```

- `planner_router`：把 Prompt 拆成 `PlanInstruction`，再生成覆盖所有指令的原子 `SubTask`；校验 ID 唯一性、指令覆盖、依赖引用、无环性及角色/工具权限，失败时只允许一次规划修复
- 规划来源：正常请求标记为 `root_llm`，参数闭合的坐标/属性/DEM 请求标记为 `guardrail`，两次 Root 规划仍无效时才标记为 `fallback`；SSE `run.plan` 会透出该来源，便于审计而不把三条路径混为一谈
- `dispatch_node`：拓扑排序后按批次执行；无依赖任务并行，同角色链式任务顺序执行；只有全部依赖成功时才调度下游
- 数据边：上游产物同时以 `dep_<task_id>` 和稳定的 `result` 引用注入下游，GeoJSON 额外保留 `geojson` 语义别名
- `assemble_node`：汇总 `SubAgentOutcome`、生成自然语言摘要与 map payload，并输出成功/失败终态
- Root 不再依靠 Judge 猜测整个 Prompt 是否完成；DAG 中所有可运行步骤完成后才进入 assemble

同角色多步示例：

```text
data_io_read → fix_geometries → reproject_layer
             → buffer → dissolve_layer → export_result
```

读取上传文件是内部前置任务；用户的“修复、重投影、缓冲、融合、导出”仍分别对应五条原子指令。

**Sub-Agent 层（`build_sub_agent.py`）**：

普通角色（`geo/poi/geometer/viz`）执行一个 Root 指定的原子步骤：

```text
Schema Planner（只绑定 required_tool_name）
  → ToolExecutor → Observer → Verifier/refinement → native_finalize
```

`native_finalize` 只依据最后一个 `ToolResult.status` 判定当前步骤：`success` 完成；`empty/error` 仅修订并重试当前步骤；达到角色上限后标记失败。它还负责持久化 `AWAITING_INPUT`，使 `/resume` 在移除普通角色 Judge 后仍可恢复。

`coder` 保留 Code-Mode 的 Planner → Sandbox → Observer → Judge 循环，用于 JSON Schema 工具无法表达的自定义计算。

#### 4.7.2 关键状态定义

```python
# AgentRootState — 顶层编排
class AgentRootState(TypedDict, total=False):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    iteration: int
    should_stop: bool
    final_output: dict
    task_plan: dict              # TaskPlan {instructions, tasks(tool_name, depends_on)}
    dispatched: dict             # task_id → [run_id, ...]
    dispatcher_events: list      # SSE 事件流
    root_verifier_output: dict   # VerifierOutput | None
    trace_id: str
    session_id: str

# SubAgentState — 单个 Sub-Agent 运行
class SubAgentState(TypedDict, total=False):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    agent_role: str              # geo / poi / geometer / viz / coder
    required_tool_name: str | None # Root 为当前原子步骤指定的唯一工具
    parent_task_id: str
    run_id: str
    verifier_output: dict | None
    max_iterations: int
    verifier_required: bool
    ...
```

#### 4.7.3 Sub-Agent 角色

| 角色 | 职责 |
|------|------|
| `geo` | 地理编码、坐标转换、地名消歧 |
| `poi` | POI 查询（高德优先、OSM 兜底） |
| `geometer` | 缓冲区、叠加分析、泰森多边形、等时圈 |
| `viz` | 地图图层构建、可视化配置 |
| `coder` | 代码解释器沙箱（自定义 Python 空间分析） |
| `verifier` | 独立审查 sub-agent 输出 |

#### 4.7.4 Verifier 审查

`verifier_node.py` 独立 LLM 调用审查 sub-agent 输出，输出 `VerifierOutput`：
```json
{"approved": bool, "reason": str, "refinement_hints": [str], "confidence": float}
```
未通过时 `refine_router` 将 hints 反馈给 sub-agent 重试。

**最大迭代上限**：Root 层 `APP_ROOT_MAX_ITERATIONS`（默认 30）；Sub-Agent 使用各角色 registry 的 `max_iterations`。普通角色超过上限将当前步骤标记失败并阻断依赖任务；`coder` 才使用 Judge 返回部分结果。

#### 4.7.5 Code-Mode Tools（LLM 写 Python，统一走 Sandbox）

Code Mode 仅用于 `coder` 回退路径。**inline 执行路径已完全移除**，所有模型生成的 Python 代码统一走 sandbox 子进程隔离；普通 GIS 工具步骤走闭合 JSON Schema 调用，不执行模型代码。

```
LLM 写 Python 代码 → fence 预处理（executor.py）
    │
    ├─ ast_guard.inspect(code)
    │   ├─ AST outright banned: Import/FunctionDef/ClassDef/Try/With/Raise/Global
    │   ├─ While/AsyncFor/range(>1M) → 可执行但记录风险
    │   └─ ASTBannedNodeError → 拒绝执行
    │
    └─ SandboxExecutor（子进程隔离，统一路径）
        ├─ tempfile IPC（避 Windows 32KB 限制）
        ├─ sitecustomize import 黑名单 + socket 禁用
        ├─ UUID sentinel stderr 回捞
        │
        ├─ namespace（干净 built-in + 只读模块 + 工具函数）
        │   ├─ 同步工具: 直接调真实库函数
        │   ├─ async 工具: _sync_proxy（_run_async 线程本地 loop 复用）
        │   └─ 安全: 不暴露 os/sys/subprocess/__import__/__builtins__
        │
        ├─ __result__ → session_vars.update(__result__)
        │
        └─ execution_to_tool_result() → ToolResult(mode="code")
```

**核心文件**（`app/agents/code_mode/`）：

| 文件 | 职责 |
|------|------|
| `executor.py` | `SandboxExecutor` — fence 预处理 → AST → sandbox 执行 + `execution_to_tool_result()` 映射 |
| `ast_guard.py` | AST 静态分析 + `ASTBannedNodeError` |
| `namespace.py` | `build_namespace()` — 白名单 built-in + async sync proxy + sandbox stub + session_vars 命名冲突防护 |
| `sandbox_runner.py` | 子进程隔离 — tempfile IPC + UUID sentinel stderr 回捞 |
| `types.py` | `ExecutionResult` / `InspectionResult` / `ASTBannedNodeError` |

**执行路径（统一 sandbox）**：

| 执行类型 | 工具 | 机制 |
|---------|------|------|
| `sync` | buffer, overlay, voronoi, isochrone, map_layer_build, geo_transform | sandbox 子进程调库 |
| `async` | geo_code, query_poi, data_io_read | 同步 proxy；上传 payload 从本机工作区读取，Redis 仅存元数据 |
| `compute` | code_executor 及空间计算工具 | sandbox 子进程 + host RPC + sitecustomize 黑名单 |

所有工具对 LLM 以统一 Python 函数暴露（必须 kwargs），底层自动注入真实 handler：

```python
# 模型写的代码（在 sandbox 子进程中执行）：
location = geo_code(address="南京新街口")  # async → _run_async → httpx
pois = query_poi(bbox=location, category="restaurant")  # async → _run_async → httpx
buffered = buffer_geometry(geom=pois, distance=500)  # sync → shapely
__result__ = {"pois": pois, "buffered": buffered}     # → session_vars.update()
```

**安全模型**（纵深防御）：
- 命名空间不暴露 `os/sys/subprocess/__import__/__builtins__`
- AST 检测 While/AsyncFor/range(>1M) → 记录风险但允许执行
- AST outright banned Import/FunctionDef/ClassDef/Try/With/Raise/Global → `ASTBannedNodeError`
- `__xxx__` 属性全禁 + 白名单 built-in + 只读模块代理
- Dunder 属性 `a.__class__.__bases__[0].__subclasses__()` → AST 拦
- sitecustomize 黑名单 + socket 禁用（子进程级别）

**Verifier 双模式**：
- `mode="code"` payload：`{code, stdout, result, traceback, session_vars_keys}` — 读代码语义判断合理性
- `mode="json"` payload：`{tool_results: [tool_name, params, output]}` — 传统路径
- 输出统一为 `VerifierOutput` JSON（不 exec 代码）

**前端 SSE trace**（`ReactTraceStep` 可选字段）：
```typescript
export interface ReactTraceStep {
  round: number;
  thinking: string;
  code?: string;          // 模型生成的 Python 代码
  stdout?: string;        // print() 输出
  result?: unknown;       // __result__ dict
  executor_type?: "sync" | "async" | "compute";
  error?: string;         // traceback（失败时折叠展示）
}
```

---

### 4.8 Hooks Pipeline

Hooks 系统提供**可插拔的中间件链**，在 Agent 编排生命周期的关键节点注入自定义逻辑。所有 hook 以装饰器/回调形式注册，按优先级排序执行。

**生命周期节点**：

```
session_start → pre_dispatch → pre_tool_call → post_tool_call → post_subagent → session_end
     │               │              │               │                │
     │               │              │               │                │
  session_init    task_schedule   tool_validate   tool_audit     verifier_hook
  context_load    risk_assess     cost_track      result_cache   result_assemble
```

**内置 Hooks**：

| Hook | 触发点 | 职责 |
|------|--------|------|
| `SessionInitHook` | session_start | 初始化 session 上下文、加载空间记忆 |
| `PreflightHook` | pre_dispatch | 执行前准入检查（见 §4.9） |
| `RiskAssessHook` | pre_dispatch | 风险评分（见 §4.10） |
| `CostTrackHook` | pre_tool_call | Token 成本预估 |
| `ToolAuditHook` | post_tool_call | 工具调用审计日志 |
| `ResultCacheHook` | post_tool_call | 结果缓存写入 |
| `DuplicateGuardHook` | pre_tool_call | 重复调用检测（见 §4.14） |
| `ContextBudgetHook` | post_tool_call | 上下文预算检查（见 §4.13） |
| `VerifierHook` | post_subagent | 子 agent 结果审查触发 |
| `AssemblyHook` | post_subagent | 多 sub-agent 结果合并 |

### 4.9 Preflight 规则

Preflight 系统在任务执行前进行**准入检查**，拦截高风险或不合规的操作请求。

**规则分类**：

| 规则类别 | 示例 | 行为 |
|---------|------|------|
| **安全规则** | 检测系统命令注入、文件路径穿越 | 拒绝执行 |
| **配额规则** | 单 session Token 预算超限 | 警告 + 降级 |
| **地域规则** | 查询坐标超出高德覆盖范围 | 自动切换 OSM |
| **数据规则** | 上传文件大小 > 100MB | 拒绝上传 |
| **沙箱规则** | 代码包含 banned AST 节点 | 拒绝执行 |

### 4.10 Risk 评分系统

Risk 系统对每次操作进行**风险评分**，根据分数决定执行策略（直行/警告/拒绝/降级）。

```python
class RiskScore:
    level: Literal["low", "medium", "high", "critical"]
    score: float          # 0.0 ~ 1.0
    factors: list[str]    # 触发的风险因子
    mitigation: str | None # 建议的降级策略

# 风险因子:
# - TOOL_COMPLEXITY: 工具复杂度（buffer=0.1, code_exec=0.6）
# - DATA_SCALE: 数据量（1k points=0.1, 100k points=0.8）
# - EXTERNAL_API: 是否调用外部 API（amap=0.2, osm=0.4）
# - CODE_EXEC: 是否涉及代码执行（sandbox_only=0.5）
# - LOOP_DEPTH: 循环深度（iteration 3=0.3, iteration 10=0.9）
# - SESSION_BUDGET: Token 预算消耗比例
```

**降级策略**：
- `low` → 直接执行
- `medium` → 执行 + 日志记录
- `high` → 执行 + 通知前端 + 降低后续并行度
- `critical` → 拒绝执行，返回安全提示

### 4.11 Toolkit 系统

Toolkit 系统管理 **~55 个工具** 的注册、发现、分组和参数校验。

**工具分组**：

| 分组 | 工具数 | 代表工具 |
|------|--------|---------|
| `geo` | ~8 | geo_code, geo_transform, coord_convert, auto_detect_crs, batch_transform |
| `poi` | ~6 | query_poi, search_around, search_polygon, poi_detail, osm_query |
| `spatial` | ~10 | buffer, overlay, voronoi, isochrone, viewshed, slope, aspect, kde, topology_check, topology_fix |
| `data` | ~8 | parse_zip, read_shp, read_geojson, read_kml, export_geojson, export_shp, attr_classify, attr_stats |
| `viz` | ~6 | build_point_layer, build_heatmap_layer, build_polygon_layer, build_polyline_layer, build_chart, build_report |
| `sandbox` | ~4 | code_executor, install_package, read_file, write_file |
| `system` | ~8 | session_memory_get, session_memory_set, fetch_cache, context_summary, risk_check, duplicate_check |
| `remote` | ~5 | dem_download, sentinel_query, weather_current, weather_forecast, traffic_status |

**工具注册模式**：

```python
@register_tool(
    group="geo",
    risk_level="low",
    cache_ttl=3600,
    timeout=5.0,
)
async def geo_code(address: str, city: str = "") -> GeoCodeResult:
    ...
```

### 4.12 Skill 系统

Skill 系统提供**可组合的技能能力**，允许将多个原子工具组合成高级技能模板。Skill 支持参数化、嵌套和条件分支。

```python
# 预定义 Skill 示例：选址分析
site_selection_skill = Skill(
    name="site_selection",
    steps=[
        Step(tool="geo_code", params={"address": "$origin"}),
        Step(tool="query_poi", params={"location": "$prev", "category": "$competitor_category", "radius": 1000}),
        Step(tool="buffer", params={"geom": "$prev", "distance": 500, "operation": "exclude"}),
        Step(tool="query_poi", params={"location": "$origin", "category": "地铁站", "radius": 1000}),
        Step(tool="overlay", params={"a": "$step_3", "b": "$prev", "how": "intersection"}),
        Step(tool="build_map_layer", params={"data": "$prev", "layer_type": "polygon"}),
    ]
)
```

Skill 可在 Planner prompt 中以 `@skill:site_selection` 语法引用，LLM 自动展开为工具调用链。

### 4.13 Context Budget 管理

`context_budget.py` 负责**上下文窗口预算管理**，防止 LLM 上下文溢出。

**核心策略**：
- **滑动窗口**：保留最近 N 条消息 + 首条 system prompt
- **工具结果截断**：大 GeoJSON 自动摘要（只保留 count、bbox、字段名）
- **Observer 优先摘要**：工具返回 > 10KB 时，observer 先做结构摘要再入 context
- **预算告警**：达到 80% 时降级（减少 detail 级别），达到 95% 时强制 summarize 历史

### 4.14 Duplicate Guard

`duplicate_guard.py` 实现**幂等检测**，避免相同参数的工具重复调用浪费 Token 和时间。

**检测策略**：
- 基于 `(tool_name, hash(params))` 构建调用指纹
- 同一 session 内，相同指纹 30 秒内命中缓存 → 直接返回缓存结果
- 跨 session 可通过 Redis 共享（TTL 由工具注册的 `cache_ttl` 决定）

### 4.15 Event 系统

事件系统定义了 **23 种事件类型**，贯穿 Agent 编排全生命周期，驱动 SSE 推送、日志记录、指标埋点和 hooks 触发。

**事件类型清单**：

| # | 事件类型 | 触发点 | 携带数据 |
|---|---------|--------|---------|
| 1 | `SESSION_STARTED` | 新会话开始 | session_id, user_id |
| 2 | `SESSION_ENDED` | 会话结束 | session_id, duration, token_total |
| 3 | `MESSAGE_RECEIVED` | 收到用户消息 | message_id, content |
| 4 | `PLANNER_STARTED` | Planner 开始拆解 | iteration, context_size |
| 5 | `PLANNER_COMPLETED` | Planner 完成 | task_plan, plan_token_count |
| 6 | `TASK_DISPATCHED` | Sub-Agent 派发 | task_id, agent_role, parent_id |
| 7 | `TASK_COMPLETED` | Sub-Agent 完成 | task_id, outcome, duration |
| 8 | `TASK_FAILED` | Sub-Agent 失败 | task_id, error, retry_count |
| 9 | `TOOL_CALL_STARTED` | 工具调用开始 | tool_name, params_hash |
| 10 | `TOOL_CALL_COMPLETED` | 工具调用完成 | tool_name, duration, result_size |
| 11 | `TOOL_CALL_FAILED` | 工具调用失败 | tool_name, error, fallback_used |
| 12 | `VERIFIER_STARTED` | Verifier 开始审查 | target_task_id |
| 13 | `VERIFIER_COMPLETED` | Verifier 完成 | approved, confidence, hints |
| 14 | `REFINEMENT_TRIGGERED` | 触发 refinement | task_id, hints |
| 15 | `CODE_EXEC_STARTED` | 代码沙箱执行开始 | code_length, executor_mode |
| 16 | `CODE_EXEC_COMPLETED` | 代码沙箱执行完成 | duration, result_size, stdout_len |
| 17 | `CODE_EXEC_FAILED` | 代码沙箱执行失败 | error_type, traceback |
| 18 | `RISK_ASSESSED` | 风险评分完成 | risk_level, score, factors |
| 19 | `CONTEXT_BUDGET_WARNING` | 上下文预算告警 | used_percent, action_taken |
| 20 | `DUPLICATE_DETECTED` | 检测到重复调用 | tool_name, cache_hit |
| 21 | `HOOK_TRIGGERED` | Hook 执行 | hook_name, stage, duration |
| 22 | `SSE_EVENT_EMITTED` | SSE 事件发送 | event_type, payload_size |
| 23 | `ERROR_OCCURRED` | 未分类错误 | error_type, message, stack_trace |

事件消费方：SSE 推送（前端实时反馈）、结构化日志（排查）、metrics（监控）。

---

### 4.16 前端实现（ChatGPT 风格单页对话流）

#### 4.16.1 设计理念

摒弃传统"左侧对话 + 右侧地图"分栏模式，采用**纯单页对话流架构**。用户所有交互在聊天框完成，Agent 返回的空间分析结果（地图、图表、报告）以**富文本组件块**形式内嵌在 AI 回复气泡中。

核心原则：
- **沉浸式体验**：无需分栏切换注意力，专注对话本身
- **数据与视图分离**：后端通过 SSE 下发结构化事件流，前端解析拼装成不同类型的 Block
- **按需渲染与内存回收**：长对话中多地图实例采用**视口可见性监听**（懒加载/休眠）

#### 4.16.2 前后端数据结构契约

```typescript
// 单个 Block 的类型定义
type MessageBlock = TextBlock | MapBlock | ChartBlock;

interface TextBlock {
  type: 'text';
  content: string; // Markdown 格式文本
}

interface MapBlock {
  type: 'map';
  layers: MapLayer[]; // 遵循后端 MapLayerBuilder 生成的 JSON 配置
  bbox: [number, number, number, number]; // 地图初始视野范围
}

interface ChartBlock {
  type: 'chart';
  config: any; // ECharts 配置项
}

// 一条完整的聊天消息
interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  blocks: MessageBlock[];
  status: 'thinking' | 'fetching' | 'done' | 'error';
}
```

#### 4.16.3 SSE 流式数据接收与分块拼装

后端通过 SSE 推送三种事件流，前端动态拼装到当前消息的 blocks 数组：

```typescript
const handleSSEEvent = (event: any) => {
  setMessages(prev => {
    const lastMsg = prev[prev.length - 1]; // 正在流式输出的那条消息

    if (event.type === 'token') {
      // 文本流：追加到最后一个 text block 中
      const lastBlock = lastMsg.blocks[lastMsg.blocks.length - 1];
      if (lastBlock && lastBlock.type === 'text') {
        lastBlock.content += event.content;
      } else {
        lastMsg.blocks.push({ type: 'text', content: event.content });
      }
    } else if (event.type === 'map') {
      // 地图流：往 blocks 里塞一个地图块
      lastMsg.blocks.push({ 
        type: 'map', 
        layers: event.layers,
        bbox: event.bbox 
      });
    } else if (event.type === 'status') {
      // 状态流：如"正在搜索备用数据源..."
      // 在气泡顶部显示状态提示
      lastMsg.status = 'fetching';
    }
    return [...prev];
  });
};
```

#### 4.16.4 混合渲染消息气泡（Markdown + 组件混排）

```tsx
const MessageBubble = ({ message }: { message: ChatMessage }) => {
  return (
    <div className="chat-bubble">
      {message.blocks.map((block, index) => {
        if (block.type === 'text') {
          return <Markdown key={index} remarkPlugins={[remarkGfm]}>{block.content}</Markdown>;
        }
        if (block.type === 'map') {
          return <LazyMapView key={index} layers={block.layers} bbox={block.bbox} />;
        }
        if (block.type === 'chart') {
          return <ChartView key={index} config={block.config} />;
        }
        return null;
      })}
    </div>
  );
};
```

#### 4.16.5 地图懒加载与内存管理（核心难点）

**问题**：20 轮对话挂载 20 个高德地图实例会导致浏览器内存溢出。

**解法**：`IntersectionObserver` 监听视口可见性，可见时实例化，离开视口时销毁。

```tsx
import { useEffect, useRef, useState } from 'react';
import AMapLoader from '@amap/amap-jsapi-loader';

const LazyMapView = ({ layers, bbox }) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapInstance = useRef<any>(null);
  const [isVisible, setIsVisible] = useState(false);

  // 1. 监听是否进入视口
  useEffect(() => {
    const observer = new IntersectionObserver(([entry]) => {
      setIsVisible(entry.isIntersecting);
    }, { threshold: 0.1 });
    if (containerRef.current) observer.observe(containerRef.current);
    return () => observer.disconnect();
  }, []);

  // 2. 视口可见时加载地图，不可见时销毁
  useEffect(() => {
    if (isVisible && !mapInstance.current) {
      AMapLoader.load({ key: "YOUR_AMAP_KEY", version: "2.0" }).then(AMap => {
        mapInstance.current = new AMap.Map(containerRef.current, { zoom: 12 });
        // 渲染 layers（标准 GeoJSON FeatureCollection）
        layers.forEach((layer: any) => {
          if (layer.type === 'FeatureCollection') {
            layer.features.forEach((feature: any) => {
              const geojson = feature.geometry;
              if (geojson.type === 'Polygon') {
                const paths = geojson.coordinates.map((ring: number[][]) => 
                  ring.map((coord: number[]) => [coord[0], coord[1]]) // 丢弃 Z
                );
                new AMap.Polygon({
                  path: paths,
                  fillColor: layer.style?.fill_color || '#3388ff',
                  fillOpacity: layer.style?.fill_opacity || 0.3,
                  strokeColor: layer.style?.stroke_color || '#3388ff',
                  strokeWeight: layer.style?.stroke_width || 2
                }).setMap(mapInstance.current);
              }
            });
          }
        });
        mapInstance.current.setBounds(new AMap.Bounds(...bbox));
      });
    } else if (!isVisible && mapInstance.current) {
      mapInstance.current.destroy();
      mapInstance.current = null;
    }
  }, [isVisible, layers]);

  return (
    <div style={{ position: 'relative', height: '400px', marginTop: '10px', border: '1px solid #eee' }}>
      {!isVisible && (
        <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', background: '#f5f5f5' }}>
          地图已休眠，滚动至此区域加载
        </div>
      )}
      <div ref={containerRef} style={{ width: '100%', height: '100%' }} />
    </div>
  );
};
```

#### 4.16.6 UI/UX 交互优化

**骨架屏与组件占位**：
Agent 正在调用高德 API 时，先渲染骨架屏：
```
正在生成空间分析地图...
```
后端 layers 数据传完后，平滑替换为 `LazyMapView`。

**地图全屏弹出模式**：
对话框内地图高度固定 400px，右上角放"全屏展开"图标，点击后以 Modal 弹出全屏地图，接收相同 layers 数据，支持更完整交互（缩放、点击 Popup 详情）。

**折叠冗长思考过程**：
Agent 的推理链（"调用高德API -> 为空 -> 调用OSM兜底"）通过 Markdown 引用语法（>）解析，渲染在 `Collapse` 折叠面板中，默认收起。只把最终地图和分析报告外露。

**消息滚动控制**：
- 自动触底：流式输出新内容时，若滚动条在底部则自动跟随；若用户手动上滚查看历史，则不再强行触底
- 滚动锚定：懒加载地图实例化导致高度突变时，保持当前滚动位置不跳动

---

## 5. 技术栈与依赖

| 层级 | 技术选型 | 用途 |
|------|---------|------|
| **Agent 编排框架** | LangGraph (StateGraph) | Root Dispatcher 三节点图 + Sub-Agent 状态机 |
| **LLM 接口** | OpenAI SDK（DeepSeek v4） | Root Planner / Schema Planner / Observer / Verifier；Judge 仅 coder |
| **Web 框架** | FastAPI | 后端 API + SSE 流式 |
| **GIS 引擎** | geopandas + shapely + pyproj | 空间计算 |
| **科学计算** | scipy + numpy + scikit-learn | 核密度、Voronoi、分类 |
| **栅格处理** | rasterio + xarray | DEM、遥感影像 |
| **地图渲染** | **高德 JS API**（前端原生 GCJ02） | 交互地图，数据与底图零偏差 |
| **前端框架** | React 18 + TypeScript | 单页对话流架构 |
| **UI 样式** | Tailwind CSS | 消息气泡、骨架屏、折叠面板 |
| **图表** | ECharts | 统计图表、剖面图 |
| **流式通信** | SSE (Server-Sent Events) | 文本流+地图流+状态流分块拼装 |
| **持久化** | Redis + SqliteSaver | 会话/空间记忆 + LangGraph checkpoint |
| **代码沙箱** | subprocess + sitecustomize | 子进程隔离 + import 黑名单 |
| **容器化** | Docker Compose | 部署 |

---

## 6. 算法实现策略：现成库 vs 自研

### 6.1 直接使用现成库（无需自研）

| 功能 | 现成库 | 调用方式 |
|------|--------|---------|
| 缓冲区 | `shapely.buffer()` | 传距离直接出 Polygon |
| 叠加分析 | `geopandas.overlay()` | `how='intersection'` 等 |
| 泰森多边形 | `scipy.spatial.Voronoi` + `shapely` 裁剪 | 算完 Voronoi，boundary 裁一下 |
| 拓扑检查 | `shapely.is_valid` / `buffer(0)` | 自动修复自相交、裂缝 |
| 投影转换 | `pyproj.Transformer` | 任何坐标系互转 |
| 核密度估计 | `scipy.stats.gaussian_kde` / `sklearn` | 输入坐标数组，输出密度栅格 |
| 坡度/坡向 | `rasterio` + `numpy.gradient` | DEM 读进来，numpy 算梯度 |
| 格式互转 | `geopandas.read_file` / `to_file` | shp/geojson/kml 通吃 |
| 自然断点分级 | `mapclassify.JenksCaspall` | 属性表自动分级 |

### 6.2 需要自行封装（算法简单，无现成服务）

| 功能 | 为什么自研 | 实现思路 |
|------|-----------|---------|
| **等时圈** | 高德只给路径规划，不给可达多边形 | 从原点向 360度 放射采样，找时间边界点，连起来成 Polygon（凸包或 alpha shape） |
| **视线/通视分析** | 无现成 Python 库做 DEM 通视 | Bresenham 画线遍历 DEM 格网，检查是否有高程遮挡 |
| **双源 POI 去重** | 高德和 OSM 数据结构完全不同 | 统一转 WGS84 -> 50m 缓冲重叠检测 -> 保留信息更全的 |
| **坐标系自动识别** | 没有库能猜"这堆数字是什么坐标系" | 根据数值范围启发式推断：>180 是投影，73-135 是中国地理坐标 |
| **高精度 WGS84 转 GCJ02** | evil_transform 近似算法在芜湖等城市有 10-50m 偏差 | pyproj 配合七参数/三参数或高精度格网改正，确保 OSM 数据与高德底图重合 |

### 6.3 需要自己写的（业务逻辑层）

这部分没有现成库，因为是项目的**核心差异化能力**：

- **自然语言 -> GIS 操作意图**："附近"转 500m，"密度高"转核密度阈值
- **工具链编排**：先查 POI -> 再 Buffer -> 再叠加 -> 最后出图
- **结果翻译**：GeoJSON 转自然语言报告
- **失败回退**：高德挂了切 OSM，OSM 没了提示用户

---

## 7. 开发路线图（Sprint 规划）

### Sprint 1：基础骨架（~3,000 行）

**目标**：坐标转换 + 高德 POI + 自然语言解析 + 单轮查询

**验证标准**：
```
输入："南京新街口500米内有多少蜜雪冰城"
输出：返回 10 个点的 GeoJSON，坐标正确
```

**核心任务**：
- FastAPI 项目骨架搭建
- `geo_transform.py`：GCJ02 <-> WGS84 + 地理编码
- `poi_query.py`：高德周边搜索 + 结果转 GeoJSON
- `planner_factory.py`：基础意图拆解（query_poi + buffer）
- 前端：地图组件 + 聊天面板

### Sprint 2：空间分析 + 数据上传（~5,000 行）

**目标**：叠加分析 + shp 上传 + 缓冲区 + 统计输出

**验证标准**：
```
输入：上传某区划 shp + "这个区有多少蜜雪冰城"
输出：叠加分析结果 + 统计表
```

**核心任务**：
- `data_io.py`：shp/zip、geojson 上传解析
- `spatial_analysis.py`：buffer、overlay、点-in-多边形
- `map_layer.py`：图层配置生成
- 坐标系自动识别（无 .prj 时）

### Sprint 3：LangGraph 编排 + 工具链（~4,000 行）

**目标**：多轮对话 + 工具链编排 + 空间记忆

**验证标准**：
```
输入："先查蜜雪冰城，再查茶百道，对比密度"
输出：自动拆两轮，分别执行，最后对比输出
```

**核心任务**：
- LangGraph StateGraph 三节点图搭建（`dispatcher.py`）
- `observer.py`：工具结果摘要
- `judge.py`：终止/重试/继续判断
- 空间记忆：记住用户常用原点
- 工具链编排：选址场景自动串

### Sprint 4：可视化 + 双源 + 拓扑（~6,000 行）

**目标**：热力图 + 专题图 + OSM 双源 + 拓扑修复

**验证标准**：
```
输入："生成蜜雪冰城密度热力图"
输出：可交互地图热力图

输入：上传坏 shp
输出：自动修复拓扑错误，返回修复报告
```

**核心任务**：
- 图层构建完善：HeatMap、Choropleth、MarkerCluster
- OSM Overpass 接入 + 双源去重
- 拓扑检查与修复
- 报告自动生成（Markdown/PDF）

### Sprint 5：进阶功能 + 横切系统（可选，~8,000 行）

**目标**：等时圈 + 3D 地形 + 外部数据链接 + 横切系统完善

**验证标准**：
```
输入："从新街口骑车15分钟能到哪"
输出：等时圈多边形 + 覆盖面积

输入："查看这片区域的 SRTM 高程数据"
输出：提供 NASA EarthData 下载链接，不直接下载存储
```

**核心任务**：
- 等时圈实现（接高德路径规划）
- **外部数据链接**：SRTM DEM、Sentinel-2 影像提供官方下载链接跳转，**不直接下载存储**（单景 GB 级，服务器带宽和硬盘会瞬间打爆）
- 如需遥感分析，引导用户下载后上传，Agent 只做本地处理
- Hooks Pipeline、Preflight Rules、Risk System 完善
- Toolkit/Skill 系统完善
- Event 系统 + 全链路可观测性
- Context Budget、Duplicate Guard、Run Control 完善

---

## 8. 关键注意事项与防坑指南

### 8.1 坐标系是万恶之源

- **高德数据是 GCJ02**，必须转 WGS84 再做空间计算。否则 Buffer 500 米实际可能是 470 米或 530 米，POI 会漏掉或误杀
- **缓冲区必须在投影坐标系下执行**（如 CGCS2000 3度带 EPSG:4548），在 WGS84 下直接用 `buffer()` 会得到角度单位，结果严重变形
- 用户上传的 shp 经常没有 `.prj`，必须做启发式推断，不能假设

### 8.2 POI 查询分页与配额

- 高德周边搜索默认 20 条/页，最多 75 页（1,500 条）。全市级查询（如"南京市所有蜜雪冰城"）需改用**多边形搜索 + 网格切分**
- **截断提示**：当 total_count > 1500 时，返回结果必须带 `truncated: true` 标记，前端提示"数据已截断，仅展示前 1500 条"，避免用户误以为"全市只有 1500 个"
- 高德有 QPS 限制，需做**指数退避重试**（sleep 1s -> 2s -> 4s）
- OSM Overpass 有超时限制（建议设 25s），高峰期可能 429，需准备备用 endpoint

### 8.3 上下文管理

- **不要传大图给 LLM**：工具返回 10MB 的 GeoJSON 时，`observer` 必须做摘要（"找到 1,247 个点，覆盖 3 个区"），只传摘要进 messages
- **设置迭代上限**：`run_control.py` 管理，Root 层默认 30，Sub-Agent 层默认 6，强制终止防止死循环
- **工具失败要显式回退**：高德限流 -> 自动切 OSM；OSM 也失败 -> 返回友好提示，不要抛原始异常
- **Context Budget**：`context_budget.py` 监控，80% 时降级，95% 时强制 summarize

### 8.4 数据质量

- OSM 的 `name` 字段中英文混杂（如 Mixue Ice Cream & Tea），POI 查询需做**模糊匹配**（如包含 蜜雪 或 Mixue）
- 用户上传的 GeoJSON 可能包含 `null` geometry 或空属性，需在 `data_io.py` 层做清洗
- 拓扑修复（`buffer(0)`）可能改变几何形状，修复后需记录变更日志
- **前端数据源视觉隔离**：高德数据用高亮精美水滴图标（橙色 #FF6B35），OSM 兜底数据用灰色半透明圆点（#999999, opacity 0.6），Popup 明确标注数据来源
- **shp 中文编码陷阱**：Linux 服务器默认 UTF-8，用户从 ArcGIS 导出的 shp 常为 GBK。`data_io.py` 必须通过 `.cpg` 声明 -> chardet 探测 `.dbf` 文件头 -> 轮询常见中文编码的三重降级机制读取

### 8.5 性能

- 全市级 POI 查询（>10,000 条）考虑异步批处理，不阻塞 HTTP 请求
- 同一区域 POI 结果做 **Redis 缓存**（TTL 24h），避免重复调用
- 大数据量地图渲染（>5,000 个点）用 **MarkerCluster**，不要直接画 Marker

### 8.6 外部 API 异常统一降级

**核心原则**：任何外部 API 的异常（TimeoutError / ConnectionError / 429 / 503）在 Tool 层统一吞掉，返回空结果。对 LLM 来说，查不到和查超时是一回事——就是没有数据。

| 外部服务 | 异常场景 | 降级行为 | 前端反馈 |
|---------|---------|---------|---------|
| 高德 POI | 超时/断连/QPS 超限 | 触发 OSM 兜底 | SSE 推送"搜索备用数据源..." |
| 高德地理编码 | 超时/断连 | 返回空坐标 | 提示"无法识别该地点" |
| 高德路径规划 | 超时/断连 | 等时圈返回空 | 提示"路径规划服务暂不可用" |
| OSM Overpass | 超时/429/拥堵 | 3 秒硬超时后直接返回空 | 提示"未找到，建议缩小范围" |
| DEM 下载 | 超时/断连 | 返回下载链接，不直接下载 | 提示"数据量较大，请手动下载" |
| 气象 API | 超时/断连 | 返回空气象数据 | 静默降级，不影响主流程 |

- **OSM 性能**：高德响应通常 200ms，OSM Overpass 可能 3-10 秒。必须设置 **3 秒硬超时**
- **SSE 状态推送**：触发 OSM 兜底时，前端显示"高德数据较少，正在搜索全球开源地图库 (OSM)..."
- **缓存**：OSM 结果做 Redis 缓存（TTL 48h），避免重复慢查询

### 8.7 代码解释器安全边界

- **风险**：LLM 自写 Python 执行时，可能生成 `import os; os.system("rm -rf /")` 或恶意网络请求
- **数据投毒风险**：通过 pickle 反序列化传入沙箱存在 RCE 漏洞；沙箱内环境变量可能泄露高德 API Key
- **v2.0 统一 sandbox 隔离**（inline 路径已移除）：
  1. **禁用危险模块**：`os`, `subprocess`, `socket`, `urllib` 等加入 sitecustomize 黑名单
  2. **子进程隔离**：代码在独立子进程运行，限制 CPU/内存
  3. **网络隔离**：sandbox 进程无网络访问权限，防止 API Key 外泄
  4. **数据传递**：通过 tempfile IPC 传递数据，禁用 pickle
  5. **AST 静态检查**：`ast_guard.py` 预扫描，拦截 Import/FunctionDef/ClassDef/Try/With/Raise/Global
  6. **超时限制**：单段代码执行不超过 30 秒，超时被 kill

---

## 9. 杀手级组合场景示例

### 场景 1：奶茶店选址分析

```
用户："帮我分析在安师老校区开奶茶店，最佳选址在哪"

Agent 执行链：
1. 地理编码 "安师老校区" -> 原点坐标
2. query_poi：查询周边 1km 现有奶茶店（竞品）
3. query_poi：查询周边地铁站、公交站（流量）
4. buffer：以竞品为中心做 500m 缓冲（排除竞争密集区）
5. overlay：交通站点 + 非竞品区 -> 候选区域
6. isochrone：从候选点做 15 分钟步行等时圈（覆盖学生宿舍）
7. visualize：生成候选点地图 + 覆盖范围叠加
8. 输出：推荐 Top 3 点位 + 理由报告
```

### 场景 2：双城 POI 密度对比

```
用户："对比南京和合肥的蜜雪冰城分布密度，做个双城对比图"

Agent 执行链：
1. 分别抓取南京、合肥蜜雪冰城 POI
2. 核密度估计（KDE）生成两城密度栅格
3. 分区统计（按区县聚合）
4. 并排专题图（Choropleth）
5. 输出：对比报告 + 交互地图
```

### 场景 3：地形剖面提取

```
用户："从庐山的这个 GPS 轨迹里，提取海拔剖面并找出最陡的坡"

Agent 执行链：
1. 解析 GPX 轨迹 -> 坐标序列
2. 下载 SRTM DEM -> 高程采样
3. 计算每段坡度 -> 找出最大坡度段
4. 生成海拔剖面图（matplotlib）
5. 输出：剖面图 + 最陡坡位置标注
```

---

## 10. 补充文档索引

本文档为核心设计概要，以下专题文档位于 docs/ 目录，提供可落地的实施细节。各专题文档自包含，可独立查阅。

### 10.1 文档清单

| 文档 | 内容 | 何时参考 |
|------|------|---------|
| [docs/01_api_spec.md](01_api_spec.md) | API 端点、SSE 事件格式、统一错误响应 | 前后端联调、接口契约 |
| [docs/02_data_models.md](02_data_models.md) | Pydantic 模型、AgentState、Redis key 规范 | 数据结构定义、存储设计 |
| [docs/03_config_env.md](03_config_env.md) | .env 清单、Settings 类、密钥管理 | 环境配置、部署 |
| [docs/04_testing_strategy.md](04_testing_strategy.md) | 坐标转换黄金用例、集成测试、E2E 场景 | 测试编写、回归基线 |
| [docs/05_llm_prompts.md](05_llm_prompts.md) | Dispatcher / Sub-Agent / Verifier 完整 Prompt | LLM 调优、Prompt 迭代 |
| [docs/06_security.md](06_security.md) | API Key 保护、文件上传安全、沙箱边界、CORS | 安全审查、上传功能 |
| [docs/07_observability.md](07_observability.md) | 结构化日志、trace_id、指标埋点 | 日志排查、监控 |
| [docs/MANUAL_TESTING.md](MANUAL_TESTING.md) | 手动回归与准确性对照手册 | 验证 Agent 答案准确性 |

### 10.2 交叉引用关系

```
01_api_spec ──┬──→ 02_data_models（SSEEvent/ChatRequest 引用）
              ├──→ 06_security（CORS/错误响应）
              └──→ 07_observability（trace_id/健康检查）
02_data_models──→ 05_llm_prompts（SpatialIntent/ToolCall）
03_config_env ──→ 06_security（密钥管理）
04_testing     ──→ 02_data_models（fixtures 对齐 schema）
06_security    ──→ 07_observability（日志脱敏）
```

### 10.3 与原文档的关系

- 原文档（§1-§9）是**核心设计概要**，描述架构、功能清单、技术方案要点
- 补充文档（docs/）是**可落地规格**，提供完整的接口定义、模型、配置、测试、Prompt、安全、可观测性细节
- 实现时以补充文档为准；原文档的代码示例为示意，具体字段以 docs/02_data_models.md 为准

---

*v2.1 变更：Root TaskPlan 升级为工具级多步骤 DAG；新增原子指令覆盖、角色/工具权限、依赖与无环校验；普通角色改为 JSON Schema 单步执行和确定性 finalizer，Judge 仅保留给 coder；补齐步骤间 result 数据边、失败阻断、失败步骤局部重试、上传读取前置任务及 PendingStore 恢复职责。*
*文档版本：v2.1*
*最后更新：2026-08-09*
