"""Deterministic transport server for component-level browser E2E tests.

The HTTP stack, Redis/SQLite persistence, LangGraph orchestration and GIS tools
are production code.  Only the external LLM transport is deterministic so the
same prompts exercise the same DAG on every developer machine.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from threading import Lock
from typing import Any

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))
# Settings uses a relative ``.env`` path, matching the documented
# ``cd backend`` startup command even when this script is launched by an
# external server harness from the repository root.
os.chdir(_BACKEND_ROOT)

import uvicorn
from langchain_core.messages import AIMessage

from app.agents.checkpointer import get_sqlite_checkpointer, reset_sqlite_checkpointer
from app.config import settings
from app.main import create_app
from app.utils.redis import create_redis_client, set_redis_instance


def _task(
    task_id: str,
    role: str,
    tool: str,
    goal: str,
    *,
    depends_on: list[str] | None = None,
    instruction_id: str = "i1",
) -> dict[str, Any]:
    return {
        "id": task_id,
        "agent_role": role,
        "tool_name": tool,
        "goal": goal,
        "depends_on": depends_on or [],
        "instruction_id": instruction_id,
    }


class ComponentPromptLLM:
    """Prompt-aware deterministic LangChain transport used only by E2E."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[str] = []
        self._lock = Lock()

    def bind_tools(self, tools: list[dict[str, Any]], **kwargs: Any) -> "ComponentPromptLLM":
        del tools, kwargs
        return self

    def invoke(self, messages: list[Any], *args: Any, **kwargs: Any) -> AIMessage:
        del args, kwargs
        system = str(getattr(messages[0], "content", "")) if messages else ""
        human = str(getattr(messages[-1], "content", "")) if messages else ""
        with self._lock:
            self.calls.append(self._channel(system))

        if "Root Dispatcher" in system:
            return AIMessage(content=json.dumps(self._root_plan(human), ensure_ascii=False))
        if "负责把工具执行结果总结" in system or "你是一个 GIS 助手 Gismind" in system:
            return AIMessage(content=json.dumps({"reply": self._summary(human)}, ensure_ascii=False))
        # Role knowledge may mention Verifier/Observer as prose.  The explicit
        # native-planner heading is therefore the higher-priority classifier.
        if "Native GIS Tool Planner" in system:
            return self._native_call(system, human)
        if "Verifier" in system:
            return AIMessage(content=json.dumps({
                "approved": True,
                "reason": "E2E deterministic verifier approved the real tool result",
                "refinement_hints": [],
                "confidence": 0.99,
            }, ensure_ascii=False))
        if "Judge" in system:
            return AIMessage(content=json.dumps({
                "decision": "FINISH",
                "reason": "E2E source artifact generated",
            }, ensure_ascii=False))
        if "Observer" in system:
            return AIMessage(content="工具执行完成，结果已进入数据平面。")
        # coder remains the one code-mode role.  It creates a source geometry;
        # buffer and rendering still execute through their real native tools.
        return AIMessage(content=(
            "feature = {'type': 'Feature', 'geometry': {'type': 'Point', "
            "'coordinates': [118.7874, 32.0206]}, "
            "'properties': {'name': '南京夫子庙', '_source': 'computed'}}\n"
            "__result__ = {'type': 'FeatureCollection', 'features': [feature]}"
        ))

    @staticmethod
    def _channel(system: str) -> str:
        if "Root Dispatcher" in system:
            return "root_planner"
        if "Native GIS Tool Planner" in system:
            return "native_planner"
        if "Verifier" in system:
            return "verifier"
        if "Judge" in system:
            return "judge"
        if "Observer" in system:
            return "observer"
        if "GIS 助手" in system:
            return "synthesis"
        return "code_planner"

    @staticmethod
    def _root_plan(user_input: str) -> dict[str, Any]:
        normalized = user_input.lower()
        if "已上传文件 id" in normalized or "上传" in normalized:
            match = re.search(r"file_[a-zA-Z0-9_-]+", user_input)
            file_id = match.group(0) if match else "missing_file_id"
            tasks = [
                _task("t1", "geometer", "data_io_read", f"读取上传图层 {file_id}"),
                _task("t2", "viz", "map_layer_build", "把上传 GeoJSON 图层显示在地图上", depends_on=["t1"]),
            ]
            instruction = "读取并显示上传的 GeoJSON"
        elif "咖啡" in user_input or "poi" in normalized:
            tasks = [
                _task("t1", "poi", "query_poi", "查询南京新街口 500 米内的咖啡店"),
                _task("t2", "viz", "map_layer_build", "把南京新街口周边咖啡店标在地图上", depends_on=["t1"]),
            ]
            instruction = "查询并显示南京新街口附近咖啡店"
        elif "缓冲" in user_input:
            tasks = [
                _task("t0", "coder", "code_executor", "生成南京夫子庙测试点要素"),
                _task("t1", "geometer", "buffer", "对南京夫子庙测试点做 500 米缓冲", depends_on=["t0"]),
                _task("t2", "viz", "map_layer_build", "把南京夫子庙 500 米缓冲区画在地图上", depends_on=["t1"]),
            ]
            instruction = "生成并显示南京夫子庙 500 米缓冲区"
        else:
            tasks = [_task("t1", "geo", "geo_code", "解析南京新街口坐标")]
            instruction = "查询南京新街口经纬度"

        return {
            "thinking": "component E2E deterministic workflow",
            "task_plan": {
                "instructions": [{"id": "i1", "text": instruction}],
                "tasks": tasks,
            },
            "need_clarification": None,
        }

    @staticmethod
    def _summary(human: str) -> str:
        if "POI" in human or "咖啡" in human:
            return "POI 查询完成，结果已标注在地图上。"
        if "空间分析" in human or "缓冲" in human:
            return "500 米缓冲区计算完成，结果已生成地图图层。"
        if "上传" in human or "图层" in human:
            return "上传文件已读取并生成地图图层。"
        return "南京新街口坐标查询完成。"

    @staticmethod
    def _native_call(system: str, human: str) -> AIMessage:
        match = re.search(r"The required tool is ([a-zA-Z0-9_]+)", system)
        if not match:
            raise AssertionError(f"Native planner did not declare a required tool: {system[:240]}")
        tool = match.group(1)
        refs = [int(value) for value in re.findall(r"^- (\d+):", system, re.MULTILINE)]
        latest_ref = max(refs) if refs else 0

        if tool == "geo_code":
            args = {"address": "南京新街口"}
        elif tool == "query_poi":
            args = {
                "query": "咖啡店",
                "location": [118.7781, 32.0439],
                "radius": 500,
                "dedup_threshold_m": 20,
            }
        elif tool == "data_io_read":
            file_match = re.search(r"file_[a-zA-Z0-9_-]+", human)
            if not file_match:
                raise AssertionError(f"No upload file_id in task goal: {human}")
            args = {"file_id": file_match.group(0)}
        elif tool == "buffer":
            args = {"geometry_from": latest_ref, "radius_m": 500}
        elif tool == "map_layer_build":
            args = {"geometry_from": latest_ref}
        else:
            raise AssertionError(f"Unexpected E2E native tool: {tool}")

        return AIMessage(
            content=f"E2E invoking {tool}",
            tool_calls=[{
                "name": tool,
                "args": args,
                "id": f"e2e_{tool}",
                "type": "tool_call",
            }],
        )


def main() -> None:
    redis_url = os.environ.get("GISMIND_TEST_REDIS_URL", "redis://localhost:6379/15")
    host = os.environ.get("GISMIND_E2E_HOST", "127.0.0.1")
    port = int(os.environ.get("GISMIND_E2E_PORT", "8000"))

    settings.REDIS_URL = redis_url
    set_redis_instance(None)

    async def _ping() -> None:
        client = create_redis_client(redis_url)
        try:
            await client.ping()
        finally:
            await client.aclose()

    try:
        asyncio.run(_ping())
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"Redis is required for component E2E: {redis_url!r}: {exc}") from exc

    reset_sqlite_checkpointer()
    db_dir = Path(tempfile.mkdtemp(prefix="gismind-component-e2e-"))
    checkpointer = get_sqlite_checkpointer(db_dir / "checkpoints.db")
    dispatcher_llm = ComponentPromptLLM("dispatcher")
    sub_agent_llm = ComponentPromptLLM("sub-agent")
    app = create_app(
        redis_client=None,
        checkpointer=checkpointer,
        dispatcher_llm=dispatcher_llm,
        sub_agent_llm=sub_agent_llm,
    )

    print(
        f"[e2e_component_server] redis={redis_url} "
        f"checkpointer={db_dir / 'checkpoints.db'} listening={host}:{port}",
        flush=True,
    )
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
