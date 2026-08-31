"""E2E backend for Playwright awaiting-input wiring tests.

Starts a real FastAPI app with:
- real Redis (GISMIND_TEST_REDIS_URL or redis://localhost:6379/15)
- real temporary SqliteSaver
- DeterministicLLM transports for root planner + sub-agent nodes

No page.route / MSW / fake HTTP handlers. Production routes and graphs only.
LLM transport is the only injected seam (formal create_app kwargs).

Responses cycle so ``reuseExistingServer`` can serve multiple Playwright runs
without exhausting the scripted queue.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Any, Iterable

# Ensure backend/ is on sys.path when launched as a script.
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

import uvicorn
from langchain_core.messages import AIMessage

from app.agents.checkpointer import get_sqlite_checkpointer, reset_sqlite_checkpointer
from app.config import settings
from app.main import create_app
from app.utils.redis import create_redis_client, set_redis_instance


class CyclingDeterministicLLM:
    """DeterministicLLM variant that re-queues responses after each pop.

    Keeps the same channel routing as ``tests.support.DeterministicLLM`` so
    production nodes still see a real LangChain-style ``invoke`` transport.
    """

    def __init__(
        self,
        *,
        planner: Iterable[dict[str, Any] | str | AIMessage] = (),
        observer: Iterable[str] = (),
        verifier: Iterable[dict[str, Any] | str | Exception] = (),
        judge: Iterable[dict[str, Any] | str | Exception] = (),
    ) -> None:
        self._responses = {
            "planner": deque(planner),
            "observer": deque(observer),
            "verifier": deque(verifier),
            "judge": deque(judge),
        }
        self.calls: list[str] = []
        self._lock = Lock()

    def bind_tools(self, tools: list[dict[str, Any]], **kwargs: Any) -> "CyclingDeterministicLLM":
        del tools, kwargs
        return self

    def invoke(self, messages: list[Any], *args: Any, **kwargs: Any) -> AIMessage:
        del args, kwargs
        system = str(getattr(messages[0], "content", "")) if messages else ""
        channel = self._channel(system)
        with self._lock:
            self.calls.append(channel)
            if not self._responses[channel]:
                raise AssertionError(f"No deterministic {channel} response remains")
            response = self._responses[channel].popleft()
            # Cycle for multi-run e2e / reuseExistingServer.
            self._responses[channel].append(response)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, AIMessage):
            return response
        if channel == "planner" and isinstance(response, dict) and response.get("name"):
            return AIMessage(
                content=str(response.get("content") or ""),
                tool_calls=[{
                    "name": str(response["name"]),
                    "args": dict(response.get("args") or {}),
                    "id": str(response.get("id") or f"call_{len(self.calls)}"),
                    "type": "tool_call",
                }],
            )
        if isinstance(response, dict):
            response = json.dumps(response, ensure_ascii=False)
        return AIMessage(content=response)

    @staticmethod
    def _channel(system_prompt: str) -> str:
        # The normal-role native planner embeds role knowledge that can mention
        # the legacy Judge in prose.  Route on its explicit contract heading
        # before the broad legacy channel markers below.
        if "Native GIS Tool Planner" in system_prompt:
            return "planner"
        if "Verifier" in system_prompt:
            return "verifier"
        if "Judge" in system_prompt:
            return "judge"
        if "Observer" in system_prompt:
            return "observer"
        return "planner"


def _plan(
    *,
    goal: str,
    agent_role: str,
    tool_name: str,
    tool_args: dict[str, Any] | None = None,
    depends_on: list[str] | None = None,
    task_id: str = "t1",
) -> str:
    """Build one valid Root workflow response for deterministic browser tests."""
    return json.dumps(
        {
            "task_plan": {
                "instructions": [{"id": "i1", "text": goal}],
                "tasks": [
                    {
                        "id": task_id,
                        "agent_role": agent_role,
                        "tool_name": tool_name,
                        "goal": goal,
                        "depends_on": list(depends_on or []),
                        "instruction_id": "i1",
                        "tool_args": dict(tool_args or {}),
                        "expected_artifacts": [],
                    }
                ]
            }
        },
        ensure_ascii=False,
    )


def _workflow_plan(goal: str, tasks: list[dict[str, Any]]) -> str:
    """Build a multi-step valid Root workflow response for browser E2E."""
    return json.dumps(
        {
            "task_plan": {
                "instructions": [{"id": "i1", "text": goal}],
                "tasks": [
                    {
                        "id": str(task["id"]),
                        "agent_role": str(task["agent_role"]),
                        "tool_name": str(task["tool_name"]),
                        "goal": str(task["goal"]),
                        "depends_on": list(task.get("depends_on") or []),
                        "instruction_id": "i1",
                        "tool_args": dict(task.get("tool_args") or {}),
                        "expected_artifacts": [],
                    }
                    for task in tasks
                ],
            }
        },
        ensure_ascii=False,
    )


def _uploaded_file_ids(text: str) -> list[str]:
    return re.findall(r"\bfile_[A-Za-z0-9_-]+\b", text)


class ScenarioRootLLM:
    """Intent-scoped Root planner used only by real browser wiring tests.

    This is a deterministic model transport, not an HTTP/SSE or tool mock:
    FastAPI, the Dispatcher DAG, all native sub-agents, Redis and SQLite stay
    on their production paths.  Each recognized marker is deliberately an
    explicit test contract, which keeps this stable suite separate from live
    Root-LLM smoke evaluation.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def invoke(self, messages: list[Any], *args: Any, **kwargs: Any) -> AIMessage:
        del args, kwargs
        prompt = str(getattr(messages[-1], "content", "") or "") if messages else ""
        self.calls.append(prompt)
        if "用户补充参数" in prompt:
            return AIMessage(content=_plan(
                goal="检查用户补充后的坐标参数",
                agent_role="geo",
                tool_name="geo_transform",
            ))
        if "栅格重分类" in prompt:
            return AIMessage(content=_plan(
                goal="按用户指定的分级阈值重分类栅格",
                agent_role="geometer",
                tool_name="reclassify_raster",
            ))
        if "E2E_POI" in prompt:
            return AIMessage(content=_workflow_plan(
                "查询南京新街口附近咖啡店并在地图上展示",
                [
                    {
                        "id": "t1", "agent_role": "geo", "tool_name": "geo_code",
                        "goal": "解析南京新街口", "depends_on": [],
                    },
                    {
                        "id": "t2", "agent_role": "poi", "tool_name": "query_poi",
                        "goal": "查询南京新街口 500 米内咖啡店", "depends_on": ["t1"],
                    },
                ],
            ))
        if "E2E_EMPTY_MAP" in prompt:
            return AIMessage(content=_plan(
                goal="判断给定坐标是否在中国范围内",
                agent_role="geo",
                tool_name="geo_transform",
            ))
        file_ids = _uploaded_file_ids(prompt)
        if "E2E_EXPIRED_UPLOAD" in prompt and file_ids:
            return AIMessage(content=_plan(
                goal="读取一个已失效的上传文件以返回正式错误状态",
                agent_role="geometer",
                tool_name="data_io_read",
            ))
        if "E2E_PARTIAL" in prompt and file_ids:
            return AIMessage(content=_workflow_plan(
                "读取上传点图层后计算坡度，以验证 partial 终态不会伪装成功",
                [
                    {
                        "id": "t1", "agent_role": "geometer", "tool_name": "data_io_read",
                        "goal": "读取上传点图层", "depends_on": [],
                    },
                    {
                        "id": "t2", "agent_role": "geometer", "tool_name": "slope",
                        "goal": "对点图层计算坡度（预期正式失败）", "depends_on": ["t1"],
                    },
                ],
            ))
        if "E2E_UPLOAD_ONE" in prompt and file_ids:
            return AIMessage(content=_workflow_plan(
                "读取上传 GeoJSON 并建立地图图层",
                [
                    {
                        "id": "t1", "agent_role": "geometer", "tool_name": "data_io_read",
                        "goal": "读取第一个上传文件", "depends_on": [],
                    },
                    {
                        "id": "t2", "agent_role": "viz", "tool_name": "map_layer_build",
                        "goal": "渲染上传图层", "depends_on": ["t1"],
                    },
                ],
            ))
        if "E2E_UPLOAD_TWO" in prompt and len(file_ids) >= 2:
            return AIMessage(content=_workflow_plan(
                "读取两个上传图层，计算相交范围并渲染",
                [
                    {
                        "id": "t1", "agent_role": "geometer", "tool_name": "data_io_read",
                        "goal": "读取第一个上传文件", "depends_on": [],
                    },
                    {
                        "id": "t2", "agent_role": "geometer", "tool_name": "data_io_read",
                        "goal": "读取第二个上传文件", "depends_on": [],
                    },
                    {
                        "id": "t3", "agent_role": "geometer", "tool_name": "overlay",
                        "goal": "计算两个图层的交集", "depends_on": ["t1", "t2"],
                    },
                    {
                        "id": "t4", "agent_role": "viz", "tool_name": "map_layer_build",
                        "goal": "渲染交集结果", "depends_on": ["t3"],
                    },
                ],
            ))
        if "E2E_UPLOAD_RASTER" in prompt and file_ids:
            return AIMessage(content=_workflow_plan(
                "读取上传 GeoTIFF，计算坡度并渲染栅格",
                [
                    {
                        "id": "t1", "agent_role": "geometer", "tool_name": "data_io_read",
                        "goal": "读取上传栅格", "depends_on": [],
                    },
                    {
                        "id": "t2", "agent_role": "geometer", "tool_name": "slope",
                        "goal": "计算上传高程栅格的坡度", "depends_on": ["t1"],
                    },
                    {
                        "id": "t3", "agent_role": "viz", "tool_name": "map_layer_build",
                        "goal": "渲染坡度栅格", "depends_on": ["t2"],
                    },
                ],
            ))
        raise AssertionError(f"Unrecognized deterministic E2E prompt: {prompt[:160]!r}")


class ScenarioSubAgentLLM:
    """Return one native function call for each Root-owned E2E workflow step."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def bind_tools(self, tools: list[dict[str, Any]], **kwargs: Any) -> "ScenarioSubAgentLLM":
        del tools, kwargs
        return self

    def invoke(self, messages: list[Any], *args: Any, **kwargs: Any) -> AIMessage:
        del args, kwargs
        system = str(getattr(messages[0], "content", "") or "") if messages else ""
        required_match = re.search(r"The required tool is ([A-Za-z0-9_]+)\.", system)
        if not required_match:
            raise AssertionError("E2E sub-agent received a non-native planner prompt")
        tool_name = required_match.group(1)
        self.calls.append(tool_name)
        args_by_tool: dict[str, dict[str, Any]] = {
            # Missing values is intentionally a real Preflight ask_user case.
            "reclassify_raster": {"src_path": "pending-dem.tif", "bins": [10, 20]},
            "geo_transform": {"operation": "out_of_china", "lng": 0, "lat": 0},
            "geo_code": {"address": "南京新街口"},
            "query_poi": {"query": "咖啡店", "location_from": 0, "radius": 500},
            # Root-owned tool_args overwrite this placeholder with each browser-uploaded id.
            "data_io_read": {"file_id": "file_root_override"},
            "overlay": {"geometry_a_from": 0, "geometry_b_from": 1, "how": "intersection"},
            "slope": {"dem_from": 0},
            "map_layer_build": {"geometry_from": 0},
        }
        if tool_name not in args_by_tool:
            raise AssertionError(f"No deterministic native call for {tool_name!r}")
        return AIMessage(
            content="",
            tool_calls=[{
                "name": tool_name,
                "args": args_by_tool[tool_name],
                "id": f"e2e_{len(self.calls)}",
                "type": "tool_call",
            }],
        )


def _build_llms() -> tuple[CyclingDeterministicLLM, CyclingDeterministicLLM]:
    return ScenarioRootLLM(), ScenarioSubAgentLLM()


def main() -> None:
    redis_url = os.environ.get("GISMIND_TEST_REDIS_URL", "redis://localhost:6379/15")
    host = os.environ.get("GISMIND_E2E_HOST", "127.0.0.1")
    port = int(os.environ.get("GISMIND_E2E_PORT", "8000"))

    settings.REDIS_URL = redis_url
    # Browser tests exercise real expiration with a bounded wait.  Normal
    # production runs retain their configured 24-hour value.
    settings.UPLOAD_TTL_S = int(os.environ.get("GISMIND_E2E_UPLOAD_TTL_S", "5"))
    set_redis_instance(None)

    import asyncio

    async def _ping() -> None:
        client = create_redis_client(redis_url)
        try:
            await client.ping()
        finally:
            await client.aclose()

    try:
        asyncio.run(_ping())
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"Real Redis required for e2e server but connection failed: "
            f"{redis_url!r} ({type(exc).__name__}: {exc}). "
            f"Set GISMIND_TEST_REDIS_URL or start Redis. No fakeredis fallback."
        ) from exc

    reset_sqlite_checkpointer()
    db_dir = Path(tempfile.mkdtemp(prefix="gismind-e2e-cp-"))
    checkpointer = get_sqlite_checkpointer(db_dir / "checkpoints.db")

    dispatcher_llm, sub_agent_llm = _build_llms()
    app = create_app(
        redis_client=None,
        checkpointer=checkpointer,
        dispatcher_llm=dispatcher_llm,
        sub_agent_llm=sub_agent_llm,
    )

    print(
        f"[e2e_awaiting_server] redis={redis_url} "
        f"checkpointer={db_dir / 'checkpoints.db'} "
        f"listening={host}:{port}",
        flush=True,
    )
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
