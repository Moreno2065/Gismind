"""Gismind FastAPI 入口。"""

import logging
import os
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
import uuid

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import runtime
from app.config import settings, validate_config
from app.runtime import ArtifactStore, RuntimeDB, RuntimeStore, RuntimeSupervisor

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    """应用日志配置，使用 settings.APP_LOG_LEVEL 和 APP_LOG_FORMAT。"""
    log_level = getattr(logging, settings.APP_LOG_LEVEL.upper(), logging.INFO)
    log_format = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    if settings.APP_LOG_FORMAT == "json":
        log_format = '{"time": "%(asctime)s", "level": "%(levelname)s", "logger": "%(name)s", "message": "%(message)s"}'
    logging.basicConfig(level=log_level, format=log_format)
    # 抑制第三方库的 DEBUG 噪音
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：启动 Runtime supervisor/worker。"""
    _configure_logging()

    # 沙箱内存硬限校验
    import sys
    if sys.platform == "win32":
        try:
            import pywintypes  # noqa: F401  — 探测 pywin32 是否已安装
        except ImportError:
            logger.warning(
                "pywin32 未安装 — 沙箱内存硬限（Job Object）将静默失效。"
                " 请执行: pip install pywin32>=306"
            )
    # Runtime execution is a process service, not an ad-hoc request side
    # effect.  Start one durable worker for the single-user deployment and
    # always stop it with the ASGI application.
    runtime = getattr(app.state, "runtime_store", None)
    artifacts = getattr(app.state, "artifact_store", None)
    supervisor = getattr(app.state, "runtime_supervisor", None)
    if runtime is None:
        runtime = RuntimeStore(RuntimeDB())
        runtime.initialize()
        app.state.runtime_store = runtime
    if artifacts is None:
        artifacts = ArtifactStore(runtime=runtime)
        app.state.artifact_store = artifacts
    if supervisor is None:
        supervisor = RuntimeSupervisor(runtime, artifacts)
        app.state.runtime_supervisor = supervisor
    worker_started = False
    try:
        if supervisor.autostart and not supervisor.health_snapshot()["running"]:
            supervisor.start(f"api-{os.getpid()}-{uuid.uuid4().hex}")
            worker_started = True
        yield
    finally:
        if worker_started or supervisor.health_snapshot()["running"]:
            supervisor.stop(timeout=5)


def create_app(
    *,
    runtime_store=None,
    artifact_store=None,
    runtime_supervisor=None,
) -> FastAPI:
    """Create the FastAPI application around the durable Runtime."""
    app = FastAPI(
        title="Gismind",
        description="Agent-driven GIS Runtime",
        version="2.0.0",
        lifespan=lifespan,
    )

    # Runtime resources are injectable for API tests and embedded single-user
    # deployments.  They are initialized lazily when omitted.
    app.state.runtime_store = runtime_store
    app.state.artifact_store = artifact_store
    app.state.runtime_supervisor = runtime_supervisor

    @app.middleware("http")
    async def request_id_middleware(request, call_next):
        request_id = request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex}"
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    # CORS 配置：allow_headers=["*"] 允许所有请求头。
    # 安全说明：当前宽松策略适用于开发环境。生产环境应将 allow_headers 限定为
    # 实际需要的请求头（如 Content-Type, Authorization, X-User-Id），
    # 并收紧 allow_origins 为明确的域名列表。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["*"],
        max_age=3600,
    )

    @app.get("/api/health")
    async def health():
        missing = validate_config()
        runtime_store = getattr(app.state, "runtime_store", None)
        supervisor = getattr(app.state, "runtime_supervisor", None)
        store_check = {"status": "error", "writable": False}
        queue_check = {
            "queued_runs": 0, "planning_runs": 0, "ready_tasks": 0,
            "active_tasks": 0, "retry_wait_tasks": 0,
        }
        try:
            if runtime_store is None:
                raise RuntimeError("runtime store is unavailable")
            # BEGIN IMMEDIATE proves that this process can acquire SQLite's
            # writer lock without adding synthetic health-check rows.
            with runtime_store.transaction() as conn:
                conn.execute("SELECT 1").fetchone()
            counts = runtime_store._read(lambda c: {
                "queued_runs": int(c.execute("SELECT COUNT(*) FROM runs WHERE status='queued'").fetchone()[0]),
                "planning_runs": int(c.execute("SELECT COUNT(*) FROM runs WHERE status='planning'").fetchone()[0]),
                "ready_tasks": int(c.execute("SELECT COUNT(*) FROM tasks WHERE status='ready'").fetchone()[0]),
                "active_tasks": int(c.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('leased','running','validating')").fetchone()[0]),
                "retry_wait_tasks": int(c.execute("SELECT COUNT(*) FROM tasks WHERE status='retry_wait'").fetchone()[0]),
            })
            queue_check.update(counts)
            store_check = {"status": "ok", "writable": True}
        except Exception as exc:
            store_check["error"] = type(exc).__name__

        worker_check = supervisor.health_snapshot() if supervisor is not None else {
            "running": False, "status": "unavailable",
        }
        worker_required = bool(supervisor is not None and supervisor.autostart)
        worker_ok = bool(worker_check.get("running")) if worker_required else supervisor is not None
        all_ok = not missing and store_check["status"] == "ok" and worker_ok

        if settings.APP_ENV == "dev":
            checks = {
                "runtime_store": store_check,
                "worker": worker_check,
                "queue": queue_check,
                "llm": "ok" if all((settings.LLM_API_KEY, settings.LLM_BASE_URL, settings.LLM_MODEL)) else "error",
                "amap": "ok" if settings.AMAP_KEY else "error",
            }
            all_ok = all_ok and checks["llm"] == "ok" and checks["amap"] == "ok"
            return {
                "status": "ok" if all_ok else "degraded",
                "version": "2.0.0",
                "checks": checks,
            }
        # 非 dev 环境：不暴露逐服务详情，仅返回整体健康状态
        return {
            "status": "ok" if all_ok else "degraded",
            "version": "2.0.0",
        }

    # 挂载 API 路由
    app.include_router(runtime.router)

    return app



# 启动时校验必填配置
_missing = validate_config()
if _missing:
    logger.error(
        "Gismind 启动配置缺失以下必填项：%s",
        ", ".join(_missing),
    )
    if settings.APP_ENV != "dev":
        raise RuntimeError(
            f"缺少必填配置，无法启动服务：{', '.join(_missing)}"
        )


app = create_app()
