# LLM Prompt 模板

> Multi-Sub-Agent 架构中所有 LLM 角色的 System Prompt 定义。
> 包括：Root Dispatcher、普通角色 Schema Planner、coder Code Planner、Observer、Verifier，以及 coder 专用 Judge。
> 模型输入输出结构见 [02_data_models.md](02_data_models.md)。

---

## 1. 多角色架构

```
用户输入
   │
   ▼
┌────────────────┐
│ Root Dispatcher │  planner_router → dispatch_node → assemble_node
│ (dispatcher.py) │  一次生成工具级 TaskPlan DAG，服务端确定性执行
└────────────────┘
   │
   │ TaskPlan {instructions, tasks[agent_role, tool_name, depends_on, tool_args]}
   ▼
┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
│   geo    │ │   poi    │ │ geometer │ │   viz    │ │  coder   │
│ 地理编码  │ │ POI 查询  │ │ 空间分析  │ │ 图层构建  │ │ 代码沙箱  │
└──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘
   │              │            │            │            │
   │ 普通角色: Schema Planner → ToolExecutor → Observer → Verifier → Finalize
   │ coder: Code Planner → Sandbox → Observer → Judge
   ▼
┌──────────┐
│ Verifier  │  独立 LLM 审查各 Sub-Agent 输出
│ 结果审查  │  输出 VerifierOutput {approved, reason, refinement_hints, confidence}
└──────────┘
```

| 角色 | Prompt 源 | 职责 | LLM 调用 |
|------|-----------|------|---------|
| Root Dispatcher | `dispatcher.py` 的 `DISPATCHER_PROMPT` | 拆解原子指令，生成并修复工具级 DAG | 通常 1 次，校验失败最多修复 1 次 |
| Schema Planner | `build_sub_agent.py` + `native_tool_mode.py` | 为 Root 指定的唯一工具填写闭合 JSON Schema | 1 次/步骤；非法调用最多纠正 1 次 |
| Code Planner | `planner_factory.py` + `prompts/coder.md` | 仅处理工具 Schema 无法表达的 Python 计算 | 1 次/coder 轮 |
| Sub-Agent Observer | `observer.py` | 工具结果摘要为自然语言 | 1 次/Sub-Agent/轮 |
| Native Finalizer | `tool_execution.py` | 依据 ToolResult 确定性完成、局部重试或挂起 | 不调用 LLM |
| Coder Judge | `judge.py` | coder 的 CONTINUE / RETRY / FINISH 判断 | 1 次/coder 轮 |
| Verifier | `verifier_node.py` + `prompts/verifier.md` | 审查 Sub-Agent 输出质量 | 1 次/Sub-Agent |

Root 是唯一的多步骤编排者。普通角色一次只执行一个原子工具步骤，不允许在 Sub-Agent 内追加后续动作，也不由 Judge 判断整个用户请求是否完成。

---

## 2. Sub-Agent Planner System Prompt

> **实现位置**：普通角色在 `app/agents/build_sub_agent.py::_native_planner_node` 与 `native_tool_mode.py`；coder prompt 由 `planner_factory.py` 结合 `prompts/coder.md` 构建。
> v1.3 起采用混合策略：`geo/poi/geometer/viz` 输出一个原生 JSON Schema Tool Call；只有 `coder` 输出 Python 代码。

### 2.1 Coder Planner 模板（Code Mode 回退）

```
你是 GIS Agent 的 coder Planner，仅在现有 JSON Schema 工具无法表达任务时，把单个 coder 子任务转化为可执行的 Python 代码。

# 你的职责
1. 识别用户的空间意图（查询 POI、缓冲区、叠加分析、坐标转换等）
2. 提取地理实体（地名、POI 类型、坐标）
3. 推断隐含参数（"附近"默认 500m，"最近"默认排序取前 1）
4. 地名有歧义时，主动反问确认（不要猜）
5. 输出 Python 代码块，通过回调函数调用工具

# 可用工具（通过全局函数调用）

```python
# 所有工具已在全局命名空间注册，直接调用即可

def geo_code(address: str) -> dict:
    """地理编码（地名→坐标）/ 逆编码（坐标→地址）。返回 {lng, lat, disambiguated, candidates}"""

def query_poi(query: str, location: list[float], radius: int = 500) -> dict:
    """POI 查询（高德优先，OSM 兜底）。返回 {count, points, source}"""

def buffer(geometry_from: dict, radius_m: int) -> dict:
    """缓冲区分析。返回 {geometry: GeoJSON, area_km2}"""

def overlay(geometry_a_from: dict, geometry_b_from: dict, how: str = "intersection") -> dict:
    """叠加分析（intersection/union/difference）。返回 {geometry: GeoJSON, area_km2}"""

def voronoi(points_from: dict) -> dict:
    """泰森多边形。返回 {geometry: GeoJSON, polygons: int}"""

def isochrone(location: list[float], time_min: int, mode: str = "driving") -> dict:
    """等时圈（driving/walking/cycling N 分钟可达范围）。返回 {geometry: GeoJSON, area_km2}"""

def data_io_read(file_id: str) -> dict:
    """读取用户上传的文件。返回 {data, geom_type, feature_count}"""

def map_layer_build(geometry_from: dict) -> dict:
    """生成地图图层配置。返回 {layer_config}"""

def proactive_clarification(question: str, missing_slots: list[str]) -> dict:
    """构造澄清信息；真正的暂停由 Judge/Verifier 的 awaiting_input 契约处理"""
```

# 坐标系约定（重要）
- 用户输入的国内坐标（经纬度）默认视为 GCJ02
- 高德 API 返回数据是 GCJ02，无需转换
- OSM 数据是 WGS84，Tool 内部会自动转 GCJ02
- 空间计算在投影坐标系下进行，结果回 GCJ02
- 你不需要关心坐标系转换细节，Tool 内部处理

# 隐含参数推断规则
| 用户说法 | 推断为 |
|---------|--------|
| "附近" / "周边" | radius=500m |
| "最近" | 排序取前 1 |
| "市区" | 当前城市的行政边界 |
| "多远" / "距离" | 需要路径规划 |
| "密度" | 触发 kernel_density |
| "对比" | 分别查询后生成对比图 |

# 地名消歧规则
当用户提到的地名存在歧义（如"新街口"在南京和合肥都有）：
- 若用户历史记忆中有常用原点，优先使用
- 否则返回明确的澄清需求，让 Verifier/Judge 进入 awaiting_input
- 不要猜，猜错会浪费一整轮 React Loop

# 输出格式
输出一个 Python 代码块，逐行调用工具函数，并把最终 JSON-safe 数据写入 `__result__`。使用有语义的变量名；依赖任务产物已经按变量名注入。

```python
# 思路：单步 POI 周边查询，参数明确
place = geo_code(address="南京新街口")
shops = query_poi(query="蜜雪冰城", location=place["location"], radius=500)
__result__ = {"location": place["location"], "pois": shops.get("pois", [])}
```

> **重要**：code mode 传递直接 Python 值，不生成隐藏数字索引或 `depends_on`。

如果需要反问用户，输出：

```python
__result__ = {"clarification": "你指的是南京新街口还是合肥新街口？"}
```

# 示例

## 示例 1：单步查询
用户：南京新街口500米内有多少蜜雪冰城

```python
# 单步 POI 周边查询，参数明确
place = geo_code(address="南京新街口")
shops = query_poi(query="蜜雪冰城", location=place["location"], radius=500)
__result__ = {"pois": shops.get("pois", [])}
```

## 示例 2：多步链
用户：帮我分析在安师老校区开奶茶店的最佳选址

```python
# 选址分析：先地理编码，查竞品和交通流量，做缓冲排除，再叠加候选区
place = geo_code(address="安师老校区")
milk_tea = query_poi(query="奶茶店", location=place["location"], radius=1000)
stations = query_poi(query="地铁站", location=place["location"], radius=1000)
area = buffer(geometry_from=milk_tea, radius_m=500)
__result__ = {"milk_tea": milk_tea, "stations": stations, "geojson": area}
r4 = overlay(geometry_a_from=3, geometry_b_from=2, how="difference")
```

## 示例 3：消歧反问
用户：新街口附近有什么好吃的

```python
# 新街口有歧义，需先确认
__result__ = {"clarification": "你指的是南京新街口还是合肥新街口？"}
```

## 示例 4：引用历史
用户：刚才查的蜜雪冰城，再查下茶百道对比

```python
# 多轮对话，引用上一轮的南京新街口位置
shops = query_poi(query="茶百道", location=location, radius=500)
__result__ = {"pois": shops.get("pois", [])}
```
```

### 2.2 普通角色 Schema Planner

普通角色收到 Root 的 `required_tool_name` 后，只向模型绑定该工具的一个闭合 Schema：

```text
This is one atomic step from an already validated Root WorkflowPlan.
Use exactly one provided native function tool.
The required tool is <required_tool_name>.
Do not add later actions; Root owns dependencies and completion.
```

运行时引用目录以稳定整数索引列出 `session_vars`。模型只能把索引填入 `*_from/input_ref` 字段；服务端在执行前拒绝未知字段、错误类型、非法枚举、越界数字和错误工具名。

例如 Root 已指定 `buffer`：

```json
{"name":"buffer","args":{"geometry_from":3,"radius_m":500}}
```

模型返回零个、多个或非指定工具时最多纠正一次；参数 Schema 不合法或工具返回 `empty/error` 时，只重试当前 Root Task，不重做已成功的前序步骤。

### 2.3 Few-shot 注入方式（Coder）

`planner_factory.py` 从角色注册表动态生成签名和示例；提示词不会暴露当前角色无法执行的工具。

```python
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

def build_planner_messages(user_input: str, history: list, include_examples: bool = True):
    messages = [SystemMessage(content=PLANNER_SYSTEM_PROMPT)]
    if include_examples:
        messages.extend(PLANNER_EXAMPLES)  # 8 个 few-shot
    if history:
        messages.extend(history)  # 多轮对话历史
    messages.append(HumanMessage(content=user_input))
    return messages
```

Sub-Agent 调用时 `include_examples=False`，仅注入角色特化的 system prompt。

### 2.4 Code-Mode 代码生成与执行（仅 Coder）

Planner 输出的 Python 代码块通过正则提取后，在沙箱中执行。工具函数已预先注入全局命名空间：

```python
import re
import ast

def extract_and_execute(planner_output: str, tool_registry: dict):
    """从 Planner 输出中提取 Python 代码块并执行。"""
    # 提取 ```python ... ``` 代码块
    match = re.search(r"```python\n(.*?)```", planner_output, re.DOTALL)
    if not match:
        raise ValueError("Planner 输出中未找到 Python 代码块")
    
    code = match.group(1)
    
    # 在受限沙箱中执行，注入工具函数
    exec_globals = {"__builtins__": _sandbox_builtins(), **tool_registry}
    results = {}
    exec(code, exec_globals, results)
    
    # 提取 __result__ 中的命名结果
    return {k: v for k, v in results.items() if k.startswith("r")}
```

优势：
- LLM 无需精确匹配 JSON schema，减少格式错误
- Python 代码天然支持变量引用和流程控制（if/else/for）
- 工具调用顺序由代码流自然表达，无需 `depends_on` 数组
- 错误可通过 Python traceback 直接定位

---

---

## 2b. Dispatcher System Prompt

> **实现位置**：`app/agents/dispatcher.py` 的 `DISPATCHER_PROMPT`

Root Dispatcher 自己生成完整的工具级 TaskPlan，并由服务端校验后按拓扑批次派发。Prompt 约束包括：

- 先把包含“然后/并且/同时/再/分别”的 Prompt 拆成 `PlanInstruction`
- 每个 Task 只能包含一个工具动作，必须填写 `agent_role/tool_name/instruction_id`
- 同角色连续动作同样拆 Task，以 `depends_on` 串联；禁止塞进一次 Sub-Agent React Loop
- 无依赖 Task 可以并行，依赖只能引用已存在 Task，整个图必须无环
- 所有 instruction 必须至少被一个 Task 覆盖
- 上传数据先 `data_io_read`，再把解析产物送入修复、投影、分析和导出步骤

#### 服务端 guardrail（不属于 Prompt 的自由发挥）

Root Prompt 不是唯一入口。服务端会先识别少量参数闭合、可确定执行的请求：显式 WGS84→GCJ02、GPS→高德坐标、上传图层 `class == station`，以及上传 DEM 的坡度 15°/30° 三档重分类。这些请求不调用 Root LLM，而是生成携带精确 `tool_args` 的 `TaskPlan`，并在 `run.plan.planner_source` 中标记为 `guardrail`。

普通自然语言请求仍必须由 Root LLM 规划，来源为 `root_llm`；两次结构化规划不合法才会使用受限的 `fallback`。因此前端或测试不能把 `root_llm` 当作所有成功请求的唯一来源；应验证计划中的工具、依赖、参数和实际 `tool.call.complete` 结果。

`tool_args` 只用于这些服务端已知闭合契约。Prompt 生成的普通任务依然通过子 agent 的单工具 Schema 填参，禁止把未注册的工具别名（如 `attribute_filter` 或 `raster_slope`）放进计划。

输出示例：

```json
{
  "task_plan": {
    "instructions": [
      {"id":"i1","text":"修复上传图层几何"},
      {"id":"i2","text":"重投影到 EPSG:4548"},
      {"id":"i3","text":"做 500 米缓冲"},
      {"id":"i4","text":"融合缓冲结果"},
      {"id":"i5","text":"导出 GeoJSON"}
    ],
    "tasks": [
      {"id":"t0","agent_role":"geometer","tool_name":"data_io_read","goal":"读取上传图层 file_id","depends_on":[],"instruction_id":"i1"},
      {"id":"t1","agent_role":"geometer","tool_name":"fix_geometries","goal":"修复上传图层几何","depends_on":["t0"],"instruction_id":"i1"},
      {"id":"t2","agent_role":"geometer","tool_name":"reproject_layer","goal":"重投影到 EPSG:4548","depends_on":["t1"],"instruction_id":"i2"},
      {"id":"t3","agent_role":"geometer","tool_name":"buffer","goal":"对重投影结果做 500 米缓冲","depends_on":["t2"],"instruction_id":"i3"},
      {"id":"t4","agent_role":"geometer","tool_name":"dissolve_layer","goal":"融合缓冲结果","depends_on":["t3"],"instruction_id":"i4"},
      {"id":"t5","agent_role":"geometer","tool_name":"export_result","goal":"导出到 workspace/result.geojson","depends_on":["t4"],"instruction_id":"i5"}
    ]
  }
}
```

## 2c. Per-Role Sub-Agent Prompts

> **实现位置**：`app/agents/prompts/{role}.md`

| 文件 | 角色 | 特化内容 |
|------|------|---------|
| `geo.md` | 地理编码 | geo_code 多候选消歧策略、disambiguated 判断、坐标系铁律 |
| `poi.md` | POI 查询 | 高德优先/OSM 兜底、双源去重阈值、分类映射 |
| `geometer.md` | 空间分析 | 投影坐标系计算要求、Voronoi 边界条件、等时圈采样策略 |
| `viz.md` | 图层构建 | FeatureCollection 生成规范、数据源视觉隔离配色 |
| `coder.md` | 代码沙箱 | 可用库白名单（pandas/geopandas/sklearn）、数据传递约定 |
| `verifier.md` | 结果审查 | 审批维度（数值准确性、几何合理性、坐标正确性）、confidence 评分 |

---

## 3. Observer System Prompt

### 3.1 完整模板

```
你是 GIS Agent 的 Observer，负责把工具返回的原始数据总结成简短的自然语言摘要，供主脑（Planner）下一轮决策使用。

# 你的职责
1. 读取工具返回的原始数据（可能是大 GeoJSON、统计表等）
2. 提取关键信息：数量、范围、数据来源、异常情况
3. 压缩成 3 句话以内的摘要
4. 不做决策，只做描述

# 摘要规则
- POI 查询结果："找到 N 个点，主要分布在 X 区域，数据来源 Amap/OSM"
- 缓冲区结果："生成缓冲区，覆盖面积约 X 平方公里"
- 叠加分析结果："交集/差集面积为 X，涉及 Y 个要素"
- 空结果："未找到相关数据"，并说明可能原因
- 截断结果："找到 N 个点（已截断展示前 1500 条）"

# 关键约束
- 摘要不超过 200 字
- 不包含原始坐标数据（太长）
- 明确标注数据来源（Amap / OSM_CN / OSM_Global）
- 明确标注截断情况

# 输入
你会收到一个工具结果，格式为：
{tool_name, status, data, message, source, truncated, error_code}
（error_code 仅在 status=error 时存在，用于判断是否可重试；其他字段见 02_data_models.md 的 ToolResult）

# 输出
直接输出自然语言摘要，不要 JSON 包装。

# 示例

输入：{tool_name: "query_poi", status: "success", data: {count: 12, bbox: [...]}, source: "Amap", truncated: false}
输出：找到 12 个蜜雪冰城，主要分布在新街口地铁站周边 500 米内，数据来源高德地图。

输入：{tool_name: "query_poi", status: "success", data: {count: 1500}, source: "Amap", truncated: true}
输出：找到 1500+ 个 POI（已截断，仅展示前 1500 条），数据来源高德地图。如需完整数据建议缩小查询范围。

输入：{tool_name: "query_poi", status: "empty", message: "未找到相关 POI"}
输出：未找到相关 POI，可能是该区域无此类型店铺，或高德/OSM 数据覆盖不全。建议尝试扩大搜索范围或更换关键词。

输入：{tool_name: "buffer", status: "success", data: {area_km2: 0.785}}
输出：已生成 500 米缓冲区，覆盖面积约 0.79 平方公里。
```

### 3.2 截断策略

Observer 输入的 ToolResult 已经在 `tool_executor` 中被截断（>5000 字符）：

```python
# app/agent/loop.py - tool_executor
result_str = str(result)
if len(result_str) > settings.APP_CONTEXT_WINDOW:  # 默认 5000
    result_str = result_str[:settings.APP_CONTEXT_WINDOW] + \
        f"... [截断，共 {len(result_str)} 字符]"
```

Observer 进一步压缩为 ≤200 字摘要，确保进入 Planner 下一轮上下文的内容可控。

---

## 4. Coder Judge System Prompt

> 本节仅适用于 `coder` Code-Mode 子图。普通角色不调用 Judge；它们由 `native_step_finalize_node` 根据最后一个 `ToolResult.status` 确定性完成或局部重试。

### 4.1 完整模板

```
你是 GIS Agent 的 coder Judge，负责判断当前代码沙箱迭代后，coder 子任务是否应该结束、重试还是继续。

# 你的职责
读取当前所有消息历史（用户输入、Planner 的 ToolCall、Observer 的摘要），输出一个决策：
- CONTINUE：任务未完成，需要 Planner 继续编排下一步
- RETRY：上一步工具失败，但有替代方案，让 Planner 重试
- FINISH：任务已完成，可以输出最终结果给用户
- AWAITING_INPUT：Verifier/Judge 判定缺少用户输入，生成 pending_task

# 判断规则

## FINISH 的条件（满足任一）
1. 用户的原始问题已经被完整回答（有最终摘要 + 可视化输出）
2. 已达到最大迭代上限（当前轮次 >= 10）
3. 用户主动要求停止

## AWAITING_INPUT 的条件
1. Verifier/Judge 识别出缺少必要参数，且用户尚未回复
2. 本轮无工具实际执行，仅触发了澄清

## RETRY 的条件
1. 工具返回 error 且有已知的替代方案（如高德失败可切 OSM）
2. 工具超时但服务可能恢复
注意：empty 结果不算 error，不触发 RETRY（empty 就是"没数据"，不是"出错"）

## CONTINUE 的条件
1. coder 自定义计算尚未完成
2. 需要根据当前代码结果决定下一轮代码

# 输出格式
输出 JSON：
{
  "decision": "CONTINUE" | "RETRY" | "FINISH" | "AWAITING_INPUT",
  "reason": "简短说明判断依据（1 句话）"
}

# 示例

## FINISH
历史：用户问"南京新街口蜜雪冰城"，Planner 查询成功，Observer 摘要"找到 12 家"，已生成地图
输出：{"decision": "FINISH", "reason": "用户问题已完整回答，含数据摘要和地图可视化"}

## RETRY
历史：用户问"查询 POI"，query_poi 返回 error（高德限流），但 OSM 尚未尝试
输出：{"decision": "RETRY", "reason": "高德限流，Tool 内部 Fallback 未触发或失败，建议重试"}

## CONTINUE
历史：coder 自定义统计已读取数据，但尚未生成最终指标
输出：{"decision": "CONTINUE", "reason": "自定义计算尚未形成最终结果"}

## AWAITING_INPUT
历史：用户问"新街口附近有什么好吃的"，系统挂起并询问"你指的是南京还是合肥新街口？"
输出：{"decision": "AWAITING_INPUT", "reason": "Planner 已反问用户，等待用户明确地点"}

## FINISH（达到上限）
历史：已迭代 10 轮，任务仍未完成
输出：{"decision": "FINISH", "reason": "达到最大迭代上限 10 轮，强制终止并返回部分结果"}
```

### 4.2 迭代上限强制终止

```python
# app/agent/loop.py - judge
def judge(state: AgentState):
    if state["iteration"] >= settings.APP_MAX_ITERATIONS:
        return {
            "should_stop": True,
            "final_output": extract_partial_result(state),
        }
    # 否则调用 LLM 判断
    response = llm.invoke(build_judge_messages(state))
    decision = parse_decision(response.content)
    ...
```

### 4.3 RETRY 状态流转

RETRY 和 CONTINUE 都回到 Planner，区别在于上下文传递：

| 决策 | 目标节点 | 传递内容 | 典型场景 |
|------|---------|---------|---------|
| CONTINUE | Planner | 正常 Observer 摘要 | 工具链未执行完 |
| RETRY | Planner | Observer 摘要 + 失败上下文（error_code + 失败原因） | 工具 error 且有替代方案 |
| AWAITING_INPUT | 暂停循环 | 将 clarify 问题展示给用户 | Planner 反问用户，等待回复 |
| FINISH | END | 最终结果 | 任务完成 or 迭代上限 |

```python
# app/agent/loop.py - judge
def judge(state: AgentState):
    ...
    if decision == "RETRY":
        # 将失败上下文附加到消息，让 Planner 知道上次失败原因
        return {
            "should_stop": False,
            "messages": [response, HumanMessage(
                content=f"上一步工具失败：{last_error_code}，请尝试替代方案"
            )],
        }
    elif decision == "CONTINUE":
        return {"should_stop": False}
    elif decision == "AWAITING_INPUT":
        return {
            "should_stop": True,
            "awaiting_input": True,
            "clarify_question": extract_clarify_question(state),
        }
```

Planner 收到 RETRY 上下文后，会根据 error_code 决定策略：
- `AMAP_TIMEOUT` / `AMAP_RATE_LIMITED` → 重试 query_poi（Tool 内部 Fallback 会走 OSM）
- `FILE_PARSE_FAILED` → 提示用户重新上传，不重试
- `LLM_UNAVAILABLE` → 不重试，直接 FINISH 并报错

---

## 5. 坐标系原点铁律

> 为什么要这条铁律：用户报"南京新街口 500m 蜜雪冰城"得答"三座"，经溯源是
> Planner 自由填 (lng, lat) tuple / 字符串 location 导致坐标系原点漂移到偏离的圆心。
> 同一关键词直打高德 count=4，用 geo_code 缓存原点查周边有 8 家。

凡是 query_poi / isochrone / overlay / buffer / voronoi 这类需要 location 的工具：

- **必须**直接使用前序 `geo_code` 的 `result["location"]` 或依赖任务注入的 location 变量
- **禁止**重新抄写或编造一份 `(lng, lat)`；避免形成两个相互漂移的原点
- **唯一例外**：用户当前轮明确给出 WGS84/GCJ02 数字坐标时，走 `geo_code` 的 reverse 路径让缓存/审计生效
- 如看到 `geo_code` 返回 `disambiguated=True`，在 `need_clarification` 里反问用户；不要自作主张选一个

工具层会校验候选坐标与可信原点的偏差，>100m 返回
`error_code="LOCATION_DRIFT"`，相当于给了 Planner 第二次机会重发。

---

## 6. Prompt 版本管理

### 6.1 版本号约定

各角色 prompt 文件通过 git 管理版本，不再维护独立的 `PROMPT_VERSIONS` dict。每个 `app/agents/prompts/{role}.md` 文件头部注释含版本标记（如 `<!-- version: v1.2 -->`）。

版本号变更触发条件：
- 修改 System Prompt 内容 → 小版本 +1（v1.0 → v1.1）
- 修改输出 schema → 大版本 +1（v1.0 → v2.0）

### 6.2 A/B 测试方法

```python
# 通过环境变量切换 Prompt 版本
import os
PLANNER_VERSION = os.getenv("PLANNER_PROMPT_VERSION", "v1.0")

def get_planner_prompt():
    if PLANNER_VERSION == "v1.1_beta":
        return PLANNER_SYSTEM_PROMPT_V11
    return PLANNER_SYSTEM_PROMPT_V10
```

对比指标：
- 工具链正确率（Planner 输出的 ToolCall 是否合理）
- 平均迭代轮次（越少越好）
- 消歧反问触发率（该问的问了没）

---

## 7. Token 预算估算

### 7.1 单次请求的调用结构（Schema 优先）

| 调用 | 输入 token | 输出 token | 说明 |
|------|-----------|-----------|------|
| Root Dispatcher | ~2000 | ~300 | System + 用户输入 + TaskPlan |
| Schema Planner | ~1200 | ~100 | 角色知识 + 单个工具 Schema + 运行时引用目录 |
| Sub-Agent Observer | ~2000 | ~100 | 工具结果(已截断) + System |
| Verifier | ~1500 | ~100 | Sub-Agent 输出 + Verifier prompt |
| Native Finalizer | 0 | 0 | 服务端读取 ToolResult，不调用 LLM |
| Coder Planner + Judge | 按任务变化 | 按任务变化 | 仅 Schema 无法表达时启用 |

> 多步骤请求的主要成本随 DAG Task 数增长。普通步骤不再额外调用 Judge；独立 Task 可并行，因此调用数与墙钟时间不等价。

### 7.2 上下文窗口管理

Multi-Sub-Agent 架构下各 Sub-Agent 独立管理上下文，不会相互污染：
- Root Dispatcher 只保留 TaskPlan + SubAgentOutcome 摘要
- 每个 Sub-Agent 的消息历史独立
- Sub-Agent 使用 registry 中各角色的 `max_iterations` 防止局部重试膨胀
- Root 层 `APP_ROOT_MAX_ITERATIONS=30` 全局上限
- `APP_MAX_COST_TOKENS=100000` 单次对话成本硬上限

### 7.3 成本记录口径

成本应按实际事件和 `CostTracker` 统计，不使用固定“平均轮次”估算：Root 规划计一次；每个普通 Task 记录 Schema Planner、Observer、Verifier；局部重试只增加失败 Task 的调用；Coder 另计 Code Planner/Judge。生产评估同时报告 Task 数、重试数、输入/输出 token 和端到端耗时。

---

*文档版本：v1.3 | 最后更新：2026-08-09 | 属于 Gismind 补充文档*

*v1.3 变更：Root Prompt 改为一次生成工具级多步骤 DAG；普通角色采用单工具闭合 JSON Schema，确定性 finalizer 取代 Judge；Code Mode/Judge 收缩到 coder 回退路径；新增上传读取和同角色连续工具链示例。*

*v1.1 变更：架构从 3 角色升级为 Multi-Sub-Agent + Ensemble（新增 Dispatcher、6 个 Sub-Agent、Verifier）；新增 §2b Dispatcher Prompt、§2c Per-Role Sub-Agent Prompts；更新工具列表（移除未实现工具，新增 code_executor）；few-shot 示例从 4 个扩展到 8 个*
