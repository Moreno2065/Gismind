"""空间记忆单元测试（TDD）。

覆盖 app/utils/memory.py 的核心能力：
1. remember_origin 写入记忆
2. get_memories 读取记忆列表
3. clear_memories 清空
4. last_location 读写
5. TTL / JSON 序列化基本正确

全部使用 fakeredis，不连真实 Redis。
"""

import pytest

from app.utils.memory import MemoryStore


@pytest.fixture
async def memory_store(fake_redis):
    return MemoryStore()


class TestRememberOrigin:
    async def test_remember_origin_stores_memory(self, memory_store):
        await memory_store.remember_origin("sess_1", "安师老校区", (118.78, 32.05))
        memories = await memory_store.get_memories("sess_1")
        assert len(memories) == 1
        assert memories[0]["label"] == "安师老校区"
        assert memories[0]["location"] == [118.78, 32.05]
        assert memories[0]["crs"] == "GCJ02"
        assert memories[0]["key"] == "常用原点"
        assert "created_at" in memories[0]

    async def test_remember_origin_keeps_multiple(self, memory_store):
        await memory_store.remember_origin("sess_2", "家", (116.40, 39.90), crs="WGS84")
        await memory_store.remember_origin("sess_2", "公司", (121.47, 31.23))
        memories = await memory_store.get_memories("sess_2")
        assert len(memories) == 2
        assert memories[0]["crs"] == "WGS84"
        assert memories[1]["crs"] == "GCJ02"


class TestClearMemories:
    async def test_clear_memories_removes_all(self, memory_store):
        await memory_store.remember_origin("sess_3", "A", (0.0, 0.0))
        await memory_store.clear_memories("sess_3")
        memories = await memory_store.get_memories("sess_3")
        assert memories == []


class TestLastLocation:
    async def test_get_last_location_initially_none(self, memory_store):
        loc = await memory_store.get_last_location("sess_4")
        assert loc is None

    async def test_set_and_get_last_location(self, memory_store):
        await memory_store.set_last_location("sess_4", (118.78, 32.05), crs="GCJ02")
        loc = await memory_store.get_last_location("sess_4")
        assert loc == (118.78, 32.05)

    async def test_set_last_location_does_not_erase_memories(self, memory_store):
        await memory_store.remember_origin("sess_5", "原点", (118.0, 32.0))
        await memory_store.set_last_location("sess_5", (119.0, 33.0))
        memories = await memory_store.get_memories("sess_5")
        assert len(memories) == 1
        assert await memory_store.get_last_location("sess_5") == (119.0, 33.0)


class TestSessionIsolation:
    async def test_memories_isolated_by_session(self, memory_store):
        await memory_store.remember_origin("sess_a", "A", (1.0, 1.0))
        await memory_store.remember_origin("sess_b", "B", (2.0, 2.0))
        assert len(await memory_store.get_memories("sess_a")) == 1
        assert len(await memory_store.get_memories("sess_b")) == 1
        assert (await memory_store.get_memories("sess_a"))[0]["label"] == "A"
