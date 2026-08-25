"""Judge：判断 React Loop 当前迭代后应 CONTINUE / RETRY / FINISH。

实现参考 docs/05_llm_prompts.md §4（Judge System Prompt + RETRY 流转）
+ GIS_Agent_技术文档.md §4.7（judge 节点）。

核心设计：
1. **迭代上限强制终止**：iteration >= max_iterations（来自 spec/state）时直接 FINISH，不调 LLM
2. **RETRY 带失败上下文**：附加 HumanMessage 含 error_code，让 Planner 知道上次失败原因
3. **empty 不触发 RETRY**：empty 是"没数据"不是"出错"，Judge 自己判断
4. **容错**：LLM 返回非法 JSON 时默认 CONTINUE（让 Planner 再看一眼），不崩
5. **返回 dict 兼容 LangGraph**：{should_stop, decision, reason, messages?}
"""

import json
import logging
from typing import Optional

from langchain_core.messages import SystemMessage, HumanMessage, BaseMessage

from app.agents.planner_factory import create_llm
from app.agents.planner_helpers import llm_invoke_with_retry, robust_parse_json
from app.models.schemas import JudgeDecision, ToolResult

logger = logging.getLogger(__name__)

# 连续解析失败计数器，避免 LLM 持续输出非法 JSON 时无限循环
_parse_failure_consecutive_count: int = 0


def _reset_parse_failure_count() -> None:
    global _parse_failure_consecutive_count
    _parse_failure_consecutive_count = 0


def _incr_parse_failure_count() -> int:
    global _parse_failure_consecutive_count
    _parse_failure_consecutive_count += 1
    return _parse_failure_consecutive_count


# ============================================================
# Judge System Prompt（docs/05_llm_prompts.md §4.1 完整模板）
# ============================================================

JUDGE_SYSTEM_PROMPT = """你是 GIS Agent 的 Judge，负责判断当前 React Loop 迭代后，任务是否应该结束、重试还是继续。

# 你的职责
读取当前所有消息历史（用户输入、Planner 的 ToolCall、Observer 的摘要），输出一个决策：
- CONTINUE：任务未完成，需要 Planner 继续编排下一步
- RETRY：上一步工具失败，但有替代方案，让 Planner 重试
- FINISH：任务已完成，可以输出最终结果给用户

# 判断规则

## FINISH 的条件（满足任一）
1. 用户的原始问题已经被完整回答（有最终摘要 + 可视化输出）
2. 已达到最大迭代上限（当前轮次 >= 10）
3. 用户主动要求停止

## RETRY 的条件
1. 工具返回 error 且有已知的替代方案（如高德失败可切 OSM）
2. 工具超时但服务可能恢复
注意：empty 结果不算 error，不触发 RETRY（empty 就是"没数据"，不是"出错"）

## CONTINUE 的条件
1. 工具链尚未执行完（Planner 编排了多步，还有未执行的）
2. 需要根据当前结果决定下一步（如查完竞品后需要做缓冲区）

# 输出格式
输出 JSON：
{
  "decision": "CONTINUE" | "RETRY" | "FINISH",
  "reason": "简短说明判断依据（1 句话）"
}

# 示例

## FINISH
历史：用户问"南京新街口蜜雪冰城"，Planner 查询成功，Observer 摘要"找到 12 家"，已生成地图
输出：{"decision": "FINISH", "reason": "用户问题已完整回答，含数据摘要和地图可视化"}

## RETRY
历史：用户问"查询 POI"，query_poi 返回 error（高德限流），但 OSM 尚未尝试
输出：{"decision": "RETRY", "reason": "高德限流，Tool 内部 Fallback 未触发或失败，建议重试"}

## CONTINUE
历史：用户问"选址分析"，已完成 query_poi，但 buffer 和 overlay 尚未执行
输出：{"decision": "CONTINUE", "reason": "工具链未完成，需继续执行 buffer 和 overlay"}

## FINISH（达到上限）
历史：已迭代 10 轮，任务仍未完成
输出：{"decision": "FINISH", "reason": "达到最大迭代上限 10 轮，强制终止并返回部分结果"}
"""


# ============================================================
# 消息构建
# ============================================================

def build_judge_messages(state: dict) -> list:
    """构建 Judge 的消息列表：System + 首条用户消息 + 最近历史。

    始终保留原始用户输入（第一条 HumanMessage），再取最近 N-1 条消息，
    确保 Judge 在多轮迭代后仍能对照原始问题判断是否已完成。
    """
    MAX_WINDOW = 10
    messages: list = [SystemMessage(content=JUDGE_SYSTEM_PROMPT)]
    state_messages = state.get("messages") or []

    if not state_messages:
        return messages

    # 找到第一条用户消息（原始输入）
    first_user = None
    for m in state_messages:
        if isinstance(m, HumanMessage) and m.content and isinstance(m.content, str):
            first_user = m
            break

    if len(state_messages) <= MAX_WINDOW:
        messages.extend(state_messages)
    else:
        # 始终包含第一条用户消息 + 最近 MAX_WINDOW-1 条
        recent = state_messages[-(MAX_WINDOW - 1):] if first_user else state_messages[-MAX_WINDOW:]
        if first_user and first_user not in recent:
            messages.append(first_user)
        messages.extend(recent)

    return messages


# ============================================================
# 决策解析
# ============================================================

def parse_decision(content: str) -> JudgeDecision:
    """解析 LLM 输出为 JudgeDecision。

    使用 robust_parse_json 统一容错解析，支持 markdown fence / 尾随逗号 / 截断等。
    解析失败时默认 CONTINUE（让 Planner 再看一眼，最安全）。
    """
    if not content or not content.strip():
        logger.warning("judge empty content, default CONTINUE")
        return JudgeDecision(decision="CONTINUE", reason="LLM 返回空，默认继续")

    try:
        data = robust_parse_json(content)
        result = JudgeDecision.model_validate(data)
        _reset_parse_failure_count()
        return result
    except (json.JSONDecodeError, ValueError) as e:
        failures = _incr_parse_failure_count()
        logger.warning("judge parse failed (consecutive=%d), default CONTINUE: %s", failures, e)
        if failures >= 2:
            # 连续 2 次解析失败，强制 FINISH 避免无限循环
            logger.warning("judge parse failed %d times consecutively, force FINISH", failures)
            return JudgeDecision(
                decision="FINISH",
                reason=f"LLM 输出连续 {failures} 次解析失败，强制终止",
            )
        return JudgeDecision(
            decision="CONTINUE",
            reason=f"LLM 输出解析失败，默认继续：{e}",
        )


# ============================================================
# 提取部分结果（迭代上限兜底）
# ============================================================

def extract_partial_result(state: dict) -> dict:
    """迭代上限强制终止时，从历史中提取部分结果。"""
    tool_results = state.get("tool_results") or []
    summaries = []
    for tr in tool_results:
        if isinstance(tr, ToolResult):
            if tr.status == "success":
                summaries.append({
                    "tool_name": tr.tool_name,
                    "source": tr.source,
                    "truncated": tr.truncated,
                })
        elif isinstance(tr, dict):
            if tr.get("status") == "success":
                summaries.append({
                    "tool_name": tr.get("tool_name"),
                    "source": tr.get("source"),
                    "truncated": tr.get("truncated", False),
                })
    return {
        "summary": "因达到最大迭代上限，返回部分结果",
        "partial_results": summaries,
        "termination_cause": "达到最大迭代上限，强制终止",
    }


# ============================================================
# 主入口
# ============================================================

def judge(state: dict, *, llm=None) -> dict:
    """判断 CONTINUE/RETRY/FINISH。

    Args:
        state: React Loop 状态 dict，含 iteration / messages / tool_results

    Returns:
        dict:
          - should_stop: bool
          - decision: "CONTINUE" | "RETRY" | "FINISH"
          - reason: str
          - messages: list[BaseMessage]（仅 RETRY 时附加失败上下文）
          - final_output: dict（仅 FINISH 时）
    """
    iteration = state.get("iteration", 0)

    # 0. AWAITING_INPUT：preflight 检测到 ask_user 类 blocking issue，挂起等待用户输入
    pending = state.get("pending_task")
    if pending:
        logger.info("judge AWAITING_INPUT at iteration=%d", iteration)
        # Persist to PendingStore (Redis) so user-input via /resume can find it.
        try:
            session_id = state.get("session_id", "")
            if session_id:
                from app.agents.pending import PendingStore, PendingTask
                pt = PendingTask(
                    sub_agent_run_id=pending.get("sub_agent_run_id") or state.get("run_id", ""),
                    original_request=pending.get("original_request") or state.get("user_input", ""),
                    missing_slots=pending.get("missing_slots") or [],
                    candidates=pending.get("candidates") or [],
                    slot_patch_schema=pending.get("slot_patch_schema") or {},
                    choices=pending.get("choices") or [],
                    correction_history=pending.get("correction_history") or [],
                    message=pending.get("message", ""),
                    issues=pending.get("issues") or [],
                )
                PendingStore().save_sync(session_id, pt)
        except Exception:
            logger.exception("judge AWAITING_INPUT: failed to persist PendingStore")

        # Emit judge.awaiting_input event via contextvar handler (if available).
        try:
            from app.agents.events.current import get_current_handler
            on_event = get_current_handler()
            if on_event is not None:
                from app.agents.events import emit_event
                emit_event(
                    on_event,
                    "judge.awaiting_input",
                    pending.get("message", "需要用户提供更多信息"),
                    pending_task=pending,
                    issues=pending.get("issues") or [],
                    run_id=state.get("run_id", ""),
                    session_id=state.get("session_id", ""),
                )
        except Exception:
            logger.exception("judge AWAITING_INPUT: failed to emit judge.awaiting_input event")

        return {
            "should_stop": True,
            "decision": "AWAITING_INPUT",
            "reason": pending.get("message", "需要用户提供更多信息"),
            "pending_task": pending,
        }

    # 1. 迭代上限强制 FINISH（不调 LLM）
    max_iter = state.get("max_iterations", 6)
    if iteration >= max_iter:
        logger.info("judge force FINISH at iteration=%d (max=%d)", iteration, max_iter)
        return {
            "should_stop": True,
            "decision": "FINISH",
            "reason": f"达到最大迭代上限 {max_iter} 轮，强制终止并返回部分结果",
            "final_output": extract_partial_result(state),
        }

    # 2. 调 LLM 判断
    messages = build_judge_messages(state)
    try:
        llm = llm or create_llm()
        logger.info("judge.invoke iteration=%d", iteration)
        response = llm_invoke_with_retry(llm, messages)
        raw = response.content if hasattr(response, "content") else str(response)
    except Exception as e:  # noqa: BLE001
        # LLM 不可用 → 默认 FINISH（避免无限循环）
        logger.error("judge LLM unavailable, force FINISH: %s", e)
        return {
            "should_stop": True,
            "decision": "FINISH",
            "reason": f"Judge LLM 不可用，强制终止：{e}",
            "final_output": extract_partial_result(state),
        }

    decision = parse_decision(raw)

    # 3. 按决策返回
    if decision.decision == "FINISH":
        return {
            "should_stop": True,
            "decision": "FINISH",
            "reason": decision.reason,
            "final_output": _build_final_output(state, decision.reason),
        }
    elif decision.decision == "RETRY":
        # 附加失败上下文，让 Planner 知道上次失败原因
        failure_ctx = _build_failure_context(state, decision.reason)
        return {
            "should_stop": False,
            "decision": "RETRY",
            "reason": decision.reason,
            "messages": failure_ctx,
        }
    else:  # CONTINUE
        return {
            "should_stop": False,
            "decision": "CONTINUE",
            "reason": decision.reason,
        }


# ============================================================
# 辅助
# ============================================================

def _build_failure_context(state: dict, reason: str) -> list:
    """构造 RETRY 时附加给 Planner 的失败上下文消息。"""
    tool_results = state.get("tool_results") or []
    error_codes = []
    error_msgs = []
    for tr in tool_results:
        status = tr.status if isinstance(tr, ToolResult) else tr.get("status")
        if status == "error":
            if isinstance(tr, ToolResult):
                if tr.error_code:
                    error_codes.append(tr.error_code)
                if tr.message:
                    error_msgs.append(tr.message)
            else:
                ec = tr.get("error_code")
                if ec:
                    error_codes.append(ec)
                msg = tr.get("message")
                if msg:
                    error_msgs.append(msg)

    parts = [f"上一步工具失败，Judge 判定 RETRY：{reason}"]
    if error_codes:
        parts.append(f"错误码：{', '.join(error_codes)}")
    if error_msgs:
        parts.append(f"失败原因：{'; '.join(error_msgs)}")
    parts.append("请尝试替代方案（如切换数据源或调整参数）。")

    return [HumanMessage(content="\n".join(parts))]


def _build_final_output(state: dict, reason: str) -> dict:
    """FINISH 时从历史中提取最终输出。"""
    tool_results = state.get("tool_results") or []
    success_results = []
    for tr in tool_results:
        if isinstance(tr, ToolResult):
            if tr.status == "success":
                success_results.append({
                    "tool_name": tr.tool_name,
                    "source": tr.source,
                    "data": tr.data,
                    "truncated": tr.truncated,
                })
        elif isinstance(tr, dict):
            if tr.get("status") == "success":
                success_results.append({
                    "tool_name": tr.get("tool_name"),
                    "source": tr.get("source"),
                    "data": tr.get("data"),
                    "truncated": tr.get("truncated", False),
                })

    # 摘要优先级：Judge reason（含最终结论）> observer 摘要 > 默认
    summary = reason.strip() if reason else ""
    if not summary:
        state_messages = state.get("messages") or []
        # 反向遍历找最接近 observer 摘要的消息：
        # - 类型必须是 human（observer 输出以 HumanMessage 注入）
        # - 跳过 JSON 和 RETRY 上下文消息
        for m in reversed(state_messages):
            content = getattr(m, "content", None)
            mtype = getattr(m, "type", "")
            if content and mtype == "human" and isinstance(content, str):
                text = content.strip()
                # 跳过纯 JSON（非 Observer 输出）
                if text.startswith("{"):
                    continue
                # 跳过 RETRY 失败上下文消息（含特定模式）
                if "Judge 判定 RETRY" in text or "请尝试替代方案" in text:
                    continue
                if text:
                    summary = text
                    break
        if not summary:
            summary = "任务已完成"

    return {
        "summary": summary,
        "results": success_results,
        "reason": reason,
    }
