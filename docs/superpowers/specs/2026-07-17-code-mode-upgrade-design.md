# Gismind Code-Mode 架构升级实现报告

> **历史文档 / 已被替代（2026-08-09）**：本文记录 2026-07-17 的“全员 Code Mode”阶段，不代表当前运行架构。当前普通角色使用工具级 WorkflowPlan DAG + 单工具 JSON Schema，只有 `coder` 保留 sandbox Code Mode；以 `docs/GIS_Agent_技术文档.md` v2.1 和 `docs/05_llm_prompts.md` v1.3 为准。

> 日期：2026-07-17
> 状态：已实现
> 关联模块：`backend/app/agents/code_mode/*`、`tool_execution.py`、`registry.py`、`state.py`、`judge.py`、`dispatcher.py`、`chat.py`

## 背景与动机

Gismind 当前所有 sub-agent 已统一走 code-mode（`app/agents/build_sub_agent.py:1` 明确写"所有 sub-agent 统一走 code-mode：Planner 输出 Python 代码，HybridExecutor 执行"）。执行引擎 `HybridExecutor` 已经能：

- fence/think-tag 预处理
- AST Guard 拦截（`while` / import 等）
- inline (ThreadPoolExecutor) vs SandboxExecutor 二分
- namespace 注入工具 proxy + session_vars
- `__result__ = {...}` 增量写回

但仍缺 7 层关键能力：本实现的目标是把这些层补齐（PineFlow 风格，但保留 Gismind 现有执行引擎不动）。

## 已敲定的 4 项决策

| # | 决策点 | 选择 |
|---|--------|------|
| 1 | v1 范围 | 7 层全做（含 AWAITING_INPUT + resume 链路） |
| 2 | Tool 命名映射 | `ToolSpec` 加 `semantic_action` 别名字段；现有 11 个 ToolSpec 零改动 |
| 3 | 模式覆盖 | Gismind 当前所有 sub-agent 已统一走 code-mode；不存在独立 JSON-mode 路径（所谓 JSON 形态仅在 tool handler 内部参数解包层） |
| 4 | WorkspaceState 存储 | 与现有 SessionStore 同走 Redis，复用 `app/utils/redis.py` 的 `make_key` / `get_redis` |

> **澄清**：上面决策表第 3 行的"全员 code-mode"与正文表述不矛盾。Gismind 当前没有"JSON-mode sub-agent 执行路径"需要单独兼容。本升级不存在"两条执行路径分流"问题。

---

## 段 1：WorkspaceState（`backend/app/agents/workspace/`）**[已实现]**

### 目标

把 `state.session_vars` 这个扁 dict 提升为 `WorkspaceState`，LLM 代码可以引用 `pois` / `南京夫子庙` / `latest` / `buffer_nanjing` 等别名，但 session_vars 的写入路径完全不变。

### 文件结构

```
backend/app/agents/workspace/
├── __init__.py        # 公开 WorkspaceState / LayerRecord
├── layer_record.py    # LayerRecord dataclass + JSON 序列化
├── state.py           # WorkspaceState（含 alias 解析）
└── resolver.py        # 别名解析算法（_LATEST_REFS / _FINAL_REFS / source stem）
```

### 对外接口

```python
from typing import Literal

@dataclass
class LayerRecord:
    layer_id: str
    name: str
    kind: Literal["vector", "raster", "table", "point", "polygon", "unknown"]
    source: str | dict | None    # 文件路径 / GeoJSON dict / Redis file_id / "memory" / tool name
    parent_ids: list[str] = field(default_factory=list)
    algorithm_id: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    # metadata 必有字段：crs / geometry_type / feature_count / fields（缺失时留空）

    def to_dict(self) -> dict: ...

class WorkspaceState:
    """无侵入包装 session_vars；保留原写入语义，仅在 GeoJSON-类数据时额外建 LayerRecord。"""

    def __init__(self, session_vars: dict) -> None:
        # 共享引用，不深拷贝（from_dict 通过 sv 引用回写时使用）
        self._sv = session_vars

    def add_layer(self, *, name, kind, source, parent_ids=(), algorithm_id="",
                  parameters=None, metadata=None, layer_id=None) -> LayerRecord: ...
    def resolve(self, ref: str) -> LayerRecord: ...
    def has_layer(self, ref: str) -> bool: ...
    def latest_layer(self) -> LayerRecord | None: ...
    def layers_dict(self) -> dict[str, Any]: ...        # {"layers":[...], "aliases":{...}}
    @classmethod from_layers_dict(cls, sv: dict, payload: dict) -> WorkspaceState: ...
```

### `source` 字段约定

- 文件上传 → Redis `upload:{file_id}`（字符串）
- 工具生成的 GeoDataFrame → `"memory"`（在 to_dict 中再 JSON 序列化）
- 外部 API 结果（query_poi / geo_code）→ tool name 字符串
- 用户 inline 构造的 GeoJSON dict → 保留为 dict，`to_dict()` 调用 `make_json_safe` 落盘

### 集成位置

`backend/app/agents/code_mode/executor.py` 在 `exec(code, ns)` 之后、session_vars 更新之前插入：

```python
# 现有写法（伪代码）：
exec(code, ns)
result_value = ns.get("__result__", {})
if isinstance(result_value, dict):
    session_vars.update(result_value)         # 保留

# 新写法：
if isinstance(result_value, dict):
    workspace = WorkspaceState(session_vars)
    for k, v in result_value.items():
        if _is_geo_like(v):                     # GeoDataFrame / GeoJSON dict / shapely geom
            workspace.add_layer(name=k, source=v if isinstance(v, dict) else "memory",
                                kind=_infer_kind(v), metadata=_infer_metadata(v))
    session_vars.update(result_value)          # 完全不变
```

### `_is_geo_like` / `_infer_kind` / `_infer_metadata` 判定规则

```python
def _is_geo_like(v: Any) -> bool:
    """判断值是否含 GIS geometry 数据。"""
    if hasattr(v, "geometry"):       # GeoDataFrame / GeoSeries
        return True
    if isinstance(v, dict):
        if v.get("type") == "FeatureCollection" or "features" in v:
            return True
        if "coordinates" in v:
            return True
    if type(v).__module__ == "shapely.geometry":
        return True
    return False

def _infer_kind(v: Any) -> LayerKind:
    """推断 layer kind。"""
    if hasattr(v, "geometry") and hasattr(v, "dtypes"):
        return "vector"   # GeoDataFrame
    if isinstance(v, dict):
        gtype = _first_geometry_type(v)
        if gtype == "Point":   return "point"
        if gtype == "Polygon": return "polygon"
        if "Line" in str(gtype): return "vector"
        return "vector"
    if type(v).__module__ == "shapely.geometry":
        return {"Point": "point", "Polygon": "polygon"}.get(type(v).__name__, "vector")
    return "unknown"

def _infer_metadata(v: Any) -> dict:
    """从值中提取 crs / geometry_type / feature_count / fields。"""
    meta = {}
    if hasattr(v, "crs") and v.crs:
        meta["crs"] = str(v.crs)
    if hasattr(v, "__len__"):
        meta["feature_count"] = len(v)
    if isinstance(v, dict):
        feats = v.get("features") or []
        meta["feature_count"] = len(feats)
        if feats:
            meta["geometry_type"] = feats[0].get("geometry", {}).get("type", "")
    if hasattr(v, "columns"):
        meta["fields"] = list(v.columns)
    return meta
```

### 不做的

- 不替换 `session_vars` 变量名
- 不写第二份存储
- 不重写 executor 主流程
- 不为非 GIS 数据建 LayerRecord

### 测试

`backend/tests/unit/test_workspace_state.py`

- ✅ `_LATEST_REFS` 解析 latest / latest_layer / 上一步结果
- ✅ `_FINAL_REFS` 解析 final / 最终结果
- ✅ multi-alias：layer_id / name / source stem 三者之一都能 resolve 到同一 record
- ✅ 非 GIS 数据（str / int / bool）穿透，不进 LayerRecord
- ✅ `to_dict` ↔ `from_layers_dict` 往返一致
- ✅ `_infer_kind` 识别 GeoDataFrame / GeoJSON / shapely

---

## 段 2：Preflight（`backend/app/agents/preflight/`）**[已实现]**

### 目标

在 `_build_code_mode_tool_fns` 的 proxy 外层包一层 `preflight → handler → postflight`，不改 exec 语义，preflight 阻塞走 `PreflightError` 让现有 `EXECUTION_ERROR` 路径接管。

### 文件结构

```
backend/app/agents/preflight/
├── __init__.py        # public: run_with_preflight / PreflightError / ValidationIssue
├── registry.py        # RuleRegistry 装饰器
├── validation.py      # ValidationIssue + RepairProposal dataclass
├── rules_buffer.py    # buffer_crs
├── rules_overlay.py   # overlay_crs_alignment
├── rules_overwrite.py # output_overwrite
├── rules_layer.py     # layer_exists + field_exists
└── postflight.py      # 空结果 / feature_count 异常检测
```

### 对外接口

```python
@dataclass
class RepairProposal:
    kind: Literal["ask_user", "confirm_action", "auto_repair", "confirm_overwrite"]
    action: str | None                  # 例如 "reproject_layer"
    patch: dict | None                  # 例如 {"input_ref": "buffer_nanjing_projected"}

@dataclass
class ValidationIssue:
    code: str                           # "buffer_crs_mismatch"
    stage: Literal["preflight", "postflight"]
    severity: Literal["error", "warning"]
    message: str                        # 给 LLM 看的自然语言（中文优先）
    repair: RepairProposal | None

class PreflightError(RuntimeError):
    """携带 issues 的异常；被现有 EXECUTION_ERROR 路径捕获。"""
    def __init__(self, message: str, issues: list[ValidationIssue]) -> None:
        super().__init__(message)
        self.issues = issues

def register_preflight_rule(name: str, *semantic_actions: str):
    """装饰器：把 rule 函数注册到 RuleRegistry。"""
def run_with_preflight(tool_name: str, args, kwargs, ctx: _ToolContext): ...
```

### 阻塞机制：方式 A（推荐）

proxy 内部用 `PreflightError`，复用现有 `EXECUTION_ERROR` traceback 路径：

```python
def _code_mode_proxy(tool_name: str, real_fn):
    def wrapper(*args, **kwargs):
        ctx = _build_ctx(tool_name, args, kwargs)
        # 查 ToolSpec.semantic_action 作为 preflight 规则 key
        spec = get_tool_spec(tool_name)
        sem_action = spec.semantic_action or spec.name
        issues = preflight_for(sem_action, ctx)         # list[ValidationIssue]
        blocking = [i for i in issues if i.severity == "error"]
        if blocking:
            raise PreflightError(
                format_blocking_message(blocking),
                issues=blocking,
            )
        result = real_fn(*args, **kwargs)              # ToolResult (with .data)
        post = postflight_for(tool_name, result, ctx)
        if post:
            warnings_payload = [i.message for i in post if i.severity == "warning"]
            if warnings_payload and isinstance(result.data, dict):
                result.data["postflight_warnings"] = warnings_payload
        return result.data
    return wrapper
```

`PreflightError` 由 `executor.py` 的 `except Exception` 捕获并继续走 `EXECUTION_ERROR` traceback 路径，LLM 看到一条包括 issues 的 traceback，自然按现有 self-repair 流程改代码。

> **为什么不用 B**：如果返回 `__preflight_blocked__` 标记 dict，会污染 LLM 编写代码时的类型认知，可能让模型写出绕过 proxy 的逻辑。

### v1 落地的 5 条规则

| 规则名 | semantic_action | 触发 |
|--------|----------------|------|
| `buffer_crs` | `buffer_layer` | 输入层 CRS ∈ {EPSG:4326, EPSG:4490, CRS:84} 时阻断；repair = `{kind: confirm_action, action: reproject_layer, patch: {input_ref: <新别名>}}` |
| `overlay_crs_alignment` | `intersect_layer`, `difference_layer`, `clip_layer` | 两个图层 CRS 不一致 |
| `output_overwrite` | `export_result` 等输出类工具 | 输出路径已存在 → `confirm_overwrite` |
| `layer_exists` | 任意引用图层别名 | `WorkspaceState.resolve(ref)` 抛 KeyError |
| `field_exists` | `select_by_expression`, `field_calculator`, `extract_by_attribute` | 公式/过滤引用的字段不在 `metadata.fields` |

CRS 推荐策略：不引入 pyproj；用预置表（详见段 7 风险）给定 layer center 经纬度→目标 EPSG:4548/4549/投影带的离线建议。

### 硬约束

- preflight **只读** `WorkspaceState`，不调外部 API、不重算 geometry
- 不解析 Python for/if 表达式（AST 仍交给 `code_mode/ast_guard`）
- 对 async 工具只做参数 schema 检查，不触发网络

### 测试

- `backend/tests/unit/test_preflight_buffer_crs.py`（约 100 行）
- `backend/tests/unit/test_preflight_overlay.py`
- `backend/tests/unit/test_preflight_postflight_warnings.py`
- `backend/tests/unit/test_preflight_no_io.py`：mock 外部 HTTP 调用，验证 preflight 阶段不发请求

---

## 段 3：ToolKit / Skill（`backend/app/agents/toolkit/` 与 `backend/app/agents/skill/`）**[已实现]**

### 目标

让 LLM 看到的工具集合可动态收紧/扩张，YAML / MD 文件驱动；v1 配 1 个 always-on toolkit（`data_io`）+ 2 个待 `select_toolkit` 切换的 toolkit（`vector_analysis` / `vector_overlay`）+ 2 份 skill。

### 新增文件

```
backend/app/agents/toolkit/
├── __init__.py
└── registry.py            # ToolKitRegistry + ToolDisclosureController

backend/app/agents/skill/
├── __init__.py
└── registry.py            # SkillRegistry（扫 .md）

backend/resources/
├── toolkits/
│   ├── data_io.yaml                 # default_active=true
│   ├── vector_analysis.yaml         # 待 select_toolkit 切换
│   └── vector_overlay.yaml
└── skills/
    ├── meter_buffer.md
    └── spatial_join.md
```

### KERNEL_TOOLS（Gismind v1）

```python
KERNEL_TOOLS = (
    "select_toolkit",
    "inspect_workspace",
    "suggest_skill",
    "load_skill",
    "proactive_clarification",
)
```

> **剔除项**：`discover_algorithms` / `algorithm_help` 是 PineFlow 的 QGIS Processing 入口；v1 不上 PyQGIS，故不暴露。`final_answer` 在 Gismind 由 Judge 节点处理，不属于 LLM 可调用工具。

### ToolKitDefinition

```python
@dataclass(frozen=True)
class ToolKitDefinition:
    name: str
    title: str
    description: str
    tools: tuple[str, ...]
    tags: tuple[str, ...] = ()
    default_active: bool = False
```

### SkillMeta（精简版）

```python
@dataclass(frozen=True)
class SkillMeta:
    name: str
    description: str
    requires_toolkits: tuple[str, ...] = ()
    workspace_attention: tuple[str, ...] = ()
    risk_awareness: tuple[str, ...] = ()
    strategy_guidance: tuple[str, ...] = ()
    max_chars: int = 0
    path: str = ""
```

### Skill MD 模板（`meter_buffer.md` 示例）

```markdown
---
name: meter_buffer
description: 米制缓冲区最佳实践
requires_toolkits: [vector_analysis]
workspace_attention: [input_crs, geometry_type]
risk_awareness: [geographic_crs_metric_buffer]
strategy_guidance:
  - "米制缓冲前必须先确认输入图层 CRS"
  - "如 CRS 是 EPSG:4326，先 reproject 到本地 UTM 带"
---

# 米制缓冲区

## 适用场景
- 用户说"N 米范围内""周边 1km"
...

## 反模式
- ❌ 直接对 EPSG:4326 数据 buffer(500) —— 弧度单位
- ✅ reproject → buffer(500, unit="m")
```

### 集成位置

- `app/agents/tool_execution._build_code_mode_tool_fns`：新增参数 `active_toolkits: tuple[str,...] = ()`；从 `ToolDisclosureController.visible_tools()` 取交集
- `app/agents/planner_factory.build_code_mode_prompt`：新增参数 `toolkit_catalog: dict` 和 `loaded_skills: dict`，注入到 system prompt 末段
- 新 semantic tool 注册（带 description，每个 handler 签名写清）：

```python
TOOL_SPECS["select_toolkit"]   = ToolSpec(
    name="select_toolkit",   executor_type="inline",
    description="激活指定 ToolKit（data_io / vector_analysis / vector_overlay），扩展可见工具集。"
)
TOOL_SPECS["inspect_workspace"]= ToolSpec(
    name="inspect_workspace",executor_type="inline",
    description="显示当前工作区所有图层的 CRS / 字段 / 几何类型。"
)
TOOL_SPECS["suggest_skill"]    = ToolSpec(
    name="suggest_skill",    executor_type="inline",
    description="（v1 占位）基于当前任务推荐 skill；暂返回可用 skill 列表。"
)
TOOL_SPECS["load_skill"]       = ToolSpec(
    name="load_skill",       executor_type="inline",
    description="加载一份 GIS best-practice skill（meter_buffer / spatial_join），内容注入后续 prompt。"
)
TOOL_SPECS["proactive_clarification"] = ToolSpec(
    name="proactive_clarification", executor_type="inline",
    description="（v1 占位）向用户提出澄清问题；暂返回可用工具列表。"
)
```

- 每个 KERNEL tool 的 handler 定义（放入 `tool_execution.py._TOOL_REGISTRY`）：

| Tool | Handler 签名 | 副作用 |
|------|-------------|--------|
| `select_toolkit` | `handler(params: {toolkits: list[str]}) -> ToolResult` | 调 `ToolDisclosureController.select_toolkits(params)` → 改 `SubAgentSpec.tool_names` 子集（下一轮生效）|
| `inspect_workspace` | `handler(params: {query_type: str}) -> ToolResult` | 调 `ToolDisclosureController.inspect_workspace(state_tree, params, registry)` → 返回 layers/fields/active_toolkits 摘要 |
| `suggest_skill` | `handler(params: {}) -> ToolResult` | v1 返回可用 skill 列表和简短匹配原因；不触发 LLM |
| `load_skill` | `handler(params: {name: str}) -> ToolResult` | 调 `SkillRegistry.get(name)` → `read_skill_content(name)` → 内容注入 `state["loaded_skills"][name]`（SubAgentState 新增字段）|
| `proactive_clarification` | `handler(params: {}) -> ToolResult` | v1 返回一个包含可选工具/图层的 `{slots: [...], message: str}` 占位；不阻塞执行 |

- 集成位置：handler 注册与现有 Gismind 工具注册方式一致（在 `_TOOL_REGISTRY` dict 加条目）。`ToolDisclosureController` 在 `toolkit/registry.py` 实例化，`SkillRegistry` 在 `skill/registry.py` 实例化。
- `SubAgentState`（`backend/app/agents/state.py`）追加 `loaded_skills: dict | None` 字段（写入 prompts）。

### 测试

- `backend/tests/unit/test_toolkits_yaml.py`（约 70 行）
- `backend/tests/unit/test_skill_loading.py`（约 60 行）
- `backend/tests/unit/test_kernel_always_visible.py`

---

## 段 4：Judge AWAITING_INPUT + PendingTask（`backend/app/agents/pending.py` + dispatcher + chat.py）**[已实现]**

### 目标

Preflight 返回 `RepairProposal(kind=ask_user)` 或 `confirm_overwrite` 类型 issue 时，让 Judge 转 AWAITING_INPUT，把 pending_task 持久化到 Redis 等用户输入；resume 后继续 dispatch。

### 对外接口

```python
@dataclass
class PendingTask:
    sub_agent_run_id: str
    original_request: str
    missing_slots: list[str]                        # ["distance", "output_path"]
    candidates: list[dict[str, Any]]                # 多候选让用户选
    message: str                                   # 给用户的中文提示
    issues: list[dict[str, Any]]                    # ValidationIssue.to_dict()
    created_at: str                                 # ISO8601

class PendingStore:
    """复用 app/utils/redis.py，与 SessionStore 同一路径。"""
    def __init__(self, redis_client=None) -> None:
        self._r = redis_client or get_redis()
    def _key(self, session_id: str) -> str:
        return make_key("pending", session_id)
    def save(self, session_id: str, pt: PendingTask) -> None: ...
    def load(self, session_id: str) -> PendingTask | None: ...
    def clear(self, session_id: str) -> None: ...
```

### 改动点

| 文件 | 改动 |
|------|------|
| `app/agents/schemas.py` | `JudgeDecision` 加 `AWAITING_INPUT`；新增 `PendingTask` dataclass |
| `app/agents/judge.py` | `parse_decision()` 接受 AWAITING_INPUT；`judge()` 调用前置 hook 检查 state.pending_task，格式为 `{slot_name: slot_value}`： |
| `app/agents/state.py` | `SubAgentState` 追加 `pending_task: dict \| None`（不删任何现有字段） |
| `app/agents/dispatcher.py` | `planner_router` 增加 `pending_resume` 分支：检测到 user_input 续传时 load `PendingStore`，merge 进 state 后回 planner |
| `app/api/chat.py` | 参考段 6 SSE endpoint 实现（POST /chat 创建 collector → GET /events 消费）；SSE `awaiting_input` 事件与段 6 EventCollector 衔接（front-end TypeScript 需同步更新 `frontend/src/types/message.ts`） |
| `app/agents/pending.py` | 新增（60 行）：`PendingTask` + `PendingStore` |

### AWAITING_INPUT 触发条件

| 触发源 | condition |
|--------|-----------|
| preflight `repair.kind == "ask_user"` | 必发 |
| preflight `repair.kind == "confirm_overwrite"` | 输出路径已存在 |
| preflight `repair.kind == "confirm_action"` 且模型未自动走 auto_repair | 用户偏好确认 |
| postflight empty_result 且无 fallback | 让用户决定 |

### 测试

- `backend/tests/unit/test_pending_store.py`：Redis round-trip
- `backend/tests/integration/test_awaiting_input_e2e.py`：planner_router 触发 → 保存 → SSE 事件 → 用户输入 → resume

---

## 段 5：实施顺序与回归基线（Code-Mode 执行集成）**[已实现]**

| 步骤 | 范围 | 完成判定 |
|------|------|---------|
| 1 | `workspace/` + 注入点到 `executor.py` + 单元测试 | `pytest tests/unit/test_code_mode_*.py tests/unit/test_workspace_state.py -v` 全过 |
| 2 | `preflight/` + ToolSpec 加 `semantic_action` 字段 + 5 条规则 + 单元测试 | + `test_preflight_*.py` 全过 |
| 3 | `toolkit/registry.py` + `toolkits/*.yaml` + `skill/registry.py` + skill 模板 + 单元测试 | + `test_toolkits_yaml.py` `test_skill_loading.py` `test_kernel_always_visible.py` |
| 4 | `_build_code_mode_tool_fns` 接 preflight wrapper + `build_code_mode_prompt` 注入 toolkit/skill | 现有 `test_code_mode_*.py` 零退步 |
| 5 | **Preflight + PendingTask + Judge AWAITING_INPUT + dispatcher resume 联调** | + `test_pending_store.py` `test_awaiting_input_e2e.py` |
| 6 | EventCollector + 全节点事件注入 + SSE endpoint + 前端类型（详见段 6） | + `test_event_collector.py` `test_sse_events.py` |
| 7 | prompt 补丁：`prompts/coder.md` `poi.md` `geometer.md` 加 §-N 新能力段 | 手动跑 `docs/MANUAL_TESTING.md` 用例 |

### 回归红线

- 任何步骤不能让 `tests/unit/test_code_mode_*.py` 退步
- 任何步骤不能让 `tests/integration/test_agent_loop.py`、`tests/integration/test_sub_agent_loop.py` 退步
- 任何步骤不能删 `state.py` 现有持久化字段（仅追加）
- `_strip_fence_and_think` / `ast_guard.inspect` 路径零修改
- 11 个现有 ToolSpec 一个都不重命名（`semantic_action=""` 让 default 与 name 相同）

---

## 段 6：Event Stream / SSE**[已实现]**

### 目标

让前端能实时看到 sub-agent 执行过程：planner 生成代码 → 代码执行 → 工具调用 → preflight/postflight 检核 → judge 决策 → awaiting_input / 完成。**不是每行 stdout 都发事件**，而是按"用户可理解的最小动作单元"发。

### 新文件

```
backend/app/agents/events/
├── __init__.py              # EventCollector + emit_event + EVENT_CONTRACTS
└── stream_adapter.py        # LangGraph run → EventCollector 接线；提供 `register_handler(session_id, on_event)` / `unregister_handler(session_id)`，使 `tool_execution.py` / `build_sub_agent.py` 无需直接引用 EventCollector

backend/tests/unit/test_event_collector.py
backend/tests/integration/test_sse_events.py
```

### 对外接口

```python
from typing import Callable, Any
from dataclasses import dataclass

EventHandler = Callable[[dict[str, Any]], None]

@dataclass
class EventCollector:
    """asyncio.Queue + contextvars；collect sync 与 async 双端事件。"""
    def emit(self, event: str, message: str, **payload) -> None: ...
    async def consume(self) -> AsyncIterator[dict[str, Any]]: ...
    def queue_has_consumer(self) -> bool: ...
        """返回是否还有 consumer 在 consume() 协程中等待。用于 POST /chat 判断 SSE 生命周期。"""
    def mark_no_consumer(self) -> None: ...
        """标记当前 collector 不再被消费。用于 SSE 断开后的 TTL 清理。"""

def emit_event(
    handler: EventHandler | None,
    event: str,
    message: str,
    **payload: Any,
) -> None:
    """同步 emit；handler 为 None 时静默丢弃。"""

EVENT_CONTRACTS: dict[str, tuple[str, str]] = {
    # 运行级
    "run.session":            ("run.session",             "progress"),
    "run.thought":            ("run.thought",             "debug"),
    "run.summary":            ("run.summary",             "result"),
    "run.completed":          ("run.completed",           "result"),
    "run.failed":             ("run.failed",              "result"),
    "run.paused":             ("run.paused",              "progress"),
    # 步骤级（一个 code block）
    "code.generation":        ("code.generation",         "workflow_step"),
    "code.execution.start":   ("code.execution.start",    "progress"),
    "code.execution.stdout":  ("code.execution.stdout",   "debug"),
    "code.execution.stderr":  ("code.execution.stderr",   "debug"),
    "code.execution.complete":("code.execution.complete", "workflow_step"),
    "code.execution.error":   ("code.execution.error",    "warning"),
    # 工具调用级
    "tool.call.start":        ("tool.call.start",         "progress"),
    "tool.preflight.warning": ("tool.preflight.warning",  "warning"),
    "tool.preflight.blocked": ("tool.preflight.blocked",  "warning"),
    "tool.call.complete":     ("tool.call.complete",      "workflow_step"),
    "tool.postflight.warning":("tool.postflight.warning", "warning"),
    "tool.postflight.empty_result": ("tool.postflight.empty_result", "warning"),
    # Judge / pending
    "judge.decision":         ("judge.decision",          "debug"),
    "judge.awaiting_input":   ("judge.awaiting_input",    "confirmation"),
}
```

**`tool.preflight.blocked` 与 `code.execution.error` 区分**：blocking preflight 在 raise `PreflightError` 之前先 emit `tool.preflight.blocked`（payload 含 `issues`），让前端能区分"preflight 阻断"与"代码运行时错误"。`PreflightError` 仍按 `code.execution.error` traceback 路径让模型自修复。

**统一事件 payload shape**

```json
{
  "event": "code.execution.stdout",
  "event_type": "code.execution.stdout",
  "display_kind": "debug",
  "message": "...",
  "session_id": "...",
  "step_index": 1,
  "step_total": 5,
  "attempt_no": 0,
  "timestamp": "2026-07-17T10:00:00Z",
  "...": "payload 按事件类型变化（code / result / error_code / pending_task / issues …）"
}
```

### EventCollector 线程安全（明确接口）

```python
class EventCollector:
    def __init__(self):
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()

    def emit(self, event: str, message: str, **payload):
        """sync / async 双端入口。async 代码直接 put_nowait；sync 代码（ThreadPoolExecutor 内的 exec / print）跨线程走 call_soon_threadsafe。

        兜底：loop 已关闭时静默丢弃并 log.warning，不抛错。
        """
        item = _build_event(event, message, payload)
        try:
            try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is self._loop:
            self._queue.put_nowait(item)
        else:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
```

### 与现有代码的衔接

| 位置 | 改动 |
|------|------|
| `app/agents/build_sub_agent.run_sub_agent()` | run 启动时 emit `run.session`；close 时 emit `run.completed` / `run.failed` |
| `app/agents/build_sub_agent.py` | 每个 node 函数 `_planner_node` / `tools_node` / `observer_node` / `verifier_node` / `judge_node` / `refine_router_node` 增加 `on_event: EventHandler` 参数（闭包方式 `_make_*_node(on_event)`）。`workflow.add_node(name, _make_*_node(on_event))` |
| `app/agents/code_mode/executor.py` | `execute` 入口 `emit_event(on_event, "code.execution.start", ...)`；**stdout / stderr 用 thread-local `sys.stdout` patch 捕获**（`print()` 默认写 `sys.stdout`，namespace 注入无效）；退出时 `code.execution.complete` / `code.execution.error` |
| `app/agents/preflight/` (新的 `runner.py`) | `_run_with_preflight` 里：对每个 `severity == warning` 的 issue emit `tool.preflight.warning`；blocking issue 先 emit `tool.preflight.blocked`（payload 含 issues）→ 再 raise `PreflightError`；最后 traceback 仍走 `code.execution.error` 让模型自修复 |
| `app/agents/pending.py` | Judge 返回 AWAITING_INPUT 时 emit `judge.awaiting_input`，payload 含 `pending_task.to_dict()`（用 `sub_agent_run_id` 作 key）+ `[i.to_dict() for i in pending_task.issues]`。前端 resume 时回带 `sub_agent_run_id` |
| `app/api/chat.py` | SSE endpoint `GET /api/chat/{session_id}/events`；返回 `StreamingResponse(text/event-stream)` |
| `frontend/src/types/message.ts` | 新增 `StreamEvent` 类型 |
| `frontend/src/hooks/useChatEvents.ts` | 新增 SSE 订阅 hook（不存在则新增） |

### stdout / stderr 捕获（thread-local sys patch）

```python
def run_inline():
    ns = build_namespace(...)
    stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = stdout_buf, stderr_buf
    try:
        exec(code, ns)
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr
        # flush buffered stdout/stderr events（debounce 在 buffer 写满后批量 emit）
        _emit_buffered(stdout_buf, stderr_buf, on_event)
```

`print()` 默认写 `sys.stdout`，namespace 注入 `print` 函数不会接管所有 print；正确做法是 patch 执行线程的 `sys.stdout/stderr`，执行后还原。debounce 策略：**每 200ms 或 50 行**批量 emit。

### SSE endpoint 形态（POST 创建 collector，GET 消费已存在 collector）

**关键约束**：EventCollector 必须在 POST `/api/chat` 进入 run 时创建，并把 collector 存到进程级 dict；GET SSE 仅消费已有 collector。前端先 POST 触发 run 立刻 GET SSE，0 丢事件。

```python
# app/api/chat.py
_collectors: dict[str, EventCollector] = {}
_COLLECTOR_TTL_S = 600  # 10 分钟；防止内存泄漏

async def chat(request):
    session_id = request.session_id
    collector = EventCollector()
    _collectors[session_id] = collector
    try:
        result = await run_sub_agent(..., on_event=collector.emit)
        ...
    finally:
        # run 完成后清理；如 SSE 已消费完则即时清掉，否则等 GET 完成
        if not collector.queue_has_consumer():
            del _collectors[session_id]

@router.get("/chat/{session_id}/events")
async def chat_events(session_id: str):
    collector = _collectors.get(session_id)
    if collector is None:
        # 早期连接（SSE 在 run 启动前打开）→ 返回 keep-alive 直到 collector 创建
        return StreamingResponse(_wait_for_collector(session_id), media_type="text/event-stream")  # 轮询 _collectors 直到 collector 出现后转入 consume |
    async def event_stream():
        try:
            async for event in collector.consume():
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            # consumer 断开 → 标记；TTL 后清理
            collector.mark_no_consumer()
    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

前端在 POST `/api/chat` 后立即建立 SSE 连接收事件。run 完成时服务 emit `run.completed` 后保持 SSE 流直到客户端断开。

### UI 渲染映射（`display_kind` + 事件子类型）

| `display_kind` | 渲染方式 | 触发事件 |
|----------------|----------|----------|
| `progress` | 状态条小字 | `code.execution.start` / `tool.call.start` / `run.session` / `run.paused` |
| `workflow_step`（CodeBlockCard） | 时间线卡片：贴 Python 代码 + stdout | `code.generation` / `code.execution.complete` |
| `workflow_step`（ToolCallCard） | 时间线卡片：参数 + preflight 结果 + return value | `tool.call.complete` |
| `warning` | 黄色高亮块 | `code.execution.error` / `tool.preflight.warning` / `tool.preflight.blocked` / `tool.postflight.warning` / `tool.postflight.empty_result` |
| `confirmation` | 确认/选择按钮（点选后 POST `/api/chat/{session_id}/resume`，回带 `sub_agent_run_id`） | `judge.awaiting_input` |
| `debug` | 默认折叠，可展开 | `code.execution.stdout` / `code.execution.stderr` / `judge.decision` / `run.thought` |
| `result` | 最终结果卡片 | `run.summary` / `run.completed` / `run.failed` |

### 关键约束

1. **stdout debounce**：200ms 或 50 行批量 emit
2. **事件去重**：仅 preflight / postflight issue 在一次 tool call 内按 `(stage, code, tool_name)` 去重；**stdout / stderr 事件不去重**
3. **线程安全**：`EventCollector.emit` 严格按"async 代码同步 put / sync 代码 `call_soon_threadsafe`"；loop 已关闭则 log.warning + 静默丢弃
4. **失败兜底**：SSE 连接断开 / emit 失败都不影响主执行流程（log 不抛）
5. **不回放历史**：SSE 只推送订阅后产生的事件。但当前 run 已产生、尚未被消费的事件会正常推送（因为 collector 在 POST /chat 时创建）。

### 测试

- `tests/unit/test_event_collector.py` (~70 行)：emit 单测；EVENT_CONTRACTS 校验；去重；debounce
- `tests/integration/test_sse_events.py`：用 httpx async client 收 SSE，触发一个 sub-agent run，断言收到 `code.generation` → `code.execution.start` → `code.execution.complete` 序列
- `tests/integration/test_sse_awaiting_input_e2e.py`：触发 preflight `ask_user` → 收到 `judge.awaiting_input` → POST resume → 收到 `run.completed`

---

## 段 7：风险与不做的事（风险系统）**[已实现]**

### 风险

| 风险 | 应对 |
|------|------|
| Token 预算膨胀（toolkit catalog + loaded skill prompt 都进 system message） | `max_chars` 截断 + KERNEL_TOOLS 严格 5 个 |
| Preflight 副作用泄漏（不该发网络却发了） | `test_preflight_no_io.py` 在 CI 上 mock http/HTTPX |
| Redis PendingStore 的 TTL | 与 session 一致 24h；超期走 `extract_partial_result` 兜底 |
| CRS 推荐建议不准（无 pyproj） | 离线建议表（按经纬度 UTM 带）；EPSG:4326 + 距离缓冲直接报"请用户确认目标 CRS"，不自动改 CRS |
| Skill YAML frontmatter parse 失败 | 失败 fallback 到 `default_active=False` 不注入；不抛错 |

### 不做（v1 范围之外）

- PyQGIS worker / SpatialBackend 抽象（PineFlow 有 Gismind 没有 → 仅 spec 留口不实现）
- Skill auto-suggest 的 LLM-based scoring（先做关键词匹配，不上 embedding）
- 输出路径冲突之外的 conflict resolution
- WorkspaceState 跨 sub-agent 共享（v1 限定在当前 sub-agent run 内）

---

## 段 8：开放问题

1. **toolkit 是否需要 hot-reload**？v1 启动期加载一次即可；后续接 watchgod 再加。
2. **PendingTask 是否需要版本号**？与 session 的 `state_version` 字段（已有）一致；v1 沿用。
3. **load_skill 是否可卸载**？v1 不提供；每次新 sub-agent run 默认从空开始。
4. **prompt 补丁放哪**？`prompts/{role}.md` §-N 段追加；不替换主 prompt，方便 git diff 审查。
