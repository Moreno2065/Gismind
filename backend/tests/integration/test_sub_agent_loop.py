"""Real-wiring integration tests for the sub-agent loop.

Only the LLM transport is deterministic. The compiled LangGraph, production
nodes, registry handlers, executor, verifier, judge, and event wiring are real.
"""

from __future__ import annotations

import pytest

from app.agents.build_sub_agent import build_sub_agent, run_sub_agent
from app.agents.events.current import (
    get_current_handler,
    reset_current_handler,
    set_current_handler,
)
from app.agents.registry import list_roles
from tests.support import DeterministicLLM


def _approve(reason: str = "结果正确") -> dict:
    return {
        "approved": True,
        "reason": reason,
        "refinement_hints": [],
        "confidence": 0.95,
    }


def _finish(reason: str = "任务完成") -> dict:
    return {"decision": "FINISH", "reason": reason}


@pytest.mark.integration
@pytest.mark.parametrize("role", list_roles())
def test_build_sub_agent_compiles_every_registered_role(role: str) -> None:
    app = build_sub_agent(role, run_id=f"compile-{role}")

    assert hasattr(app, "invoke")


@pytest.mark.integration
def test_real_registry_handler_runs_through_compiled_graph() -> None:
    llm = DeterministicLLM(
        planner=[
            {"name": "geo_transform", "args": {"operation": "out_of_china", "lng": 0, "lat": 0}}
        ],
        verifier=[_approve()],
        judge=[_finish()],
    )

    result = run_sub_agent(
        "geo",
        "判断零度经纬度是否在中国境外",
        run_id="real-registry",
        llm=llm,
    )

    assert result["should_stop"] is True
    assert result["session_vars"] == {}
    assert result["tool_results"][-1].status == "success"
    assert result["tool_results"][-1].data["out_of_china"] is True
    assert llm.calls == ["planner"]


@pytest.mark.integration
def test_native_success_is_not_replanned_by_advisory_verifier() -> None:
    llm = DeterministicLLM(
        planner=[
            {"name": "geo_transform", "args": {"operation": "out_of_china", "lng": 118, "lat": 32}},
            {"name": "geo_transform", "args": {"operation": "out_of_china", "lng": 0, "lat": 0}},
        ],
        verifier=[
            {
                "approved": False,
                "reason": "需要验证境外坐标",
                "refinement_hints": ["改用明确的境外坐标"],
                "confidence": 0.9,
            },
            _approve("境外坐标已验证"),
        ],
        judge=[_finish("修正后任务完成")],
    )

    result = run_sub_agent(
        "geo",
        "验证境内外坐标判断",
        run_id="real-refine",
        llm=llm,
    )

    assert result["should_stop"] is True
    assert result["iteration"] == 1
    assert len(result["refine_history"]) == 0
    assert result["session_vars"] == {}
    assert llm.calls == ["planner"]


@pytest.mark.integration
def test_run_sub_agent_scopes_event_handler_with_contextvar() -> None:
    outer_events: list[dict] = []
    inner_events: list[dict] = []
    outer_handler = outer_events.append
    outer_token = set_current_handler(outer_handler)
    llm = DeterministicLLM(
        planner=[{"name": "geo_transform", "args": {"operation": "out_of_china", "lng": 0, "lat": 0}}],
        verifier=[_approve()],
        judge=[_finish()],
    )

    try:
        result = run_sub_agent(
            "geo",
            "返回答案",
            run_id="real-events",
            on_event=inner_events.append,
            llm=llm,
        )

        assert result["should_stop"] is True
        assert get_current_handler() is outer_handler
    finally:
        reset_current_handler(outer_token)

    event_names = [event["event"] for event in inner_events]
    assert event_names == [
        "tool.call.start",
        "tool.call.complete",
    ]
    assert inner_events[0]["display_kind"] == "progress"
    assert inner_events[0]["tool_name"] == "geo_transform"
    assert inner_events[0]["params"]["operation"] == "out_of_china"
    assert inner_events[1]["display_kind"] == "workflow_step"
    assert inner_events[1]["status"] == "success"
    assert inner_events[1]["duration_ms"] >= 0
    assert "result" in inner_events[1]
    assert outer_events == []


@pytest.mark.integration
def test_native_success_does_not_depend_on_verifier_transport() -> None:
    llm = DeterministicLLM(
        planner=[{"name": "geo_transform", "args": {"operation": "out_of_china", "lng": 0, "lat": 0}}],
        verifier=[RuntimeError("transport unavailable")],
        judge=[_finish("确定性质量门后完成")],
    )

    result = run_sub_agent(
        "geo",
        "返回答案",
        run_id="real-verifier-fallback",
        llm=llm,
    )

    assert result["should_stop"] is True
    assert result["iteration"] == 1
    assert "verifier_output" not in result or result["verifier_output"] is None
    assert llm.calls == ["planner"]


@pytest.mark.integration
def test_coder_infinite_loop_is_sandbox_timeout() -> None:
    """The explicit coder escape hatch must remain killable by the sandbox."""
    import concurrent.futures

    from app.config import settings

    original_timeout = settings.APP_SANDBOX_TIMEOUT_S
    settings.APP_SANDBOX_TIMEOUT_S = 2
    llm = DeterministicLLM(
        planner=[
            "x = 0\n"
            "while True:\n"
            "    x += 1\n"
            "__result__ = {'x': x}"
        ],
        judge=[_finish()],
    )
    try:
        # Wall-clock guard: main-process exec of while True would hang forever.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(
                run_sub_agent,
                "coder",
                "运行一个无限循环",
                run_id="sandbox-timeout-coder",
                llm=llm,
            )
            result = fut.result(timeout=15)
    finally:
        settings.APP_SANDBOX_TIMEOUT_S = original_timeout

    tr = result["tool_results"][-1]
    assert tr.status == "error", f"expected error, got {tr.status}: {tr.message} data={tr.data}"
    assert tr.data["executor_type"] == "sandbox"
    err = tr.error_code or (tr.data or {}).get("error_code") or ""
    blob = f"{err} {tr.message or ''} {(tr.data or {}).get('traceback') or ''} {(tr.data or {}).get('stderr') or ''}".lower()
    assert "timeout" in blob or err == "SANDBOX_TIMEOUT", f"expected sandbox timeout, got err={err!r} blob={blob!r}"


@pytest.mark.integration
def test_coder_code_executor_reachable_via_real_registry_wiring() -> None:
    """coder role's code_executor must be reachable through real registry wiring.

    Sandbox tool stubs must not block the tool; LLM code that calls
    code_executor(code=...) must succeed via host-side registry RPC.
    """
    llm = DeterministicLLM(
        planner=[
            "out = code_executor(code=\"print(1+1)\")\n"
            "__result__ = {'stdout': out.get('stdout') if isinstance(out, dict) else str(out)}"
        ],
        verifier=[_approve()],
        judge=[_finish()],
    )

    result = run_sub_agent(
        "coder",
        "在沙箱中打印 1+1",
        run_id="coder-code-executor",
        llm=llm,
    )

    assert result["should_stop"] is True
    tr = result["tool_results"][-1]
    assert tr.status == "success", f"expected success, got {tr.status}: {tr.message} data={tr.data}"
    # code_executor returns data with stdout from nested sandbox
    session = result.get("session_vars") or {}
    stdout = session.get("stdout")
    if stdout is None and isinstance(tr.data, dict):
        # may be nested under result
        nested = tr.data.get("result") or {}
        stdout = nested.get("stdout") if isinstance(nested, dict) else None
    assert stdout is not None
    assert "2" in str(stdout)
