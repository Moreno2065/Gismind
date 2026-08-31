"""PendingTask + PendingStore: preflight ask_user 场景下的用户输入挂起与恢复。

当 preflight 规则检测到 blocking issue 且 repair.kind == "ask_user" 时，
sub-agent 不应直接失败，而是生成一个 PendingTask 存入 Redis，
通过 SSE 的 judge.awaiting_input 事件通知前端，等用户回复后 resume。

PendingTask dataclass 定义在 ``app.agents.schemas``；本模块只负责
序列化与 Redis 持久化，避免两个定义分叉。

Redis 客户端来自 ``app.utils.redis.get_redis()``，是 async 接口（redis.asyncio）。
因此 PendingStore 全部方法均为 async；调用方需在 async 上下文中 await。
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any, Optional

from app.agents.schemas import PendingTask

logger = logging.getLogger(__name__)


__all__ = ["PendingTask", "PendingStore"]


class PendingStore:
    """Redis 持久化的 PendingTask 存储。

    Key: ``pending:{session_id}``
    Value: JSON 序列化（ensure_ascii=False）
    TTL: 24h（86400 秒）
    """

    _TTL = 86400  # 24 小时
    # A resume can legitimately run several external GIS operations.  Keep the
    # lease long enough for that bounded run, but never indefinitely after a
    # process crash.
    _CLAIM_TTL = 900  # 15 minutes

    def __init__(self, redis_client: Any = None) -> None:
        """``redis_client`` 可选；不传则通过 ``get_redis()`` 取 async 客户端。"""
        self._r = redis_client

    def _get_redis(self) -> Any:
        if self._r is not None:
            return self._r
        from app.utils.redis import get_redis
        return get_redis()

    def _key(self, session_id: str) -> str:
        from app.utils.redis import make_key
        return make_key("pending", session_id)

    def _claim_key(self, session_id: str) -> str:
        from app.utils.redis import make_key
        return make_key("pending-claim", session_id)

    async def save(self, session_id: str, pt: PendingTask) -> None:
        """将 PendingTask 写入 Redis；TTL 24h。"""
        r = self._get_redis()
        await r.set(
            self._key(session_id),
            json.dumps(pt.to_dict(), ensure_ascii=False),
            ex=self._TTL,
        )
        # A freshly persisted follow-up question supersedes any lease for the
        # prior question.  The active resumer owns that transition; a later
        # request must claim the new pending row again.
        await r.delete(self._claim_key(session_id))

    async def load(self, session_id: str) -> Optional[PendingTask]:
        """从 Redis 读取 PendingTask；不存在 / 损坏时返回 None。"""
        r = self._get_redis()
        raw = await r.get(self._key(session_id))
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return PendingTask.from_dict(data)
        except (json.JSONDecodeError, TypeError):
            return None

    async def clear(self, session_id: str) -> None:
        """从 Redis 删除 PendingTask（不存在的 key 静默忽略）。"""
        r = self._get_redis()
        await r.delete(self._key(session_id), self._claim_key(session_id))

    async def claim(self, session_id: str, sub_agent_run_id: str) -> Optional[str]:
        """Atomically lease one pending task for a single ``/resume`` caller.

        The pending payload remains readable while the lease exists; only the
        successful caller may later clear or replace it.  ``None`` means
        another resume already owns the same session's pending transition.
        ``sub_agent_run_id`` is stored for diagnostics only: the endpoint has
        already checked it against the authoritative pending payload.
        """
        r = self._get_redis()
        token = secrets.token_urlsafe(24)
        value = json.dumps(
            {"token": token, "sub_agent_run_id": str(sub_agent_run_id)},
            ensure_ascii=False,
        )
        claimed = await r.set(
            self._claim_key(session_id),
            value,
            nx=True,
            ex=self._CLAIM_TTL,
        )
        return token if claimed else None

    async def release_claim(self, session_id: str, token: str) -> bool:
        """Release only the caller's own lease, never a newer caller's lease."""
        r = self._get_redis()
        key = self._claim_key(session_id)
        # The stored JSON additionally contains the run id, so compare the
        # decoded token atomically in Lua.  This prevents a timed-out caller
        # from deleting a replacement lease obtained after its TTL expired.
        script = (
            "local raw = redis.call('GET', KEYS[1]); "
            "if not raw then return 0 end; "
            "local ok, value = pcall(cjson.decode, raw); "
            "if ok and value['token'] == ARGV[1] then "
            "return redis.call('DEL', KEYS[1]); end; return 0"
        )
        try:
            deleted = await r.eval(script, 1, key, token)
            return bool(deleted)
        except Exception:
            # Compatibility fallback for constrained Redis providers.  The
            # normal local Redis route uses the atomic Lua branch above.
            raw = await r.get(key)
            if not raw:
                return False
            try:
                value = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return False
            if value.get("token") != token:
                return False
            await r.delete(key)
            return True

    # ------------------------------------------------------------------
    # Sync wrappers — never call run_coroutine_threadsafe on the *current*
    # running loop (deadlocks the calling thread waiting on itself).
    # Use a thread-local / worker-thread event loop instead.
    # ------------------------------------------------------------------

    def save_sync(self, session_id: str, pt: PendingTask) -> None:
        """同步包装：从非 async 上下文（judge node / 测试）调用。"""
        _run_sync(self.save(session_id, pt))

    def load_sync(self, session_id: str) -> Optional[PendingTask]:
        """同步包装。"""
        return _run_sync(self.load(session_id))

    def clear_sync(self, session_id: str) -> None:
        """同步包装。"""
        _run_sync(self.clear(session_id))


# Thread-local persistent loop for sync wrappers when no loop is running.
_thread_local = __import__("threading").local()


def _get_thread_loop():
    """Return (and cache) a persistent event loop for the current thread."""
    import asyncio

    loop = getattr(_thread_local, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        _thread_local.loop = loop
    return loop


def _run_sync(coro):
    """Run *coro* safely from sync code.

    - No running loop: use a thread-local persistent loop + ``run_until_complete``.
    - Already inside a running loop: spin up a daemon worker thread with its
      own loop so we never ``run_coroutine_threadsafe`` on the current loop
      (which would deadlock when the caller blocks on ``.result()``).
    """
    import asyncio
    import threading

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = _get_thread_loop()
        return loop.run_until_complete(coro)

    result: dict[str, Any] = {}
    error: list[BaseException] = []

    def _worker() -> None:
        worker_loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(worker_loop)
            result["value"] = worker_loop.run_until_complete(coro)
        except BaseException as exc:  # noqa: BLE001 — re-raise on caller
            error.append(exc)
        finally:
            try:
                worker_loop.close()
            except Exception:
                pass
            asyncio.set_event_loop(None)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=30)
    if t.is_alive():
        raise TimeoutError("PendingStore._run_sync worker timed out")
    if error:
        raise error[0]
    return result.get("value")
