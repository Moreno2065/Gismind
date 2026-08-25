"""Deterministic LLM transport used by real-wiring integration tests."""

from __future__ import annotations

import json
from collections import deque
from threading import Lock
from typing import Any, Iterable

from langchain_core.messages import AIMessage


class DeterministicLLM:
    """Return scripted responses selected by the production system prompt."""

    def __init__(
        self,
        *,
        planner: Iterable[dict[str, Any] | str | AIMessage] = (),
        observer: Iterable[str | Exception] = (),
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
        self.bound_tools: list[dict[str, Any]] = []
        self._lock = Lock()

    def bind_tools(self, tools: list[dict[str, Any]], **kwargs: Any) -> "DeterministicLLM":
        del kwargs
        self.bound_tools = list(tools)
        return self

    def invoke(self, messages: list[Any], *args: Any, **kwargs: Any) -> AIMessage:
        del args, kwargs
        system = str(getattr(messages[0], "content", "")) if messages else ""
        channel = self._channel(system)
        with self._lock:
            self.calls.append(channel)
            if channel == "observer" and not self._responses[channel]:
                return AIMessage(content="工具执行完成。")
            if not self._responses[channel]:
                raise AssertionError(f"No deterministic {channel} response remains")
            response = self._responses[channel].popleft()
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
        if "Verifier" in system_prompt:
            return "verifier"
        if "Judge" in system_prompt:
            return "judge"
        if "Observer" in system_prompt:
            return "observer"
        # Root dispatcher system prompt ("Root Dispatcher") routes as planner.
        return "planner"
