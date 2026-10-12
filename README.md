# Gismind

Gismind 是一个单机、单用户的 **Agent-driven GIS Runtime**。LLM 只负责把自然语言编译成受约束的 `PlannerDraft`；确定性的 Compiler、Worker、Artifact Store 和 Event Log 负责执行、恢复与验证。

## 当前主链

```text
自然语言
  -> FastAPI /api/runs
  -> OpenAICompatiblePlanner
  -> PlannerDraft -> Compiler/PlanIR
  -> SQLite durable queue -> Worker/独立 GIS 子进程
  -> typed Artifacts + SQLite Event Log/CAS
  -> /api/runs/{id}/events SSE -> React
```

运行事实只存在于 Runtime SQLite 和本地 CAS：Session、Message、Run、Plan、Task、TaskAttempt、Artifact、Event 以及上传记录均使用 `artifact_id` 和具名端口传递。SSE 是可重放的显示投影，不是事实存储。旧 `/api/chat` 与 `/api/upload` 已明确返回 `410 LEGACY_RUNTIME_REMOVED`；`docs/v1/` 只作为历史归档。

## 能力边界

- 59 个公开 Runtime ToolContract，包含 Vector、Raster、POI、Coordinate、Table、MapLayer、File 和 Code 操作；
- Compiler 在执行前检查输入输出类型、端口基数、CRS、参数 schema、DAG、资源预算和 Code Mode 安全边界；
- Vector/Raster/Code 高风险任务在可终止子进程中执行，SQLite 租约支持 at-least-once 恢复；
- Artifact 使用不可变 `art_*` 身份与 SHA-256 CAS Blob 分离，支持血缘、缓存、验证和 GC；
- Event Log 与状态变更同事务提交，SSE 支持 `Last-Event-ID` 重放、追尾和断线恢复；
- 前端提供运行时间线、awaiting-input 恢复、地图图层和 Artifact 下载。
- 取消结果不确定时，幂等且无副作用 Task 可在 resume 事务内回到 `ready`；副作用/非幂等 Task 保持 `awaiting_input` 并要求显式 `task_decisions.<task_id>="retry"`，不会自动排队。

## 本地启动

### 后端

需要 Python 3.12+、Node.js 20+、LLM OpenAI-compatible API key，以及地理编码/POI 所需的高德 key。Runtime 不需要 Redis、Celery 或 LangGraph。

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# 编辑 .env：LLM_API_KEY、LLM_BASE_URL、LLM_MODEL、AMAP_KEY、AMAP_JS_KEY、AMAP_JS_SECURITY_CODE
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

### 前端

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

浏览器打开 <http://127.0.0.1:5173>。Runtime 数据默认位于 `.gismind/`，包括 `runtime.db`、`artifacts/`、`staging/`、`quarantine/` 和 `uploads/`。

## 验证

```powershell
cd backend
python -m pytest tests -q
python -m app.runtime.gate --phase post-delete

cd ..\frontend
npm run typecheck
npm run build
```

真实模型测试是显式 opt-in，不用 mock 冒充真实成功：

```powershell
$env:GISMIND_LIVE_LLM='1'
cd backend
python -m pytest tests/live/test_runtime_live_llm_actions.py -q
```

该套件使用 200 条不泄露 operation 名称的自然语言意图，分别统计首次编译率与至多一次 repair 后的最终编译率；没有 provider 凭据时测试会跳过，不能把跳过写成通过。

## 文档与删除记录

- [GIS Runtime 技术架构与实施规格](docs/GIS_Runtime_技术架构与实施规格.md)
- [Runtime 旧代码删除清单](docs/runtime_removal_manifest.json)
- [v1 历史归档](docs/v1/)

主文档区分当前实现、迁移中间态和目标规格；任何 Runtime 行为、API、schema、ToolContract 或已验证部署事实发生变化，都应先更新主文档。旧 Dispatcher/Sub-Agent、Redis/LangGraph 主链、数字结果引用、旧 SSE/chat 测试证据和黑盒目录不再是当前实现的一部分。

## 设计范围

这是单机、单用户 Runtime，不承诺多租户认证、分布式队列、Kafka/Celery、高可用或公网部署。外部 LLM、地理编码和 POI 服务仍可能超时或返回空结果；这些失败会作为持久化 Task/Run 事实处理，而不会让 API 进程承担 GIS 计算。
