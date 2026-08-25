"""根据 verifier_output 决定: 回 planner / 上 judge。

refine_router 检查 verifier 输出和当前迭代次数，决定流程走向：
- verifier approved → 交给 judge 做最终判定
- verifier refused & iteration < max_iterations → 附加 refinement hint 消息回 planner
- verifier refused & iteration >= max_iterations → 终止（REFINE_LIMIT_EXCEEDED）
"""

from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage

from app.agents.errors import ErrorCode
from app.agents.schemas import RefineNote, VerifierOutput
from app.agents.state import SubAgentState

logger = logging.getLogger(__name__)

_GENERIC_REFINEMENT_HINTS = {"改进", "请改进", "重新检查", "再试一次", "重试"}


def _parse_verifier(d: dict | None) -> VerifierOutput | None:
    """安全解析 verifier_output dict 为 VerifierOutput。"""
    if not d:
        return None
    try:
        return VerifierOutput.model_validate(d)
    except Exception:
        return None


_ALLOWED_PENDING_SLOTS = {"distance", "output_path", "output_format", "target_layer"}
_SYSTEM_INPUT_REASON_TERMS = {"工具", "超时", "数据为空", "模型", "计算失败", "重试"}


def verifier_pending_task(state: SubAgentState) -> dict | None:
    """Return a safe verifier-requested pending task, or ``None`` when invalid."""
    if state.get("pending_task") is not None:
        return None

    verifier = _parse_verifier(state.get("verifier_output"))
    if verifier is None or verifier.approved or not verifier.needs_input:
        return None

    input_reason = verifier.input_reason
    if not isinstance(input_reason, str) or not input_reason.strip():
        return None
    if any(
        term in reason
        for reason in (verifier.reason, input_reason)
        for term in _SYSTEM_INPUT_REASON_TERMS
    ):
        return None

    missing_slots = verifier.missing_slots
    choices = verifier.choices
    if not missing_slots and not choices:
        return None
    if any(slot not in _ALLOWED_PENDING_SLOTS for slot in missing_slots):
        return None
    if any(
        not isinstance(choice, dict)
        or not isinstance(choice.get("id"), str)
        or not choice["id"].strip()
        for choice in choices
    ):
        return None

    slot_patch_schema: dict = {}
    for slot in missing_slots:
        if slot == "distance":
            slot_patch_schema[slot] = {"type": "number", "unit": "m"}
        elif slot in {"output_path", "output_format", "target_layer"}:
            slot_patch_schema[slot] = {"type": "string"}

    return {
        "sub_agent_run_id": state.get("run_id", ""),
        "original_request": state.get("user_input", ""),
        "missing_slots": missing_slots,
        "choices": choices,
        "message": input_reason,
        "issues": [],
        "slot_patch_schema": slot_patch_schema,
        "correction_history": [],
    }


def refine_router(state: SubAgentState) -> dict:
    """Refine 路由节点：决定回 planner 重新规划或交给 judge 做最终判定。

    Args:
        state: 当前 SubAgentState，需含 verifier_output / iteration / max_iterations。

    Returns:
        delta:
          - should_stop: bool
          - messages: list[BaseMessage] (refine hint 消息，仅在需要回 planner 时)
          - refine_history: list[dict] (追加了新的 RefineNote)
          - termination_cause: str (仅在达到上限时)

        Note: does not return or increment ``iteration``; the limit boundary
        compares ``state.iteration`` vs ``state.max_iterations`` only.
    """
    verifier = _parse_verifier(state.get("verifier_output"))
    iteration = state.get("iteration", 0)
    max_iter = state.get("max_iterations", 6)

    # Verifier 不可用（parse 失败）→ fail open，写回一致的 verifier_output 并交由确定性质量门。
    if verifier is None:
        logger.warning("verifier output is None (parse failure), failing open")
        fail_open = VerifierOutput(
            approved=True,
            reason="verifier unavailable; deterministic quality gates remain active",
            confidence=0.0,
            verifier_unavailable=True,
        )
        return {
            "verifier_output": fail_open.model_dump(),
            "should_stop": False,
        }

    pending_task = verifier_pending_task(state)
    if pending_task is not None:
        return {
            "should_stop": False,
            "pending_task": pending_task,
            "verifier_output": verifier.model_dump(),
        }

    # 任意批准都直接上 judge；confidence 只供观测，不触发 refinement。
    if verifier.approved:
        return {"should_stop": False}

    actionable_hints = [
        hint for hint in verifier.refinement_hints
        if hint.strip() and hint.strip() not in _GENERIC_REFINEMENT_HINTS
    ]
    if not actionable_hints:
        logger.warning("normalizing non-actionable verifier rejection")
        normalized = verifier.model_copy(
            update={
                "approved": True,
                "invalid_rejection": True,
                "needs_input": False,
            }
        )
        return {"verifier_output": normalized.model_dump(), "should_stop": False}

    if iteration >= max_iter:
        logger.warning(
            "refine limit exceeded: iteration=%d max_iterations=%d",
            iteration, max_iter,
        )
        return {
            "should_stop": True,
            "termination_cause": ErrorCode.REFINE_LIMIT_EXCEEDED.value,
        }

    # 未达上限 → 构造 hint 消息回 planner
    hint_text = "; ".join(verifier.refinement_hints) or verifier.reason
    new_msgs = [
        HumanMessage(
            content=f"[verifier @ iter {iteration}] {verifier.reason} | hints: {hint_text}"
        )
    ]

    note = RefineNote(
        iteration=iteration,
        verifier_reason=verifier.reason,
        refinement_hints=verifier.refinement_hints,
        applied=True,
    )
    history = list(state.get("refine_history") or [])
    history.append(note.model_dump())

    logger.info(
        "refine_router: refusing iteration=%d hints=%s",
        iteration, hint_text[:120],
    )

    return {
        "messages": new_msgs,
        "refine_history": history,
        "should_stop": False,
    }
