# Gismind

Gismind 是一个面向单机使用的空间智能 GIS Agent。用户可以用自然语言完成 POI 查询、坐标转换、文件读取、矢量与栅格分析，并在 React 对话界面中查看执行轨迹和地图结果。

> 当前定位：个人本地开发与自用项目。核心链路已经能够重复跑通，但不以公网部署、多用户隔离或生产级高可用为目标。

## 核心链路

```text
React ChatPanel
  -> Vite /api proxy
  -> FastAPI /api/upload + /api/chat SSE
  -> Root Dispatcher TaskPlan DAG
  -> Sub-Agent / GIS Tool
  -> SSE status / run.plan / tool / map / token / done
  -> 前端回答、TraceTimeline 与高德地图
```

Dispatcher 是唯一的多步骤编排骨架，负责 DAG 校验、依赖传递、子 Agent 调度、checkpoint 与结果汇总。规划来源会记录为：

- `root_llm`：Root Planner 生成计划；
- `guardrail`：少量明确且参数强约束的确定性计划；
- `fallback`：Root Planner 失败后使用受限兼容计划。

## 已有能力

- 自然语言 POI 查询，高德优先、OSM 兜底；
- WGS84、GCJ02、BD09 坐标转换；
- GeoJSON、Shapefile ZIP、KML、GeoTIFF 等文件读取；
- 缓冲、叠加、裁剪、融合、空间连接、属性筛选等矢量分析；
- 坡度、坡向、阴影、重分类、分区统计等栅格分析；
- React SSE 流式回答、执行 trace、地图与图表；
- Redis 会话/上传索引、SQLite LangGraph checkpoint；
- `awaiting_input -> resume`、停止运行、会话切换与刷新恢复；
- Code Mode AST 检查与子进程 sandbox。

完整架构见 [GIS Agent 技术文档](docs/GIS_Agent_技术文档.md)，当前可用性和验证边界见 [单机状态说明](docs/LOCAL_SINGLE_MACHINE_STATUS.md)。

## 本地启动

### 1. 准备环境

- Python 3.12+
- Node.js 20+
- Redis 7+
- LLM OpenAI-compatible API key
- 高德 Web 服务 key 与 JS API key

如果本机没有 Redis，可以用 Docker 启动：

```powershell
docker run --name gismind-redis -p 6379:6379 -d redis:7-alpine
```

### 2. 启动后端

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

编辑 `backend/.env`，至少填写：

```dotenv
LLM_API_KEY=
LLM_BASE_URL=
LLM_MODEL=
AMAP_KEY=
AMAP_JS_KEY=
AMAP_JS_SECURITY_CODE=
REDIS_URL=redis://localhost:6379/0
```

启动 FastAPI：

```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

### 3. 启动前端

新开一个 PowerShell：

```powershell
cd frontend
npm ci
@"
VITE_AMAP_KEY=你的高德_JS_Key
VITE_AMAP_SECURITY_CODE=你的安全密钥
"@ | Set-Content .env.local
$env:GISMIND_VITE_API_TARGET='http://127.0.0.1:8000'
npm run dev -- --host 127.0.0.1
```

浏览器打开 <http://127.0.0.1:5173>。上面的环境变量会让 Vite 把 `/api` 明确代理到 IPv4 地址 `http://127.0.0.1:8000`，避免部分 Windows 环境把 `localhost` 优先解析为 IPv6。

## 验证

后端测试：

```powershell
cd backend
python -m pytest tests/ -q
```

前端类型检查与构建：

```powershell
cd frontend
npm run typecheck
npm run build
```

真实 Chromium、Vite、FastAPI、Redis 与 SQLite 的确定性端到端测试：

```powershell
cd frontend
npx playwright install chromium
npm run test:e2e
```

Root Planner 真实服务冒烟需要先启动后端，并会调用真实 LLM 和外部服务：

```powershell
cd C:\path\to\Gismind
.\backend\.venv\Scripts\python.exe blackbox/root_planner_synonym_suite.py `
  --base-url http://127.0.0.1:8000 `
  --cases all `
  --output blackbox-results/root-planner-latest.json
```

更详细的复测方法见 [手动回归手册](docs/MANUAL_TESTING.md)。

## 当前验证结论

2026-08-25 的本地验证覆盖了文本 POI、浏览器上传、双文件叠加、追问恢复、停止、会话切换、刷新恢复以及错误态。确定性 Chromium 套件为 7 条通过。最新完整后端单元测试命令 `python -m pytest tests/unit -q` 的结果为 1247 条通过、1 条跳过；其中包含坐标、缓冲面积、叠加拓扑、属性字段、栅格统计和导出可读性等 GIS 语义检查。

真实 LLM/外部服务会受到模型和数据源波动影响。最近一次 7 条 Root Planner 冒烟中 6 条通过，1 条 POI 查询因外部结果为空失败。因此这些冒烟用于发现风险，不应被描述成确定性回归。

## 已知限制

- POI、地理编码和 LLM 依赖外部服务，偶尔会超时、返回空结果或产生不同计划；
- 浏览器 E2E 已验证地图数据和 UI 链路，但尚未做像素级地图视觉回归；
- 项目按单进程、单机自用设计，没有生产认证、限流和多用户隔离；
- 导出结果目前写入本机 workspace，没有面向远程用户的下载服务；
- `PineFlow-main/` 是本地参考项目，已明确排除在本仓库之外。

## 文档

- [技术架构](docs/GIS_Agent_技术文档.md)
- [API 与 SSE 契约](docs/01_api_spec.md)
- [数据模型](docs/02_data_models.md)
- [配置说明](docs/03_config_env.md)
- [测试策略](docs/04_testing_strategy.md)
- [LLM Prompts](docs/05_llm_prompts.md)
- [安全边界](docs/06_security.md)
- [可观测性](docs/07_observability.md)
- [手动回归](docs/MANUAL_TESTING.md)

## 仓库说明

本仓库不提交 `.env`、API key、本地数据库、上传文件、运行 workspace、黑盒结果、虚拟环境、`node_modules` 和 PineFlow 参考源码。请从示例配置创建自己的本地环境。
