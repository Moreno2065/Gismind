"""Observer：把工具返回的原始数据总结成简短自然语言摘要。

实现参考 docs/05_llm_prompts.md §3（Observer System Prompt 完整模板）
+ GIS_Agent_技术文档.md §4.7（React Loop 状态机 observer 节点）。

核心设计：
1. **只做描述，不做决策**：Observer 不判断 CONTINUE/RETRY/FINISH，只压缩信息
2. **≤200 字硬约束**：避免摘要本身撑大下一轮 Planner 上下文
3. **明确标注数据来源与截断**：供 Planner 判断是否换源或缩小范围
4. **empty/error 区分**：empty 是"没数据"、error 是"出错"，摘要措辞不同
5. **容错**：LLM 返回超长时截断到 200 字；LLM 异常时降级为模板化摘要
"""

import json
import logging
from typing import Any, Optional

from langchain_core.messages import SystemMessage, HumanMessage
from pydantic import ValidationError

from app.agents.planner_factory import create_llm
from app.agents.planner_helpers import llm_invoke_with_retry
from app.models.schemas import ToolResult

logger = logging.getLogger(__name__)

# 缓存 LLM 实例，避免每次 observe() 都重建。
# 保留 create_llm 原始引用，便于在测试 patch 时识别并跳过缓存。
_original_create_llm = create_llm
_cached_llm: Optional[Any] = None


def _get_llm(llm: Optional[Any] = None) -> Any:
    """获取 LLM 实例。

    优先使用调用方传入的 llm；否则复用模块级缓存实例，
    首次调用时通过 create_llm() 创建。

    若 create_llm 在运行时被 patch（测试场景），不返回缓存实例，
    而是重新调用 patch 后的 create_llm，避免测试间相互污染。
    """
    global _cached_llm
    if llm is not None:
        return llm
    if create_llm is not _original_create_llm:
        return create_llm()
    if _cached_llm is None:
        _cached_llm = create_llm()
    return _cached_llm


# Observer 输出硬上限（字数），超出则截断
_OBSERVER_MAX_CHARS = 200


# ============================================================
# Observer System Prompt（docs/05_llm_prompts.md §3.1 完整模板）
# ============================================================

OBSERVER_SYSTEM_PROMPT = """你是 GIS Agent 的 Observer，负责把工具返回的原始数据总结成简短的自然语言摘要，供主脑（Planner）下一轮决策使用。

# 你的职责
1. 读取工具返回的原始数据（可能是大 GeoJSON、统计表等）
2. 提取关键信息：数量、范围、数据来源、异常情况
3. 压缩成 3 句话以内的摘要
4. 不做决策，只做描述

# 摘要规则
- POI 查询结果："找到 N 个点，主要分布在 X 区域，数据来源 Amap/OSM"
- 缓冲区结果："生成缓冲区，覆盖面积约 X 平方公里"
- 叠加分析结果："交集/差集面积为 X，涉及 Y 个要素"
- 空结果："未找到相关数据"，并说明可能原因
- 截断结果："找到 N 个点（已截断展示前 1500 条）"

# 关键约束
- 摘要不超过 200 字
- 不包含原始坐标数据（太长）
- 明确标注数据来源（Amap / OSM_CN / OSM_Global）
- 明确标注截断情况

# 输入
你会收到一个工具结果，格式为：
{tool_name, status, data, message, source, truncated, error_code}
（error_code 仅在 status=error 时存在，用于判断是否可重试；其他字段见 02_data_models.md 的 ToolResult）

# 输出
直接输出自然语言摘要，不要 JSON 包装。

# 示例

输入：{tool_name: "query_poi", status: "success", data: {count: 12, bbox: [...]}, source: "Amap", truncated: false}
输出：找到 12 个蜜雪冰城，主要分布在新街口地铁站周边 500 米内，数据来源高德地图。

输入：{tool_name: "query_poi", status: "success", data: {count: 1500}, source: "Amap", truncated: true}
输出：找到 1500+ 个 POI（已截断，仅展示前 1500 条），数据来源高德地图。如需完整数据建议缩小查询范围。

输入：{tool_name: "query_poi", status: "empty", message: "未找到相关 POI"}
输出：未找到相关 POI，可能是该区域无此类型店铺，或高德/OSM 数据覆盖不全。建议尝试扩大搜索范围或更换关键词。

输入：{tool_name: "buffer", status: "success", data: {area_km2: 0.785}}
输出：已生成 500 米缓冲区，覆盖面积约 0.79 平方公里。
"""


# ============================================================
# 消息构建
# ============================================================

def build_observer_messages(tool_result: ToolResult) -> list:
    """构建 Observer 的消息列表：System + 工具结果。

    当 tool_result data 过大时，压缩 data 字段以控制在 token 预算内。
    """
    from app.agents.context_budget import estimate_tokens

    dump = tool_result.model_dump(exclude_none=True)
    tool_result_json = json.dumps(dump, ensure_ascii=False)

    # Budget guard: if payload exceeds 800 tokens, trim the data field
    _OBSERVER_DATA_BUDGET = 800
    if estimate_tokens(tool_result_json) > _OBSERVER_DATA_BUDGET:
        data = dump.get("data")
        if isinstance(data, dict):
            # Keep only summary-level keys, drop large nested structures
            trimmed_data = {}
            for k, v in data.items():
                v_str = json.dumps(v, ensure_ascii=False, default=str) if not isinstance(v, str) else v
                if len(v_str) > 500:
                    trimmed_data[k] = v_str[:500] + "... (truncated)"
                else:
                    trimmed_data[k] = v
            dump["data"] = trimmed_data
        elif isinstance(data, str) and len(data) > 1000:
            dump["data"] = data[:1000] + "... (truncated)"
        tool_result_json = json.dumps(dump, ensure_ascii=False)

    return [
        SystemMessage(content=OBSERVER_SYSTEM_PROMPT),
        HumanMessage(content=tool_result_json),
    ]


# ============================================================
# 降级摘要（LLM 不可用时的兜底）
# ============================================================

def _fallback_summary(tool_result: ToolResult) -> str:
    """LLM 异常时的模板化摘要，确保不阻塞 React Loop。"""
    if tool_result.status == "success":
        data = tool_result.data or {}
        count = data.get("count")
        if count is None and isinstance(data.get("pois"), list):
            count = len(data["pois"])
        source = tool_result.source or "未知"
        truncated = "（已截断）" if tool_result.truncated else ""
        if count is not None:
            return f"找到 {count} 个结果{truncated}，数据来源 {source}。"
        return f"工具执行成功{truncated}，数据来源 {source}。"
    elif tool_result.status == "empty":
        return f"未找到相关数据。{tool_result.message or ''}"
    elif tool_result.status == "error":
        return f"工具执行失败：{tool_result.message or ''}（错误码 {tool_result.error_code or '未知'}）"
    return "工具执行完成。"


# ============================================================
# code-mode 摘要（不传给 LLM 完整 payload）
# ============================================================

def _observe_code_mode(tool_result: ToolResult) -> str:
    """对 code-mode ToolResult 生成摘要。

    code-mode 的 data 含 {code, stdout, result, traceback, executor_type}，
    直接提取关键字段摘要，不走 LLM。
    """
    data = tool_result.data or {}
    status = tool_result.status
    code_snippet = (data.get("code") or "")[:80]
    stdout_snippet = (data.get("stdout") or "")[:100]
    result_snippet = str(data.get("result") or "")[:80]
    executor_type = data.get("executor_type", "inline")

    if status == "success":
        parts = [f"[code-mode/{executor_type}] 执行成功"]
        if stdout_snippet:
            parts.append(f"stdout: {stdout_snippet}")
        if result_snippet and result_snippet != "{}":
            parts.append(f"result: {result_snippet}")
        return "; ".join(parts)[:_OBSERVER_MAX_CHARS]
    else:
        stderr_snippet = (data.get("stderr") or "")[:200]
        if stderr_snippet:
            return f"[code-mode/{executor_type}] {stderr_snippet}"[:_OBSERVER_MAX_CHARS]
        else:
            tb_snippet = (data.get("traceback") or "")[-150:]
            return f"[code-mode/{executor_type}] 执行失败: {tb_snippet}"[:_OBSERVER_MAX_CHARS]


# ============================================================
# 主入口
# ============================================================

def observe(tool_result: ToolResult, llm: Optional[Any] = None) -> str:
    """把工具结果摘要成 ≤200 字自然语言。

    Args:
        tool_result: 工具执行结果（ToolResult）
        llm: 可选，外部注入的 LLM 实例（测试用）

    Returns:
        自然语言摘要字符串，≤200 字

    注意：
        LLM 异常时降级为模板化摘要，不抛异常，避免打断 React Loop。
    """
    if not isinstance(tool_result, ToolResult):
        # 兼容 dict 输入
        try:
            tool_result = ToolResult.model_validate(tool_result)
        except ValidationError as e:
            logger.error("observer input invalid ToolResult: %s", e)
            return "工具结果格式异常，无法摘要。"

    # code-mode 分支：提取 code/stdout/result 摘要，不传给 LLM 完整 payload
    tr_mode = getattr(tool_result, "mode", None)
    if tr_mode == "code":
        return _observe_code_mode(tool_result)

    messages = build_observer_messages(tool_result)

    try:
        llm_instance = _get_llm(llm)
        logger.info(
            "observer.invoke tool_name=%s status=%s",
            tool_result.tool_name, tool_result.status,
        )
        response = llm_invoke_with_retry(llm_instance, messages)
        raw = response.content if hasattr(response, "content") else str(response)
    except Exception as e:  # noqa: BLE001
        # LLM 不可用 → 降级模板摘要，不阻塞 Loop
        logger.warning("observer LLM unavailable, fallback: %s", e)
        return _fallback_summary(tool_result)

    # 硬截断到 200 字符（Python 字符串切片本身是 Unicode 感知的，安全）
    if len(raw) > _OBSERVER_MAX_CHARS:
        raw = raw[:_OBSERVER_MAX_CHARS - 3] + "..."
    # LLM 偶尔返回 JSON 包装，尝试提取 summary 字段为纯文本
    try:
        import json
        stripped = raw.strip()
        if stripped.startswith("{") and "summary" in stripped:
            obj = json.loads(stripped)
            if isinstance(obj, dict) and obj.get("summary"):
                return str(obj["summary"]).strip()[:_OBSERVER_MAX_CHARS]
    except (json.JSONDecodeError, ValueError):
        pass
    return raw.strip()
