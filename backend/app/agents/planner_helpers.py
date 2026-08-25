"""Provides llm_invoke_with_retry and robust_parse_json.

- llm_invoke_with_retry: tenacity-wrapped LLM invoke for §12.1.
- robust_parse_json: 集中式 LLM JSON 容错解析器，供 dispatcher / sub-agent /
  assemble 等所有需要解析 LLM JSON 输出的路径复用。

Note: create_llm is intentionally NOT re-exported here to avoid a circular
import with app.agents.planner_factory (which imports llm_invoke_with_retry from this
module). Sub-agent modules should import create_llm directly from
app.agents.planner_factory.
"""

import json
import logging
import re
import unicodedata

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.config import settings

logger = logging.getLogger(__name__)


def llm_invoke_with_retry(llm, messages):
    """带 tenacity 重试的 LLM 调用。

    在 TimeoutError / ConnectionError 时最多重试
    APP_MAX_LLM_RETRIES（默认 3）次，指数退避 1s → 2s → 4s。

    Args:
        llm: 实现了 .invoke(messages) 的 LLM 实例（如 ChatOpenAI）。
        messages: 消息列表（list[BaseMessage]）。

    Returns:
        LLM 响应对象（通常有 .content 属性）。

    Raises:
        TimeoutError / ConnectionError: 最后一次重试仍失败时透传。
    """
    @retry(
        stop=stop_after_attempt(settings.APP_MAX_LLM_RETRIES if settings.APP_MAX_LLM_RETRIES is not None else 3),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        retry=retry_if_exception_type((TimeoutError, ConnectionError)),
        reraise=True,
    )
    def _invoke():
        return llm.invoke(messages)

    return _invoke()


# ============================================================
# Robust JSON Parser（LLM 输出容错）
# ============================================================

def robust_parse_json(raw: str) -> dict | list:
    """从 LLM 原始输出中提取并解析 JSON，兼容常见畸形。

    处理场景（按优先级）：
    1. UTF-8 BOM / 零宽字符剥离
    2. Markdown 代码块围栏（```json ... ``` 或 ``` ... ```）
    3. 前后多余文本（提取第一个 { 到最后一个 } 之间的片段）
    4. Python 字面量替换（True/False/None → true/false/null）
    5. 单引号 → 双引号（仅在 JSON 键值位置）
    6. 尾随逗号（,] 或 ,}）
    7. 字符串内未转义的控制字符（\\x00-\\x1f）
    8. 截断修复：未闭合的括号/引号自动补全
    9. raw_decode 提取第一个合法 JSON 对象

    Args:
        raw: LLM 原始输出字符串。

    Returns:
        解析后的 dict 或 list。

    Raises:
        json.JSONDecodeError: 所有修复手段均失败后抛出，附带原始前 200 字符用于调试。
    """
    if not raw or not raw.strip():
        raise json.JSONDecodeError("LLM 输出为空", raw or "", 0)

    text = raw

    # --- Step 1: BOM + 零宽字符 + Unicode 规范化 ---
    text = text.lstrip("\ufeff")  # UTF-8 BOM
    # 移除零宽字符（\u200b \u200c \u200d \ufeff）
    text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text)
    # 统一 Unicode 空白为 ASCII 空格（保留 \n \r \t）
    text = _normalize_whitespace(text)

    # --- Step 2: Markdown 代码块围栏 ---
    text = _strip_markdown_fence(text)
    
    # --- Step 3: 控制字符转义（必须在首次 json.loads 之前执行）---
    # JSON 规范禁止字符串值中出现字面控制字符（\x00-\x1f），
    # 包括字面换行符 \n、回车符 \r、制表符 \t。
    # LLM 经常在 thinking 字段中输出多行文本，导致 json.loads 在
    # 字面换行符位置报 "Expecting value" 错误。
    text = _escape_control_chars(text)
    
    # --- Step 4: 尝试直接解析 ---
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    
    # --- Step 5: 提取 { ... } 或 [ ... ] 片段 ---
    text = _extract_json_span(text)
    
    # --- Step 6: Python 字面量替换 ---
    text = _replace_python_literals(text)
    
    # --- Step 7: 单引号 → 双引号（保守替换）---
    text = _single_to_double_quotes(text)
    
    # --- Step 8: 尾随逗号 ---
    text = re.sub(r",\s*([}\]])", r"\1", text)
    
    # --- Step 9: 尝试解析修复后的文本 ---
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    
    # --- Step 10: raw_decode 提取第一个合法 JSON ---
    try:
        decoder = json.JSONDecoder()
        obj, _ = decoder.raw_decode(text)
        return obj
    except (json.JSONDecodeError, ValueError):
        pass
    
    # --- Step 11: 截断修复（补全未闭合的括号/引号）---
    repaired = _repair_truncated_json(text)
    if repaired:
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            pass
        try:
            decoder = json.JSONDecoder()
            obj, _ = decoder.raw_decode(repaired)
            return obj
        except (json.JSONDecodeError, ValueError):
            pass
    
    # --- 全部失败：输出详细调试信息 ---
    preview = raw[:500].replace("\n", "\\n")
    logger.error(
        "robust_parse_json exhausted all repair strategies. "
        "raw length=%d, preview (first 500 chars): %s",
        len(raw), preview,
    )
    raise json.JSONDecodeError(
        f"robust_parse_json: 所有修复手段均失败。原始输出前 500 字符: {preview}",
        raw,
        0,
    )


# ============================================================
# 内部辅助函数
# ============================================================

def _normalize_whitespace(text: str) -> str:
    """将 Unicode 非常规空白字符替换为 ASCII 空格，保留 \\n \\r \\t。"""
    result = []
    for ch in text:
        if ch in "\n\r\t":
            result.append(ch)
        elif unicodedata.category(ch) == "Zs" and ch != " ":
            result.append(" ")
        else:
            result.append(ch)
    return "".join(result)


def _strip_markdown_fence(text: str) -> str:
    """去掉 ```json ... ``` 或 ``` ... ``` 围栏。"""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    # 去掉首行
    lines = stripped.split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    # 去掉末行
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _extract_json_span(text: str) -> str:
    """提取第一个 { 到最后一个 } 或第一个 [ 到最后一个 ] 之间的片段。"""
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    bracket_start = text.find("[")
    bracket_end = text.rfind("]")

    candidates = []
    if brace_start >= 0 and brace_end > brace_start:
        candidates.append((brace_start, brace_end))
    if bracket_start >= 0 and bracket_end > bracket_start:
        candidates.append((bracket_start, bracket_end))

    if not candidates:
        return text

    # 取最外层（最早开始、最晚结束）
    start = min(s for s, _ in candidates)
    end = max(e for _, e in candidates)
    return text[start:end + 1]


def _replace_python_literals(text: str) -> str:
    """把 Python 的 True / False / None 替换为 JSON 的 true / false / null。

    使用 \\b 边界匹配，避免误伤键名或字符串内容中的子串。
    """
    text = re.sub(r"\bTrue\b", "true", text)
    text = re.sub(r"\bFalse\b", "false", text)
    text = re.sub(r"\bNone\b", "null", text)
    return text


def _single_to_double_quotes(text: str) -> str:
    """保守地将 JSON 键值位置的单引号替换为双引号。

    仅在 { : , [ 后面紧跟 ' 时替换，避免误伤字符串内容。
    """
    # 键位置：{ 或 , 后的空白 + '
    text = re.sub(r"([{,])\s*'", r'\1"', text)
    # 值位置：: 后的 '
    text = re.sub(r":\s*'", ': "', text)
    # 闭合位置：' 后跟 , } ]
    text = re.sub(r"'(\s*[,}\]])", r'"\1', text)
    return text


def _escape_control_chars(text: str) -> str:
    """转义 JSON 字符串值内部的字面控制字符（\\x00-\\x1f）。

    JSON 规范（RFC 8259 §7）禁止字符串值中出现字面控制字符，
    包括 \\n、\\r、\\t。这些字符必须以转义序列形式出现。

    LLM 经常在 thinking 等字段中输出多行文本（字面换行符），
    导致 json.loads 在该位置报 "Expecting value" 错误。

    **重要**：本函数使用状态机区分字符串内外：
    - 字符串外部的控制字符（如 JSON 格式化换行）保留不动（它们是合法空白）
    - 字符串内部控制字符转义为 \\uXXXX 形式
    - 已经以 \\n、\\t 等转义形式存在的序列不受影响
    """
    result: list[str] = []
    in_string = False
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]

        if not in_string:
            # 字符串外部：保留所有字符（包括控制字符作为合法空白）
            if ch == '"':
                in_string = True
            result.append(ch)
        else:
            # 字符串内部
            if ch == '\\' and i + 1 < n:
                # 转义序列：原样保留（\\n, \\t, \\" 等）
                result.append(ch)
                result.append(text[i + 1])
                i += 2
                continue
            elif ch == '"':
                # 字符串结束
                in_string = False
                result.append(ch)
            elif ord(ch) < 0x20:
                # 字符串内的控制字符：转义
                result.append(f"\\u{ord(ch):04x}")
            else:
                result.append(ch)
        i += 1

    return "".join(result)


def _repair_truncated_json(text: str) -> str | None:
    """尝试修复被截断的 JSON：补全未闭合的引号、括号。

    策略：
    1. 统计未闭合的 " 引号 → 如果是奇数，末尾补 "
    2. 统计未闭合的 { [ → 按 LIFO 顺序补 } ]
    """
    if not text or not text.strip():
        return None

    # 统计引号（排除已转义的 \"）
    unescaped_quotes = len(re.findall(r'(?<!\\)"', text))
    if unescaped_quotes % 2 == 1:
        text += '"'

    # 用栈追踪未闭合的括号
    stack: list[str] = []
    in_string = False
    escape_next = False

    for ch in text:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            if in_string:
                escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                expected = "{" if ch == "}" else "["
                if stack[-1] == expected:
                    stack.pop()

    # 按 LIFO 补全
    for bracket in reversed(stack):
        text += "}" if bracket == "{" else "]"

    return text
