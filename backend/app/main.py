"""Gismind FastAPI 入口。"""

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agents.checkpointer import get_sqlite_checkpointer
from app.api import chat, upload, memory, sessions
from app.config import settings, validate_config

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
    """应用生命周期：启动时确保 SqliteSaver 检查点数据库文件存在。"""
    _configure_logging()
    get_sqlite_checkpointer()

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
    yield


def create_app(
    *,
    redis_client=None,
    checkpointer=None,
    dispatcher_llm=None,
    sub_agent_llm=None,
) -> FastAPI:
    """Create the FastAPI application.

    Args:
        redis_client: optional pre-built async Redis client stored on
            ``app.state.redis`` for PendingStore and tests. Defaults to the
            process-wide client from ``get_redis()`` when first needed.
        checkpointer: optional LangGraph checkpointer stored on
            ``app.state.checkpointer``. Defaults to the SqliteSaver singleton.
        dispatcher_llm: optional LLM transport for root planner_router,
            stored on ``app.state.dispatcher_llm`` (tests only; production None).
        sub_agent_llm: optional LLM transport for sub-agent planner/verifier/judge,
            stored on ``app.state.sub_agent_llm`` (tests/e2e only; production None).
    """
    app = FastAPI(
        title="Gismind",
        description="基于 React Loop 的空间智能 GIS Agent",
        version="1.6.0",
        lifespan=lifespan,
    )

    # Runtime resource seams for routes/tests (real Redis / SqliteSaver).
    app.state.redis = redis_client
    app.state.checkpointer = checkpointer
    app.state.dispatcher_llm = dispatcher_llm
    app.state.sub_agent_llm = sub_agent_llm

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
        all_ok = not missing

        if settings.APP_ENV == "dev":
            # 真正 ping Redis，不造假
            redis_status = "ok"
            try:
                from app.utils.redis import get_redis
                client = getattr(app.state, "redis", None) or get_redis()
                await client.ping()
            except Exception:
                redis_status = "error"

            checks = {
                "redis": redis_status,
                "celery": "skipped",  # Celery 未在 dev 环境部署，标记 skipped
                "llm": "ok" if settings.LLM_API_KEY else "error",
                "amap": "ok" if settings.AMAP_KEY else "error",
            }
            # celery skipped 不算 error
            all_ok = all_ok and all(
                v in ("ok", "skipped") for v in checks.values()
            )
            return {
                "status": "ok" if all_ok else "degraded",
                "version": "1.6.0",
                "checks": checks,
            }
        # 非 dev 环境：不暴露逐服务详情，仅返回整体健康状态
        return {
            "status": "ok" if all_ok else "degraded",
            "version": "1.6.0",
        }

    # 挂载 API 路由
    app.include_router(chat.router)
    app.include_router(upload.router)
    app.include_router(memory.router)
    app.include_router(sessions.router)

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
