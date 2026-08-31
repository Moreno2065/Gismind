# Gismind 单机运行状态

更新日期：2026-08-31

## 结论

Gismind 当前已经适合个人在一台 Windows 机器上开发和使用。核心聊天、上传、GIS 工具、SSE、执行轨迹、地图、会话与恢复链路能够跑通。

这里的“可用”特指：

- 单机、单进程、单用户；
- React 开发服务器通过 Vite 代理连接本地 FastAPI；
- 本机 Redis 保存会话、上传索引和 pending 数据；
- 本机 SQLite 保存 LangGraph checkpoint；
- LLM、高德和 OSM 使用真实外部服务；
- 外部服务偶发空结果或超时允许重试，不作为生产 SLA。

它不表示公网生产、多用户并发或无人值守高可用已经完成。

## 已贯通的主路径

每个聊天请求实际经过：

```text
React ChatPanel
  -> Vite /api proxy
  -> POST /api/upload（有文件时）
  -> POST /api/chat SSE
  -> FastAPI
  -> run_react_loop
  -> Root Dispatcher DAG
  -> Sub-Agent / Tool
  -> SSE
  -> React 状态、TraceTimeline、回答与地图
```

Dispatcher 保持为唯一编排骨架，继续承担 DAG、依赖、子 Agent、checkpoint 和汇总职责。

## 规划来源

运行 trace 中的 `planner_source` 必须按真实来源解释：

| 值 | 含义 |
|---|---|
| `root_llm` | Root Planner LLM 生成并通过服务端校验的 DAG |
| `guardrail` | 明确、安全且参数强约束的确定性计划 |
| `fallback` | Root Planner 失败后启用的受限兼容计划 |

关键词或兼容目录命中不能写成“Root LLM 规划通过”。

## 自动化覆盖

### 真实前后端耦合

Playwright 使用真实 Chromium，通过 Vite 连接隔离 FastAPI 测试服务；HTTP、SSE 和前端请求不做浏览器拦截或伪造。LLM 可替换为确定性实现，以消除模型随机性。

当前覆盖：

- 文本 POI：`status -> run.plan -> tool -> map -> token -> done`；
- 浏览器上传 GeoJSON，验证同一 `file_id` 进入聊天和 `data_io_read`；
- 两文件顺序、依赖产物与 overlay；
- `awaiting_input -> resume` 的横幅、payload 与收敛；
- 停止后旧 token 不再进入 UI；
- 会话切换、旧 SSE 隔离与刷新恢复；
- 空地图、过期上传、上传拒绝和后端错误。

2026-08-31 在最终代码上重新执行的确定性 Chromium 套件结果：32 条通过（单 worker，1.8 分钟）。JUnit 证据为 `blackbox-results/CHROMIUM_32_FINAL_20260831.xml`；完整用例与事件契约见 `CHROMIUM_32_RESULT_2026-08-29.md`。该结果证明真实浏览器前后端耦合，不等同于 Root LLM 语义能力证明。

### GIS 黄金语义

后端测试不只检查 HTTP 或“地图非空”，还覆盖：

- WGS84/GCJ02 黄金坐标与往返误差；
- 500 米缓冲面积接近 `pi * r^2`；
- overlay 交集/并集面积与拓扑；
- 属性筛选、字段保留和字段计算；
- 坡度、重分类、分区统计与 nodata；
- GeoJSON/GPKG 导出与重新读取。

2026-08-31 在最终代码上重新执行后端完整测试：1519 个测试项中 1517 条通过、2 条跳过、0 条失败，78 条现有依赖/弃用警告。JUnit 证据为 `blackbox-results/BACKEND_FULL_FINAL_20260831.xml`。复跑命令为：

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest tests\ -q
```

### Root Planner 真实冒烟

真实 LLM 套件通过公开 API 调用同一运行路径，并记录请求 payload、SSE 序列、`planner_source`、计划、工具终态、最终答案和地图要素数。

2026-08-31 在当前代码上执行了 RP01–RP07 全量受控冒烟，并在修复导出 CRS 后单独重跑 RP07：

| 指标 | 结果 |
|---|---:|
| 用例数 | 7 |
| 通过 | 7 |
| 失败 | 0 |
| `root_llm` | 6 |
| `guardrail` | 1 |
| `fallback` | 0 |

| 用例 | 来源 | 结果正确性证据 |
|---|---|---|
| RP01 POI | `root_llm` | 工具/地图 8/8，300 米查询最远 272.679 米 |
| RP02 坐标转换 | `guardrail` | 输出落在黄金坐标 30 米误差内 |
| RP03 单文件属性 | `root_llm` | 精确选择 2 条，名称为站点 C、站点 D |
| RP04 双文件 overlay | `root_llm` | 1 个交集要素，面积 9,411,658.922 平方米 |
| RP05 栅格 | `root_llm` | 1000 个有效像元分级计数为 250/250/500 |
| RP06 多轮 | `root_llm` | 上轮工具/地图 8/8，本轮 2/2；最远 472.369/501.273 米，比较方向正确 |
| RP07 buffer/export | `root_llm` | 4 个缓冲要素，WGS84 导出重读后的面积增长 3,435,521.113 平方米，导出重读 4 条；GCJ02 明确转为 WGS84 |

证据文件：

- `blackbox-results/ROOT_PLANNER_LIVE_ALL_20260831.json`：RP01–RP07 全量 7/7；
- `blackbox-results/ROOT_PLANNER_LIVE_RP07_EXPORT_CRS_FINAL_20260831.json`：导出修复后使用新增 CRS 黄金断言再次通过；
- `blackbox-results/ROOT_PLANNER_LIVE_RP01_RP06_20260830_CURRENT.json` 与后续 RP06 报告：保留外部服务故障和“源故障误判为空”的历史失败证据。

上述 `blackbox-results` 文件是本机复跑生成的机器报告，按仓库约定不提交到 Git；执行本节命令即可重新生成同类证据文件。

历史失败推动了生产语义修复：POI 或地理编码的所有数据源均不可用时，现在分别返回 `POI_SOURCE_UNAVAILABLE`、`GEOCODE_SOURCE_UNAVAILABLE`，不再伪装为“合法零结果”；至少一个数据源给出权威空响应时才允许 `empty`。RP07 又暴露了 GCJ02 FeatureCollection 导出时丢失 CRS 的问题；`export_result` 现在会把 GCJ02 数学反偏转为标准 WGS84、返回源/目标 CRS 和转换标记，未知 CRS 则拒绝导出。外部服务延迟、限流和瞬时不可用仍是单机运行风险。

## 日常复跑

```powershell
# 后端
cd backend
python -m pytest tests/ -q

# 前端
cd ..\frontend
npm run typecheck
npm run build
npx playwright install chromium
.\scripts\run-e2e-local.ps1 `
  -BackendEnvPath C:\path\to\Gismind\backend\.env `
  -FrontendEnvPath C:\path\to\Gismind\frontend\.env.local
```

真实服务冒烟：

```powershell
# 先在另一个终端启动 backend :8000，再从仓库根目录执行
cd C:\path\to\Gismind
.\backend\.venv\Scripts\python.exe blackbox/root_planner_synonym_suite.py `
  --base-url http://127.0.0.1:8000 `
  --cases all `
  --output blackbox-results/root-planner-latest.json
```

测试结束后确认 18000、15173 等隔离测试端口没有残留监听。`run-e2e-local.ps1` 持有它启动的 FastAPI/Vite 精确 PID，并在 `finally` 中停止；Playwright 仅负责浏览器执行，避免 Windows 连续运行时 webServer 进程树清理竞态。

## 单机使用时仍可能遇到的问题

- LLM 偶尔输出不合法计划：系统会重试或进入受限 fallback；
- 高德/OSM 偶尔超时或不可用：现在会与合法空结果区分并显示错误；重试同一请求通常可以恢复；
- 高德 JS key 未配置时，回答和 trace 仍可能成功，但地图会显示加载失败；
- 大栅格和复杂 overlay 会占用较多内存，停止操作对正在执行的同步工具不是强制抢占；
- Windows 上过期上传目录遇到短暂文件锁时会有限重试，仍被占用则延后清理；Redis 索引会按 TTL 失效，但磁盘残留仍需观察；
- ECharts 生产构建块超过 500 kB，目前只影响首屏体积，不影响正确性；
- workspace 和导出文件需要用户偶尔自行清理。

这些问题不阻止个人本地使用，但应保留在测试报告和问题记录中。

## 当前非目标

- 公网部署与正式身份认证；
- 多租户数据隔离；
- 多 worker 一致性；
- 大规模并发、负载与长时间 soak test；
- 生产 SLA、告警和自动扩缩容。
