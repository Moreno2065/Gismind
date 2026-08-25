"""Tests for app.agents.code_mode.ast_guard.

D2 决策后：`inspect()` 始终返回 `required_executor="sandbox"`。
测试覆盖 banned 节点检测（ASTBannedNodeError）和 call_graph 追踪。
outright banned 节点抛 `ASTBannedNodeError` 异常。
"""
from __future__ import annotations

import pytest

from app.agents.code_mode.ast_guard import inspect as ast_inspect
from app.agents.code_mode.ast_guard import (
    InspectionResult,
    ASTBannedNodeError,
)


# ============================================================
# 基础契约 — 默认走 sandbox（D2: inline deprecated）
# ============================================================

def test_inspect_returns_inspection_result():
    """inspect() 必须返回 InspectionResult dataclass，不是 dict/tuple。"""
    result = ast_inspect("x = 1")
    assert isinstance(result, InspectionResult)


def test_inspect_clean_code_returns_sandbox():
    """纯赋值/计算无副作用 → required_executor="sandbox"（D2: inline deprecated）。"""
    result = ast_inspect("x = 1\ny = 2\nz = x + y")
    assert result.required_executor == "sandbox"
    assert result.reasons == []
    assert result.call_graph == {}


def test_for_loop_is_sandbox():
    """for 循环也走 sandbox。"""
    code = "total = 0\nfor i in range(10):\n    total += i\n"
    result = ast_inspect(code)
    assert result.required_executor == "sandbox"


def test_if_branch_is_sandbox():
    code = "if x > 0:\n    y = 1\nelse:\n    y = 2\n"
    result = ast_inspect(code)
    assert result.required_executor == "sandbox"


def test_list_comprehension_is_sandbox():
    result = ast_inspect("xs = [i * 2 for i in range(10)]")
    assert result.required_executor == "sandbox"


def test_dict_comprehension_is_sandbox():
    result = ast_inspect("d = {k: v for k, v in [('a', 1)]}")
    assert result.required_executor == "sandbox"


def test_empty_code_is_sandbox():
    """空字符串 / 只有注释：sandbox。"""
    assert ast_inspect("").required_executor == "sandbox"
    assert ast_inspect("# only comment\n").required_executor == "sandbox"


def test_function_def_is_sandbox():
    """FunctionDef 已從 banned 列表移除，應放行爲 sandbox。"""
    result = ast_inspect("def helper():\n    return 1\n")
    assert result.required_executor == "sandbox"


def test_function_def_import_inside_is_still_banned():
    """函數體內的 Import 仍然被攔截。"""
    with pytest.raises(ASTBannedNodeError) as exc_info:
        ast_inspect("def malicious():\n    import os\n    os.system('ls')\n")
    assert "Import" in str(exc_info.value)


# ============================================================
# 路由到 sandbox — While / AsyncFor / range / sandbox tool call（D2 后全部走 sandbox）
# ============================================================

def test_routes_while_loop_to_sandbox():
    """While 路由到 sandbox — D2 后所有代码走 sandbox，reasons 始终为空。"""
    code = "while True:\n    pass\n"
    result = ast_inspect(code)
    assert result.required_executor == "sandbox"
    assert result.reasons == []


def test_routes_while_with_condition_to_sandbox():
    """带条件的 while 也路由。"""
    code = "while x > 0:\n    x -= 1\n"
    result = ast_inspect(code)
    assert result.required_executor == "sandbox"


def test_routes_async_for_to_sandbox():
    """AsyncFor 同等对待 While — D2 后全部走 sandbox。"""
    code = "async for item in some_async_iter():\n    print(item)\n"
    result = ast_inspect(code)
    assert result.required_executor == "sandbox"
    assert result.reasons == []


def test_routes_range_huge_literal_to_sandbox():
    """range(Constant > 1_000_000) 路由到 sandbox — D2 后 range 炸弹检测已移除，统一走 sandbox。"""
    code = "for i in range(10**9):\n    pass\n"
    result = ast_inspect(code)
    assert result.required_executor == "sandbox"
    assert result.reasons == []


def test_routes_range_one_million_plus_to_sandbox():
    """range(1_000_001) 也算炸弹 — D2 后统一走 sandbox。"""
    code = "for i in range(1_000_001):\n    pass\n"
    result = ast_inspect(code)
    assert result.required_executor == "sandbox"


def test_allows_range_one_million_exactly():
    """边界：range(1_000_000) — D2 后统一走 sandbox。"""
    code = "for i in range(1_000_000):\n    pass\n"
    result = ast_inspect(code)
    assert result.required_executor == "sandbox"


def test_allows_range_small_literal():
    """range(10) — D2 后统一走 sandbox。"""
    code = "for i in range(10):\n    print(i)\n"
    result = ast_inspect(code)
    assert result.required_executor == "sandbox"


def test_allows_range_with_variable():
    """range(n) — D2 后统一走 sandbox。"""
    code = "for i in range(n):\n    print(i)\n"
    result = ast_inspect(code)
    assert result.required_executor == "sandbox"


def test_routes_sandbox_tool_call_to_sandbox():
    """调用了 sandbox-typed 工具的代码 — D2 后统一走 sandbox，reasons 为空。"""
    code = "data = parse_zip(raw_bytes)"
    result = ast_inspect(
        code,
        known_tools={"parse_zip": "sandbox"},
    )
    assert result.required_executor == "sandbox"
    assert result.reasons == []


# ============================================================
# outright banned — 抛 ASTBannedNodeError 异常
# ============================================================

@pytest.mark.parametrize(
    "code",
    [
        "import os",
        "import os, sys",
        "from os import system",
        "from gismind.tools import buffer_geometry",
        "async def helper():\n    return 1\n",
        "class Foo:\n    pass\n",
        "del x\n",
        "with open('x') as f:\n    data = f.read()\n",
        "raise ValueError('x')\n",
        "assert x == 1\n",
        "global x\n",
        "nonlocal x\n",
        "lambda x: x + 1\n",
    ],
)
def test_raises_ast_banned_for_outright_nodes(code):
    """outright banned 节点抛 ASTBannedNodeError 异常（不是返回 inline=false）。"""
    with pytest.raises(ASTBannedNodeError) as exc_info:
        ast_inspect(code)
    # 异常信息含节点名
    assert exc_info.value.node_type, "ASTBannedNodeError 必须含 node_type"
    assert exc_info.value.snippet, "ASTBannedNodeError 必须含 snippet"


def test_importfrom_thoroughly_banned():
    """ImportFrom 完全禁（不部分放行 `from gismind.tools import xxx`）— 减少模型记忆负担。"""
    with pytest.raises(ASTBannedNodeError):
        ast_inspect("from gismind.tools import buffer_geometry")


# ============================================================
# 禁 dunder 属性
# ============================================================

@pytest.mark.parametrize(
    "code",
    [
        "x = obj.__class__",
        "x = obj.__dict__",
        "x = obj.__bases__",
        "x = obj.__subclasses__()",
        "x = obj.__globals__",
        "x = obj.__import__('os')",
        "x = ''.__class__.__mro__",
    ],
)
def test_blocks_dunder_attribute_access(code):
    """dunder 属性访问抛 ASTBannedNodeError。"""
    with pytest.raises(ASTBannedNodeError) as exc_info:
        ast_inspect(code)
    assert "__" in exc_info.value.snippet or "dunder" in exc_info.value.snippet.lower()


# ============================================================
# 禁危险 call
# ============================================================

@pytest.mark.parametrize(
    "func_name",
    [
        "eval", "exec", "compile", "open",
        "getattr", "setattr", "delattr",
        "globals", "locals", "vars",
        "__import__",
    ],
)
def test_blocks_dangerous_calls(func_name):
    """危险内置函数调用抛 ASTBannedNodeError。"""
    code = f"x = {func_name}('foo')"
    with pytest.raises(ASTBannedNodeError) as exc_info:
        ast_inspect(code)
    assert func_name in exc_info.value.snippet


def test_blocks_nested_dangerous_call():
    """嵌套调用也算：foo(eval('x'))。"""
    code = "foo(eval('1+1'))"
    with pytest.raises(ASTBannedNodeError) as exc_info:
        ast_inspect(code)
    assert "eval" in exc_info.value.snippet


# ============================================================
# call_graph 构造（executor_type 已知时记录）
# ============================================================

def test_call_graph_records_known_inline_tool():
    """call_graph 把已知 inline 工具记录到 graph — D2 后 required_executor 为 sandbox。"""
    code = "result = buffer_geometry(geom, 500)"
    result = ast_inspect(
        code,
        known_tools={"buffer_geometry": "inline"},
    )
    assert result.call_graph == {"buffer_geometry": "inline"}
    assert result.required_executor == "sandbox"


def test_call_graph_records_async_tool():
    code = "data = fetch_from_redis('k1')"
    result = ast_inspect(
        code,
        known_tools={"fetch_from_redis": "async"},
    )
    assert result.call_graph == {"fetch_from_redis": "async"}
    assert result.required_executor == "sandbox"


def test_call_graph_records_sandbox_tool_and_marks_sandbox_required():
    """调用 sandbox 工具的代码 required_executor="sandbox" — D2 后 reasons 为空。"""
    code = "data = parse_zip(raw_bytes)"
    result = ast_inspect(
        code,
        known_tools={"parse_zip": "sandbox"},
    )
    assert result.call_graph == {"parse_zip": "sandbox"}
    assert result.required_executor == "sandbox"
    assert result.reasons == []


def test_unknown_function_call_is_sandbox_by_default():
    """未知函数默认走 sandbox（D2: inline deprecated）。"""
    code = "x = some_random_function(1, 2)"
    result = ast_inspect(code, known_tools={})
    assert result.call_graph == {"some_random_function": "inline"}
    assert result.required_executor == "sandbox"


def test_call_graph_records_multiple_calls():
    """混合调用 inline/async + sandbox 工具 — D2 后不再抛 MixedToolCall，统一走 sandbox。"""
    code = (
        "raw = fetch_from_redis('k1')\n"
        "data = parse_zip(raw)\n"
        "buffered = buffer_geometry(data, 500)\n"
    )
    result = ast_inspect(
        code,
        known_tools={
            "fetch_from_redis": "async",
            "parse_zip": "sandbox",
            "buffer_geometry": "inline",
        },
    )
    assert result.required_executor == "sandbox"
    assert result.call_graph == {
        "fetch_from_redis": "async",
        "parse_zip": "sandbox",
        "buffer_geometry": "inline",
    }


def test_call_graph_deduplicates():
    """同一函数多次调用只记一次。"""
    code = (
        "buffer_geometry(a, 100)\n"
        "buffer_geometry(b, 200)\n"
        "buffer_geometry(c, 300)\n"
    )
    result = ast_inspect(
        code,
        known_tools={"buffer_geometry": "inline"},
    )
    assert result.call_graph == {"buffer_geometry": "inline"}
    assert result.required_executor == "sandbox"


def test_call_graph_attribute_call_recorded_by_attr_name():
    """Attribute.func 形态的 call（如 module.buffer_geometry()）按 attr 名记录。"""
    code = "data = tools.buffer_geometry(geom, 500)"
    result = ast_inspect(
        code,
        known_tools={"buffer_geometry": "inline"},
    )
    assert result.call_graph.get("buffer_geometry") == "inline"
    assert result.required_executor == "sandbox"


# ============================================================
# 语法错误处理
# ============================================================

def test_invalid_python_is_sandbox_with_empty_reasons():
    """语法错误 → required_executor="sandbox"（D2: inline deprecated），
    reasons 为空。
    """
    result = ast_inspect("def (broken syntax")
    assert result.required_executor == "sandbox"
    assert result.reasons == []


# ============================================================
# reasons 字段结构 — D2 后 reasons 始终为空
# ============================================================

def test_reasons_is_always_empty_list():
    """reasons 必须是空 list — D2 后不再填充 reasons。"""
    result = ast_inspect("while True:\n    pass\nfor i in range(10**9):\n    pass\n")
    assert isinstance(result.reasons, list)
    assert result.reasons == []


def test_sandbox_result_has_empty_reasons():
    """sandbox 路径时 reasons 始终为空（D2 后一致性）。"""
    result = ast_inspect("x = 1 + 2")
    assert result.required_executor == "sandbox"
    assert result.reasons == []


# ============================================================
# ASTBannedNodeError 异常类
# ============================================================

def test_ast_banned_node_error_carries_node_info():
    """ASTBannedNodeError 必须含 node_type 和 snippet，方便 executor / verifier 报错。"""
    try:
        ast_inspect("import os")
    except ASTBannedNodeError as e:
        assert e.node_type  # 非空
        assert e.snippet  # 非空
        assert "Import" in e.node_type or "Import" in e.snippet
    else:
        pytest.fail("expected ASTBannedNodeError")


# ============================================================
# 综合：真实模型可能写的代码
# ============================================================

def test_realistic_code_simple_pipeline():
    """模型真实写的代码：geocode → query_poi → buffer 组合 — D2 后走 sandbox。"""
    code = """
location = geo_code("南京新街口")
pois = query_poi(bbox=location, category="restaurant")
buffered = buffer_geometry(pois, 500)
__result__ = {"pois": pois, "buffered": buffered}
"""
    result = ast_inspect(
        code,
        known_tools={
            "geo_code": "async",
            "query_poi": "async",
            "buffer_geometry": "inline",
        },
    )
    assert result.required_executor == "sandbox"  # D2: all to sandbox
    assert result.call_graph == {
        "geo_code": "async",
        "query_poi": "async",
        "buffer_geometry": "inline",
    }


def test_realistic_code_with_while_for_filtering():
    """while 循环 + async 工具 — D2 后不再抛 MixedToolCall，统一走 sandbox。"""
    code = """
pois = query_poi(bbox=location, category="restaurant")
filtered = []
i = 0
while i < len(pois):
    if pois[i].rating > 4.0:
        filtered.append(pois[i])
    i += 1
__result__ = {"filtered": filtered}
"""
    result = ast_inspect(
        code,
        known_tools={"query_poi": "async"},
    )
    assert result.required_executor == "sandbox"
    assert result.call_graph["query_poi"] == "async"
    # D2 后 MixedToolCall 不再抛异常，call_graph 记录了所有调用（含 len、append 等内置）