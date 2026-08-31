# Manual Testing — 手动回归与准确性对照手册

Gismind 工具的"准确不准"判定手册。当 Agent 给的答案看着不对劲时，按这份手册做。

v1.5，更新于 2026-08-31——补充 32 条真实 Chromium 前后端耦合结果、规划来源统计和导出 CRS 语义核验口径。当前单机可用性结论见 [Gismind 单机运行状态](LOCAL_SINGLE_MACHINE_STATUS.md)。

---

## 1. 三种验证层次（按代价递增）

### L1 — 直接打数据源官方 API（最快、不依赖 Gismind）
用来获取**裸答案**：把 Gismind 整体隔离开，单独看上游数据源说什么。

```bash
# 高德 Web 服务
curl 'https://restapi.amap.com/v3/place/text?key=$AMAP_KEY&keywords=蜜雪冰城&city=南京&citylimit=true&offset=50&extensions=all'
# 数 pois[] 长度

curl 'https://restapi.amap.com/v3/place/around?key=$AMAP_KEY&keywords=蜜雪冰城&location=118.7845,32.0429&radius=500&offset=50&extensions=all'
# 看南京新街口 500m 蜜雪冰城"裸"几条
```

OSM 兜底路径：
```bash
curl -X POST 'https://overpass-api.de/api/interpreter' --data-urlencode 'data=
[out:json][timeout:3];
(node["name"~"蜜雪冰城"](32.04,118.78,32.05,118.79););out body;'
```

如果 L1 已经把"真值"确定了 → Gismind 答案对不对就有了对照基准。

### L2 — 绕过 LLM 直调工具（最快、最准）
**关键能力**：让上游数据源和工具代码本身被验证，而 LLM 规划错误 / ReAct 抖动都不参与。

```bash
cd backend
python -c "
from app.tools.poi_query import POIQuery
from app.tools.geo_code import GeoCoder
from app.config import settings
import asyncio

# 地理编码（geo_code 工具可单独验证）
g = asyncio.run(GeoCoder(amap_key=settings.AMAP_KEY).geocode('南京新街口'))
print('geocoded:', g.get('location'), g.get('formatted_address'), 'cached=', g.get('cached'))

# POI 查询（query_poi 工具可单独验证）
loc = g['location']
raw = POIQuery(
    amap_key=settings.AMAP_KEY,
    osm_endpoint=settings.OSM_ENDPOINT,
    osm_backup_endpoints=settings.OSM_BACKUP_ENDPOINTS,
).search_poi_tool('蜜雪冰城', tuple(loc), 500)
print('source=', raw.get('source'))
pois = (raw.get('data') or {}).get('pois', [])
print('after-dedup count=', len(pois))
for p in pois[:8]:
    print(' -', p['name'], 'd=', p.get('distance'), 'addr=', p.get('address'))
"
```

可直调的其它工具（同样不经过 Agent）：
```python
# 坐标转换 — 黄金用例对比
from app.tools.geo_transform import wgs84_to_gcj02, gcj02_to_wgs84
print(wgs84_to_gcj02(116.3912, 39.9075))   # 应近似 (116.3975, 39.9094)
print(gcj02_to_wgs84(120.21, 30.27))       # 往返应接近原值

# 缓冲区 / 叠加 / 等时圈 — 需要构造 GeoDataFrame
from app.tools.spatial_analysis import SpatialAnalyzer
import geopandas as gpd
from shapely.geometry import Point
gdf = gpd.GeoDataFrame({'name':['a','b'], 'geometry':[Point(118.78,32.04), Point(118.79,32.05)]}, crs='EPSG:4326')
gdf.attrs['crs_label'] = 'GCJ02'
print(SpatialAnalyzer().buffer(gdf, 500))

# 数据 IO
from app.tools.data_io import DataIO
print(DataIO().read_upload(open(r'C:\tmp\sample.geojson','rb').read(), 'sample.geojson'))
```

### L3 — 跑 pytest 黄金用例与真实耦合集成覆盖
```bash
cd backend
python -m pytest tests/unit/test_poi_query.py -v          # POI 去重 / Fallback / bbox
python -m pytest tests/unit/test_geo_transform.py -v       # 坐标往返
python -m pytest tests/unit/test_geo_code.py -v            # 高德 + 缓存
python -m pytest tests/unit/test_data_io.py -v             # ZIP / GeoJSON / KML
python -m pytest tests/unit/test_spatial_analysis.py -v    # buffer / overlay / voronoi / isochrone
python -m pytest tests/integration/test_agent_loop.py -v   # Agent Loop 集成场景（覆盖 ~55 工具）
python -m pytest tests/unit/test_code_mode_namespace.py -v  # code_mode 命名空间隔离 / tool 可见性
python -m pytest tests/unit/test_kernel_always_visible.py -v # kernel 工具始终可见
python -m pytest tests/unit/test_tool_registry_extended.py -v # TOOL_SPECS 注册完整性
python -m pytest tests/unit/test_dispatcher.py tests/unit/test_poi_query.py tests/unit/test_spatial_analysis_extended.py tests/unit/test_raster_analysis.py -q
```

真实 Chromium、Vite、FastAPI、Redis 与 SQLite 耦合测试：

```powershell
cd frontend
npm run test:e2e
```

这组浏览器测试允许注入确定性 LLM，但不拦截或伪造浏览器 HTTP/SSE 请求。真实 LLM 冒烟必须另外记录 `planner_source`，不能把 `guardrail` 命中写成 Root Planner 通过。

`tests/fixtures/golden_coords.json` 收录 5 城市 WGS84↔GCJ02 用例（公开近似算法，**允许 200-500m 偏差**）。

---

## 2. 工具 Prompt 套件

下表所有 prompt 都可以直接复制到前端输入框。每条都对照 `backend/app/agents/registry.py` 中 `TOOL_SPECS` 已注册工具（~55 个，含 raster / vector / kernel / 属性 / IO 等分类）。

| # | 工具 | Prompt | 期望链路 | 关键观察点 |
|---|------|--------|---------|----------|
| 1 | `geo_code` | `南京新街口的经纬度是多少` | 单步 geo_code | 思考链里能看到 `address` 字段；返回 GCJ02 lng/lat |
| 2 | `query_poi` | `南京新街口 500 米内有多少蜜雪冰城` | 单步 query_poi | 关键对账用例——radius=500 与 L1 裸数据对一下 |
| 3 | `buffer` + `map_layer_build` | `找出南京夫子庙 1km 内的所有地铁站，然后把 1km 缓冲区画出来` | query_poi → buffer → map_layer_build | 思考链 2-3 步；地图上能看到橙色缓冲区环 |
| 4 | `overlay` | `南京新街口 500m 蜜雪冰城覆盖区与夫子庙 500m 蜜雪冰城覆盖区，求交集并标出来` | query_poi×2 → buffer×2 → overlay → map_layer_build | 多步实验；交集多边形应在地图上叠加 |
| 5 | `voronoi` | `把这 4 个 POI 做泰森多边形：中山陵、夫子庙、新街口、玄武湖` | geo_code×4 → voronoi | 必须点数 ≥ 4 否则报"点数过少(<4)" |
| 6 | `isochrone` | `画一个上海人民广场步行 15 分钟可达范围` | geo_code → isochrone | 必须 `AMAP_KEY` 配齐；否则返回 empty 不报错 |
| 7 | `data_io_read` + `map_layer_build` | 先上传 `sample.geojson`（见 §3），再问 `把这个文件按字段 class 分级设色显示` | data_io_read → map_layer_build | 检查地图 `map` 事件里的 layers 配置 |
| 8 | `map_layer_build`（独立） | 在 7 号基础上追加：`再加一层，把所有 class 是 poi 的点放大 2 倍` | map_layer_build 增量更新 | 验证样式表渲染 |
| 9 | `clip_layer` | `用南京市行政区划裁剪这个 POI 图层` | load_vector → clip_layer | 输出的几何应当只保留在裁剪面内的要素 |
| 10 | `dissolve_layer` | `按区域字段融合相邻地块` | dissolve_layer（by=region） | 融合后行数减少；几何边界消失 |
| 11 | `merge_layers` | `把玄武湖图层和紫金山图层合并成一个` | merge_layers | 输出总行数 = 两个输入之和 |
| 12 | `join_by_location` | `统计每个街道里有多少个 POI` | count_points_in_polygon（或 join_by_location） | 输出带计数的面图层 |
| 13 | `join_by_nearest` | `给每个 POI 关联最近的公交站点` | join_by_nearest | 输出带最近站点名称/距离的点图层 |
| 14 | `slope` + `aspect` + `hillshade` | `加载 DEM，计算坡度、坡向、山体阴影，并叠加显示` | load_raster → slope → aspect → hillshade | 3 个输出栅格；在地图上叠加可见 |
| 15 | `zonal_statistics` | `用行政区分区统计 DEM 的平均海拔` | load_raster → load_vector → zonal_statistics | 输出带 mean/min/max 统计值的矢量图层 |
| 16 | `field_calculator` | `添加一个面积字段 area_km2` | field_calculator（expression=`$area/1e6`） | 输出带新字段的图层 |
| 17 | `reproject_layer` | `把这个图层从 GCJ02 转为 WGS84` | reproject_layer（target_crs=EPSG:4326） | 坐标偏移约 300-500m，肉眼可辨 |
| 18 | `extract_by_attribute` | `筛选出 class 是 station 的所有要素` | extract_by_attribute | 输出行数 ≤ 输入行数 |
| 19 | `convex_hull` | `计算这组 POI 的外包凸包` | convex_hull | 输出单个多边形 |
| 20 | `reclassify_raster` | `把坡度分成 0-15° / 15-30° / >30° 三档` | slope → reclassify_raster | 3 个值类别的栅格 |

### 2.1 高风险结果核对（不要只看 `done`）

| 场景 | 可直接输入的 Prompt | 必须核对的结果 |
|------|----------------------|----------------|
| GPS→高德 | `请把 GPS 点 116.397、39.908 换成高德地图可直接使用的坐标。` | `run.plan` 为 `geo_transform`，`operation=wgs84_to_gcj02`；输出必须与输入有合理 GCJ02 偏移，不能原样回显 |
| 属性筛选 | 上传带 `class` 字段的数据后：`挑出 class 等于 station 的记录并显示。` | 工具为 `extract_by_attribute`，参数为 `field=class/operator==/value=station`；要素数与原文件属性过滤结果一致 |
| 坡度分级 | 上传 DEM 后：`先求坡度，再按小于15度、15到30度和大于30度分三级显示。` | 链路为 `data_io_read → slope → reclassify_raster`，`bins=[15,30]`、`values=[1,2,3]`；15° 属于第 2 类，30° 属于第 3 类 |
| 零距离最近邻 | 上传一个 POI 与一个公交站完全重合，另放一个 100m 外站点后：`最大距离 0 米关联最近公交站。` | 仅重合/相交要素保留，`distance_m=0`；100m 外站点绝不能被“最近”兜底匹配 |
| 多轮数量比较 | 先问 `南京新街口500米内蜜雪冰城`，再问 `沿用刚才的位置，改查茶百道并和上一轮数量比较。` | 结果同时写出两轮数量和差异/密度判断；不可只给当前品牌的数量 |

若 POI 请求出现高德超时，日志中应能看到按 `OSM_ENDPOINT` → `OSM_BACKUP_ENDPOINTS` 的顺序尝试。两者都不可用时应明确记录为数据源暂时不可用/空结果，而不能伪造“未找到”的业务结论。

如果某条 prompt 走不通，按下面的诊断树：

```
前端无反应        → 看 SSE 流是否断了 / token 事件
返回 error        → 看 trace_id，对照 docs/06_security.md §3
返回 done 但地图空 → 多半是某个 tool 空结果被吞掉，看 observer 输出
答案与 L1 不一致   → 走 §1 L2 直调工具，对比 raw 输出
```

---

## 3. 准备测试文件

最小可用样例（geojson，不需要 SHP）：

```bash
mkdir -p C:\tmp
python -c "
import geopandas as gpd
from shapely.geometry import Point
gdf = gpd.GeoDataFrame({
    'name':['蜜雪A','蜜雪B','站点C','站点D'],
    'class':['poi','poi','station','station'],
    'geometry':[Point(118.78+x*0.005, 32.042+y*0.005) for x,y in [(0,0),(3,3),(5,0),(8,2)]]
}, crs='EPSG:4326')
gdf.to_file(r'C:\tmp\sample.geojson', driver='GeoJSON')
print('wrote', r'C:\tmp\sample.geojson')
"
```

也支持 SHP ZIP：
```bash
python -c "
import geopandas as gpd
from shapely.geometry import Point
gdf = gpd.GeoDataFrame({'name':['x'],'geometry':[Point(118.78,32.04)]}, crs='EPSG:4326')
gdf.to_file(r'C:\tmp\sample.shp')
# 然后手动 zip 成 sample.zip（含 shp/shx/dbf/prj）
"
```

---

## 4. 已知 Bug 与风险点清单

- 外部 POI 数据源是非确定性依赖：高德或任一 Overpass 实例可独立限流、超时或拒绝请求。先检查 `tool.call.complete` 和 provider 日志，再判断是否为 GIS 结果错误；不要把一次外部失败写成“附近没有店”。
- 上传后的国内数据应带 `crs_label=GCJ02`。如果地图或 reproject 输出出现约数百米的第二次偏移，优先检查这个标记是否在 `DataIO → SpatialAnalyzer` 之间丢失。
- `max_distance=0` 不是普通最近邻阈值；它是严格拓扑相交语义。任何 0 外距离的候选都必须排除。
- GeoJSON 没有 CRS 或文件本身 CRS 错标时，系统只能按上传解析结果处理；先在 L2 检查原始数据坐标和 `UploadResponse.crs/original_crs`。

---

## 5. 验证产物留档

出问题时这样留底，便于回溯：

```bash
# 1. 抓 SSE 全量流
curl -N -X POST 'http://localhost:8000/api/chat' \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"diag-001","message":"<复制原始 prompt>"}' > diag-001-sse.log

# 2. L2 直调工具对比
python -c "...见 §1 L2..." > diag-001-tools.log

# 3. 差异表（人工）
# 把以上两份 log 放在同一目录，问题答案 vs L2 raw 对齐
```

后端开 `LOG_LEVEL=DEBUG` 把整个 trace 写盘：
```bash
# .env
LOG_LEVEL=DEBUG
```

---

## 6. 多指令 DAG 手动回归

上传一个包含面要素的 GeoJSON 或 Shapefile ZIP，发送：

> 修复上传图层几何，重投影到 EPSG:4548，做 500 米缓冲，融合后导出 GeoJSON

前端执行时间线应先收到一个 `run.plan`，其中包含以下有向链：

```text
data_io_read → fix_geometries → reproject_layer
             → buffer → dissolve_layer → export_result
```

检查点：

- 用户的五个动作分别出现在 `instructions`，读取文件只作为第一个动作的内部前置 Task
- 每个 Task 只显示一个 `tool_name`；不应出现一个 geometer Task 内连续调用五个工具
- 时间线按依赖顺序由 pending → running → success；无依赖分支才允许并行
- `tool.call.start/complete` 的 `task_id` 与 `run.plan.tasks[].id` 对齐
- 任一步返回 `empty/error` 时，后续依赖步骤不执行；重试只发生在当前 Task
- 成功时最终结果包含 `export_result` 及导出路径，文件位于 `APP_WORKSPACE_DIR` 白名单内
- 缺少输出路径等用户必填参数时出现“等待你的回答”，提交后 `/api/chat/{session_id}/resume` 返回 `resumed`

自动回归对应命令：

```bash
cd backend
python -m pytest tests/unit/test_state_schemas.py \
  tests/unit/test_native_tool_mode.py \
  tests/unit/test_runtime_contract_regressions.py \
  tests/integration/test_dependency_artifact_e2e.py -q

cd ../frontend
npm run test:e2e:awaiting
```

## 7. 阅读路径

- 工具注册（TOOL_SPECS）：`backend/app/agents/registry.py` `TOOL_SPECS`
- 工具运行时 handler：`backend/app/agents/tool_execution.py` `_TOOL_REGISTRY`
- Planner Prompt：`backend/app/agents/planner_factory.py` `PLANNER_SYSTEM_PROMPT`
- Root Dispatcher：`backend/app/agents/dispatcher.py`
- Sub-Agent 编译：`backend/app/agents/build_sub_agent.py`
- Per-Role Prompts：`backend/app/agents/prompts/{role}.md`
- 数据模型 ToolResult：`backend/app/models/schemas.py:39` 起
- 测试策略总览：`docs/04_testing_strategy.md`
- 设计文档：`docs/GIS_Agent_技术文档.md`
