"""LangGraph TypedDict states for the sub-agent orchestration graph (Phase 1 / Task 1.3).

Defines:
- SubAgentState  – the state each individual sub-agent node operates on.
- AgentRootState – the top-level orchestrator state that holds task plans and dispatch
                   tables.
"""

from __future__ import annotations

from typing import Annotated, Any, Optional, Sequence

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class SubAgentState(TypedDict, total=False):
    """Per-run state for a single sub-agent invocation."""

    # --- inherited from root (shared) ---
    messages: Annotated[Sequence[BaseMessage], add_messages]
    iteration: int
    tool_results: list
    planner_output: Any
    final_output: dict
    should_stop: bool
    user_input: str
    termination_cause: str
    session_vars: dict  # 跨 step 持久化变量（LLM 通过 __result__ = {...} 写入）

    # --- sub-agent specific ---
    agent_role: str
    required_tool_name: Optional[str]  # Root DAG 为当前原子步骤指定的唯一工具
    required_tool_args: dict[str, Any]  # Root-owned exact arguments for the atomic tool
    parent_task_id: Optional[str]
    run_id: str
    session_id: str  # session ID for session memory
    refine_history: list
    verifier_output: Optional[dict]
    max_iterations: int
    verifier_required: bool
    loaded_skills: Optional[dict]  # {"skill_name": "content text"} 由 load_skill 写入
    enabled_toolkits: list[str]
    duplicate_actions: list  # DuplicateActionGuard 动作历史记录
    pending_task: Optional[dict]  # preflight ask_user 挂起时的 PendingTask dict


class AgentRootState(TypedDict, total=False):
    """Top-level orchestrator state that drives the sub-agent dispatch loop."""

    messages: Annotated[Sequence[BaseMessage], add_messages]
    iteration: int
    should_stop: bool
    final_output: dict
    user_input: str
    session_vars: dict  # 跨 sub-agent 共享的持久化变量

    task_plan: dict
    # Provenance of the plan that entered the dispatcher DAG.  ``run.plan``
    # exposes this so deterministic fallback work is never misreported as
    # Root LLM planning.
    planner_source: str  # guardrail | root_llm | fallback
    dispatched: dict[str, list[str]]
    dispatcher_events: list   # list of {event, data} dicts for SSE emission
    root_verifier_output: Optional[dict]
    root_iteration: int
    termination_cause: str

    # trace_id / session_id 贯穿整个 React Loop，供内部节点日志关联
    trace_id: str
    session_id: str
    run_id: str  # Run 控制器 ID，用于暂停/取消
    upload_file_ids: list[str]

    # preflight ask_user / confirm_overwrite 挂起时的 PendingTask dict
    # 由 planner_router (resume 入口) / judge (sub-agent) 写入；resume 后清空
    pending_task: Optional[dict]

    # Normalized user answer patch from /resume (e.g. {"distance": 500.0}).
    # planner_router merges original_request + resume_patch into user_input
    # and re-plans via the normal LLM path; cleared after replan.
    resume_patch: dict[str, object]

    # Completed / partial sub-agent outcomes keyed by task_id.
    # Resume re-dispatch skips task_ids already present with success status.
    sub_results: dict[str, list]

    # Note: on_event handler is now passed via contextvar (events/current.py)
    # instead of through LangGraph state — callables cannot be serialised
    # by SqliteSaver and would cause checkpoint failures.


def new_root_state(
    user_input: str,
    trace_id: str = "",
    session_id: str = "",
    run_id: str = "",
    upload_file_ids: list[str] | None = None,
) -> AgentRootState:
    """Return a fresh AgentRootState with sensible defaults."""
    return {
        "messages": [],
        "iteration": 0,
        "should_stop": False,
        "final_output": {},
        "user_input": user_input,
        "task_plan": {"tasks": []},
        "planner_source": "",
        "dispatched": {},
        "root_verifier_output": None,
        "dispatcher_events": [],
        "root_iteration": 0,
        "termination_cause": "",
        "session_vars": {
            "upload_file_ids": list(upload_file_ids or []),
            **{
                f"upload_{idx}": {"file_id": file_id}
                for idx, file_id in enumerate(upload_file_ids or [])
            },
        },
        "trace_id": trace_id,
        "session_id": session_id,
        "run_id": run_id,
        "upload_file_ids": list(upload_file_ids or []),
        "pending_task": None,
        "resume_patch": {},
        "sub_results": {},
    }
