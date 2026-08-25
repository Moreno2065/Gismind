"""异步 Redis 客户端封装。

生产用 `redis.asyncio`，测试时通过注入 `redis_instance` 使用 fakeredis。

关键设计：Redis 连接具有事件循环亲和性（asyncio connection pool 绑定创建时的 loop）。
当 `asyncio.run()` 创建临时事件循环时，必须用独立的 Redis 客户端，否则连接池会被
已关闭 loop 的死连接污染，导致后续请求抛出 "Event loop is closed"。

因此本模块将 Redis 客户端直接挂在事件循环对象的属性上（loop._gismind_redis），
每个 loop 拥有独立的客户端与连接池。loop 关闭后被 GC 时，客户端随之消亡。
测试注入（set_redis_instance）优先级最高，注入后所有 loop 共用同一实例。
"""

import asyncio
from typing import Optional
import redis.asyncio as aioredis
from app.config import settings


# 测试注入的实例（优先级最高，绕过 per-loop 逻辑）
_redis_override: Optional[aioredis.Redis] = None

# loop 对象上存储 Redis 客户端的属性名
_LOOP_ATTR = '_gismind_redis_v2'


def set_redis_instance(instance: Optional[aioredis.Redis]) -> None:
    """测试或外部注入 Redis 实例。注入后 get_redis() 始终返回该实例。"""
    global _redis_override
    _redis_override = instance


def _create_redis(url: str | None = None) -> aioredis.Redis:
    """创建新的 Redis 异步客户端。protocol=2 兼容旧版 Redis。"""
    return aioredis.from_url(
        url or settings.REDIS_URL,
        decode_responses=True,
        protocol=2,
        max_connections=50,
        retry_on_timeout=True,
        health_check_interval=30,
    )


def create_redis_client(url: str | None = None) -> aioredis.Redis:
    """Public factory for an explicit Redis client (tests / app.state injection)."""
    return _create_redis(url)


def get_redis() -> aioredis.Redis:
    """返回当前事件循环的 Redis 异步客户端。

    客户端以属性形式直接挂在事件循环对象上，因此：
    - 不同 loop 绝不可能混淆（不依赖可复用的 id()）
    - loop 被 GC 时客户端自动消亡，不泄露
    - 测试注入的实例优先返回
    """
    if _redis_override is not None:
        return _redis_override

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 无运行中的事件循环（极罕见路径，如非异步上下文直接调用）
        return _create_redis()

    # 同一 loop 内只有单协程在跑（协作式调度），无并发竞争
    try:
        return getattr(loop, _LOOP_ATTR)
    except AttributeError:
        client = _create_redis()
        object.__setattr__(loop, _LOOP_ATTR, client)
        return client


def make_key(namespace: str, identifier: str) -> str:
    """统一 key 命名：namespace:identifier"""
    return f"{namespace}:{identifier}"
