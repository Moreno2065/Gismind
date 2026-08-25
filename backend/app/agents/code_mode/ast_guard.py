"""AST 静态分析：检查 banned 节点，追踪 call_graph，统一路由到 sandbox。

API：`inspect(code) -> InspectionResult`，字段 `required_executor: "sandbox"`（始终）。
outright banned 节点抛 `ASTBannedNodeError`。

D2 决策：inline 执行路径已废弃。所有代码统一走子进程 sandbox 执行，
消除 inline/sandbox 双路径带来的复杂度（混合调用检测、超时策略分歧等）。
AST 保留 banned 节点检查（架构违规早发现）和 call_graph 追踪（供 namespace 构造），
但不再做路由决策。
"""
from __future__ import annotations

import ast
from typing import Optional

from app.agents.code_mode.types import ASTBannedNodeError, InspectionResult


# outright banned 节点（架构违规）—— AST 看到即抛异常
_BANNED_NODES = {
    "Import": "import 语句",
    "ImportFrom": "from-import 语句（namespace 已注入工具，模型不需要 import）",
    "AsyncFunctionDef": "异步函数定义（async def）",
    "ClassDef": "类定义（class）",
    "With": "with 语句（含上下文管理器，可能触发文件 IO）",
    "Raise": "raise 语句",
    "Assert": "assert 语句",
    "Global": "global 声明",
    "Nonlocal": "nonlocal 声明",
    "Lambda": "lambda 表达式",
    "Delete": "del 语句（可能删除 namespace 工具函数或 session_vars）",
    "TryStar": "except* 语句（Python 3.11+ ExceptionGroup，sandbox 不支持）",
}

# NOTE: FunctionDef（def）**不在 banned 列表中**，有意允许 LLM 定义纯计算辅助函数。
# 安全由 namespace 和子进程沙箱兜底。

# D2: inline deprecated, all code goes to sandbox — routing constants removed


def inspect(
    code: str,
    known_tools: Optional[dict[str, str]] = None,
) -> InspectionResult:
    """静态分析 code 字符串。

    Args:
        code: Python 源代码字符串。
        known_tools: 已知工具名 → executor_type 的映射（registry 的子集）。
                    顶层 Name call 不在 known_tools 时默认按 "inline" 处理。

    Returns:
        InspectionResult: required_executor（始终 "sandbox"）/ reasons（始终空）/ call_graph。

    Raises:
        ASTBannedNodeError: outright banned 节点（Import / ClassDef / dunder / 危险 call）。
        这是**架构违规**信号，不是路由信号 — executor 捕获后让 planner refine。
    """
    # D2: inline deprecated, all code goes to sandbox
    call_graph: dict[str, str] = {}
    tools = known_tools or {}

    # 1. AST 解析失败 → 返回 sandbox（让 executor 在子进程跑出 SyntaxError）
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return InspectionResult(
            required_executor="sandbox",
            reasons=[],
            call_graph=call_graph,
        )

    # 2. 单次 walk 覆盖 banned 节点 / dunder 属性 / 禁调用 / call_graph 追踪
    for node in ast.walk(tree):
        # 2a. outright banned 节点 — 立即抛异常（架构违规）
        if isinstance(node, ast.Import):
            names = ", ".join(alias.name for alias in node.names)
            snippet = _truncate(f"import {names}")
            raise ASTBannedNodeError(
                node_type="Import",
                snippet=snippet,
                lineno=node.lineno,
            )
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = ", ".join(alias.name for alias in node.names)
            snippet = _truncate(f"from {module} import {names}")
            raise ASTBannedNodeError(
                node_type="ImportFrom",
                snippet=snippet,
                lineno=node.lineno,
            )
        if type(node).__name__ in _BANNED_NODES:
            label = _BANNED_NODES[type(node).__name__]
            snippet = _snippet_of(code, node)
            raise ASTBannedNodeError(
                node_type=type(node).__name__,
                snippet=snippet,
                lineno=getattr(node, "lineno", None),
            )

        # 2b. dunder 属性 — 抛异常
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                snippet = _truncate(f".{node.attr}")
                raise ASTBannedNodeError(
                    node_type="Attribute(dunder)",
                    snippet=snippet,
                    lineno=node.lineno,
                )

        # 2c. 禁调用（Name 形态）— 抛异常
        if isinstance(node, ast.Call):
            func_name = _extract_call_name(node)
            if func_name is not None:
                if _is_dangerous_call(func_name):
                    snippet = _snippet_of(code, node)
                    raise ASTBannedNodeError(
                        node_type="Call(dangerous)",
                        snippet=snippet,
                        lineno=node.lineno,
                    )
                # 记录 call_graph（首次出现 — 供 namespace 构造，不再用于路由）
                if func_name not in call_graph:
                    executor = tools.get(func_name, "inline")
                    call_graph[func_name] = executor

    # D2: inline deprecated, all code goes to sandbox
    return InspectionResult(
        required_executor="sandbox",
        reasons=[],
        call_graph=call_graph,
    )


def _is_dangerous_call(func_name: str) -> bool:
    """判断是否为禁调用函数。"""
    return func_name in {
        "eval", "exec", "compile", "open",
        "getattr", "setattr", "delattr",
        "globals", "locals", "vars",
        "__import__",
    }


def _extract_call_name(call_node: ast.Call) -> Optional[str]:
    """从 ast.Call 节点提取函数名（仅处理 Name.func 和 Attribute.attr 两层形态）。

    返回:
        Name.func.id（如 buffer_geometry）
        Attribute.attr（如 module.buffer_geometry 中的 buffer_geometry）
        None（复杂形态如 lambda / 嵌套 call，不记录 call_graph）
    """
    func = call_node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _truncate(text: str, max_len: int = 80) -> str:
    """截断文本到 max_len 字符（异常 message 友好）。"""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def _snippet_of(code: str, node: ast.AST) -> str:
    """从源代码提取 AST 节点对应的代码片段。"""
    try:
        lines = code.splitlines()
        if hasattr(node, "lineno") and node.lineno:
            start = node.lineno - 1
            end = getattr(node, "end_lineno", node.lineno)
            if 0 <= start < len(lines) and start < end <= len(lines):
                snippet = "\n".join(lines[start:end])
                return _truncate(snippet)
    except Exception:
        pass
    return _truncate(ast.dump(node))