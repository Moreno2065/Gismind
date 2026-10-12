# 环境变量与配置

> 所有配置项的统一定义。配置分层、密钥管理、启动校验。
> 密钥安全见 [06_security.md](06_security.md)，可观测性相关配置见 [07_observability.md](07_observability.md)。

---

## 1. 配置分层

优先级从高到低：

```
环境变量（OS 级）  >  .env 文件  >  代码默认值
```

- **环境变量**：生产环境通过 Docker / systemd 注入，不落盘
- **.env 文件**：本地开发用，已在 `.gitignore` 中排除
- **代码默认值**：`Settings` 类的字段默认值，仅用于非敏感项（如超时、上限）

敏感项（API Key、密钥）**不设代码默认值**，缺失时启动报错。

---

## 2. 完整 .env 清单

### 2.1 LLM 相关

| 变量 | 用途 | 默认 | 必填 | 示例 |
|------|------|------|------|------|
| `LLM_API_KEY` | LLM 服务密钥 | - | 是 | `sk-xxxxx` |
| `LLM_BASE_URL` | OpenAI 兼容端点 | - | 是 | `https://api.example.com/v1` |
| `LLM_MODEL` | 模型名 | - | 是 | `k2.7-code` |
| `LLM_TIMEOUT` | 请求超时秒 | 60 | 否 | `60` |
| `LLM_MAX_TOKENS` | 单次响应上限 | 4096 | 否 | `4096` |
| `LLM_TEMPERATURE` | 采样温度 | 0.3 | 否 | `0.3` |

### 2.2 高德 API

| 变量 | 用途 | 默认 | 必填 | 示例 |
|------|------|------|------|------|
| `AMAP_KEY` | 服务端 key（POI/地理编码/路径规划） | - | 是 | `a1b2c3...` |
| `AMAP_JS_KEY` | 前端 JS API key（地图渲染） | - | 是 | `js_key_xxx` |
| `AMAP_JS_SECURITY_CODE` | JS API 安全密钥 | - | 是 | `sec_code_xxx` |
| `AMAP_QPS_LIMIT` | QPS 限制 | 5 | 否 | `5` |
| `AMAP_TIMEOUT` | 请求超时秒 | 3 | 否 | `3` |
| `AMAP_MAX_RETRY` | 失败重试次数 | 3 | 否 | `3` |

> **注意**：`AMAP_KEY`（服务端）和 `AMAP_JS_KEY`（前端）是两个不同的 key。服务端 key 不可暴露给前端，前端 JS key 需在高德控制台设置域名白名单和配额限制（→ 详见 [06_security.md](06_security.md) §API Key 保护）。

### 2.3 OSM

| 变量 | 用途 | 默认 | 必填 | 示例 |
|------|------|------|------|------|
| `OSM_ENDPOINT` | Overpass 主端点 | `https://overpass-api.de/api/interpreter` | 否 | - |
| `OSM_BACKUP_ENDPOINTS` | 备用端点（逗号分隔） | `https://overpass.kumi.systems/api/interpreter` | 否 | - |
| `OSM_TIMEOUT` | 硬超时秒 | 3 | 否 | `3` |
| `OSM_MAX_RETRY` | 重试次数 | 1 | 否 | `1` |

`OSM_BACKUP_ENDPOINTS` 是逗号分隔的 Overpass 端点列表。运行时先请求 `OSM_ENDPOINT`；仅当该端点超时、连接失败、HTTP 失败或返回非法 JSON 时，才顺序尝试备用端点。主端点返回合法空结果时，不会访问备用端点，避免把“没有数据”误判为服务故障或无谓增加外部请求。

### 2.4 Redis

| 变量 | 用途 | 默认 | 必填 | 示例 |
|------|------|------|------|------|
| `REDIS_URL` | Redis 连接字符串 | `redis://localhost:6379/0` | 是 | `redis://:pass@host:6379/0` |
| `CACHE_TTL_POI` | 高德 POI 缓存 TTL 秒 | 86400（24h） | 否 | `86400` |
| `CACHE_TTL_OSM` | OSM 缓存 TTL 秒 | 172800（48h） | 否 | `172800` |
| `CACHE_TTL_GEOCODE` | 地理编码缓存 TTL 秒 | 604800（7d） | 否 | `604800` |

### 2.5 Celery ⚠️ 已废弃

> **Celery 相关配置已废弃，后续版本将移除。** 项目已迁移至纯内存内 Agent 循环架构，不再依赖 Celery 分布式任务队列。

| 变量 | 用途 | 默认 | 必填 |
|------|------|------|------|
| `CELERY_BROKER_URL` | 消息队列 | 同 `REDIS_URL` | 否 |
| `CELERY_RESULT_BACKEND` | 结果存储 | 同 `REDIS_URL` | 否 |
| `CELERY_TASK_TIME_LIMIT` | 单任务硬超时秒 | 600 | 否 |
| `CELERY_WORKER_CONCURRENCY` | 并发数 | 2 | 否 |

### 2.6 前端（Vite 环境变量，需 `VITE_` 前缀）

| 变量 | 用途 | 默认 | 必填 |
|------|------|------|------|
| `VITE_AMAP_KEY` | 高德 JS API key（= 后端 `AMAP_JS_KEY`） | - | 是 |
| `VITE_AMAP_SECURITY_CODE` | JS API 安全密钥（= `AMAP_JS_SECURITY_CODE`） | - | 是 |
| `VITE_API_BASE_URL` | 后端 API 地址 | `/api` | 否 |
| `VITE_SENTRY_DSN` | 前端错误上报 DSN（可选） | - | 否 |

### 2.7 应用配置

| 变量 | 用途 | 默认 | 必填 |
|------|------|------|------|
| `APP_ENV` | 环境 | `dev` | 否 |
| `APP_DEBUG` | 调试模式 | `false` | 否 |
| `APP_HOST` | 监听地址 | `0.0.0.0` | 否 |
| `APP_PORT` | 监听端口 | `8000` | 否 |
| `APP_MAX_ITERATIONS` | coder/旧循环兼容上限 | 10 | 否 |
| `APP_ROOT_MAX_ITERATIONS` | Root DAG 最多调度的 Task 数 | 30 | 否 |
| `APP_CONTEXT_WINDOW` | ToolMessage 截断阈值字符 | 5000 | 否 |
| `APP_LOG_LEVEL` | 日志级别 | `INFO` | 否 |
| `APP_LOG_FORMAT` | 日志格式 | `console`（dev 默认；prod 须显式设 `json`） | 否 |
| `APP_CHECKPOINT_DB` | SqliteSaver 数据库路径 | `.gismind/checkpoints.db` | 否 |
| `APP_MAX_COST_TOKENS` | 单次对话 Token 成本上限 | 100000 | 否 |
| `APP_MAX_LLM_RETRIES` | LLM 调用失败最大重试次数 | 3 | 否 |
| `APP_INLINE_TIMEOUT_S` | 兼容配置占位；inline 路径已废弃 | 30 | 否 |
| `APP_WORKSPACE_DIR` | Agent 工作区及导出路径白名单根目录 | `./workspace` | 否 |

普通角色固定使用 JSON Schema 工具调用，`coder` 固定使用 sandbox Code Mode，当前没有 `APP_CODE_MODE_ENABLED` 运行时分流开关。各角色的局部重试上限来自 `app/agents/registry.py` 的 `SubAgentSpec.max_iterations`。

### 2.8 Ensemble 配置 ⚠️ 已移除

> 当前实现不再使用投票冗余 Ensemble，以下变量仅用于识别旧 `.env`，`Settings` 不读取它们：

| 变量 | 用途 | 默认 | 必填 |
|------|------|------|------|
| `APP_ENSEMBLE_BASELINE_N` | Ensemble 基础冗余度（同任务最少并行数） | 2 | 否 |
| `APP_ENSEMBLE_MAX_N` | Ensemble 最大冗余度 | 3 | 否 |
| `APP_ENSEMBLE_FALLBACK` | 共识投票全否决时的兜底策略 | `accept_most_recent` | 否 |

### 2.9 代码沙箱配置

| 变量 | 用途 | 默认 | 必填 |
|------|------|------|------|
| `APP_SANDBOX_ENABLED` | 是否启用代码解释器沙箱 | `true` | 否 |
| `APP_SANDBOX_TIMEOUT_S` | 沙箱单次执行硬超时秒 | 60 | 否 |
| `APP_SANDBOX_MEMORY_MB` | 沙箱子进程内存上限 MB | 512 | 否 |
| `APP_SANDBOX_NETWORK_ALLOWLIST` | 沙箱网络白名单（逗号分隔 host:port），空=全部 deny | `` | 否 |

### 2.10 文件上传

| 变量 | 用途 | 默认 | 必填 |
|------|------|------|------|
| `UPLOAD_MAX_SIZE` | 单文件大小上限 MB | 50 | 否 |
| `UPLOAD_ZIP_MAX_TOTAL_SIZE` | ZIP 解压总大小上限 MB | 500 | 否 |
| `UPLOAD_ZIP_MAX_FILE_COUNT` | ZIP 内文件数上限 | 100 | 否 |
| `UPLOAD_TTL_S` | 本机上传 payload 与 Redis 索引的存活秒数 | 86400 | 否 |

上传原始文件保存在 `APP_WORKSPACE_DIR/uploads/{file_id}/`，Redis `upload:{file_id}` 仅保存带 TTL 的 `storage_path` 等元数据。单机环境应把 `APP_WORKSPACE_DIR` 置于持久磁盘，并确保进程对该目录有读写权限。

### 2.11 CORS

| 变量 | 用途 | 默认 | 必填 |
|------|------|------|------|
| `CORS_ORIGINS` | 允许的 origin（逗号分隔） | `http://localhost:5173` | 否 |

---

## 3. dev / prod 配置差异

| 配置项 | dev | prod |
|--------|-----|------|
| `APP_DEBUG` | true | false |
| `APP_LOG_FORMAT` | console（人类可读） | json（结构化） |
| `APP_LOG_LEVEL` | DEBUG | INFO |
| `CORS_ORIGINS` | `http://localhost:5173` | 实际域名 |
| `LLM_TEMPERATURE` | 0.5（便于调试观察） | 0.3（稳定输出） |
| Redis | 本地单实例 | 独立实例 + 密码 |
| Celery | 可选（同步执行） | 已废弃（不再使用 Celery） |

---

## 4. 密钥管理

### 4.1 本地开发

- 所有密钥写在 `.env` 文件
- `.env` 必须在 `.gitignore` 中（项目提供 `.env.example` 模板，不含真实值）
- 前端密钥写在 `frontend/.env.local`（Vite 自动加载）

### 4.2 生产环境

- 通过 Docker Compose 的 `environment` 或 `env_file` 注入
- **禁止**将 `.env` 文件打包进镜像
- **禁止**将密钥硬编码在代码或 docker-compose.yml 中提交到 git
- 敏感变量（`LLM_API_KEY`、`AMAP_KEY`、`REDIS_URL` 含密码时）启动时校验非空

### 4.3 前端可见密钥的处理

`VITE_AMAP_KEY` 和 `VITE_AMAP_SECURITY_CODE` 会被打包进前端 JS，任何人可见。缓解措施：

1. 在高德控制台为 JS key 设置**域名白名单**（仅允许生产域名调用）
2. 设置**每日配额限制**（防止被盗刷）
3. 服务端 key（`AMAP_KEY`）与前端 JS key **分离**，服务端 key 永不暴露给前端

---

## 5. Pydantic Settings 配置类

```python
# backend/app/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # LLM
    LLM_API_KEY: str
    LLM_BASE_URL: str
    LLM_MODEL: str
    LLM_TIMEOUT: int = 60
    LLM_MAX_TOKENS: int = 4096
    LLM_TEMPERATURE: float = 0.3

    # 高德
    AMAP_KEY: str
    AMAP_JS_KEY: str
    AMAP_JS_SECURITY_CODE: str
    AMAP_QPS_LIMIT: int = 5
    AMAP_TIMEOUT: int = 3
    AMAP_MAX_RETRY: int = 3

    # OSM
    OSM_ENDPOINT: str = "https://overpass-api.de/api/interpreter"
    OSM_BACKUP_ENDPOINTS: str = "https://overpass.kumi.systems/api/interpreter"
    OSM_TIMEOUT: int = 3
    OSM_MAX_RETRY: int = 1

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    CACHE_TTL_POI: int = 86400
    CACHE_TTL_OSM: int = 172800
    CACHE_TTL_GEOCODE: int = 604800  # 7d

    # Celery（已废弃）
    CELERY_BROKER_URL: str = ""
    CELERY_RESULT_BACKEND: str = ""

    # 应用
    APP_ENV: str = "dev"
    APP_DEBUG: bool = False
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    APP_MAX_ITERATIONS: int = 10
    APP_ROOT_MAX_ITERATIONS: int = 30
    APP_CONTEXT_WINDOW: int = 5000
    APP_LOG_LEVEL: str = "INFO"
    APP_LOG_FORMAT: str = "console"
    APP_CHECKPOINT_DB: str = ".gismind/checkpoints.db"
    APP_MAX_COST_TOKENS: int = 100000
    APP_MAX_LLM_RETRIES: int = 3
    APP_INLINE_TIMEOUT_S: int = 30  # 兼容占位，inline 已废弃
    APP_WORKSPACE_DIR: str = "./workspace"

    # Sandbox
    APP_SANDBOX_ENABLED: bool = True
    APP_SANDBOX_TIMEOUT_S: int = 60
    APP_SANDBOX_MEMORY_MB: int = 512
    APP_SANDBOX_NETWORK_ALLOWLIST: str = ""

    # 上传
    UPLOAD_MAX_SIZE: int = 50
    UPLOAD_ZIP_MAX_TOTAL_SIZE: int = 500
    UPLOAD_ZIP_MAX_FILE_COUNT: int = 100
    UPLOAD_TTL_S: int = 86400

    # CORS
    CORS_ORIGINS: str = "http://localhost:5173"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",")]

    @property
    def celery_broker_url_resolved(self) -> str:
        return self.CELERY_BROKER_URL or self.REDIS_URL

@lru_cache
def get_settings() -> Settings:
    return Settings()

settings = get_settings()
```

---

## 6. 启动校验

应用启动时执行必填项检查，缺失时 fail-fast：

```python
# backend/app/main.py
from app.config import settings

REQUIRED_KEYS = [
    "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
    "AMAP_KEY", "AMAP_JS_KEY", "AMAP_JS_SECURITY_CODE",
    "REDIS_URL",
]

def validate_config():
    missing = []
    for key in REQUIRED_KEYS:
        value = getattr(settings, key, None)
        if not value or (isinstance(value, str) and not value.strip()):
            missing.append(key)
    if missing:
        raise SystemExit(
            f"启动失败：以下必填配置缺失：{missing}。"
            f"请检查 .env 文件或环境变量。参考 .env.example。"
        )

app.add_event_handler("startup", validate_config)
```

---

## 7. .env.example 模板

项目根目录提供 `.env.example`，包含所有变量名和注释，不含真实值：

```bash
# === LLM ===
LLM_API_KEY=
LLM_BASE_URL=
LLM_MODEL=k2.7-code

# === 高德 API ===
AMAP_KEY=
AMAP_JS_KEY=
AMAP_JS_SECURITY_CODE=
AMAP_QPS_LIMIT=5
AMAP_TIMEOUT=3

# === OSM ===
OSM_ENDPOINT=https://overpass-api.de/api/interpreter
OSM_BACKUP_ENDPOINTS=https://overpass.kumi.systems/api/interpreter
OSM_TIMEOUT=3
OSM_MAX_RETRY=1

# === Redis ===
REDIS_URL=redis://localhost:6379/0

# === 应用 ===
APP_ENV=dev
APP_DEBUG=true
APP_MAX_ITERATIONS=10
APP_INLINE_TIMEOUT_S=30
APP_WORKSPACE_DIR=./workspace

# === 上传 ===
UPLOAD_MAX_SIZE=50
UPLOAD_ZIP_MAX_TOTAL_SIZE=500
UPLOAD_ZIP_MAX_FILE_COUNT=100
UPLOAD_TTL_S=86400

# === CORS ===
CORS_ORIGINS=http://localhost:5173
```

前端 `frontend/.env.example`：

```bash
VITE_AMAP_KEY=
VITE_AMAP_SECURITY_CODE=
VITE_API_BASE_URL=/api
```

---

*文档版本：v1.3 | 最后更新：2026-08-09 | 属于 Gismind 补充文档*

*v1.3 变更：移除实现中不存在的 APP_CODE_MODE_ENABLED / APP_SUB_AGENT_MAX_ITERATIONS；明确普通角色 Schema 模式、coder sandbox 模式和 registry.max_iterations；修正 APP_INLINE_TIMEOUT_S / APP_WORKSPACE_DIR 默认值。*
*v1.1 变更：新增 §2.8 Sub-Agent/Ensemble 配置、§2.9 沙箱配置；Settings 类新增 APP_SUB_AGENT_MAX_ITERATIONS / APP_ROOT_MAX_ITERATIONS / APP_CHECKPOINT_DB / APP_SANDBOX_* / APP_ENSEMBLE_* 等字段*
