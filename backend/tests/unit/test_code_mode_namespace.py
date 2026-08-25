"""Tests for app.agents.code_mode.namespace + registry ToolSpec.

覆盖：
- 干净 namespace（os/sys/subprocess/__builtins__ 不可达）
- async 工具同步包装器（_sync_proxy）
- sandbox 工具在 inline 路径注入 NotInSandboxError stub（引导修复文案）
- session_vars 命名冲突防护（工具优先，跳过变量 + warning）
- async context 内调用 sync_proxy 不崩
- ToolSpec / TOOL_SPECS / SubAgentSpec 分桶属性
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from typing import Any

import pytest

from app.agents.code_mode.namespace import (
    build_namespace,
    _sync_proxy,
    NOT_IN_SANDBOX_GUIDE_MESSAGE,
)
from app.agents.registry import (
    TOOL_SPECS,
    ToolSpec,
    SubAgentSpec,
    REGISTRY,
)


# ============================================================
# ToolSpec + TOOL_SPECS
# ============================================================

def test_tool_spec_has_required_fields():
    """ToolSpec 必含 name / executor_type / is_async / description。"""
    spec = ToolSpec(
        name="test_tool",
        executor_type="inline",
        is_async=False,
        description="test",
    )
    assert spec.name == "test_tool"
    assert spec.executor_type == "inline"
    assert spec.is_async is False
    assert spec.description == "test"


def test_TOOL_SPECS_contains_required_tools():
    """TOOL_SPECS 必须含所有已接入的工具 + 拆分后的新工具。"""
    required_tools = {
        "geo_code", "query_poi",  # async (existing)
        "buffer", "overlay", "voronoi", "isochrone", "map_layer_build",  # inline (existing)
        "code_executor",  # sandbox (existing)
        "fetch_from_redis", "parse_zip",  # 新拆分
    }
    actual_tools = set(TOOL_SPECS.keys())
    missing = required_tools - actual_tools
    assert not missing, f"TOOL_SPECS 缺失工具：{missing}"


def test_TOOL_SPECS_async_classification():
    """async 类工具：geo_code / query_poi / fetch_from_redis / data_io_read。"""
    async_tools = {
        name for name, spec in TOOL_SPECS.items()
        if spec.executor_type == "async"
    }
    assert "geo_code" in async_tools
    assert "query_poi" in async_tools
    assert "fetch_from_redis" in async_tools


def test_TOOL_SPECS_inline_classification():
    """inline 类工具：buffer / overlay / voronoi / isochrone / map_layer_build。"""
    inline_tools = {
        name for name, spec in TOOL_SPECS.items()
        if spec.executor_type == "inline"
    }
    assert "buffer" in inline_tools
    assert "overlay" in inline_tools
    assert "map_layer_build" in inline_tools


def test_TOOL_SPECS_sandbox_classification():
    """sandbox 类工具：parse_zip / code_executor。"""
    sandbox_tools = {
        name for name, spec in TOOL_SPECS.items()
        if spec.executor_type == "sandbox"
    }
    assert "parse_zip" in sandbox_tools
    assert "code_executor" in sandbox_tools


def test_TOOL_SPECS_is_async_consistent_with_executor_type():
    """async 工具必须 is_async=True（LLM 调的是同步 proxy）。"""
    for name, spec in TOOL_SPECS.items():
        if spec.executor_type == "async":
            assert spec.is_async is True, (
                f"{name} is executor_type='async' but is_async={spec.is_async}"
            )


# ============================================================
# SubAgentSpec 分桶属性
# ============================================================

def test_sub_agent_inline_tools_property():
    """SubAgentSpec.inline_tools 只返回 inline 类型的工具名。"""
    geo_spec = REGISTRY["geo"]
    inline = geo_spec.inline_tools
    # geo sub-agent 有 geo_code（async）+ geo_transform（inline）
    assert "geo_transform" in inline
    assert "geo_code" not in inline


def test_sub_agent_async_tools_property():
    """SubAgentSpec.async_tools 只返回 async 类型的工具名。"""
    poi_spec = REGISTRY["poi"]
    async_tools = poi_spec.async_tools
    assert "geo_code" in async_tools or "query_poi" in async_tools


def test_sub_agent_sandbox_tools_property():
    """SubAgentSpec.sandbox_tools 只返回 sandbox 类型的工具名。"""
    coder_spec = REGISTRY["coder"]
    sandbox = coder_spec.sandbox_tools
    assert "code_executor" in sandbox


def test_unknown_tool_in_sub_agent_defaults_to_sandbox():
    """未在 TOOL_SPECS 登记的工具默认 sandbox + warning（向后兼容）。"""
    custom_spec = SubAgentSpec(
        agent_role="custom",
        system_prompt_path="dummy",
        tool_names=["unknown_tool_xyz"],
    )
    # 应该归到 sandbox（默认）
    assert "unknown_tool_xyz" in custom_spec.sandbox_tools
    assert "unknown_tool_xyz" not in custom_spec.inline_tools
    assert "unknown_tool_xyz" not in custom_spec.async_tools


# ============================================================
# 干净 namespace — 禁危险符号
# ============================================================

def test_build_namespace_excludes_os():
    """namespace 中 os 不可达（NameError）。"""
    ns = build_namespace(session_vars={}, allowed_tool_specs=[], tool_fns={})
    assert "os" not in ns
    with pytest.raises(NameError):
        eval("os.system('echo pwned')", ns)


def test_build_namespace_excludes_sys():
    """namespace 中 sys 不可达。"""
    ns = build_namespace(session_vars={}, allowed_tool_specs=[], tool_fns={})
    assert "sys" not in ns


def test_build_namespace_excludes_subprocess():
    """namespace 中 subprocess 不可达。"""
    ns = build_namespace(session_vars={}, allowed_tool_specs=[], tool_fns={})
    assert "subprocess" not in ns


def test_build_namespace_excludes_dunder_builtins():
    """namespace 不暴露 __builtins__（除非通过白名单 built-in）。"""
    ns = build_namespace(session_vars={}, allowed_tool_specs=[], tool_fns={})
    # 不暴露原始 __builtins__
    raw_builtins = ns.get("__builtins__")
    if raw_builtins is not None:
        # 如果存在，必须是 dict（白名单 dict，不是 module）
        assert isinstance(raw_builtins, dict), (
            f"__builtins__ 应该是 dict（白名单），实际：{type(raw_builtins)}"
        )
        # 且 dict 里不能含 os/sys/subprocess/__import__
        assert "os" not in raw_builtins
        assert "sys" not in raw_builtins
        assert "subprocess" not in raw_builtins
        assert "__import__" not in raw_builtins


def test_build_namespace_includes_safe_builtins():
    """namespace 包含安全 built-in 白名单。"""
    ns = build_namespace(session_vars={}, allowed_tool_specs=[], tool_fns={})
    for safe in ["len", "range", "print", "int", "str", "list", "dict", "sum", "max", "min"]:
        assert safe in ns, f"safe built-in {safe!r} 应在 namespace"


def test_build_namespace_includes_readonly_modules():
    """namespace 包含 math / json / re 等只读模块代理。"""
    ns = build_namespace(session_vars={}, allowed_tool_specs=[], tool_fns={})
    assert "math" in ns
    assert "json" in ns
    assert "re" in ns


def test_exec_import_in_namespace_raises_blocked_error():
    """namespace 中执行代码不能 import（__import__ 被白名单覆盖）。"""
    ns = build_namespace(session_vars={}, allowed_tool_specs=[], tool_fns={})
    code = "import os"
    with pytest.raises((NameError, ImportError)):
        exec(code, ns)


# ============================================================
# async 工具同步包装器
# ============================================================

def test_sync_proxy_returns_value_not_coroutine():
    """_sync_proxy(real_async_fn)(*args) 直接返回 await 的结果，不是 coroutine。"""
    async def my_async_fn(x, y):
        await asyncio.sleep(0)
        return x + y

    proxy = _sync_proxy(my_async_fn)
    result = proxy(2, 3)
    assert result == 5
    assert not inspect.iscoroutine(result)


def test_sync_proxy_preserves_name_and_doc():
    """_sync_proxy 保留原函数的 __name__ 和 __doc__（verifier 读这些字段）。"""
    async def my_async_fn(x):
        """My docstring."""
        return x

    proxy = _sync_proxy(my_async_fn)
    assert proxy.__name__ == "my_async_fn"
    assert proxy.__doc__ == "My docstring."


def test_sync_proxy_in_async_context_does_not_crash():
    """🔴 关键：在 asyncio running loop 内调用 _sync_proxy 不崩（_run_async 自动 fallback）。

    LangGraph async node 调用 sync_proxy 时已经在 event loop 线程里，
    _run_async 必须正确处理这种情况，不能 "RuntimeError: This event loop is already running"。
    """
    async def my_async_fn(x):
        await asyncio.sleep(0)
        return x * 2

    proxy = _sync_proxy(my_async_fn)

    async def main():
        # 现在 event loop 在跑，proxy 内部 _run_async 必须 fallback 到独立线程
        result = proxy(21)
        assert result == 42

    asyncio.run(main())


# ============================================================
# sandbox 工具在 inline 路径注入 NotInSandboxError stub
# ============================================================

def test_sandbox_tool_in_inline_namespace_raises_guide_error():
    """inline namespace 中调用 sandbox 工具抛引导修复文案（不是普通 NameError）。"""
    ns = build_namespace(
        session_vars={},
        allowed_tool_specs=[TOOL_SPECS["parse_zip"]],
        tool_fns={"parse_zip": lambda raw: "real parse"},  # 真函数不该被调用
        execution_path="inline",
    )
    assert "parse_zip" in ns
    with pytest.raises(RuntimeError) as exc_info:
        ns["parse_zip"](b"bytes")
    assert NOT_IN_SANDBOX_GUIDE_MESSAGE in str(exc_info.value)


def test_sandbox_tool_in_sandbox_namespace_works():
    """sandbox namespace 中 sandbox 工具是真实函数。"""
    ns = build_namespace(
        session_vars={},
        allowed_tool_specs=[TOOL_SPECS["parse_zip"]],
        tool_fns={"parse_zip": lambda raw: "parsed:" + str(len(raw))},
        execution_path="sandbox",
    )
    result = ns["parse_zip"](b"hello")
    assert result == "parsed:5"


def test_inline_tool_in_inline_namespace_works():
    """inline namespace 中 inline 工具是真实函数。"""
    ns = build_namespace(
        session_vars={},
        allowed_tool_specs=[TOOL_SPECS["buffer"]],
        tool_fns={"buffer": lambda geom, distance: f"buffered({distance})"},
        execution_path="inline",
    )
    result = ns["buffer"]("geom", 500)
    assert result == "buffered(500)"


def test_async_tool_in_inline_namespace_wrapped_as_sync_proxy():
    """async 工具在 inline namespace 中以 sync proxy 形式注入（不是原始 coroutine）。"""
    async def async_geo_code(address):
        await asyncio.sleep(0)
        return {"location": (0, 0), "address": address}

    ns = build_namespace(
        session_vars={},
        allowed_tool_specs=[TOOL_SPECS["geo_code"]],
        tool_fns={"geo_code": async_geo_code},
        execution_path="inline",
    )
    assert "geo_code" in ns
    assert not inspect.iscoroutinefunction(ns["geo_code"]), (
        "async 工具在 namespace 里必须是 sync proxy，不能是 async 函数"
    )


# ============================================================
# session_vars 命名冲突防护
# ============================================================

def test_session_vars_do_not_shadow_tool_names(capsys):
    """🔴 关键：session_vars[k] 与工具名冲突时，工具优先，跳过变量 + warning。"""
    ns = build_namespace(
        session_vars={"buffer": "this is a string, not a tool"},
        allowed_tool_specs=[TOOL_SPECS["buffer"]],
        tool_fns={"buffer": lambda geom, distance: f"buffered({distance})"},
        execution_path="inline",
    )
    # 工具保留
    assert ns["buffer"]("geom", 500) == "buffered(500)"
    # 变量被跳过
    assert ns["buffer"]("geom", 500) != "this is a string, not a tool"
    # warning 输出到 stdout
    captured = capsys.readouterr()
    assert "shadow" in captured.out.lower() or "buffer" in captured.out


def test_session_vars_no_conflict_includes_value():
    """无冲突时 session_vars 正常注入 namespace。"""
    ns = build_namespace(
        session_vars={"my_data": [1, 2, 3]},
        allowed_tool_specs=[],
        tool_fns={},
    )
    assert ns["my_data"] == [1, 2, 3]


def test_session_vars_iteration_in_namespace():
    """session_vars 里的 dict / list 正常可迭代。"""
    ns = build_namespace(
        session_vars={"pois": [{"id": 1}, {"id": 2}, {"id": 3}]},
        allowed_tool_specs=[],
        tool_fns={},
    )
    assert len(ns["pois"]) == 3
    assert ns["pois"][1]["id"] == 2


# ============================================================
# 综合
# ============================================================

def test_realistic_inline_namespace():
    """模拟 geometer sub-agent 的真实 namespace：inline + async 工具。"""
    ns = build_namespace(
        session_vars={"origin": (118.78, 32.04)},  # 南京新街口
        allowed_tool_specs=[
            TOOL_SPECS["buffer"],
            TOOL_SPECS["overlay"],
            TOOL_SPECS["map_layer_build"],
        ],
        tool_fns={
            "buffer": lambda geom, distance_m: f"buffer({distance_m}m)",
            "overlay": lambda a, b, how="intersection": f"overlay({how})",
            "map_layer_build": lambda geom: "layer",
        },
        execution_path="inline",
    )
    assert callable(ns["buffer"])
    assert callable(ns["overlay"])
    assert callable(ns["map_layer_build"])
    assert ns["origin"] == (118.78, 32.04)
    assert ns["buffer"]("geom", 500) == "buffer(500m)"


def test_realistic_sandbox_namespace_includes_sandbox_tools():
    """模拟 sandbox 路径的 namespace：sandbox 工具可用。"""
    async def fetch_from_redis(key):
        await asyncio.sleep(0)
        return b"redis-bytes"

    def parse_zip(raw):
        return f"parsed({len(raw)})"

    ns = build_namespace(
        session_vars={},
        allowed_tool_specs=[
            TOOL_SPECS["fetch_from_redis"],
            TOOL_SPECS["parse_zip"],
        ],
        tool_fns={
            "fetch_from_redis": fetch_from_redis,
            "parse_zip": parse_zip,
        },
        execution_path="sandbox",
    )
    # 两个工具都能用
    assert ns["fetch_from_redis"]("k1") == b"redis-bytes"
    assert ns["parse_zip"](b"hello") == "parsed(5)"


def test_data_io_read_is_primary_upload_interface():
    """模型只应使用能完成读取和解析的单一上传接口。"""
    assert "data_io_read" in TOOL_SPECS
    assert "fetch_from_redis" in TOOL_SPECS
    assert "parse_zip" in TOOL_SPECS
    assert TOOL_SPECS["data_io_read"].deprecated is False
    assert TOOL_SPECS["fetch_from_redis"].deprecated is True
    assert TOOL_SPECS["parse_zip"].deprecated is True


# ============================================================
# NOT_IN_SANDBOX_GUIDE_MESSAGE 内容
# ============================================================

def test_guide_message_contains_framework_keywords():
    """引导文案含 'framework' / 'automatically' / 'sandbox' 等关键词，让模型知道框架兜底。"""
    assert "framework" in NOT_IN_SANDBOX_GUIDE_MESSAGE.lower()
    assert "automatic" in NOT_IN_SANDBOX_GUIDE_MESSAGE.lower()
    assert "sandbox" in NOT_IN_SANDBOX_GUIDE_MESSAGE.lower()
