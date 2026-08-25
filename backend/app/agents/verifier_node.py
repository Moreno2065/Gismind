"""sub-agent 内嵌 verifier: 独立 system prompt, 调同 DeepSeek 但 prompt 隔离。

Verifier 节点在 observer 之后、judge 之前运行。它用独立的 LLM 调用审计
上一轮 sub-agent 的产出，判断是否需要 refine 后再提交给 judge。
"""

from __future__ import annotations

import json
import logging

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.planner_factory import create_llm
from app.agents.metrics import increment_verifier_approvals, increment_verifier_calls
from app.agents.planner_helpers import llm_invoke_with_retry, robust_parse_json
from app.agents.schemas import VerifierOutput
from app.agents.state import SubAgentState

logger = logging.getLogger(__name__)

VERIFIER_SYSTEM_PROMPT = (
    "你是 GIS Agent 的 Verifier，负责独立审查上一个 sub-agent 是否完成任务。\n\n"
    "只读最近一轮的 (tool_results + observer 摘要)。不要写工具调用。\n\n"
    "输出 JSON:\n"
    '{"approved": bool, "reason": str, "refinement_hints": list[str], "confidence": float(0-1), '
    '"needs_input": bool, "missing_slots": list[str], "choices": list[object]}\n\n'
    "判断：\n"
    "- approved=true：上次结果已能回答 sub-task goal\n"
    "- approved=false：必须给可执行的 refinement_hints（planner 能照着修改）\n"
    "- needs_input=true 仅用于必须由用户补充的参数或用户才能消除的歧义；"
    "不得用于工具、数据或模型失败。此时 missing_slots 或 choices 必须提供，"
    "且 choices 的每项必须含 id。\n\n"
    "绝不接受\"近似正确\"。如果有任何 blocker，给 false + 具体 hint。"
)


def verifier_node(state: SubAgentState, *, llm=None) -> dict:
    """Verifier 节点：用独立 LLM 调用审计上一轮 sub-agent 产出。

    Args:
        state: 当前 SubAgentState，需含 tool_results / messages / user_input 等。

    Returns:
        delta: {"verifier_output": dict} 包含 VerifierOutput 的 model_dump。
    """
    last_results = state.get("tool_results") or []

    # 检测最后一个 tool_result 的 mode：code-path vs json-path
    last_tr = last_results[-1] if last_results else None
    tr_mode = getattr(last_tr, "mode", None) if hasattr(last_tr, "mode") else None
    # fallback: 如果 tr 是 dict 就 .get("mode")
    if tr_mode is None and isinstance(last_tr, dict):
        tr_mode = last_tr.get("mode")

    if tr_mode == "code":
        return _verifier_code_mode(state, last_results, llm=llm)
    return _verifier_json_mode(state, last_results, llm=llm)


def _verifier_json_mode(state: SubAgentState, last_results: list, *, llm=None) -> dict:
    """JSON-mode verifier（原值，读 tool_results）。"""
    messages = state.get("messages") or []
    recent = messages[-6:] if len(messages) > 6 else messages

    payload = {
        "user_input": state.get("user_input", ""),
        "mode": "json",
        "tool_results_count": len(last_results),
        "last_tool_results": [
            tr.model_dump(exclude_none=True)
            if hasattr(tr, "model_dump") else tr
            for tr in last_results[-3:]
        ],
        "recent_messages": [
            {"type": getattr(m, "type", ""), "content": str(getattr(m, "content", ""))[:500]}
            for m in recent
        ],
    }
    return _call_verifier(payload, llm=llm)


def _verifier_code_mode(state: SubAgentState, last_results: list, *, llm=None) -> dict:
    """Code-mode verifier：读 Python 代码 + stdout + result + traceback。

    用 `code` / `result` / `traceback` / `session_vars_keys` 替代 JSON tool_results。
    """
    last_tr = last_results[-1]
    tr_data = getattr(last_tr, "data", {}) if hasattr(last_tr, "data") else (last_tr.get("data", {}) if isinstance(last_tr, dict) else {})

    # 提取 __result__ 的 keys 供 verifier 判断持久化是否正确
    result = tr_data.get("result", {}) if isinstance(tr_data, dict) else {}
    session_vars_keys = list(result.keys()) if isinstance(result, dict) else []

    payload = {
        "user_input": state.get("user_input", ""),
        "mode": "code",
        "code": tr_data.get("code", ""),
        "stdout": tr_data.get("stdout", ""),
        "result": result,
        "traceback": (tr_data.get("traceback") or "")[:3000] if isinstance(tr_data, dict) else "",
        "executor_type": tr_data.get("executor_type", "?"),
        "session_vars_keys": session_vars_keys,
    }
    return _call_verifier(payload, llm=llm)


_CODE_MODE_VERIFIER_PROMPT = (
    "你是 GIS Agent 的 Verifier，负责独立审查一段由 LLM 生成的 Python 代码及其执行结果。\n\n"
    "代码和执行输出会在下面提供。不要写工具调用。\n\n"
    "输出 JSON:\n"
    '{"approved": bool, "reason": str, "refinement_hints": list[str], "confidence": float(0-1), '
    '"needs_input": bool, "missing_slots": list[str], "choices": list[object]}\n\n'
    "判断：\n"
    "- 代码逻辑是否正确完成了任务目标？\n"
    "- 是否有明显的错误（如变量未定义、结果为空、坐标异常）？\n"
    "- 如果失败，是代码错误还是数据问题？\n"
    "- session_vars_keys 是否反映了预期的中间结果持久化？\n"
    "- needs_input=true 仅用于必须由用户补充的参数或用户才能消除的歧义；"
    "不得用于工具、数据或模型失败。此时 missing_slots 或 choices 必须提供，"
    "且 choices 的每项必须含 id。\n\n"
    "绝不接受\"近似正确\"。如果有任何 blocker，给 false + 具体 hint。"
)


def _call_verifier(payload: dict, *, llm=None) -> dict:
    """通用 verifier 调用（JSON / code 模式共用）。"""
    payload_json = json.dumps(payload, ensure_ascii=False, default=str)

    is_code_mode = payload.get("mode") == "code"
    system_prompt = _CODE_MODE_VERIFIER_PROMPT if is_code_mode else VERIFIER_SYSTEM_PROMPT

    msgs = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=payload_json),
    ]

    try:
        llm = llm or create_llm()
        resp = llm_invoke_with_retry(llm, msgs)
        raw = resp.content if hasattr(resp, "content") else str(resp)
        data = robust_parse_json(raw)
        out = VerifierOutput.model_validate(data)
    except Exception as e:
        logger.warning("verifier LLM unavailable, failing open: %s", e)
        out = VerifierOutput(
            approved=True,
            reason=(
                "verifier unavailable; deterministic quality gates remain active: "
                f"{e}"
            ),
            confidence=0.0,
            verifier_unavailable=True,
        )

    increment_verifier_calls()
    if out.approved:
        increment_verifier_approvals()

    return {"verifier_output": out.model_dump()}
