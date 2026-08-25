# 数据模型 Schema

> 所有 Pydantic 模型、前端 TypeScript 类型、Redis 存储结构的统一定义。
> API 端点引用的模型见 [01_api_spec.md](01_api_spec.md)。

---

## 1. 模型分层总览

```
┌─────────────────────────────────────────────┐
│  Agent 层    PlannerOutput / JudgeDecision /  │
│              ToolResult / AgentRootState /    │
│              SubAgentState / TaskPlan /       │
│              SubAgentOutcome / VerifierOutput │
├─────────────────────────────────────────────┤
│  工具层      POI / BufferRequest / ...         │
│              GeoJSON 约定                     │
├─────────────────────────────────────────────┤
│  前端契约层  MapLayer / MessageBlock /          │
│              ChatMessage / StreamEvent        │
├─────────────────────────────────────────────┤
│  存储层      Redis key 规范 / TTL / 序列化       │
└─────────────────────────────────────────────┘
```

---

## 2. Agent 层模型

### 2.1 ToolResult —— 工具执行结果

```python
class ToolResult(BaseModel):
    tool_call_id: str
    status: Literal["success", "empty", "error"]
    data: Optional[dict] = Field(
        default=None,
        description="成功时的结果数据（GeoJSON / 统计值 / 图层配置）"
    )
    message: Optional[str] = Field(
        default=None,
        description="empty/error 时的人类可读说明"
    )
    error_code: Optional[str] = None
    duration_ms: int
    source: Optional[str] = Field(
        default=None,
        description="数据来源标记：Amap / OSM_CN / OSM_Global / Upload / inline / async / sandbox"
    )
    truncated: bool = Field(default=False, description="结果是否被截断")
    mode: Optional[str] = Field(
        default=None,
        description='"json" 或 "code"：JSON 工具调用 vs code-mode Python 执行。'
                    'code-mode 时 tool_name="__code_block__"'
    )
```

#### geo_code 返回值（v1，含 candidates）

`geo_code.tool_call.data` 是一个 dict，shape 如下：

```python
{
    "status": "success" | "empty",
    "location": [lng, lat],                  # 主坐标
    "formatted_address": str,
    "source": "Amap" | "Redis",
    "candidates": list[{                     # v1 新增：top-N 候选
        "rank": int,
        "location": [lng, lat],
        "formatted_address": str,
        "location_type": "POI" | "地铁站" | ...,
        "distance_to_principal": float       # 与主点 Haversine 距离（米）
    }],
    "confidence": float,                     # 主点置信度 0.0–1.0
    "disambiguated": bool,                   # 是否建议 LLM 反问
    "principal_rank": int,                   # 主点是 rank=X 候选
    "cached": bool,                          # Redis 命中
}
```

#### query_poi.data 透传候选（v1 新增）

`query_poi.tool_call.data` 在原始 POI 结果上仅在 `disambiguated=True`
时追加：

```python
{
    "count": int,
    "pois": [...],
    "candidates": [...],                     # 来自 geo_code 透传
    "confidence": float,
    "disambiguated": true,
}
```

#### LOCATION_DRIFT 错误（v1 新增）

当 query_poi / isochrone 的 `params.location` 与最近可信 geo_code
原点偏差 >100m 时：

```python
ToolResult(
    status="error",
    error_code="LOCATION_DRIFT",
    message="你填的坐标与可信 geo_code 原点偏差 1234m，超过 100m。
             请直接复用 geo_code 返回的 location 变量。"
)
```

### 2.2 AgentRootState —— 顶层编排状态

> **实现位置**：`app/agents/state.py` 的 `AgentRootState`

```python
from typing import TypedDict, Annotated, Optional, Sequence
from langchain_core.messages import BaseMessage

class AgentRootState(TypedDict, total=False):
    """Root Dispatcher 的顶层编排状态"""
    messages: Annotated[Sequence[BaseMessage], "add_messages"]
    iteration: int
    should_stop: bool
    final_output: dict
    user_input: str

    task_plan: dict                    # TaskPlan {instructions, tasks}
    dispatched: dict[str, list[str]]   # task_id → [run_id, ...]
    sub_results: dict[str, list[dict]] # task_id → [SubAgentOutcome, ...]
    dispatcher_events: list            # SSE 事件流 [{event, data}, ...]
    root_verifier_output: Optional[dict]  # VerifierOutput | None
    root_iteration: int
    termination_cause: str
    trace_id: str
    session_id: str
    run_id: str
    upload_file_ids: list[str]
    session_vars: dict                 # Root 数据面及依赖产物
    pending_task: Optional[dict]
    resume_patch: dict[str, object]
```

### 2.3 SubAgentState —— 单个 Sub-Agent 运行状态

> **实现位置**：`app/agents/state.py` 的 `SubAgentState`

```python
class SubAgentState(TypedDict, total=False):
    """单个 Sub-Agent 的运行状态"""
    messages: Annotated[Sequence[BaseMessage], "add_messages"]
    iteration: int
    tool_results: list
    planner_output: Any
    final_output: dict
    should_stop: bool
    user_input: str
    termination_cause: str

    agent_role: str              # geo / poi / geometer / viz / coder
    required_tool_name: Optional[str]  # Root DAG 指定的唯一原子工具
    parent_task_id: Optional[str]
    run_id: str
    session_id: str
    session_vars: dict
    refine_history: list
    verifier_output: Optional[dict]
    max_iterations: int
    verifier_required: bool
    pending_task: Optional[dict]
```

### 2.4 TaskPlan / SubTask —— 任务规划

> **实现位置**：`app/agents/schemas.py`

```python
class PlanInstruction(BaseModel):
    id: str
    text: str                          # 一条原子用户指令

class SubTask(BaseModel):
    id: str
    agent_role: str                    # geo / poi / geometer / viz / coder
    tool_name: Optional[str] = None     # 新计划必填；旧 checkpoint 兼容可空
    goal: str                          # 本任务目标描述
    depends_on: list[str] = []         # 依赖的前序 SubTask ID
    instruction_id: Optional[str] = None # 所覆盖的原子指令
    expected_artifacts: list[str] = []
    tool_args: dict[str, Any] = {}     # Root 维护的不可协商精确参数（可空）

class TaskPlan(BaseModel):
    instructions: list[PlanInstruction] = []
    tasks: list[SubTask]
```

新生成的计划必须满足：Task ID 唯一；`depends_on` 只引用现有 Task；DAG 无环；每条 instruction 至少被一个 Task 覆盖；每个 Task 的 `tool_name` 必须属于其 `agent_role` 白名单。缺失 `instructions/tool_name` 只用于读取旧 checkpoint，不是新规划的合法输出。

`tool_args` 不是任意模型参数透传通道。它只由 Root 的参数闭合 guardrail 写入（例如用户给出的坐标、`max_distance=0`、属性比较和值、栅格阈值），在 native Schema Planner 完成引用填充后覆盖同名字段。这样用户明确给出的数值不会在下游被模型改写或遗漏。

### 2.5 SubAgentOutcome / VerifierOutput

```python
class VerifierOutput(BaseModel):
    approved: bool
    reason: str
    refinement_hints: list[str] = []
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    needs_input: bool = False
    missing_slots: list[str] = []
    choices: list[dict] = []
    input_reason: Optional[str] = None

class SubAgentOutcome(BaseModel):
    task_id: str
    run_id: str
    agent_role: str
    status: Literal["success", "refined", "failed", "awaiting_input"]
    artifacts: dict[str, Any] = {}
    duration_ms: int = 0
    iteration_used: int = 0
    verifier_output: Optional[VerifierOutput] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    pending_task: Optional[dict] = None
```

普通角色的 `artifacts` 保留角色语义键（如 `geojson/pois/locations/layers`），并额外提供稳定的 `result` 数据边与 `result_tool_name` 元数据。下游通过 `dep_<task_id>` 取得无冲突的完整 artifacts，也可通过展开的 `result` 引用最近一步产物。

### 2.6 PlannerOutput / JudgeDecision —— Planner 结构化输出与裁决

Planner 的 LLM 输出包装结构（见 [05_llm_prompts.md](05_llm_prompts.md)）：

```python
class NeedClarification(BaseModel):
    question: str = Field(description="反问用户的问题")

class PlannerOutput(BaseModel):
    thinking: str = Field(description="Planner 的分析思路，1-2 句")
    code: Optional[str] = Field(default=None, description="生成的 Python 代码块")
    summary: Optional[str] = Field(default=None, description="对用户的自然语言摘要")
    need_clarification: Optional[NeedClarification] = None
```

Judge 决策输出：

```python
class JudgeDecision(BaseModel):
    decision: Literal["CONTINUE", "RETRY", "FINISH", "AWAITING_INPUT"]
    reason: str
```

---

## 3. 工具层模型

### 3.1 POI 对象

```python
class POI(BaseModel):
    name: str
    address: Optional[str] = None
    tel: Optional[str] = None
    location: tuple  # (lng, lat)
    crs: Literal["GCJ02", "WGS84"]
    source: Literal["Amap", "OSM_CN", "OSM_Global", "Upload"]
    category: Optional[str] = Field(
        default=None,
        description="高德分类码或 OSM tag，已统一映射"
    )
    poi_id: Optional[str] = None  # 高德 POI ID 或 OSM node ID
    distance: Optional[float] = None  # 距查询中心距离，米
```

### 3.2 空间分析请求模型

```python
class BufferRequest(BaseModel):
    geometry: dict  # GeoJSON Geometry
    crs: str        # 输入坐标系
    radius_m: float
    segments: int = 16  # 缓冲圆近似分段数

class OverlayRequest(BaseModel):
    geometry_a: dict
    geometry_b: dict
    crs: str
    how: Literal["intersection", "union", "difference", "symmetric_difference"]

class VoronoiRequest(BaseModel):
    points: List[tuple]  # [(lng, lat), ...]
    crs: str
    boundary: Optional[dict] = None  # 裁剪边界 GeoJSON Polygon

class IsochroneRequest(BaseModel):
    origin: tuple  # (lng, lat), GCJ02
    mode: Literal["driving", "walking", "riding"]
    time_min: int
```

### 3.3 GeoJSON 约定

项目内所有 GeoJSON 遵循 RFC 7946，并额外约定坐标系标注：

```json
{
  "type": "FeatureCollection",
  "crs": {"type": "name", "properties": {"name": "GCJ02"}},
  "features": [
    {
      "type": "Feature",
      "geometry": {"type": "Point", "coordinates": [118.78, 32.04]},
      "properties": {"name": "蜜雪冰城", "_source": "Amap"}
    }
  ]
}
```

**坐标系标注规则**（因 GCJ02 无标准 EPSG 编码，不能复用 EPSG:4490）：

| 坐标系 | `crs.properties.name` 值 | 说明 |
|--------|--------------------------|------|
| WGS84 | `EPSG:4326` | 标准 |
| GCJ02 | `GCJ02` | 项目自定义标识 |
| BD09 | `BD09` | 项目自定义标识 |
| CGCS2000 | `EPSG:4490` | 地理坐标 |
| CGCS2000 3度带 118° | `EPSG:4548` | 投影坐标，中央经线 118° |
| CGCS2000 3度带 120° | `EPSG:4549` | 投影坐标，中央经线 120° |

所有工具入口必须校验 `crs` 字段，GCJ02 数据先转 WGS84 再做投影计算（→ 详见主设计文档 GIS_Agent_技术文档.md §4.2）。

---

## 4. 前端契约层模型

### 4.1 MapLayer（后端生成、前端渲染）

```python
class MapLayerBase(BaseModel):
    type: str
    style: Optional[dict] = None

class PointLayer(MapLayerBase):
    type: Literal["point"] = "point"
    coordinates: List[List[float]]  # [[lng, lat], ...] GCJ02
    source: Literal["Amap", "OSM_CN", "OSM_Global", "Upload"]
    popup_fields: List[str] = []

class HeatmapLayer(MapLayerBase):
    type: Literal["heatmap"] = "heatmap"
    coordinates: List[List[float]]
    weights: Optional[List[float]] = None
    radius: int = 25
    gradient: Optional[dict] = None

class PolygonLayer(MapLayerBase):
    type: Literal["polygon"] = "polygon"
    coordinates: List[List[List[List[float]]]]  # 多面，每面含环
    fill_color: str = "#3388ff"
    fill_opacity: float = 0.3

class PolylineLayer(MapLayerBase):
    type: Literal["polyline"] = "polyline"
    coordinates: List[List[List[float]]]  # 多线
    stroke_color: str = "#FF6B35"
    stroke_width: int = 4

class FeatureCollectionLayer(MapLayerBase):
    """标准 GeoJSON，支持孔洞和多维坐标，推荐使用"""
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: List[dict]  # 标准 Feature 对象

class RasterLayer(MapLayerBase):
    """栅格图层，用于 DEM / 热力栅格 / 卫星影像叠加"""
    type: Literal["raster"] = "raster"
    url: str = Field(description="栅格 Tile URL 模板或单张图片 URL")
    bounds: List[List[float]] = Field(description="[[south, west], [north, east]]")
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    z_index: int = Field(default=0, description="叠放层级")

MapLayer = PointLayer | HeatmapLayer | PolygonLayer | PolylineLayer | FeatureCollectionLayer | RasterLayer
```

**推荐使用 `FeatureCollectionLayer`**：它直接承载标准 GeoJSON，前端 `AMap.GeoJSON` 插件可统一解析点/线/面/孔洞，避免为每种几何写单独渲染逻辑。其他类型保留是为了简化前端代码（如热力图需要特殊数据结构）。

### 4.2 MessageBlock —— 消息内嵌块

```typescript
// frontend/src/types/message.ts
type MessageBlock = TextBlock | MapBlock | ChartBlock;

interface TextBlock {
  type: 'text';
  content: string;  // Markdown
}

interface MapBlock {
  type: 'map';
  layers: MapLayer[];
  bbox: [number, number, number, number];  // GCJ02
}

interface ChartBlock {
  type: 'chart';
  config: any;  // ECharts option
}
```

### 4.3 ChatMessage

```typescript
interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  blocks: MessageBlock[];
  status: 'thinking' | 'fetching' | 'summarizing' | 'done' | 'error';
  trace_id?: string;
  created_at: number;  // epoch ms
}
```

### 4.4 StreamEvent —— SSE 事件 payload

```typescript
type StreamEvent =
  | { event: 'status'; data: { status: string; message: string } }
  | { event: 'token'; data: { content: string } }
  | { event: 'tool_call'; data: { tool: string; args: object; trace_id: string } }
  | { event: 'map'; data: { layers: MapLayer[]; bbox: [number, number, number, number] } }
  | { event: 'chart'; data: { config: object } }
  | { event: 'error'; data: { code: string; message: string; trace_id: string } }
  | { event: 'done'; data: { trace_id: string } };
```

---

## 5. 存储层（Redis）

### 5.1 key 命名规范

所有 key 使用 `命名空间:标识` 格式，冒号分隔层级。

| key 模式 | 值 | TTL | 用途 |
|---------|-----|-----|------|
| `session:{session_id}` | JSON（消息列表摘要） | 24h | 多轮对话上下文 |
| `memory:{session_id}` | JSON（记忆条目数组） | 30d | 空间记忆（常用原点等） |
| `task:{task_id}` | JSON（TaskStatus） | 7d | 异步任务状态 |
| `upload:{file_id}` | JSON 元数据（filename/storage_path/size） | 24h（可配置） | 本机上传索引；原始 payload 在 `APP_WORKSPACE_DIR/uploads/` |
| `cache:poi:{hash}` | JSON（POI 列表） | 24h | 高德 POI 查询缓存 |
| `cache:osm:{hash}` | JSON（POI 列表） | 48h | OSM 查询缓存 |
| `cache:geocode:{hash}` | JSON（坐标） | 7d | 地理编码结果缓存 |

`{hash}` 为查询参数标准化后的 MD5，例如 `cache:poi:md5("南京新街口|500|蜜雪冰城")`。

### 5.2 序列化

所有 Redis 值序列化为 JSON 字符串。单机模式不把上传 payload/base64 放入 Redis；文件原子写入工作区，Redis 只保存带 TTL 的索引元数据。

### 5.3 session 上下文结构

```json
{
  "session_id": "sess_abc123",
  "messages": [
    {"role": "user", "content": "南京新街口500米内有多少蜜雪冰城"},
    {"role": "assistant", "summary": "找到 12 家，其中 3 家来自 OSM", "tool_results_ref": ["result_001"]}
  ],
  "last_location": [118.78, 32.05],
  "last_poi_type": "蜜雪冰城",
  "created_at": "2026-07-10T08:00:00Z",
  "updated_at": "2026-07-10T08:05:00Z"
}
```

**上下文压缩策略**：只存消息摘要，不存原始 ToolResult（ToolResult 超过 5000 字符已截断）。完整工具结果在 `task:{id}` 或 `upload:{id}` 中按需引用。

### 5.4 memory 结构

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

## 6. 枚举定义汇总

| 枚举 | 值 | 用于 |
|------|-----|------|
| `data_source` | amap, osm, upload, auto | 工具层参数 |
| `output_format` | geojson, shp, html_map, chart | 工具层参数 |
| `crs` | WGS84, GCJ02, BD09, CGCS2000, CGCS2000_3D_118(=EPSG:4548), CGCS2000_3D_120(=EPSG:4549) | POI.crs / GeoJSON 标注（GeoJSON 内用 EPSG 编码，见 §3.3） |
| `poi_source` | Amap, OSM_CN, OSM_Global, Upload | POI.source / PointLayer.source |
| `tool_status` | success, empty, error | ToolResult.status |
| `message_status` | thinking, fetching, summarizing, done, error | ChatMessage.status |
| `stream_event_type` | run.plan, run.task.start/complete, tool.call.start/complete, code.execution.*, judge.awaiting_input, status, token, map, error, done | StreamEvent.event |
| `task_status` | pending, running, done, failed | TaskStatus.status |
| `overlay_how` | intersection, union, difference, symmetric_difference | OverlayRequest.how |
| `isochrone_mode` | driving, walking, riding | IsochroneRequest.mode |
| `judge_decision` | CONTINUE, RETRY, FINISH, AWAITING_INPUT | JudgeDecision.decision |
| `map_layer_type` | point, heatmap, polygon, polyline, FeatureCollection, raster | MapLayer.type |

---

## 7. 坐标系标注与转换边界

**核心原则**（与主设计文档 GIS_Agent_技术文档.md §4.2 一致）：

1. **GCJ02 是国内绝对基准**：所有国内数据最终对齐 GCJ02 以套合高德底图
2. **GCJ02 不能直接进 pyproj**：无标准 EPSG，必须先用数学偏转算法转 WGS84，再投影计算
3. **空间计算在投影坐标系下进行**：CGCS2000 3度带（EPSG:4548/4549），结果转回 WGS84 再转 GCJ02
4. **国外数据保持 WGS84**：高德海外底图自动切换为无偏移 WGS84

**模型层校验点**：

- `POI.crs` / GeoJSON `crs` 字段必须显式标注，不标注视为非法
- `SpatialAnalyzer` 入口 `_ensure_wgs84()` 强制校验（主设计文档 §4.4）
- `DataIO.read_upload()` 输出统一标注为 GCJ02（国内）或 WGS84（国外）

---

*文档版本：v1.3 | 最后更新：2026-08-09 | 属于 Gismind 补充文档*

*v1.3 变更：TaskPlan 升级为 PlanInstruction + 工具级 SubTask DAG；补充 tool_name/instruction_id/required_tool_name、sub_results、依赖 artifacts 数据边及 awaiting_input 状态；移除已废弃的 Ensemble 状态字段。*
