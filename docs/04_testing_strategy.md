# 测试策略

> 分层测试体系。坐标转换是万恶之源，必须有黄金用例回归基线。
> 数据模型见 [02_data_models.md](02_data_models.md)，API 契约见 [01_api_spec.md](01_api_spec.md)。
> 2026-08-25 的单机验证范围、结果数字与已知边界见 [Gismind 单机运行状态](LOCAL_SINGLE_MACHINE_STATUS.md)。

---

## 1. 测试金字塔

```
            ┌──────────┐
            │   E2E    │  少量，覆盖杀手级场景（§4）
            ├──────────┤
            │ 集成测试  │  中量，覆盖 React Loop / Fallback / 上传 / SSE
            ├──────────┤
            │  单元测试 │  大量，覆盖工具层每个函数
            └──────────┘
```

**覆盖率目标：**

| 层 | 目标 | 工具 |
|----|------|------|
| 工具层（tools/） | 90%+ | pytest + coverage |
| Agent 层（agents/） | 80%+ | pytest + mock LLM |
| 前端组件 | 60%+ | vitest + @testing-library/react |
| E2E | 核心聊天、上传、多文件、恢复、取消、会话与错误态 | Playwright + 真实 Chromium/Vite/FastAPI/Redis/SQLite |

---

## 2. 单元测试

### 2.1 坐标转换黄金用例（最高优先级）

**为什么**：原文档 §8.1 明确"坐标系是万恶之源"。GCJ02 偏转算法实现错误会导致下游所有空间计算偏移几十到几百米，且不易察觉。必须有已知正确结果的回归基线。

**数据文件**：`tests/fixtures/golden_coords.json`

```json
{
  "description": "WGS84 <-> GCJ02 转换黄金用例，覆盖国内主要城市",
  "source": "高精度七参数算法计算 + 实地校准点",
  "accuracy_target_m": 2,
  "cases": [
    {
      "name": "北京天安门",
      "wgs84": [116.3912, 39.9075],
      "gcj02": [116.3975, 39.9087],
      "city": "北京"
    },
    {
      "name": "上海东方明珠",
      "wgs84": [121.4955, 31.2396],
      "gcj02": [121.5018, 31.2408],
      "city": "上海"
    },
    {
      "name": "南京新街口",
      "wgs84": [118.7782, 32.0417],
      "gcj02": [118.7845, 32.0429],
      "city": "南京"
    },
    {
      "name": "乌鲁木齐红山",
      "wgs84": [87.6168, 43.8256],
      "gcj02": [87.6232, 43.8268],
      "city": "乌鲁木齐"
    },
    {
      "name": "海口钟楼",
      "wgs84": [110.3310, 20.0310],
      "gcj02": [110.3374, 20.0322],
      "city": "海口"
    }
  ]
}
```

**测试代码**：

```python
# backend/tests/unit/test_geo_transform.py
import json
import pytest
from pathlib import Path
from app.tools.geo_transform import wgs84_to_gcj02, gcj02_to_wgs84

@pytest.fixture
def golden_cases():
    path = Path(__file__).parent / "fixtures" / "golden_coords.json"
    return json.loads(path.read_text(encoding="utf-8"))["cases"]

def haversine_m(p1, p2):
    """两点距离米，用于判断偏差"""
    ...

def test_wgs84_to_gcj02_golden(golden_cases):
    for case in golden_cases:
        result = wgs84_to_gcj02(*case["wgs84"])
        expected = case["gcj02"]
        deviation = haversine_m(result, expected)
        assert deviation < 2, (
            f"{case['name']} 偏差 {deviation:.1f}m 超过 2m 阈值: "
            f"got {result}, expected {expected}"
        )

def test_gcj02_to_wgs84_roundtrip(golden_cases):
    """GCJ02 -> WGS84 -> GCJ02 往返一致性"""
    for case in golden_cases:
        wgs = gcj02_to_wgs84(*case["gcj02"])
        back = wgs84_to_gcj02(*wgs)
        deviation = haversine_m(back, case["gcj02"])
        assert deviation < 0.5, f"{case['name']} 往返偏差 {deviation:.2f}m"

def test_is_china_bbox():
    from app.tools.geo_transform import is_china_bbox
    assert is_china_bbox((116.39, 39.90, 116.41, 39.92)) is True   # 北京
    assert is_china_bbox((118.77, 32.04, 118.79, 32.06)) is True   # 南京
    assert is_china_bbox((-122.4, 37.7, -122.3, 37.8)) is False    # 旧金山
    assert is_china_bbox((139.7, 35.6, 139.8, 35.7)) is False      # 东京
```

### 2.2 坐标系自动识别测试

```python
# backend/tests/unit/test_geo_transform.py（CRS 启发式推断内聚在同文件）
from app.utils.crs_heuristics import auto_detect_crs

def test_detect_wgs84_by_range():
    assert auto_detect_crs(bbox=(116.0, 39.0, 117.0, 40.0)) == "EPSG:4326"

def test_detect_gauss_projected_by_large_numbers():
    # 经度 > 180，说明是平面坐标
    # 中央经线 118° 的 3 度带横坐标约为 394xxxxx
    crs = auto_detect_crs(bbox=(39450000, 3900000, 39460000, 3910000))
    assert "4548" in crs  # CGCS2000 3度带 118°

def test_detect_no_prj_with_bbox():
    # 模拟无 .prj 文件时的启发式推断
    ...
```

### 2.3 shp 编码探测测试

```python
# backend/tests/unit/test_data_io.py
from pathlib import Path
from app.tools.data_io import DataIO

@pytest.mark.parametrize("zip_name,expected_encoding", [
    ("utf8.zip", "utf-8"),
    ("gbk.zip", "gbk"),
    ("gb2312.zip", "gbk"),
    ("with_cpg.zip", "utf-8"),  # .cpg 声明优先
    ("no_cpg_no_dbf_hint.zip", "gbk"),  # chardet 探测
])
def test_shp_encoding_detection(zip_name, expected_encoding):
    data = Path(f"tests/fixtures/shp/{zip_name}").read_bytes()
    io = DataIO()
    result = io.read_upload(data, zip_name)
    assert result["crs"] == "GCJ02"
    assert result["feature_count"] > 0

def test_shp_all_encodings_failed():
    """所有编码都失败时返回友好错误，不抛 UnicodeDecodeError"""
    data = b"\xff\xfe\x00\x01..."  # 故意构造无法解析的二进制
    io = DataIO()
    result = io.read_upload(data, "bad.zip")
    assert result["status"] == "error"
    assert "编码解析失败" in result["message"]
```

### 2.4 POI 去重测试（R-Tree）

```python
# backend/tests/unit/test_poi_query.py
from app.tools.poi_query import POIQuery

def test_dedup_cross_source():
    """高德和 OSM 的同一 POI 应去重，保留信息更全的"""
    amap_pois = [
        {"name": "蜜雪冰城", "location": (118.7845, 32.0429), "source": "Amap", "address": "中山路1号"},
    ]
    osm_pois = [
        {"name": "Mixue Ice Cream & Tea", "location": (118.7846, 32.0430), "source": "OSM_CN", "address": None},
    ]
    query = POIQuery(amap_key="test")
    merged = query._deduplicate(amap_pois, osm_pois, threshold=50)
    assert len(merged) == 1
    assert merged[0]["source"] == "Amap"  # 高德信息更全，保留
    assert merged[0]["address"] == "中山路1号"

def test_dedup_same_source_no_merge():
    """同源不同 POI 不应被误合并"""
    pois = [
        {"name": "A店", "location": (118.78, 32.04), "source": "Amap"},
        {"name": "B店", "location": (118.79, 32.05), "source": "Amap"},
    ]
    query = POIQuery(amap_key="test")
    merged = query._deduplicate(pois, [], threshold=50)
    assert len(merged) == 2

def test_dedup_requires_same_crs():
    """去重前必须统一坐标系，否则距离计算错误"""
    # GCJ02 和 WGS84 同一点坐标差约 50-500m，不统一会误判为不同点
    ...
```

### 2.5 工具层其他单测

- `buffer`：半径 500m 时，结果面积应约为 π×500²（允许 5% 误差，因投影变形）
- `overlay`：两个完全重叠的多边形 intersection 面积等于原面积
- `voronoi`：点数 < 4 时返回明确错误，不崩溃
- `isochrone`：海边原点应过滤朝向水面的无效采样点

### 2.6 2026-08 语义回归红线

以下用例专门防止“HTTP 200 但 GIS 结果错误”的回归；应断言工具参数、数值或产物，而不能只断言状态码。

| 风险 | 测试位置 | 必须断言 |
|------|----------|----------|
| GPS→高德与闭合属性/DEM 请求 | `tests/unit/test_dispatcher.py` | `planner_source=guardrail`，并保留 `geo_transform.operation/lng/lat`、`class == station`、`bins=[15,30]` 与 `values=[1,2,3]` |
| 国内上传数据二次偏移 | `tests/unit/test_spatial_analysis_extended.py`、`tests/unit/test_runtime_contract_regressions.py` | `crs_label=GCJ02` 经空间计算后不再发生第二次 WGS84→GCJ02 偏转 |
| 最近邻零距离 | `tests/unit/test_spatial_analysis_extended.py` | `max_distance=0` 仅保留相交/重合要素，结果距离恰为 0；负距离报错 |
| 栅格阈值边界 | `tests/unit/test_raster_analysis.py` | 15° 与 30° 归入上一个阈值对应的类别，避免浮点边界漂移 |
| POI 主/备 Overpass | `tests/unit/test_poi_query.py` | 主端点请求失败后访问备用端点；合法空响应不额外请求备用端点 |
| 多轮 POI 比较 | `tests/unit/test_dispatcher.py` | 最终摘要同时包含上一轮和当前轮数量，比较基于数字而非模型猜测 |

---

## 3. 集成测试

### 3.1 React Loop 状态流转

入口为 `app/agents/tool_execution.py:run_react_loop`，集成测试位于 `backend/tests/integration/test_agent_loop.py`。

测试覆盖 observer / judge 两个核心节点，以及 `_TOOL_REGISTRY` handler 层的 LOCATION_DRIFT 校验：

**observer 节点**（`app/agents/observer.py:observe`）：

```python
# backend/tests/integration/test_agent_loop.py
from unittest.mock import patch, MagicMock
from app.agents import observer as observer_mod

class TestObserverObserve:
    @patch("app.agents.observer.create_llm")
    def test_observe_returns_string(self, mock_create_llm):
        """observer.observe 返回 ≤200 字自然语言摘要"""
        mock_llm = MagicMock()
        resp = MagicMock()
        resp.content = "找到 12 个蜜雪冰城，来源高德。"
        mock_llm.invoke.return_value = resp
        mock_create_llm.return_value = mock_llm

        summary = observer_mod.observe(_success_tool_result())
        assert isinstance(summary, str)
        assert len(summary) <= 200

    @patch("app.agents.observer.create_llm")
    def test_observe_truncated_result_mentioned(self, mock_create_llm):
        """截断结果应标注"截断"字样"""
        ...

    def test_observer_system_prompt_content(self):
        """OBSERVER_SYSTEM_PROMPT 含 ≤200 字、摘要、来源等关键约束"""
        from app.agents.observer import OBSERVER_SYSTEM_PROMPT
        assert "Observer" in OBSERVER_SYSTEM_PROMPT
        assert "200" in OBSERVER_SYSTEM_PROMPT
```

**coder Judge 节点**（`app/agents/judge.py:judge`，普通角色不经过此节点）：

```python
class TestJudgeJudge:
    @patch("app.agents.judge.create_llm")
    def test_judge_finish(self, mock_create_llm):
        """任务完成 → FINISH"""
        ...

    @patch("app.agents.judge.create_llm")
    def test_judge_retry(self, mock_create_llm):
        """工具失败 → RETRY，附加失败上下文消息"""
        ...

    @patch("app.agents.judge.create_llm")
    def test_judge_force_finish_at_max_iterations(self, mock_create_llm):
        """iteration >= APP_MAX_ITERATIONS 时强制 FINISH，不调 LLM"""
        ...
```

**工具级 WorkflowPlan DAG**：

```python
def test_same_role_tool_chain_is_a_first_class_dag():
    """fix → reproject → buffer → dissolve → export 均为独立 Task。"""
    ...

def test_dispatch_executes_same_role_workflow_dag_and_carries_each_edge():
    """按拓扑顺序执行，并把上一步 result/dep_<task_id> 注入下游。"""
    ...

def test_failed_native_step_revises_only_current_step_without_finishing():
    """empty/error 不得误判完成，只修订当前失败步骤。"""
    ...

def test_native_finalize_persists_pending_without_legacy_judge():
    """普通角色移除 Judge 后，PendingStore 与 /resume 仍然可用。"""
    ...
```

必须覆盖的断言：

- Root 规划的 instruction 覆盖、Task ID、依赖引用、DAG 无环与角色/工具权限校验
- 一个 Task 只绑定 `required_tool_name` 对应的 Schema，模型不能换工具或追加后续动作
- 独立 Task 同批并行；依赖失败时下游不得执行
- `empty/error/final_output.status=failed` 均转换为 failed outcome
- GeoJSON 与导出路径不会在 Sub-Agent → Root → API 组装过程中丢失
- Coder 的历史数字引用顺序不因 Native `result` 数据边改变

**LOCATION_DRIFT 集成校验**：

```python
class TestLocationDriftIntegration:
    def test_query_poi_free_tuple_rejected_when_anchor_exists(self, fake_redis):
        """geo_code 锚定 (118.78, 32.04)，query_poi 偏移 ~12km → LOCATION_DRIFT"""
        from app.agents.tool_execution import _TOOL_REGISTRY
        # 直接调 handler，验证 LOCATION_DRIFT error_code
        ...

    def test_query_poi_drift_within_tolerance_passes(self, fake_redis):
        """偏移 ~14m 在容差内 → 正常通过"""
        ...
```

### 3.2 POI 双源 Fallback

```python
# backend/tests/unit/test_poi_query.py 及相关集成测试
from unittest.mock import patch
from app.tools.poi_query import POIQuery

@patch("app.tools.poi_query.requests.get")
def test_amap_empty_triggers_osm(mock_get):
    """高德返回空时自动走 OSM"""
    mock_get.side_effect = [
        MockResponse(json_data={"pois": []}),  # 高德空
        MockResponse(json_data={"elements": [{"type":"node","lat":32.04,"lon":118.78,"tags":{"name":"蜜雪冰城"}}]}),  # OSM 有
    ]
    query = POIQuery(amap_key="test")
    result = query.search_poi_tool("蜜雪冰城", "南京新街口", 500)
    assert result["status"] == "success"
    assert result["source"] == "OSM_CN"  # 国内 OSM 数据转了 GCJ02

@patch("app.tools.poi_query.requests.get")
def test_both_empty_returns_empty(mock_get):
    """双源都空时返回 empty，不抛异常"""
    mock_get.side_effect = [
        MockResponse(json_data={"pois": []}),
        MockResponse(json_data={"elements": []}),
    ]
    query = POIQuery(amap_key="test")
    result = query.search_poi_tool("不存在的店", "南京新街口", 500)
    assert result["status"] == "empty"

@patch("app.tools.poi_query.requests.get")
def test_amap_timeout_triggers_osm(mock_get):
    """高德超时（TimeoutError）对 LLM 来说也是空，触发 OSM"""
    import requests
    mock_get.side_effect = [
        requests.Timeout("amap timeout"),
        MockResponse(json_data={"elements": [...]}),
    ]
    query = POIQuery(amap_key="test")
    result = query.search_poi_tool("蜜雪冰城", "南京新街口", 500)
    assert result["status"] == "success"
```

还必须覆盖主 Overpass 失败后的备用端点：`test_osm_backup_endpoint_is_used_after_primary_failure` 断言调用顺序为 AMap → 主 OSM → 备用 OSM，并要求最终保留国内 OSM 的 GCJ02 坐标语义。

### 3.3 文件上传全流程

```python
# backend/tests/integration/test_api.py
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_upload_shp_zip():
    with open("tests/fixtures/shp/nanjing_poi_utf8.zip", "rb") as f:
        resp = client.post(
            "/api/upload",
            files={"file": ("nanjing.zip", f, "application/zip")},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["crs"] == "GCJ02"
    assert data["feature_count"] > 0
    assert "file_id" in data

def test_upload_rejected_type():
    resp = client.post(
        "/api/upload",
        files={"file": ("malware.exe", b"MZ...", "application/octet-stream")},
    )
    assert resp.status_code == 422

def test_upload_zip_bomb_rejected():
    """ZIP 炸弹应被拦截"""
    # 构造压缩比异常的 zip
    ...
```

### 3.4 SSE 流式输出

```python
# backend/tests/integration/test_sse_events.py
def test_chat_sse_events(client):
    with client.stream("POST", "/api/chat", json={
        "session_id": "test",
        "message": "南京新街口蜜雪冰城",
    }) as resp:
        assert resp.status_code == 200
        events = []
        for line in resp.iter_lines():
            if line.startswith("event: "):
                events.append(line[7:])
        assert "status" in events
        assert "token" in events
        assert events[-1] == "done"
```
	
### 3.5 code_mode 测试

code_mode（`app/agents/code_mode/`）是 LLM 直接生成并执行 Python 代码的沙箱路径。相关单元测试覆盖以下关键模块：

**AST Guard**（`backend/tests/unit/test_code_mode_ast_guard.py`）：

```python
from app.agents.code_mode.ast_guard import inspect as ast_inspect, ASTBannedNodeError

def test_inspect_clean_code_returns_sandbox():
    """纯赋值/计算无副作用 → required_executor="sandbox" """
    result = ast_inspect("x = 1\ny = 2\nz = x + y")
    assert result.required_executor == "sandbox"

def test_banned_node_os_system_raises():
    """os.system 是 outright banned → ASTBannedNodeError"""
    with pytest.raises(ASTBannedNodeError):
        ast_inspect("import os\nos.system('rm -rf /')")

def test_call_graph_tracks_complex_flow():
    """AST 追踪跨函数调用链，标记危险模式"""
    ...
```

**Namespace 构建**（`backend/tests/unit/test_code_mode_namespace.py`）：

```python
from app.agents.code_mode.namespace import build_namespace, _sync_proxy

def test_clean_namespace_no_os_sys():
    """构建的 namespace 不应包含 os/sys/subprocess/__builtins__"""
    ns = build_namespace(session_vars={}, tool_registry={})
    assert "os" not in ns
    assert "sys" not in ns

def test_sync_proxy_wraps_async_tools():
    """_sync_proxy 将 async 工具包装为同步可调用对象"""
    ...

def test_session_vars_naming_conflict_warns():
    """session_vars 与工具名冲突时跳过变量 + warning"""
    ...
```

**Sandbox Runner IPC**（`backend/tests/unit/test_sandbox_runner_ipc.py`）：

```python
from app.agents.code_mode.sandbox_runner import (
    _write_session_vars,
    _parse_result_sentinel,
)

def test_write_session_vars_creates_tempfile():
    """session_vars 通过 tempfile env 注入子进程（不经命令行）"""
    ...

def test_parse_result_sentinel():
    """UUID sentinel 标记的 stderr 输出被正确解析"""
    ...
```

**Sandbox 安全审计**（`backend/tests/unit/test_sandbox_audit.py`）：

验证 sandbox runner 子进程的审计日志、资源限制、超时控制等安全策略。

**code_mode 路由与状态**（`backend/tests/unit/test_code_mode_routing.py`、`backend/tests/unit/test_code_mode_state.py`）：

测试 code_mode 的路由决策逻辑（哪些查询走 code_mode）和状态机流转。

**Prompt 构建**（`backend/tests/unit/test_build_code_mode_prompt.py`）：

测试 code_mode 的 system prompt 组装，包括工具签名注入、session_vars 说明等。

---

## 4. E2E 测试场景

对齐原文档 §9 的三个杀手级场景。使用 Playwright。

### 4.0 Root Planner 同义表达实测（HTTP/SSE）

`blackbox/root_planner_synonym_suite.py` 通过公开 HTTP API 发送真实请求，解析 SSE 的 `run.plan`、`tool.call.*`、`map`、`done/error` 事件，并核对可执行参数和关键数值。它不替代可重复的 pytest：该套件依赖已配置的 LLM、高德和 Overpass，适合作为单机发布前的实测记录，**不应**作为无网络 CI 的必过门槛。

当前套件覆盖：周边 POI、GPS→高德坐标、上传 `class == station`、面交集、DEM 坡度 15°/30° 分级、多轮茶百道/蜜雪冰城数量比较，以及上传面缓冲 GeoJSON 导出。每个 case 在 `root_planner_synonym_cases.json` 声明允许的 `planner_source`：一般请求为 `root_llm`，闭合契约为 `guardrail`。

通过标准：HTTP 200、计划工具/依赖/必需参数正确、所有终态工具成功、需要地图时地图非空、存在 `done` 且没有 `run.failed/error`，以及坐标/导出等 case 专属语义断言通过。

### 4.1 场景 1：单点 POI 查询

```typescript
// e2e/scenario1_poi_query.spec.ts
import { test, expect } from '@playwright/test';

test('南京新街口蜜雪冰城查询', async ({ page }) => {
  await page.goto('/');
  await page.fill('input[placeholder*="输入"]', '南京新街口500米内有多少蜜雪冰城');
  await page.press('input', 'Enter');

  // 等待地图块出现
  const mapBlock = page.locator('[data-testid="map-block"]').first();
  await expect(mapBlock).toBeVisible({ timeout: 30000 });

  // 验证文字回复包含数量
  const textBlock = page.locator('[data-testid="text-block"]').last();
  await expect(textBlock).toContainText(/找到\s*\d+\s*家/);
});
```

### 4.2 场景 2：文件上传 + 叠加分析

```typescript
test('上传 shp 并查询区内 POI', async ({ page }) => {
  await page.goto('/');
  await page.setInputFiles('input[type="file"]', 'e2e/fixtures/nanjing_district.zip');
  await page.waitForSelector('text=上传成功');

  await page.fill('input[placeholder*="输入"]', '这个区有多少蜜雪冰城');
  await page.press('input', 'Enter');

  await expect(page.locator('[data-testid="map-block"]')).toBeVisible();
  await expect(page.locator('text=蜜雪冰城')).toBeVisible();
});
```

### 4.3 场景 3：多轮对话

```typescript
test('多轮对话状态保持', async ({ page }) => {
  await page.goto('/');
  await page.fill('input', '南京新街口500米内蜜雪冰城');
  await page.press('input', 'Enter');
  await page.waitForSelector('[data-testid="map-block"]');

  // 第二轮，引用上一轮结果
  await page.fill('input', '再查下茶百道，对比密度');
  await page.press('input', 'Enter');

  await expect(page.locator('[data-testid="map-block"]').nth(1)).toBeVisible();
  await expect(page.locator('text=对比')).toBeVisible();
});
```

---

## 5. 测试数据管理

### 5.1 fixtures 目录结构

```
backend/tests/
├── fixtures/
│   ├── golden_coords.json          # 坐标转换黄金用例（已提交）
│   └── golden_drift.json           # geo_code 漂移与消歧基线（已提交）
├── unit/
│   ├── test_geo_transform.py       # 坐标转换 + CRS 启发式
│   ├── test_poi_query.py           # POI 查重 + 双源 Fallback
│   ├── test_data_io.py             # shp 编码探测
│   ├── test_data_io_extended.py
│   ├── test_code_mode_ast_guard.py # AST 安全检查
│   ├── test_code_mode_namespace.py # 沙箱 namespace 构建
│   ├── test_code_mode_routing.py   # code_mode 路由
│   ├── test_code_mode_state.py     # code_mode 状态机
│   ├── test_sandbox_runner_ipc.py  # sandbox IPC 通信
│   ├── test_sandbox_audit.py       # sandbox 审计
│   ├── test_dispatcher.py          # dispatcher 调度
│   ├── test_build_code_mode_prompt.py
│   ├── test_planner.py
│   ├── test_registry.py
│   ├── test_session.py
│   ├── test_metrics.py
│   ├── test_cost.py
│   ├── test_errors.py
│   └── ...（共 40+ 个单测文件）
├── integration/
│   ├── test_agent_loop.py          # observer/judge + LOCATION_DRIFT
│   ├── test_api.py                 # HTTP 端点 + upload
│   ├── test_sse_events.py          # SSE 流式输出
│   ├── test_guardrail_pipeline.py  # preflight 规则链
│   ├── test_data_io_e2e.py         # Data I/O 端到端
│   ├── test_vector_tools_e2e.py
│   ├── test_raster_tools_e2e.py
│   └── ...（共 10+ 个集成测试文件）
└── conftest.py
```

**说明**：
- `golden_coords.json` 是已提交的唯一静态 fixture，用于坐标转换回归基线。
- shp 编码测试（utf8/gbk/gb2312/cpg/no_prj/bad_encoding）由 `backend/tests/unit/test_data_io.py` 通过 `_make_shp_zip` 等辅助函数在内存中动态生成，不依赖外部 fixture 文件，也不提交到仓库。
- mock API 响应与大规模 GeoJSON 样本目前未提交；需要时可在 `backend/tests/fixtures/` 下按上述结构补充，但现有测试代码中尚未引用这些路径。

### 5.1.1 黄金用例：geo_code 漂移与消歧（增量）

新文件 `backend/tests/fixtures/golden_drift.json` 收录两个端到端基线：

- `"南京新街口 500m 蜜雪冰城"` → 期望 source_count >= 5、candidates >= 2、
  principal_in_nanjing=true（在南京，而不是合肥/广州）、
  tolerance_m=50
- `"新街口"` → 期望 disambiguated=true、candidate_count >= 2
  （多个同名地点至少给出 top-2）

每次重大重构后跑：
```bash
cd backend && python -c "
import asyncio, json
from app.tools.geo_code import GeoCoder
from app.config import settings
gc = GeoCoder(amap_key=settings.AMAP_KEY)
golden = json.load(open('backend/tests/fixtures/golden_drift.json'))
for c in golden:
    r = asyncio.run(gc.geocode(c['input']))
    assert r['status'] == 'success' and len(r.get('candidates', [])) >= 1
    print(f\"  {c['input']} -> {len(r['candidates'])} cands, conf={r['confidence']:.2f}\")
"
```

### 5.2 mock 数据生成

高德/OSM 响应样本从真实 API 抓取一次（开发阶段），脱敏后存为 JSON 文件。测试时 mock `requests.get` 返回这些样本，避免依赖网络和消耗配额。

```python
# backend/tests/conftest.py
import json
from pathlib import Path

@pytest.fixture
def amap_poi_response():
    path = Path(__file__).parent / "fixtures" / "mock_responses" / "amap_poi_nanjing.json"
    return json.loads(path.read_text(encoding="utf-8"))
```

---

## 6. 运行命令

### 6.1 后端

```bash
# 进入后端目录
cd backend

# 运行所有测试
pytest

# 仅单元测试
pytest tests/unit/ -v

# 仅集成测试
pytest tests/integration/ -v

# 带覆盖率
pytest --cov=app --cov-report=html --cov-report=term-missing

# 仅坐标转换测试（快速回归）
pytest tests/unit/test_geo_transform.py -v

# 本轮高风险语义回归
pytest tests/unit/test_dispatcher.py tests/unit/test_poi_query.py \
  tests/unit/test_spatial_analysis_extended.py tests/unit/test_raster_analysis.py -q

# code_mode 相关测试
pytest tests/unit/test_code_mode_ast_guard.py tests/unit/test_code_mode_namespace.py tests/unit/test_sandbox_runner_ipc.py -v

# 并行执行
pytest -n auto  # 需安装 pytest-xdist
```

### 6.2 前端

```bash
cd frontend

# 单元测试
vitest

# 带覆盖率
vitest run --coverage

# watch 模式
vitest watch
```

### 6.3 E2E

```bash
# 需先启动后端 + 前端
npx playwright test

# 带 UI
npx playwright test --ui

# 仅某个场景
npx playwright test e2e/scenario1_poi_query.spec.ts
```

真实 Root Planner/SSE 核验（需先启动后端；不需要前端）：

```powershell
cd blackbox
..\backend\.venv\Scripts\python.exe root_planner_synonym_suite.py `
  --base-url http://127.0.0.1:8000 `
  --output ..\blackbox-results\root-planner-$(Get-Date -Format yyyyMMdd-HHmmss).json `
  --case-deadline 180
```

输出报告应随测试留档；若某次失败，先检查报告内 `tool.call.complete` 的 `status/error_code` 与 provider 日志，区分外部 API 暂时不可用、LLM 计划错误和工具数值错误，不能只重跑后把失败忽略。

---

## 7. CI 集成建议

```yaml
# .github/workflows/ci.yml（示例）
jobs:
  backend-test:
    runs-on: ubuntu-latest
    services:
      redis:
        image: redis:7
        ports: ["6379:6379"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -r backend/requirements.txt
      - run: pip install pytest pytest-cov pytest-xdist
      - run: cd backend && pytest --cov=app --cov-fail-under=80
        env:
          LLM_API_KEY: test_key
          LLM_BASE_URL: http://localhost
          LLM_MODEL: test
          AMAP_KEY: test
          AMAP_JS_KEY: test
          AMAP_JS_SECURITY_CODE: test

  frontend-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: "20" }
      - run: cd frontend && npm ci && npm run test -- --coverage
```

**CI 中的密钥处理**：使用 CI secret 注入测试 key，不使用真实生产 key。LLM 和高德 API 在 CI 中走 mock，不打真实请求。

---

*文档版本：v1.3 | 最后更新：2026-08-09 | 属于 Gismind 补充文档*

*v1.3 变更：新增工具级 DAG、同角色连续步骤、确定性 finalizer、依赖产物数据边、失败阻断和 Native PendingStore 回归要求；Judge 测试明确仅覆盖 coder。*

*v1.2 变更：测试文件引用路径从 `tests/` 更新为 `backend/tests/`；Agent 层入口从 `app/agent/loop.py:build_app` 更新为 `app/agents/tool_execution.py:run_react_loop`；集成测试引用更新为实际测试文件名（`test_agent_loop.py`, `test_sse_events.py`, `test_api.py`, `test_guardrail_pipeline.py`）；新增 §3.5 code_mode 测试说明*
*v1.1 变更：Agent 层路径从 `agent/` 更新为 `agents/`（Multi-Sub-Agent 架构）*
