"""会话持久化单元测试（TDD）。

覆盖 app/utils/session.py 的核心能力：
1. append_message 追加消息
2. get_messages 读取最近 N 条并返回 LangChain 消息对象
3. 超过 limit 时保留最新的 N 条
4. clear_session 清空

全部使用 fakeredis，不连真实 Redis。
"""

import pytest
from langchain_core.messages import HumanMessage, AIMessage

from app.utils.session import SessionStore


@pytest.fixture
async def session_store(fake_redis):
    return SessionStore()


class TestAppendAndRead:
    async def test_append_user_message(self, session_store):
        await session_store.append_message("sess_1", "user", "你好")
        msgs = await session_store.get_messages("sess_1")
        assert len(msgs) == 1
        assert isinstance(msgs[0], HumanMessage)
        assert msgs[0].content == "你好"

    async def test_append_assistant_message(self, session_store):
        await session_store.append_message("sess_1", "assistant", "找到 12 家")
        msgs = await session_store.get_messages("sess_1")
        assert len(msgs) == 1
        assert isinstance(msgs[0], AIMessage)
        assert msgs[0].content == "找到 12 家"

    async def test_messages_returned_in_order(self, session_store):
        await session_store.append_message("sess_2", "user", "Q1")
        await session_store.append_message("sess_2", "assistant", "A1")
        await session_store.append_message("sess_2", "user", "Q2")
        msgs = await session_store.get_messages("sess_2")
        assert [m.content for m in msgs] == ["Q1", "A1", "Q2"]
        assert isinstance(msgs[0], HumanMessage)
        assert isinstance(msgs[1], AIMessage)
        assert isinstance(msgs[2], HumanMessage)

    async def test_append_message_persists_execution_trace_metadata(self, session_store):
        trace = [{"event": "run.plan", "tasks": [{"id": "t1"}]}]
        await session_store.append_message(
            "sess_trace",
            "assistant",
            "完成",
            metadata={"execution_trace": trace},
        )

        records = await session_store.list_messages("sess_trace")
        assert records is not None
        assert records[0]["execution_trace"] == trace


class TestLimit:
    async def test_get_messages_keeps_last_n(self, session_store):
        for i in range(25):
            role = "user" if i % 2 == 0 else "assistant"
            await session_store.append_message("sess_3", role, f"msg_{i}")
        msgs = await session_store.get_messages("sess_3", limit=20)
        assert len(msgs) == 20
        # 保留最新的 20 条
        assert msgs[0].content == "msg_5"
        assert msgs[-1].content == "msg_24"

    async def test_get_messages_default_limit(self, session_store):
        for i in range(25):
            await session_store.append_message("sess_4", "user", str(i))
        msgs = await session_store.get_messages("sess_4")
        assert len(msgs) == 20


class TestClearSession:
    async def test_clear_session_removes_messages(self, session_store):
        await session_store.append_message("sess_5", "user", "hello")
        await session_store.clear_session("sess_5")
        msgs = await session_store.get_messages("sess_5")
        assert msgs == []


class TestSessionIsolation:
    async def test_sessions_isolated(self, session_store):
        await session_store.append_message("sess_a", "user", "A")
        await session_store.append_message("sess_b", "user", "B")
        assert len(await session_store.get_messages("sess_a")) == 1
        assert len(await session_store.get_messages("sess_b")) == 1
        assert (await session_store.get_messages("sess_a"))[0].content == "A"
        assert (await session_store.get_messages("sess_b"))[0].content == "B"
