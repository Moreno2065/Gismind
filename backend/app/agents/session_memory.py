"""SessionMemory: persistent cross-turn memory for spatial context.

Inspired by PineFlow's session_memory.md approach. Stores accumulated
knowledge (locations, POI preferences, analysis patterns) in Redis,
keyed by session_id with a 30-day TTL.

The memory is injected into the sub-agent's system prompt via the
BEFORE_PROMPT_BUILD hook, giving the Planner awareness of prior
conversation context without replaying full message history.

All methods are async. Callers in worker threads must use
``_run_async`` (from ``app.agents.tool_execution``) to bridge sync/coroutine.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

from app.utils.redis import get_redis, make_key

logger = logging.getLogger(__name__)

# Redis TTL for session memory: 30 days
MEMORY_TTL = 30 * 24 * 60 * 60


class SessionMemory:
    """Cross-turn spatial memory bound to a session.

    Each memory entry is a dict with:
        - category: str (e.g. "location", "poi_preference", "analysis_pattern")
        - content: str (natural language fact)
        - created_at: float (epoch seconds)
    """

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._key = make_key("session_memory", session_id)

    def _get_redis(self):
        return get_redis()

    async def add_fact(self, category: str, content: str) -> None:
        """Append a memory fact to this session."""
        r = self._get_redis()
        entry = {
            "category": category,
            "content": content,
            "created_at": time.time(),
        }
        await r.rpush(self._key, json.dumps(entry, ensure_ascii=False))
        await r.expire(self._key, MEMORY_TTL)

    async def get_facts(
        self, category: Optional[str] = None, limit: int = 10,
    ) -> list[dict]:
        """Retrieve memory facts, optionally filtered by category."""
        r = self._get_redis()
        # redis.asyncio returns awaitables; do not wrap with to_thread.
        raw_items = await r.lrange(self._key, 0, -1)
        facts: list[dict] = []
        for raw in raw_items or []:
            try:
                entry = json.loads(raw)
                if category and entry.get("category") != category:
                    continue
                facts.append(entry)
            except (json.JSONDecodeError, TypeError):
                continue
        facts.sort(key=lambda f: f.get("created_at", 0), reverse=True)
        return facts[:limit]

    async def to_prompt_snippet(self) -> str:
        """Format memory as a prompt-injectable text snippet.

        Limited to ~600 tokens (~2100 chars at 3.5 chars/token).
        """
        from app.agents.context_budget import ContextBudget

        facts = await self.get_facts(limit=15)
        if not facts:
            return ""

        lines = []
        for f in facts:
            cat = f.get("category", "general")
            content = f.get("content", "")
            if content:
                lines.append(f"- [{cat}] {content}")

        snippet = "\n".join(lines)

        budget = ContextBudget()
        return budget.allocate("session_memory", snippet)

    async def extract_and_store(self, results: list[dict]) -> None:
        """Automatically extract knowledge from tool results and store.

        Extracts:
        - From geo results: resolved locations
        - From POI results: query patterns and area preferences
        - From geometer results: analysis patterns
        """
        for r in results:
            if not isinstance(r, dict):
                continue
            tool_name = r.get("tool_name", "")
            data = r.get("data") or {}
            if not isinstance(data, dict):
                continue

            # Geo results: store resolved locations
            if tool_name == "geo_code":
                location = data.get("location")
                addr = data.get("formatted_address", "")
                if location and addr:
                    await self.add_fact(
                        "location",
                        f"已解析地点: {addr}",
                    )

            # POI results: store query patterns
            elif tool_name == "query_poi":
                inner = data.get("result") if isinstance(data, dict) else None
                pois = []
                if isinstance(inner, dict):
                    pois = inner.get("pois", [])
                if not pois and isinstance(data.get("pois"), list):
                    pois = data["pois"]
                if pois:
                    count = len(pois)
                    names = ", ".join(
                        p.get("name", "?") for p in pois[:3]
                    ) if isinstance(pois, list) else ""
                    await self.add_fact(
                        "poi_preference",
                        f"查询过 {count} 个 POI: {names}",
                    )

            # Geometer results: store analysis patterns
            elif tool_name in ("buffer", "overlay", "voronoi", "isochrone"):
                features = data.get("features", [])
                if isinstance(features, list) and features:
                    await self.add_fact(
                        "analysis_pattern",
                        f"执行过 {tool_name} 分析，生成 {len(features)} 个要素",
                    )

    async def clear(self) -> None:
        """Clear all memory for this session."""
        r = self._get_redis()
        await r.delete(self._key)
