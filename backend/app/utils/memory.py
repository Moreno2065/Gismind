"""空间记忆存储：记住用户常用原点、最近查询位置等。

参考 docs/02_data_models.md §5.4：
- Redis key: memory:{session_id}
- 值: JSON（记忆条目数组 + 最近一次位置 + user_id）
- TTL: 30d

认证模型（临时方案）:
  user_id 存储在每个 session/memory 的 JSON 内部。
  通过 X-User-Id 请求头传入，默认 "anonymous"。
  生产环境必须替换为 JWT / OAuth2 并验证签名。
"""

import json
from datetime import datetime, timezone
from typing import Optional

from app.utils.redis import get_redis, make_key


MEMORY_TTL = 30 * 24 * 60 * 60  # 30 天


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_location(location: tuple) -> list[float]:
    """把 (lng, lat) 元组转换为 [lng, lat] 浮点列表。"""
    if not isinstance(location, (list, tuple)) or len(location) != 2:
        raise ValueError("location 必须是 (lng, lat) 形式的二元组")
    return [float(location[0]), float(location[1])]


class MemoryStore:
    """空间记忆存储：记住用户常用原点。"""

    async def _load(self, session_id: str) -> dict:
        r = get_redis()
        key = make_key("memory", session_id)
        raw = await r.get(key)
        if raw:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"session_id": session_id, "memories": [], "user_id": "anonymous"}
        return {"session_id": session_id, "memories": [], "user_id": "anonymous"}

    async def _save(self, session_id: str, data: dict) -> None:
        r = get_redis()
        key = make_key("memory", session_id)
        data["updated_at"] = _now_iso()
        await r.set(key, json.dumps(data, ensure_ascii=False), ex=MEMORY_TTL)

    async def remember_origin(
        self,
        session_id: str,
        label: str,
        location: tuple,
        crs: str = "GCJ02",
        user_id: Optional[str] = None,
    ) -> None:
        """记住一个原点。location=(lng,lat)。"""
        data = await self._load(session_id)
        if user_id and data.get("user_id", "anonymous") == "anonymous":
            data["user_id"] = user_id
        data["memories"].append({
            "key": "常用原点",
            "label": label,
            "location": _ensure_location(location),
            "crs": crs,
            "created_at": _now_iso(),
        })
        await self._save(session_id, data)

    async def get_memories(self, session_id: str) -> list:
        """读取 session_id 的所有记忆。"""
        data = await self._load(session_id)
        return data.get("memories", [])

    async def get_user_id(self, session_id: str) -> str:
        """返回该 session 记忆的归属 user_id，不存在时返回 'anonymous'。"""
        data = await self._load(session_id)
        return data.get("user_id", "anonymous")

    async def clear_memories(self, session_id: str) -> None:
        """清除记忆。"""
        r = get_redis()
        key = make_key("memory", session_id)
        await r.delete(key)

    async def get_last_location(self, session_id: str) -> Optional[tuple]:
        """获取最近一次查询位置。"""
        data = await self._load(session_id)
        loc = data.get("last_location")
        if loc and isinstance(loc, (list, tuple)) and len(loc) == 2:
            return (float(loc[0]), float(loc[1]))
        return None

    async def set_last_location(
        self,
        session_id: str,
        location: tuple,
        crs: str = "GCJ02",
        user_id: Optional[str] = None,
    ) -> None:
        """更新最近一次位置。"""
        data = await self._load(session_id)
        if user_id and data.get("user_id", "anonymous") == "anonymous":
            data["user_id"] = user_id
        data["last_location"] = _ensure_location(location)
        data["last_location_crs"] = crs
        await self._save(session_id, data)
