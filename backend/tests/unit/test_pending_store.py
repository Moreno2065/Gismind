"""PendingStore 单元测试（TDD / Task 5 / 段 4）。

覆盖：
1. save / load 往返一致（含 missing_slots / candidates / issues / created_at）
2. 写入后 key 在 Redis 中存在（key 格式 ``pending:{session_id}``）
3. TTL 24h（ex=86400）
4. load 不存在 key 返回 None（不抛错）
5. clear 后 load 返回 None
6. PendingTask dataclass 字段（sub_agent_run_id 为 primary id，非 task_id）
7. from_dict 容错（额外字段被忽略）

所有测试使用 fakeredis（conftest.py 的 autouse fixture 注入 async 实例）。
PendingStore 提供 sync 包装（save_sync / load_sync / clear_sync），通过
asyncio.run 在无 running loop 时直接执行；在已有 loop 时通过
run_coroutine_threadsafe 调度。
"""

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from app.agents.pending import PendingStore, PendingTask
from app.utils.redis import make_key


def _run(coro):
    """Run an async coroutine from a sync test context.

    PendingStore's sync wrappers (save_sync / load_sync / clear_sync) call
    asyncio.run when there's no running loop.  This helper is for tests that
    want to use the async methods directly.
    """
    return asyncio.run(coro)


class TestPendingTaskDataclass:
    """PendingTask dataclass 字段契约。"""

    def test_required_fields(self):
        pt = PendingTask(
            sub_agent_run_id="run_abc",
            original_request="南京夫子庙 1km 缓冲区",
        )
        assert pt.sub_agent_run_id == "run_abc"
        assert pt.original_request == "南京夫子庙 1km 缓冲区"
        assert pt.missing_slots == []
        assert pt.candidates == []
        assert pt.message == ""
        assert pt.issues == []
        assert pt.created_at != ""

    def test_created_at_auto_set(self):
        pt = PendingTask(sub_agent_run_id="r1", original_request="x")
        # ISO8601 UTC
        assert "T" in pt.created_at
        assert pt.created_at.endswith("+00:00") or pt.created_at.endswith("Z")

    def test_created_at_preserved_if_set(self):
        pt = PendingTask(
            sub_agent_run_id="r1",
            original_request="x",
            created_at="2026-01-01T00:00:00+00:00",
        )
        assert pt.created_at == "2026-01-01T00:00:00+00:00"

    def test_to_dict_round_trip(self):
        pt = PendingTask(
            sub_agent_run_id="run_xyz",
            original_request="测试",
            missing_slots=["distance", "output_path"],
            candidates=[{"name": "A"}, {"name": "B"}],
            message="请输入距离",
            issues=[{"code": "buffer_crs_mismatch", "severity": "error"}],
        )
        d = pt.to_dict()
        assert d["sub_agent_run_id"] == "run_xyz"
        assert d["original_request"] == "测试"
        assert d["missing_slots"] == ["distance", "output_path"]
        assert d["candidates"] == [{"name": "A"}, {"name": "B"}]
        assert d["message"] == "请输入距离"
        assert d["issues"][0]["code"] == "buffer_crs_mismatch"
        assert "created_at" in d

        # Round trip via from_dict
        pt2 = PendingTask.from_dict(d)
        assert pt2.sub_agent_run_id == "run_xyz"
        assert pt2.missing_slots == ["distance", "output_path"]

    def test_from_dict_ignores_unknown_fields(self):
        # 容错：extra fields 静默丢弃
        data = {
            "sub_agent_run_id": "r1",
            "original_request": "x",
            "unknown_extra": "should_be_dropped",
        }
        pt = PendingTask.from_dict(data)
        assert pt.sub_agent_run_id == "r1"
        assert not hasattr(pt, "unknown_extra")


class TestPendingStoreRoundTrip:
    """PendingStore 的 Redis 序列化往返。"""

    def _make_pt(self, **overrides) -> PendingTask:
        defaults = {
            "sub_agent_run_id": "run_test_001",
            "original_request": "用户原始请求",
            "missing_slots": ["distance"],
            "message": "请提供缓冲距离",
        }
        defaults.update(overrides)
        return PendingTask(**defaults)

    def test_save_and_load_round_trip(self, fake_redis):
        """save → load 一致。"""
        store = PendingStore()
        pt = self._make_pt()

        _run(store.save("sess_1", pt))
        loaded = _run(store.load("sess_1"))

        assert loaded is not None
        assert loaded.sub_agent_run_id == "run_test_001"
        assert loaded.original_request == "用户原始请求"
        assert loaded.missing_slots == ["distance"]
        assert loaded.message == "请提供缓冲距离"
        assert loaded.created_at == pt.created_at

    def test_load_missing_returns_none(self, fake_redis):
        """不存在的 key 返回 None（不抛错）。"""
        store = PendingStore()
        result = _run(store.load("sess_nonexistent"))
        assert result is None

    def test_clear_removes_entry(self, fake_redis):
        """clear 后 load 返回 None。"""
        store = PendingStore()
        pt = self._make_pt()
        _run(store.save("sess_2", pt))
        assert _run(store.load("sess_2")) is not None
        _run(store.clear("sess_2"))
        assert _run(store.load("sess_2")) is None

    def test_key_uses_pending_namespace(self, fake_redis):
        """Redis key 格式: pending:{session_id}（复用 make_key）。"""
        store = PendingStore()
        pt = self._make_pt()
        _run(store.save("sess_key_check", pt))

        expected_key = make_key("pending", "sess_key_check")
        exists = _run(fake_redis.exists(expected_key))
        assert exists == 1

    def test_ttl_set_to_24h(self, fake_redis):
        """TTL 设置为 24h (86400 秒)。"""
        store = PendingStore()
        pt = self._make_pt()
        _run(store.save("sess_ttl", pt))

        expected_key = make_key("pending", "sess_ttl")
        ttl = _run(fake_redis.ttl(expected_key))
        # TTL should be ~86400 (allow small variance)
        assert 86390 <= ttl <= 86400

    def test_save_stores_json(self, fake_redis):
        """Redis 中存储的是 JSON 序列化字符串（非 pickle）。"""
        store = PendingStore()
        pt = self._make_pt(message="中文测试")
        _run(store.save("sess_json", pt))

        expected_key = make_key("pending", "sess_json")
        raw = _run(fake_redis.get(expected_key))
        assert raw is not None
        raw_str = raw if isinstance(raw, str) else raw.decode("utf-8")
        # JSON 解析（中文保留）
        data = json.loads(raw_str)
        assert data["message"] == "中文测试"
        assert data["sub_agent_run_id"] == "run_test_001"

    def test_load_corrupted_json_returns_none(self, fake_redis):
        """Redis 中存了非 JSON 数据，load 应返回 None 而不抛错。"""
        store = PendingStore()
        expected_key = make_key("pending", "sess_corrupt")
        _run(fake_redis.set(expected_key, "{not_valid_json,,,}"))

        result = _run(store.load("sess_corrupt"))
        assert result is None


class TestPendingStoreWithMock:
    """PendingStore 与 mock Redis 客户端的兼容性。"""

    def _make_async_mock(self):
        """Make a MagicMock whose ``set/get/delete`` are AsyncMock-like."""
        from unittest.mock import AsyncMock
        m = MagicMock()
        m.set = AsyncMock(return_value=True)
        m.get = AsyncMock(return_value=None)
        m.delete = AsyncMock(return_value=1)
        return m

    def test_save_calls_redis_set_with_ttl(self):
        """save 触发 redis.set(key, value, ex=86400)。"""
        mock_r = self._make_async_mock()
        store = PendingStore(redis_client=mock_r)
        pt = PendingTask(sub_agent_run_id="r1", original_request="x")

        _run(store.save("sess_mock", pt))

        mock_r.set.assert_awaited_once()
        args, kwargs = mock_r.set.call_args
        # positional: key, value
        assert args[0] == "pending:sess_mock"
        # ttl = ex=86400
        assert kwargs.get("ex") == 86400

    def test_load_calls_redis_get(self):
        """load 触发 redis.get(key)。"""
        mock_r = self._make_async_mock()
        store = PendingStore(redis_client=mock_r)

        result = _run(store.load("sess_mock"))
        assert result is None
        mock_r.get.assert_awaited_once_with("pending:sess_mock")

    def test_clear_calls_redis_delete(self):
        """clear 触发 redis.delete(key)。"""
        mock_r = self._make_async_mock()
        store = PendingStore(redis_client=mock_r)

        _run(store.clear("sess_mock"))
        mock_r.delete.assert_awaited_once_with("pending:sess_mock")

    def test_save_load_round_trip_with_mock(self):
        """Mock Redis 模拟完整 round-trip。"""
        backing: dict[str, str] = {}

        async def _set(key, value, ex=None):
            backing[key] = value
            return True

        async def _get(key):
            return backing.get(key)

        async def _delete(key):
            backing.pop(key, None)
            return 1

        mock_r = MagicMock()
        # Use plain functions (not Mock attributes) for backing-store behavior
        mock_r.set = _set
        mock_r.get = _get
        mock_r.delete = _delete

        store = PendingStore(redis_client=mock_r)
        pt = PendingTask(
            sub_agent_run_id="r1",
            original_request="原请求",
            missing_slots=["distance"],
            message="请输入距离",
        )

        _run(store.save("sess_round", pt))
        loaded = _run(store.load("sess_round"))

        assert loaded is not None
        assert loaded.sub_agent_run_id == "r1"
        assert loaded.message == "请输入距离"
        assert loaded.missing_slots == ["distance"]

        _run(store.clear("sess_round"))
        assert _run(store.load("sess_round")) is None


class TestPendingStoreSyncWrappers:
    """save_sync / load_sync / clear_sync — sync 入口。"""

    def test_save_sync_load_sync(self, fake_redis):
        """sync 包装等价于 async 入口。"""
        store = PendingStore()
        pt = PendingTask(sub_agent_run_id="r1", original_request="sync test")

        store.save_sync("sess_sync", pt)
        loaded = store.load_sync("sess_sync")

        assert loaded is not None
        assert loaded.sub_agent_run_id == "r1"

    def test_clear_sync(self, fake_redis):
        """clear_sync 后 load_sync 返回 None。"""
        store = PendingStore()
        pt = PendingTask(sub_agent_run_id="r1", original_request="x")

        store.save_sync("sess_clear", pt)
        assert store.load_sync("sess_clear") is not None
        store.clear_sync("sess_clear")
        assert store.load_sync("sess_clear") is None

    def test_load_sync_missing(self, fake_redis):
        """sync load 不存在的 key 返回 None。"""
        store = PendingStore()
        assert store.load_sync("sess_missing") is None

    def test_sync_wrappers_safe_when_loop_already_running(self, fake_redis):
        """已有 running loop 时不得 run_coroutine_threadsafe 死锁；应走独立线程 loop。"""
        store = PendingStore()
        pt = PendingTask(sub_agent_run_id="r_loop", original_request="loop-safe")

        async def _call_sync_from_running_loop():
            # Inside a running loop: sync wrappers must still complete.
            store.save_sync("sess_loop_safe", pt)
            loaded = store.load_sync("sess_loop_safe")
            store.clear_sync("sess_loop_safe")
            return loaded

        loaded = asyncio.run(_call_sync_from_running_loop())
        assert loaded is not None
        assert loaded.sub_agent_run_id == "r_loop"
        assert store.load_sync("sess_loop_safe") is None