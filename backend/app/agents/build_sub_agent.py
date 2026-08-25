"""把 sub-agent spec 编译为 LangGraph 实例（schema-first + code fallback）。

普通 GIS 角色使用模型原生 tool calling；只有 coder 使用 Python code-mode。

结构（verifier_required=True）:

  planner → [tool call/code → tools | end → END]
  tools → observer → verifier → [approved → judge | refused → refine_router]
  refine_router → [should_stop? → END | approved? → judge | 回 planner]
  judge → [should_stop? → END | 回 planner]

结构（verifier_required=False）:

  planner → [tool call/code → tools | end → END]
  tools → observer → judge → [should_stop? → END | 回 planner]
"""

from __future__ import annotations

import logging
import os
import re
from functools import partial
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph

from app.agents import tool_execution
from app.agents.planner_factory import (
    build_code_mode_prompt,
    create_code_mode_llm,
)
from app.agents.native_tool_mode import (
    build_native_tool_schemas,
    native_reference_prompt,
)
from app.agents.registry import get_spec
from app.agents.state import SubAgentState
from app.agents.refine_router import refine_router
from app.agents.verifier_node import verifier_node
from app.models.schemas import PlannerOutput

logger = logging.getLogger(__name__)


# ============================================================
# Role 知识加载（从 prompts/{role}.md 提取领域知识）
# ============================================================

def _load_role_knowledge(agent_role: str) -> str:
    """从 prompts/{role}.md 加载角色特定領域知識，剝離 JSON 輸出格式部分。

    Returns:
        純領域知識文本（無輸出格式限制、無 JSON 範例）。
    """
    prompt_dir = os.path.join(os.path.dirname(__file__), "prompts")
    prompt_path = os.path.join(prompt_dir, f"{agent_role}.md")
    if not os.path.exists(prompt_path):
        return f"你是一个 {agent_role}。"

    with open(prompt_path, "r", encoding="utf-8") as f:
        content = f.read()

    lines = content.splitlines()
    result_lines = []
    skip_output_section = False
    for line in lines:
        # 跳過「輸出格式」及其後直到下一個 ## 標題
        if line.strip().startswith("## 输出格式") or line.strip().startswith("## 輸出格式"):
            skip_output_section = True
            continue
        if skip_output_section and line.strip().startswith("## "):
            skip_output_section = False
        if skip_output_section:
            continue
        result_lines.append(line)

    result = "\n".join(result_lines)
    # 移除 JSON code block
    result = re.sub(r'```json\s*.*?```', '', result, flags=re.DOTALL)
    # 移除 tool_call JSON 參數範例（``` 中的非 json 塊）
    result = re.sub(r'```\s*\n\{.*?\n```', '', result, flags=re.DOTALL)
    return result.strip()


# ============================================================
# Code-mode planner node（输出 Python 代码）
# ============================================================

def _planner_node(state: SubAgentState, *, llm: Any | None = None) -> dict:
    """Code-mode planner：调用 LLM 生成 Python 代码。

    流程：
    1. 加载角色领域知识（prompts/{role}.md 的非 JSON 部分）
    2. 加载 code-mode tool prompt（build_code_mode_prompt）
    3. 合并為完整 system prompt
    4. 用 create_code_mode_llm（无 response_format 约束）
    5. 从 LLM 响应中提取 Python 代码（剥 fence）
    6. 存入 planner_output.code
    7. 空代码重试（最多 3 次）
    """
    agent_role = state.get("agent_role", "geo")
    role_knowledge = _load_role_knowledge(agent_role)
    tool_prompt = build_code_mode_prompt(agent_role)

    # 合併：領域知識在前，代碼規則在後
    # --- Context budget: trim prompt sections to fit token budget ---
    from app.agents.context_budget import ContextBudget, trim_messages

    budget = ContextBudget()
    role_knowledge = budget.allocate("role_knowledge", role_knowledge)
    tool_prompt = budget.allocate("tool_prompt", tool_prompt)

    # --- Session memory: inject cross-turn spatial knowledge ---
    session_id = state.get("session_id", "")
    memory_snippet = ""
    if session_id:
        try:
            from app.agents.session_memory import SessionMemory
            mem = SessionMemory(session_id)
            # to_prompt_snippet is async; run in thread-local event loop
            import asyncio as _asyncio
            memory_snippet = _asyncio.run(mem.to_prompt_snippet())
        except Exception:
            logger.warning(
                "SessionMemory.to_prompt_snippet failed session=%s",
                session_id, exc_info=True,
            )

    combined_prompt = f"{role_knowledge}\n\n{tool_prompt}"

    # --- ToolKit catalog + loaded skills injection ---
    try:
        from app.agents.planner_factory import inject_toolkit_and_skills
        from app.agents.toolkit.registry import ToolKitRegistry

        tk_reg = ToolKitRegistry()
        session_vars = state.get("session_vars") or {}
        active_names = list(
            state.get("enabled_toolkits")
            or session_vars.get("__enabled_toolkits__")
            or tk_reg.default_active()
        )
        tk_catalog = {}
        for tk_name in active_names:
            tk_def = tk_reg.get(tk_name)
            if tk_def:
                tk_catalog[tk_name] = {
                    "description": tk_def.description,
                    "tools": list(tk_def.tools),
                }

        loaded_skills = (
            state.get("loaded_skills")
            or session_vars.get("__loaded_skills__")
            or {}
        )
        combined_prompt = inject_toolkit_and_skills(
            combined_prompt,
            toolkit_catalog=tk_catalog if tk_catalog else None,
            loaded_skills=loaded_skills if loaded_skills else None,
        )
    except Exception:
        pass  # toolkit/skill modules not ready

    if memory_snippet:
        combined_prompt = f"{combined_prompt}\n\n## Session Memory\n{memory_snippet}"

    state_messages = state.get("messages") or []
    history = [m for m in state_messages if getattr(m, "type", "") != "system"]
    # Trim history to fit within history_steps budget
    history = trim_messages(history, budget.section_limit("history_steps"))
    messages = [SystemMessage(content=combined_prompt)]
    if history:
        messages.extend(history)
    messages.append(HumanMessage(content=str(state.get("user_input", ""))))

    llm = llm or create_code_mode_llm()
    code = ""
    raw = ""
    max_retries = 3
    for attempt in range(max_retries):
        response = llm.invoke(messages)
        raw = response.content if hasattr(response, "content") else str(response)

        # 剥 fence 提取纯代码
        code = raw.strip()
        fences = re.findall(r"```(?:python|py)?\s*\n(.*?)\n```", code, re.DOTALL)
        if fences:
            code = fences[-1].strip()

        if code:
            break
        logger.warning(
            "code-mode planner empty code attempt %d/%d (role=%s)",
            attempt + 1, max_retries, agent_role,
        )

    if not code:
        logger.error(
            "code-mode planner failed to produce code after %d attempts (role=%s)",
            max_retries, agent_role,
        )

    iteration = state.get("iteration", 0) + 1

    planner_output = PlannerOutput(
        thinking=raw[:200],
        code=code,
    )

    # --- Event emission: code.generation ---
    try:
        from app.agents.events.current import get_current_handler
        from app.agents.events import emit_event
        on_event = get_current_handler()
        if on_event and code:
            emit_event(on_event, "code.generation", "生成分析代码",
                       code=code[:2000], role=agent_role,
                       task_id=state.get("parent_task_id") or "",
                       agent_role=agent_role,
                       iteration=state.get("iteration", 0) + 1,
                       max_iterations=state.get("max_iterations", 6))
    except Exception:
        pass

    return {
        "planner_output": planner_output,
        "iteration": iteration,
        "messages": [AIMessage(content=raw)],
    }


def _native_planner_node(state: SubAgentState, *, llm: Any | None = None) -> dict:
    """Fill the JSON Schema call for one atomic Root WorkflowPlan step."""
    agent_role = state.get("agent_role", "geo")
    spec = get_spec(agent_role)
    role_knowledge = _load_role_knowledge(agent_role)
    required_tool_name = state.get("required_tool_name")
    if required_tool_name and required_tool_name not in spec.tool_names:
        raise ValueError(
            f"Root workflow assigned tool {required_tool_name!r} to incompatible "
            f"role {agent_role!r}"
        )
    visible_tools = [required_tool_name] if required_tool_name else spec.tool_names
    schemas = build_native_tool_schemas(visible_tools)
    required_tool_args = state.get("required_tool_args") or {}
    if not isinstance(required_tool_args, dict):
        required_tool_args = {}
    reference_catalog = native_reference_prompt(dict(state))
    system_prompt = (
        f"{role_knowledge}\n\n"
        "## Native GIS Tool Planner\n"
        "This is one atomic step from an already validated Root WorkflowPlan. "
        "Use exactly one of the provided native function tools for this step. "
        "Do not output Python and do not write a JSON tool_calls wrapper. "
        "Fill only parameters declared by the selected tool schema. "
        "Do not add later actions; the Root executor owns dependencies and completion. "
        + (f"The required tool is {required_tool_name}.\n\n" if required_tool_name else "\n\n")
        + f"{reference_catalog}"
    )

    history = [
        message
        for message in (state.get("messages") or [])
        if getattr(message, "type", "") != "system"
    ]
    messages = [SystemMessage(content=system_prompt)]
    messages.extend(history)
    messages.append(HumanMessage(content=str(state.get("user_input", ""))))

    llm = llm or create_code_mode_llm()
    bound = llm.bind_tools(schemas, tool_choice="auto")
    response = None
    normalized_calls: list[dict[str, Any]] = []
    for attempt in range(2):
        response = bound.invoke(messages)
        raw_calls = list(getattr(response, "tool_calls", None) or [])
        if len(raw_calls) == 1:
            raw_call = raw_calls[0]
            candidate = {
                "id": str(raw_call.get("id") or f"native_{state.get('iteration', 0) + 1}"),
                "name": str(raw_call.get("name") or ""),
                "args": dict(raw_call.get("args") or {}),
            }
            if not required_tool_name or candidate["name"] == required_tool_name:
                # The Root plan owns exact constraints extracted from a
                # documented request. References still come from the native
                # planner, while these values override any omitted or altered
                # model argument before schema validation/execution.
                candidate["args"] = {**candidate["args"], **required_tool_args}
                normalized_calls = [candidate]
                break
        messages.append(HumanMessage(content=(
            "Your previous response did not contain the one required native tool call. "
            f"Return exactly one {required_tool_name or 'allowed'} tool call now."
        )))
        logger.warning(
            "native planner invalid tool call count=%d attempt=%d role=%s required=%s returned=%s",
            len(raw_calls), attempt + 1, agent_role,
            required_tool_name,
            [str(item.get("name") or "") for item in raw_calls if isinstance(item, dict)],
        )
    if not normalized_calls or response is None:
        raise RuntimeError(f"native planner failed to return exactly one tool call for {agent_role}")

    iteration = state.get("iteration", 0) + 1
    planner_output = PlannerOutput(
        thinking=str(getattr(response, "content", "") or "")[:500],
        tool_calls=normalized_calls,
    )
    return {
        "planner_output": planner_output,
        "iteration": iteration,
        "messages": [response],
    }


def _route_after_planner(state: SubAgentState) -> str:
    """Planner 后路由：有 native tool call 或 code → tools，否则 END。"""
    planner_output = state.get("planner_output")
    if not planner_output:
        return "end"
    if getattr(planner_output, "code", None):
        return "tools"
    if getattr(planner_output, "tool_calls", None):
        return "tools"
    return "end"


def _route_after_judge_sub(state: SubAgentState) -> str:
    """Judge 后路由：should_stop → END，否则 → planner。"""
    return "end" if state.get("should_stop") else "planner"


def _route_after_verifier_sub(state: SubAgentState) -> str:
    """Verifier 后路由：approved → judge，否则 → refine_router。"""
    verifier = state.get("verifier_output") or {}
    if verifier.get("approved"):
        return "judge"
    return "refine_router"


def _route_after_refine(state: SubAgentState) -> str:
    """Refine router 后路由：pending → judge；should_stop → END；approved → judge；否则 → planner。"""
    if state.get("pending_task"):
        return "judge"
    if state.get("should_stop"):
        return "end"
    verifier = state.get("verifier_output") or {}
    if verifier.get("approved"):
        return "judge"
    return "planner"


def _route_after_verifier_native(state: SubAgentState) -> str:
    """Every native result must pass through the deterministic step finalizer.

    The verifier may annotate quality, but it cannot own retry/termination for
    an atomic executor result.  Skipping finalize was the source of unbounded
    duplicate actions and ignored iteration limits.
    """
    return "finalize"


def _route_after_refine_native(state: SubAgentState) -> str:
    """Retry only the current failed Root step; never invoke Judge."""
    if state.get("should_stop"):
        return "end"
    if state.get("pending_task"):
        return "finalize"
    verifier = state.get("verifier_output") or {}
    return "finalize" if verifier.get("approved") else "planner"


def _route_after_native_finalize(state: SubAgentState) -> str:
    return "end" if state.get("should_stop") else "planner"


def build_sub_agent(
    agent_role: str,
    run_id: str,
    parent_task_id: str | None = None,
    checkpointer=None,
    llm: Any | None = None,
    interrupt_before: list[str] | None = None,
) -> Any:
    """编译一个 schema-first、coder 可回退 code-mode 的 sub-agent 图。

    普通角色输出一个原生工具调用；coder 输出 Python 并交给沙箱执行。

    Args:
        agent_role: registry 中注册的角色名（如 "geo", "poi", "geometer"）。
        run_id: 当前运行的唯一 ID。
        parent_task_id: 父 task ID（用于追踪）。
        checkpointer: 可选 LangGraph checkpointer (e.g. SqliteSaver)。
        llm: 可选 LLM transport；不放入 graph state，生产默认使用配置工厂。
        interrupt_before: 可选，原样传给 workflow.compile()；默认 None。

    Returns:
        compiled StateGraph，可 invoke。

    Raises:
        KeyError: agent_role 未在 registry 中注册。
    """
    spec = get_spec(agent_role)
    workflow = StateGraph(SubAgentState)

    # --- 节点注册（LLM transport 通过节点闭包注入，不进入可持久化 state）---
    planner_node = _planner_node if spec.execution_mode == "code" else _native_planner_node
    planner = partial(planner_node, llm=llm) if llm is not None else planner_node
    observer = (
        partial(tool_execution.observer_node, llm=llm)
        if llm is not None else tool_execution.observer_node
    )
    workflow.add_node("planner", planner)
    tools_node = (
        tool_execution.code_executor_node
        if spec.execution_mode == "code"
        else tool_execution.native_tool_executor_node
    )
    workflow.add_node("tools", tools_node)
    workflow.add_node("observer", observer)

    # --- 入口 ---
    workflow.set_entry_point("planner")

    # --- planner 后条件路由（有 code → tools，否则 END）---
    workflow.add_conditional_edges(
        "planner",
        _route_after_planner,
        {"end": END, "tools": "tools"},
    )

    if spec.execution_mode != "code":
        # Root Dispatcher owns the multi-step WorkflowPlan.  A normal-role
        # sub-agent executes exactly one atomic JSON Schema step. The native
        # tool result is the authoritative contract, so extra Observer and
        # Verifier model calls cannot change control flow and are intentionally
        # skipped. This removes two latency/failure points per DAG step.
        workflow.add_node("native_finalize", tool_execution.native_step_finalize_node)
        workflow.add_edge("tools", "native_finalize")
        workflow.add_conditional_edges(
            "native_finalize",
            _route_after_native_finalize,
            {"end": END, "planner": "planner"},
        )
        return workflow.compile(
            checkpointer=checkpointer,
            interrupt_before=interrupt_before,
        )

    # coder keeps the existing code-mode Planner/Verifier/Judge loop.
    workflow.add_edge("tools", "observer")
    judge = (
        partial(tool_execution.judge_node, llm=llm)
        if llm is not None else tool_execution.judge_node
    )
    workflow.add_node("judge", judge)

    if spec.verifier_required:
        verifier = partial(verifier_node, llm=llm) if llm is not None else verifier_node
        workflow.add_node("verifier", verifier)
        workflow.add_node("refine_router", refine_router)
        # --- observer -> verifier（固定边）---
        workflow.add_edge("observer", "verifier")

        # --- verifier 后条件路由 ---
        workflow.add_conditional_edges(
            "verifier",
            _route_after_verifier_sub,
            {"judge": "judge", "refine_router": "refine_router"},
        )

        # --- refine_router 后条件路由（END / judge / planner）---
        workflow.add_conditional_edges(
            "refine_router",
            _route_after_refine,
            {"end": END, "planner": "planner", "judge": "judge"},
        )

        # --- judge 后条件路由 ---
        workflow.add_conditional_edges(
            "judge",
            _route_after_judge_sub,
            {"end": END, "planner": "planner"},
        )
    else:
        # --- observer -> judge（固定边，无 verifier）---
        workflow.add_edge("observer", "judge")

        # --- judge 后条件路由 ---
        workflow.add_conditional_edges(
            "judge",
            _route_after_judge_sub,
            {"end": END, "planner": "planner"},
        )

    return workflow.compile(
        checkpointer=checkpointer,
        interrupt_before=interrupt_before,
    )


def run_sub_agent(
    agent_role: str,
    user_input: str,
    *,
    run_id: str,
    parent_task_id: str | None = None,
    required_tool_name: str | None = None,
    required_tool_args: dict[str, Any] | None = None,
    checkpointer=None,
    session_id: str = "",
    session_vars: dict | None = None,
    on_event: Any | None = None,
    llm: Any | None = None,
    interrupt_before: list[str] | None = None,
) -> dict:
    """便利函数：编译并运行 sub-agent，返回最终状态。

    普通角色走原生工具调用；coder 走 code_executor_node。

    Args:
        agent_role: registry 中注册的角色名。
        user_input: 用户的自然语言输入。
        run_id: 当前运行的唯一 ID。
        parent_task_id: 父 task ID。
        required_tool_name: Root WorkflowPlan 为该原子步骤指定的唯一工具。
        required_tool_args: Root WorkflowPlan 为该工具锁定的精确参数。
        checkpointer: 可选 LangGraph checkpointer，传给 build_sub_agent。
        session_id: 会话 ID，用于 session memory。
        on_event: EventHandler（judge.awaiting_input 等事件回传）。
        llm: 可选 LLM transport；仅替换模型传输层。
        interrupt_before: 可选，原样传给 build_sub_agent / compile。

    Returns:
        运行结束后的完整 SubAgentState。
    """
    app = build_sub_agent(
        agent_role=agent_role,
        run_id=run_id,
        parent_task_id=parent_task_id,
        checkpointer=checkpointer,
        llm=llm,
        interrupt_before=interrupt_before,
    )
    spec = get_spec(agent_role)

    initial_session_vars = dict(session_vars or {})
    state: SubAgentState = {
        "messages": [],
        "iteration": 0,
        "tool_results": [],
        "should_stop": False,
        "user_input": user_input,
        "agent_role": agent_role,
        "parent_task_id": parent_task_id,
        "required_tool_name": required_tool_name,
        "required_tool_args": dict(required_tool_args or {}),
        "run_id": run_id,
        "refine_history": [],
        "max_iterations": (
            spec.max_iterations if spec.execution_mode == "code" else 2
        ),
        "verifier_required": spec.verifier_required,
        "duplicate_actions": [],
        "session_id": session_id,
        "session_vars": initial_session_vars,
        "enabled_toolkits": list(initial_session_vars.get("__enabled_toolkits__") or ["data_io"]),
        "loaded_skills": dict(initial_session_vars.get("__loaded_skills__") or {}),
        "pending_task": None,
    }

    config: dict[str, Any] = {
        "configurable": {
            "thread_id": f"{agent_role}-{run_id}",
            "checkpoint_ns": f"{agent_role}:{parent_task_id or '_'}:{run_id}",
        }
    }

    from app.agents.events.current import event_handler_context

    with event_handler_context(on_event):
        return app.invoke(dict(state), config=config)
