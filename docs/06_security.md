# 安全与合规

> 个人 demo 项目，无认证，聚焦服务端防护。
> 配置见 [03_config_env.md](03_config_env.md)，日志脱敏见 [07_observability.md](07_observability.md)。

---

## 1. 威胁模型

个人 demo 场景，无用户认证，威胁面聚焦：

| 威胁 | 风险 | 优先级 |
|------|------|--------|
| API Key 泄露（高德/LLM） | 被盗刷配额，产生费用 | 高 |
| 文件上传攻击（ZIP 炸弹/路径穿越） | 服务器资源耗尽 / 任意文件覆盖 | 高 |
| 代码解释器 RCE（功能 38） | 服务器被完全控制 | 高（若启用） |
| LLM Prompt 注入 | 工具链被误导，查询非预期数据 | 中 |
| 依赖漏洞 | 已知 CVE 被利用 | 中 |
| CORS 配置过宽 | 跨站请求被滥用 | 低（无认证） |

---

## 2. API Key 保护

### 2.1 高德 Key 分离

项目使用**两个独立的高德 Key**，职责分离：

| Key | 用途 | 暴露面 | 保护措施 |
|-----|------|--------|---------|
| `AMAP_KEY`（服务端） | 后端调 POI/地理编码/路径规划 API | 仅后端，不暴露 | 环境变量，不进 git |
| `AMAP_JS_KEY`（前端） | 前端高德 JS API 渲染地图 | 前端 JS 可见 | 域名白名单 + 配额限制 |

**前端 Key 的缓解措施**（无法完全隐藏，只能提高盗用成本）：

1. **域名白名单**：在高德控制台为 JS Key 设置允许调用的域名（开发 `localhost`，生产实际域名）
2. **配额限制**：设置每日调用上限，防止被盗刷
3. **安全密钥**：`AMAP_JS_SECURITY_CODE` 配合 JS API 使用，增加一层校验

```typescript
// frontend/src/index.tsx - 高德 JS API 初始化
window._AMapSecurityConfig = {
  securityJsCode: import.meta.env.VITE_AMAP_SECURITY_CODE,
};
AMapLoader.load({
  key: import.meta.env.VITE_AMAP_KEY,
  version: '2.0',
});
```

### 2.2 LLM Key

- 仅后端持有（`LLM_API_KEY`）
- 前端不直接调用 LLM，所有请求经后端 `/api/chat`
- 日志中脱敏（→ §8 日志脱敏）

### 2.3 OSM

- 无需 Key
- 但 Overpass API 有隐式配额，频繁请求会被 429。本项目通过缓存（TTL 48h）和 3 秒硬超时缓解

---

## 3. 文件上传安全

### 3.1 ZIP 炸弹防护

```python
# backend/app/tools/data_io.py
import zipfile
from app.config import settings

MAX_TOTAL_SIZE = settings.UPLOAD_ZIP_MAX_TOTAL_SIZE * 1024 * 1024  # 500MB
MAX_FILE_COUNT = settings.UPLOAD_ZIP_MAX_FILE_COUNT  # 100

def validate_zip_safety(zip_buffer: bytes) -> None:
    """校验 ZIP 包安全性，防止 ZIP 炸弹"""
    with zipfile.ZipFile(zip_buffer, 'r') as zip_ref:
        infos = zip_ref.infolist()

        # 1. 文件数量限制
        if len(infos) > MAX_FILE_COUNT:
            raise ValueError(f"ZIP 内文件数 {len(infos)} 超过上限 {MAX_FILE_COUNT}")

        # 2. 解压后总大小限制
        total_size = sum(info.file_size for info in infos)
        if total_size > MAX_TOTAL_SIZE:
            raise ValueError(
                f"ZIP 解压后总大小 {total_size / 1024 / 1024:.1f}MB "
                f"超过上限 {settings.UPLOAD_ZIP_MAX_TOTAL_SIZE}MB"
            )

        # 3. 压缩比异常检测（正常 ZIP 压缩比 < 100，炸弹 > 1000）
        compressed_size = sum(info.compress_size for info in infos)
        if compressed_size > 0:
            ratio = total_size / compressed_size
            if ratio > 200:
                raise ValueError(f"ZIP 压缩比 {ratio:.0f} 异常，疑似 ZIP 炸弹")
```

### 3.2 ZipSlip 路径穿越防护

```python
import os

def validate_zip_paths(zip_ref: zipfile.ZipFile) -> None:
    """防止 ZipSlip 攻击（解压路径包含 ../ 跳出目标目录）"""
    for info in zip_ref.infolist():
        target_path = os.path.normpath(info.filename)
        if target_path.startswith("..") or os.path.isabs(target_path):
            raise ValueError(f"可疑路径：{info.filename}")
        # 进一步校验：解压后路径必须在预期目录内
        # 本项目内存解压不落盘，但防御性校验仍保留
```

### 3.3 文件类型白名单

```python
ALLOWED_EXTENSIONS = {".zip", ".geojson", ".json", ".kml"}

def validate_file_type(filename: str) -> None:
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(
            f"不支持的文件类型 {ext}，仅支持 {', '.join(ALLOWED_EXTENSIONS)}"
        )
```

### 3.4 完整上传校验流程

```python
# backend/app/api/upload.py
from fastapi import UploadFile, HTTPException

@router.post("/api/upload")
async def upload(file: UploadFile):
    # 1. 大小校验（先于内容读取）
    if file.size and file.size > settings.UPLOAD_MAX_SIZE * 1024 * 1024:
        raise HTTPException(413, detail={
            "code": "FILE_TOO_LARGE",
            "message": f"上传文件超过 {settings.UPLOAD_MAX_SIZE}MB 限制",
        })

    # 2. 类型校验
    try:
        validate_file_type(file.filename)
    except ValueError as e:
        raise HTTPException(422, detail={"code": "UNSUPPORTED_FILE_TYPE", "message": str(e)})

    content = await file.read()

    # 3. ZIP 安全校验
    if file.filename.endswith(".zip"):
        try:
            validate_zip_safety(content)
            with zipfile.ZipFile(io.BytesIO(content), 'r') as zf:
                validate_zip_paths(zf)
        except ValueError as e:
            raise HTTPException(422, detail={"code": "FILE_PARSE_FAILED", "message": str(e)})

    # 4. 解析（含编码探测，见原文档 §4.5）
    result = DataIO().read_upload(content, file.filename)
    ...
```

---

## 4. 代码执行安全

> **当前状态**：代码沙箱已实现，采用 `code_mode/` AST 静态分析 + 子进程沙箱隔离的双层防线。`code_mode/` 在代码进入执行前对其进行 AST 级别的安全审查；沙箱子进程提供运行时资源隔离和网络限制。

### 4.1 AST 静态分析（`code_mode/`）

LLM 生成的代码在进入子进程执行前，先经 `code_mode/` 模块的 AST 静态分析：

1. **导入白名单/黑名单检查**：遍历 AST `Import` / `ImportFrom` 节点，拦截危险模块（`os`, `sys`, `subprocess`, `ctypes`, `socket`, `pickle` 等）
2. **危险调用检测**：禁止 `eval()`, `exec()`, `compile()`, `open()`, `__import__()` 等内置函数调用
3. **属性访问审计**：禁止 `__class__`, `__bases__`, `__subclasses__`, `__globals__` 等沙箱逃逸路径
4. **语法结构限制**：禁止 `with` 语句（防 `__enter__`/`__exit__` 副作用）、`async`/`await`（无异步需求）

AST 分析在 Python 编译阶段完成，不执行用户代码，零运行时开销，即使子进程沙箱被绕过也能提供第一道防线。

### 4.2 沙箱子进程隔离（6 层防御）

通过 AST 检查的代码进入隔离子进程执行，实现位于 `app/sandbox/runner.py` + `app/sandbox/sitecustomize_gismind.py`：

| 层 | 实现 | 防什么 |
|---|------|--------|
| 1. AST 静态分析 | `code_mode/` 模块遍历 AST 节点 | 危险模块导入、eval/exec、沙箱逃逸 |
| 2. 进程隔离 | `subprocess.Popen([sys.executable, "-S", ...])` | 与主进程隔离、fork bomb |
| 3. 资源硬限（Windows） | `pywin32 Job Object` — `JOB_OBJECT_LIMIT_PROCESS_MEMORY` + `KILL_ON_JOB_CLOSE` | OOM / 子进程僵尸 |
| 4. 导入黑名单 | `sitecustomize_gismind.py` 修改 `builtins.__import__` | os/subprocess/ctypes/multiprocessing 等危险模块 |
| 5. 网络隔离 | `socket.socket` monkey-patch 默认永远拒绝 | 数据外泄 |
| 6. 墙钟 + 内存硬限 | `subprocess.Popen.communicate(timeout=)` + `APP_SANDBOX_MEMORY_MB=512` | 死循环 / 无限等待 / OOM |

```python
# backend/app/sandbox/runner.py — 子进程隔离
# 1. AST 静态分析: code_mode/ 在启动子进程前完成安全检查
# 2. 进程隔离: subprocess.Popen([sys.executable, "-S", ...])
# 3. 资源硬限 (Windows): pywin32 Job Object — JOB_OBJECT_LIMIT_PROCESS_MEMORY + KILL_ON_JOB_CLOSE
# 4. 导入黑名单: sitecustomize_gismind.py 修改 builtins.__import__
# 5. 网络隔离: socket.socket monkey-patch 默认永远拒绝
# 6. 墙钟超时: subprocess.Popen.communicate(timeout=)
```

**沙箱边界声明**：此沙箱是"防止误操作 + 资源硬限"的安全网，不是抗恶意攻击的隔离边界。适用场景：用户上传数据清洗、scikit-learn 聚类、自定义公式计算。不适用：未审计的第三方脚本、对外暴露的生产 API。

### 4.3 RestrictedPython（备选方案，未使用）

> **状态**：备选方案，当前未启用。以下为设计文档，保留供后续评估。

```python
# backend/app/tools/code_interpreter.py
from RestrictedPython import compile_restricted
from RestrictedPython.Guards import safer_getattr

SAFE_BUILTINS = {
    # 允许的基础内置
    'abs', 'min', 'max', 'sum', 'len', 'range', 'enumerate', 'zip',
    'sorted', 'reversed', 'map', 'filter', 'round', 'int', 'float',
    'str', 'list', 'dict', 'tuple', 'set', 'bool',
    # 允许的数学/统计库（白名单导入）
    'math', 'statistics',
}

DANGEROUS_MODULES = {
    'os', 'sys', 'subprocess', 'socket', 'urllib', 'urllib2',
    'requests', 'http', 'ftplib', 'smtplib', 'telnetlib',
    'pickle', 'shelve', 'marshal', 'ctypes', 'multiprocessing',
    'threading', 'asyncio', 'signal', 'resource',
    'builtins', '__builtin__', 'importlib',
}

def execute_user_code(code: str, data_parquet_path: str, timeout: int = 30):
    """执行 LLM 生化的 Python 代码，沙箱隔离"""
    # 1. 静态检查：禁止 import 危险模块
    for mod in DANGEROUS_MODULES:
        if f"import {mod}" in code or f"from {mod}" in code:
            raise SecurityError(f"禁止导入模块 {mod}")

    # 2. RestrictedPython 编译
    byte_code = compile_restricted(code, filename='<user_code>', mode='exec')

    # 3. 受限执行环境
    restricted_globals = {
        '__builtins__': SAFE_BUILTINS,
        '_getattr_': safer_getattr,
        '_write_': lambda obj: obj,  # 允许写，但对象受限
        'pd': __import__('pandas'),
        'np': __import__('numpy'),
        'gpd': __import__('geopandas'),
        'data': __import__('pandas').read_parquet(data_parquet_path),  # 只读数据
    }

    # 4. 超时执行
    import signal
    def handler(signum, frame):
        raise TimeoutError("代码执行超时")
    signal.signal(signal.SIGALRM, handler)
    signal.alarm(timeout)
    try:
        exec(byte_code, restricted_globals)
    finally:
        signal.alarm(0)

    return restricted_globals.get('result')
```

### 4.4 数据传递安全

- **禁用 pickle**：存在 RCE 漏洞（反序列化可执行任意代码）
- **使用 Parquet**：只读挂载，LLM 代码通过 `pd.read_parquet('/data/input.parquet')` 读取
- **输出限制**：结果只能通过 stdout 返回，不能写文件

### 4.5 审计

每一条代码执行调用记录写入 `backend/.gismind/sandbox_audit.log`（结构化 JSONL），由 `app/audit/sandbox_audit.py` 管理。

### 4.6 当沙箱禁用时

设置 `APP_SANDBOX_ENABLED=false` 后，代码执行工具返回 `status="empty", message="code_executor 沙箱已禁用"`。

---

## 5. CORS 配置

### 5.1 配置

```python
# backend/app/main.py
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=False,  # 无认证，不需要 credentials
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
    max_age=3600,  # 预检结果缓存 1 小时
)
```

### 5.2 dev / prod 差异

| 环境 | `CORS_ORIGINS` |
|------|----------------|
| dev | `http://localhost:5173,http://localhost:3000` |
| prod | `https://your-domain.com`（单域名） |

**禁止**：生产环境用 `*`（允许所有来源），虽然无认证但会放行任意网站的跨站请求。

### 5.3 SSE 与 CORS

SSE（`text/event-stream`）受 CORS 约束。前端 `EventSource` 不支持自定义 header，所以 session_id 必须放在 URL query 或请求体中。本项目的 `/api/chat` 是 POST + 请求体，用 `fetch` + `ReadableStream` 实现 SSE，而非原生 `EventSource`，以支持 POST 方法和自定义 header。

---

## 6. 输入校验

### 6.1 Pydantic 校验所有 API 输入

```python
# 所有 API 端点使用 Pydantic 模型校验请求体（Pydantic v2 API）
from pydantic import BaseModel, field_validator

class ChatRequest(BaseModel):
    session_id: str
    message: str
    upload_file_ids: list[str] = []

    @field_validator('session_id')
    @classmethod
    def validate_session_id(cls, v: str) -> str:
        if not v or len(v) > 128:
            raise ValueError('session_id 长度须在 1-128 之间')
        # 字符集校验：只允许字母数字、连字符、下划线（防 Redis key 注入）
        if not v.replace('-', '').replace('_', '').isalnum():
            raise ValueError('session_id 只允许字母数字、连字符、下划线')
        return v

    @field_validator('message')
    @classmethod
    def validate_message(cls, v: str) -> str:
        if not v.strip():
            raise ValueError('message 不能为空')
        if len(v) > 10000:
            raise ValueError('message 超过 10000 字符上限')
        return v
```

### 6.2 Redis 命令注入防护

Redis 命令通过 `redis-py` 库调用，参数化传递，不存在 SQL 式注入。`session_id` 的字符集校验已在 §6.1 的 `ChatRequest` 中统一完成（只允许字母数字、连字符、下划线），Redis 层无需重复校验：

```python
# session_id 已在 API 入口经 Pydantic 校验，此处直接拼接安全
r.set(f"session:{session_id}", data)

# 若非 API 入口传入的标识符（如内部生成的 task_id），用 uuid 生成，无需校验
task_id = f"task_{uuid.uuid4().hex[:12]}"
```

### 6.3 命令注入防护

项目代码**不调用** `os.system` / `subprocess.run(shell=True)`。必须执行外部命令时（如 GDAL 命令行工具）：

```python
# 错误：shell=True + 字符串拼接
subprocess.run(f"gdal_translate {filename} output.tif", shell=True)

# 正确：参数列表，不经过 shell
subprocess.run(["gdal_translate", filename, "output.tif"], shell=False)
```

---

## 7. 速率限制（可选）

个人 demo 默认不启用。若部署到公网，建议基于 Redis 的简单限流：

```python
# backend/app/middleware/rate_limit.py
from fastapi import Request, HTTPException
from app.utils.redis import get_redis

RATE_LIMIT_PER_MINUTE = 30

async def rate_limit_middleware(request: Request, call_next):
    client_ip = request.client.host
    key = f"rate:{client_ip}:{int(time.time()) // 60}"
    redis = get_redis()

    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, 60)
    if count > RATE_LIMIT_PER_MINUTE:
        raise HTTPException(429, detail={
            "code": "RATE_LIMITED",
            "message": f"请求过于频繁，每分钟限 {RATE_LIMIT_PER_MINUTE} 次",
        })

    return await call_next(request)
```

---

## 8. 日志脱敏

API Key、用户输入中的敏感信息不进日志：

```python
# backend/app/utils/log_sanitizer.py
import re

SENSITIVE_PATTERNS = [
    (re.compile(r'(sk-)[a-zA-Z0-9]+'), r'\1***'),           # LLM key
    (re.compile(r'(amap_key=)[a-zA-Z0-9]+'), r'\1***'),      # 高德 key
    (re.compile(r'(password=)[^\s&]+'), r'\1***'),           # 密码
    (re.compile(r'(token=)[a-zA-Z0-9]+'), r'\1***'),         # token
]

def sanitize(text: str) -> str:
    for pattern, replacement in SENSITIVE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text
```

详细日志配置见 [07_observability.md](07_observability.md)。

---

## 9. 依赖安全

### 9.1 定期审计

```bash
# 后端
pip install pip-audit
pip-audit -r backend/requirements.txt

# 前端
cd frontend && npm audit
```

### 9.2 依赖锁定

- 后端：`requirements.txt` 固定版本号（`==`），不用 `>=`
- 前端：`package-lock.json` 提交到 git

### 9.3 CI 集成

```yaml
# .github/workflows/security.yml
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install pip-audit && pip-audit -r backend/requirements.txt
      - run: cd frontend && npm audit --audit-level=moderate
```

---

*文档版本：v1.2 | 最后更新：2026-07-17 | 属于 Gismind 补充文档*

*v1.2 变更：§4 重写为 code_mode/ AST guard + sandbox subprocess 主方案；6 层防御栈整合进 §4.2；移除 Docker 沙箱设计（未实现）；RestrictedPython 标注为"备选方案（未使用）"*
