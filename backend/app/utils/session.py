"""会话上下文持久化到 Redis。

参考 docs/02_data_models.md §5.3：
- Redis key: session:{session_id}
- 值: JSON（消息列表 + 元信息 + user_id）
- TTL: 24h

Sprint 1 增量：
- 增加 scan / create / rename / get_meta / list_messages
- 持久化 title / created_at / updated_at（epoch ms）
- list 端按 updated_at 降序返回

认证模型（临时方案）:
  user_id 存储在每个 session/memory 的 JSON 内部。
  通过 X-User-Id 请求头传入，默认 "anonymous"。
  生产环境必须替换为 JWT / OAuth2 并验证签名。
"""

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    AIMessage,
    ToolMessage,
)

from app.models.schemas import SessionMeta
from app.utils.redis import get_redis, make_key

logger = logging.getLogger(__name__)


SESSION_TTL = 24 * 60 * 60  # 24 小时


def _now_ms() -> int:
    return int(time.time() * 1000)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_tool_calls(tool_calls: list) -> list:
    """将 save_turn 存储的简化 tool_call 格式转为 LangChain 标准格式。

    save_turn 存储格式: [{"tool_name": "query_poi"}]
    LangChain 标准格式: [{"name": "query_poi", "args": {}, "id": "call_..."}]

    已经是标准格式的条目保持不变。
    """
    normalized = []
    for i, tc in enumerate(tool_calls):
        if not isinstance(tc, dict):
            continue
        # 已经是 LangChain 标准格式（有 "name" 字段）
        if "name" in tc:
            normalized.append(tc)
        # save_turn 的简化格式（只有 "tool_name" 字段）
        elif "tool_name" in tc:
            normalized.append({
                "name": tc["tool_name"],
                "args": tc.get("params", {}),
                "id": tc.get("id", f"hist_{i}"),
            })
        else:
            # 未知格式，跳过
            logger.warning("unknown tool_call format, skipping: %s", tc)
    return normalized


def _record_to_message(record: dict) -> BaseMessage:
    """把持久化的消息记录转成 LangChain 消息。"""
    role = record.get("role")
    content = record.get("content", "")
    if role == "assistant":
        tool_calls = record.get("tool_calls")
        if tool_calls:
            # save_turn 存的 tool_calls 格式为 {"tool_name": "xxx"}
            # 需转为 LangChain 标准 {"name": "xxx", "args": {}, "id": "..."}
            normalized = _normalize_tool_calls(tool_calls)
            return AIMessage(content=content, tool_calls=normalized if normalized else None)
        return AIMessage(content=content)
    if role == "tool":
        return ToolMessage(
            content=content,
            tool_call_id=record.get("tool_call_id", ""),
        )
    return HumanMessage(content=content)


def _msg_role(m) -> str:
    """安全获取消息的 role，兼容 dict 和 LangChain BaseMessage。"""
    if isinstance(m, dict):
        return m.get("role", "")
    return getattr(m, "role", getattr(m, "type", ""))


def _msg_content(m) -> str:
    """安全获取消息的 content，兼容 dict 和 LangChain BaseMessage。"""
    if isinstance(m, dict):
        return m.get("content") or ""
    return getattr(m, "content", "") or ""


def _msg_tool_calls(m) -> list:
    """安全获取消息的 tool_calls，兼容 dict 和 LangChain BaseMessage。"""
    if isinstance(m, dict):
        return m.get("tool_calls") or []
    return getattr(m, "tool_calls", None) or []


def _derive_title_from_messages(messages: list, fallback: str = "新会话") -> str:
    """从消息列表懒生成 title：取首条 user 消息前 24 字。"""
    for m in messages:
        if _msg_role(m) == "user":
            text = _msg_content(m).strip().replace("\n", " ")
            if text:
                return text[:24] + ("…" if len(text) > 24 else "")
    return fallback


DEFAULT_SESSION_TITLE = "新会话"


def _is_placeholder_title(title: Optional[str]) -> bool:
    """占位 title（空 / 默认值）应当被自动命名覆盖。"""
    if not title:
        return True
    return title.strip() == DEFAULT_SESSION_TITLE


def _summarize_meta(session_id: str, data: dict) -> SessionMeta:
    """从持久化的 session dict 生成 SessionMeta。

    懒生成 title 规则：
    - 若 stored title 是占位符（"新会话"）或为空，但 messages 里已有 user 消息，
      自动用首条 user 消息前 24 字作为 title。
    - 这样 save_turn 后再调 list 时，title 会从"新会话"升级到首句摘要。
    """
    messages = data.get("messages", [])
    tool_count = 0
    has_map = False
    for m in messages:
        if _msg_role(m) == "assistant":
            tool_count += len(_msg_tool_calls(m))
        # blocks / map 字段仅存在于 dict 格式
        if isinstance(m, dict):
            for blk in (m.get("blocks") or []):
                if isinstance(blk, dict) and blk.get("type") == "map":
                    has_map = True
            if m.get("map") or m.get("has_map"):
                has_map = True
    stored_title = data.get("title")
    if _is_placeholder_title(stored_title):
        derived = _derive_title_from_messages(messages, fallback=DEFAULT_SESSION_TITLE)
        title = derived if derived != DEFAULT_SESSION_TITLE else (stored_title or DEFAULT_SESSION_TITLE)
    else:
        title = stored_title
    created_at = data.get("created_at") or _now_ms()
    updated_at = data.get("updated_at") or _now_ms()
    return SessionMeta(
        id=session_id,
        title=title,
        created_at=int(created_at),
        updated_at=int(updated_at),
        message_count=len(messages),
        tool_count=tool_count,
        has_map=has_map,
    )


class SessionStore:
    """会话上下文持久化到 Redis。"""

    async def _load(self, session_id: str) -> dict:
        r = get_redis()
        key = make_key("session", session_id)
        raw = await r.get(key)
        if raw:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"session_id": session_id, "messages": [], "user_id": "anonymous"}
        return {"session_id": session_id, "messages": [], "user_id": "anonymous"}

    async def _save(self, session_id: str, data: dict) -> None:
        r = get_redis()
        key = make_key("session", session_id)
        data["updated_at"] = _now_ms()
        await r.set(key, json.dumps(data, ensure_ascii=False), ex=SESSION_TTL)

    async def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_call_id: Optional[str] = None,
        tool_calls: Optional[list] = None,
        user_id: Optional[str] = None,
        create_if_missing: bool = True,
        metadata: Optional[dict] = None,
    ) -> bool:
        """追加消息摘要到 session:{id}。

        Args:
            create_if_missing: 为 False 时，若 session 不存在则跳过写入并返回 False，
                避免“复活”已被用户删除的会话。默认为 True（向后兼容）。

        Returns:
            True 表示写入成功，False 表示 session 不存在且 create_if_missing=False。
        """
        if not create_if_missing:
            if not await self.exists(session_id):
                logger.debug(
                    "append_message skipped: session %s does not exist",
                    session_id,
                )
                return False
        data = await self._load(session_id)
        if "created_at" not in data:
            data["created_at"] = _now_ms()
        if user_id and data.get("user_id", "anonymous") == "anonymous":
            data["user_id"] = user_id
        record: dict = {
            "role": role,
            "content": content,
            "created_at": _now_iso(),
        }
        if tool_call_id is not None:
            record["tool_call_id"] = tool_call_id
        if tool_calls is not None:
            record["tool_calls"] = tool_calls
        if metadata:
            # Keep display metadata extensible without allowing callers to
            # overwrite the canonical message identity fields above.
            for key, value in metadata.items():
                if key not in {"role", "content", "created_at", "tool_call_id", "tool_calls"}:
                    record[key] = value
        data["messages"].append(record)
        await self._save(session_id, data)
        return True

    async def get_messages(
        self,
        session_id: str,
        limit: int = 20,
    ) -> list[BaseMessage]:
        """读取面向用户的对话历史。

        Redis 保存可展示的跨轮消息；SqliteSaver 保存 LangGraph 运行状态与恢复点，
        二者职责不同，Root Planner 会读取这里的最近消息作为语义上下文。
        """
        data = await self._load(session_id)
        records = data.get("messages", [])[-limit:]
        return [_record_to_message(r) for r in records]

    async def exists(self, session_id: str) -> bool:
        """检查 session 是否存在于 Redis。"""
        r = get_redis()
        key = make_key("session", session_id)
        return bool(await r.exists(key))

    async def clear_session(self, session_id: str) -> None:
        """清除会话。"""
        r = get_redis()
        key = make_key("session", session_id)
        await r.delete(key)

    # ------------------------------------------------------------------
    # Sprint 1 增量：list / create / rename / get_meta / list_messages
    # ------------------------------------------------------------------

    async def create(self, user_id: str = "anonymous") -> str:
        """创建一个新会话（空骨架），返回 session_id。"""
        r = get_redis()
        session_id = f"sess_{uuid.uuid4().hex[:16]}"
        key = make_key("session", session_id)
        now = _now_ms()
        skeleton = {
            "session_id": session_id,
            "title": "新会话",
            "messages": [],
            "created_at": now,
            "updated_at": now,
            "user_id": user_id,
        }
        await r.set(key, json.dumps(skeleton, ensure_ascii=False), ex=SESSION_TTL)
        return session_id

    async def rename(self, session_id: str, title: str) -> bool:
        """重命名会话。返回 True 表示成功，False 表示 session 不存在。"""
        data = await self._load(session_id)
        # 区分 session 不存在 vs 存在但空：_load 总返回非 None
        # 用 exists 二次确认
        r = get_redis()
        key = make_key("session", session_id)
        if not await r.exists(key):
            return False
        data["title"] = title.strip()[:200] or "新会话"
        await self._save(session_id, data)
        return True

    async def get_meta(self, session_id: str) -> Optional[SessionMeta]:
        """获取单个 session 的元信息。"""
        r = get_redis()
        key = make_key("session", session_id)
        if not await r.exists(key):
            return None
        data = await self._load(session_id)
        return _summarize_meta(session_id, data)

    async def get_user_id(self, session_id: str) -> str:
        """返回该 session 的归属 user_id，不存在时返回 'anonymous'。"""
        data = await self._load(session_id)
        return data.get("user_id", "anonymous")

    async def list_all(self, limit: int = 200, user_id: Optional[str] = None) -> List[SessionMeta]:
        """扫描所有 session，按 updated_at 降序返回。

        若提供 user_id，则仅返回该用户的会话。
        注意：此方法为 O(n) 全量扫描，会话量过大时应迁移至 Sorted Set 方案。
        硬上限：最多扫描 5000 个 key 后停止。
        """
        r = get_redis()
        items: list[SessionMeta] = []
        scanned = 0
        max_scan = 5000  # 硬上限，防止 50K+ session 时 OOM
        async for key in r.scan_iter(match=make_key("session", "*")):
            scanned += 1
            if scanned > max_scan:
                logger.warning(
                    "session list_all: reached scan limit %d, results truncated",
                    max_scan,
                )
                break
            raw = await r.get(key)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            # user_id 过滤
            if user_id and user_id != "anonymous":
                stored_user = data.get("user_id", "anonymous")
                if stored_user != "anonymous" and stored_user != user_id:
                    continue
            # 从 key 末段取 session_id（"session:{id}" → id）
            session_id = key.split(":", 1)[-1] if ":" in key else key
            items.append(_summarize_meta(session_id, data))
        items.sort(key=lambda m: m.updated_at, reverse=True)
        return items[:limit]

    async def list_messages(self, session_id: str) -> Optional[list]:
        """返回 session 的完整消息记录列表（dict，非 LangChain 对象）。

        返回 None 表示 session 不存在；否则返回 messages 数组。
        """
        r = get_redis()
        key = make_key("session", session_id)
        if not await r.exists(key):
            return None
        data = await self._load(session_id)
        return data.get("messages", [])

    async def save_full_messages(self, session_id: str, messages: list) -> bool:
        """覆盖式写入完整 messages（用于前端批量恢复/迁移）。

        返回 False 表示 session 不存在。
        """
        r = get_redis()
        key = make_key("session", session_id)
        if not await r.exists(key):
            return False
        data = await self._load(session_id)
        data["messages"] = messages
        await self._save(session_id, data)
        return True


async def save_turn(
    session_id: str,
    user_input: str,
    final_output: dict,
) -> None:
    """把一轮对话的用户输入与助手摘要写入会话。

    同时从 final_output.results 提取 tool_calls，供 session list 统计 tool_count。

    重要：如果 session 已被用户删除（Redis key 不存在），本函数不会重新创建它，
    避免已删除的会话“复活”。
    """
    store = SessionStore()
    # 先检查 session 是否仍存在，避免复活已删除的会话
    if not await store.exists(session_id):
        logger.debug(
            "save_turn skipped: session %s was deleted, not recreating",
            session_id,
        )
        return
    ok = await store.append_message(
        session_id, "user", user_input, create_if_missing=False,
    )
    if not ok:
        return
    assistant_content = (
        final_output.get("summary")
        or final_output.get("text")
        or ""
    )
    # 从 final_output.results 提取工具调用名称列表
    results = final_output.get("results") or []
    tool_calls = [
        {"tool_name": r["tool_name"]}
        for r in results
        if isinstance(r, dict) and r.get("tool_name")
    ]
    await store.append_message(
        session_id, "assistant", assistant_content,
        tool_calls=tool_calls if tool_calls else None,
        metadata={"execution_trace": final_output.get("execution_trace") or []},
        create_if_missing=False,
    )
