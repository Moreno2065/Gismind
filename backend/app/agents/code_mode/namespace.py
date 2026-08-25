"""code_mode 干净 namespace 构造。

`build_namespace(session_vars, allowed_tool_specs, tool_fns, execution_path)` 返回一个
**安全的** Python 命名空间 dict，可直接用作 `exec(code, namespace)` / `eval(expr, namespace)`。

安全模型：
- 白名单 built-in（无 os/sys/subprocess/__import__）
- 只读模块代理（math / json / re）
- tool_fns 按 ToolSpec 注入（async 工具自动 sync proxy，sandbox 工具在 inline 路径注入引导 stub）
- session_vars 最后注入；与工具名冲突时工具优先 + warning

安全靠**干净 namespace**（不暴露危险符号），AST 是性能优化。
"""
from __future__ import annotations

import asyncio
import builtins
import inspect
import json as _json
import math as _math
import re as _re
import sys as _sys
import threading
from typing import Any, Callable, Optional

# ============================================================
# 引导文案（sandbox 工具在 inline 路径注入的 stub 抛的异常 message）
# ============================================================

NOT_IN_SANDBOX_GUIDE_MESSAGE = (
    "is only available in sandbox execution mode. "
    "The framework will automatically route your code to sandbox if it contains "
    "while loops, file parsing operations, or other sandbox-only tool calls. "
    "You don't need to change anything."
)


# ============================================================
# Built-in 白名单
# ============================================================

SAFE_BUILTIN_NAMES = frozenset({
    "len", "range", "enumerate", "zip", "map", "filter", "sum",
    "min", "max", "abs", "round", "int", "float", "str", "bool",
    "list", "dict", "set", "tuple", "print", "isinstance", "type",
    "sorted", "reversed", "any", "all", "repr", "hash", "id",
    "next", "iter", "len", "ord", "chr", "hex", "oct", "bin",
    "True", "False", "None",
    # exceptions（让 LLM 代码能 raise/except，但 raise 本身被 AST 禁）
    "Exception", "ValueError", "TypeError", "KeyError", "IndexError",
    "StopIteration", "RuntimeError", "AttributeError", "OSError",
    "ZeroDivisionError", "NotImplementedError", "FileNotFoundError",
})

# 只读模块代理（model 可用 math.* / json.dumps / re.match 等）
SAFE_MODULES = {
    "math": _math,
    "json": _json,
    "re": _re,
}


# ============================================================
# async 工具同步包装器
# ============================================================

def _sync_proxy(real_fn: Callable) -> Callable:
    """把 async 函数包成 sync 函数（LLM 代码是同步的）。

    内部用 `tool_execution._run_async`（tool_execution.py:109）复用线程本地 loop，
    自动处理嵌套 event loop 的情况（在 async context 中调用不崩）。

    若 real_fn 实际是 sync 函数（_build_code_mode_tool_fns 已是 sync wrapper），
    则直接调用后返回结果，不走 _run_async。
    """
    def wrapper(*args, **kwargs):
        result = real_fn(*args, **kwargs)
        if asyncio.iscoroutine(result):
            from app.agents.tool_execution import _run_async
            return _run_async(result)
        return result

    wrapper.__name__ = real_fn.__name__
    wrapper.__doc__ = real_fn.__doc__
    wrapper.__signature__ = inspect.signature(real_fn)  # type: ignore[attr-defined]
    wrapper.__wrapped__ = real_fn  # 标记原始函数，便于测试 / 调试
    return wrapper


# ============================================================
# NotInSandboxError stub
# ============================================================

def _make_not_in_sandbox_stub(tool_name: str) -> Callable:
    """生成 sandbox 工具在 inline 路径的 stub 函数（抛引导修复异常）。"""
    def stub(*args, **kwargs):
        raise RuntimeError(
            f"{tool_name}() {NOT_IN_SANDBOX_GUIDE_MESSAGE}"
        )
    stub.__name__ = tool_name
    stub.__doc__ = f"[Stub] {tool_name} {NOT_IN_SANDBOX_GUIDE_MESSAGE}"
    return stub


# ============================================================
# 干净 namespace 构造
# ============================================================

def build_namespace(
    session_vars: dict[str, Any],
    allowed_tool_specs: list,  # list[ToolSpec]
    tool_fns: Optional[dict[str, Callable]] = None,
    execution_path: str = "inline",
) -> dict:
    """构造 exec/eval 用的干净 namespace。

    Args:
        session_vars: 跨 step 持久化的变量（LLM 通过 `__result__ = {...}` 写入）。
        allowed_tool_specs: 当前 sub-agent 允许的工具列表（list[ToolSpec]）。
        tool_fns: 工具名 → 真实函数的映射（async 工具是原始 async 函数，inline/sandbox 是同步）。
        execution_path: 当前执行路径（"inline" / "sandbox"）。
                        inline 路径：sandbox 工具注入 NotInSandboxError stub。
                        sandbox 路径：sandbox 工具注入真实函数。

    Returns:
        dict: 可直接用作 exec(code, ns) / eval(expr, ns) 的 namespace。
    """
    tool_fns = tool_fns or {}
    ns: dict[str, Any] = {}

    # 1. Built-in 白名单（覆盖原始 __builtins__）
    # 注意：不把 __builtins__ 自身放入 safe_builtins，避免 LLM 代码通过
    # __builtins__['__import__'] 绕过 AST guard 实现 RCE。
    safe_builtins = {name: getattr(builtins, name) for name in SAFE_BUILTIN_NAMES if hasattr(builtins, name)}

    # 2. 只读模块代理
    for mod_name, mod_obj in SAFE_MODULES.items():
        safe_builtins[mod_name] = mod_obj

    ns.update(safe_builtins)

    # 设置受限 __builtins__：exec 需要此键，否则 Python 会注入真实 builtins 模块。
    # 只暴露白名单内容，不暴露 __import__/getattr/setattr 等危险函数。
    ns["__builtins__"] = dict(safe_builtins)  # 独立副本，不含 __builtins__ 键自身

    # 3. 工具注入（先工具，再 session_vars — 工具优先）
    injected_tools: set[str] = set()
    for spec in allowed_tool_specs:
        name = spec.name
        real_fn = tool_fns.get(name)
        if spec.executor_type == "sandbox" and execution_path == "inline":
            # inline 路径下 sandbox 工具注入引导 stub
            ns[name] = _make_not_in_sandbox_stub(name)
        elif real_fn is None:
            # 没传真实函数 — 注入一个占位 stub（不抛错，让模型看到 NameError 自行修复）
            ns[name] = _make_placeholder_stub(name)
        elif spec.is_async or spec.executor_type == "async":
            # async 工具 → 同步 proxy
            ns[name] = _sync_proxy(real_fn)
        else:
            # inline / sandbox 真实路径 → 直接注入
            ns[name] = real_fn
        injected_tools.add(name)

    # 4. session_vars 注入（命名冲突防护：工具优先）
    shadowed = []
    for k, v in session_vars.items():
        if k in injected_tools:
            shadowed.append(k)
            continue
        ns[k] = v

    # 5. 命名冲突 warning（stdout 让模型能看到）
    if shadowed:
        print(
            f"Warning: session variables {shadowed} shadow tool names, ignored",
            file=_sys.stdout,
        )

    return ns


def _make_placeholder_stub(tool_name: str) -> Callable:
    """未传真实函数时的占位 stub（raise NotImplementedError）。"""
    def stub(*args, **kwargs):
        raise NotImplementedError(
            f"{tool_name}() has no implementation registered in this namespace"
        )
    stub.__name__ = tool_name
    return stub