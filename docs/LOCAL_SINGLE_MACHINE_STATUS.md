# Gismind 单机运行状态

更新日期：2026-08-25

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

2026-08-25 的确定性 Chromium 套件结果：7 条通过。

### GIS 黄金语义

后端测试不只检查 HTTP 或“地图非空”，还覆盖：

- WGS84/GCJ02 黄金坐标与往返误差；
- 500 米缓冲面积接近 `pi * r^2`；
- overlay 交集/并集面积与拓扑；
- 属性筛选、字段保留和字段计算；
- 坡度、重分类、分区统计与 nodata；
- GeoJSON/GPKG 导出与重新读取。

2026-08-25 重新执行完整单元测试：1247 条通过、1 条跳过。复跑命令为：

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest tests\unit -q
```

### Root Planner 真实冒烟

真实 LLM 套件通过公开 API 调用同一运行路径，并记录请求 payload、SSE 序列、`planner_source`、计划、工具终态、最终答案和地图要素数。

最近一次整套结果：

| 指标 | 结果 |
|---|---:|
| 用例数 | 7 |
| 通过 | 6 |
| 失败 | 1 |
| `root_llm` | 4 |
| `guardrail` | 3 |
| `fallback` | 0 |

失败项是多轮 POI 查询返回 empty。它反映真实外部服务波动，不应通过降低断言或改写报告来隐藏。

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
npm run test:e2e
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

测试结束后确认 18000、15173 等隔离测试端口没有残留监听。Playwright 正常退出时会自动停止它启动的服务。

## 单机使用时仍可能遇到的问题

- LLM 偶尔输出不合法计划：系统会重试或进入受限 fallback；
- 高德/OSM 偶尔为空或超时：重试同一请求通常可以恢复；
- 高德 JS key 未配置时，回答和 trace 仍可能成功，但地图会显示加载失败；
- 大栅格和复杂 overlay 会占用较多内存，停止操作对正在执行的同步工具不是强制抢占；
- workspace 和导出文件需要用户偶尔自行清理。

这些问题不阻止个人本地使用，但应保留在测试报告和问题记录中。

## 当前非目标

- 公网部署与正式身份认证；
- 多租户数据隔离；
- 多 worker 一致性；
- 大规模并发、负载与长时间 soak test；
- 生产 SLA、告警和自动扩缩容。
