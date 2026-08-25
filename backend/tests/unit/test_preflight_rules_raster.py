"""Unit tests for raster preflight guardrail rules."""
from __future__ import annotations

import pytest

from app.agents.workspace.state import WorkspaceState
from app.agents.preflight.rules_raster import (
    _check_raster_band,
    _check_raster_nodata,
    _check_reclassify_fields,
    _check_reclassify_io_types,
)
from app.agents.preflight.registry import preflight_for


# ------------------------------------------------------------------
# raster_band_exists
# ------------------------------------------------------------------

def test_raster_band_valid():
    """band=1 应通过。"""
    issues = _check_raster_band({"kwargs": {"band": 1}})
    assert len(issues) == 0


def test_raster_band_default():
    """band 未传 → 默认值 1，不报错。"""
    issues = _check_raster_band({"kwargs": {}})
    assert len(issues) == 0


def test_raster_band_negative():
    """band=-1 应阻止。"""
    issues = _check_raster_band({"kwargs": {"band": -1}})
    assert len(issues) == 1
    assert issues[0].code == "raster_band_invalid"
    assert issues[0].severity == "error"


def test_raster_band_zero():
    """band=0 应阻止。"""
    issues = _check_raster_band({"kwargs": {"band": 0}})
    assert len(issues) == 1
    assert issues[0].code == "raster_band_invalid"
    assert issues[0].severity == "error"


def test_raster_band_string():
    """band="abc" 非整数应阻止。"""
    issues = _check_raster_band({"kwargs": {"band": "abc"}})
    assert len(issues) == 1
    assert issues[0].code == "raster_band_invalid"


def test_raster_band_large():
    """band=100 应通过（不检查实际 band count）。"""
    issues = _check_raster_band({"kwargs": {"band": 100}})
    assert len(issues) == 0


# ------------------------------------------------------------------
# raster_band_exists — registry integration
# ------------------------------------------------------------------

def test_raster_band_registry_slope():
    """通过 registry 对 slope 执行规则。"""
    issues = preflight_for("slope", {"kwargs": {"band": "x"}})
    codes = [i.code for i in issues]
    assert "raster_band_invalid" in codes


def test_raster_band_registry_aspect():
    """通过 registry 对 aspect 执行规则。"""
    issues = preflight_for("aspect", {"kwargs": {"band": 0}})
    codes = [i.code for i in issues]
    assert "raster_band_invalid" in codes


# ------------------------------------------------------------------
# raster_nodata_warning
# ------------------------------------------------------------------

def test_raster_nodata_warning_always_emits():
    """NoData warning 总是输出。"""
    issues = _check_raster_nodata({"kwargs": {}})
    assert len(issues) == 1
    assert issues[0].code == "raster_nodata_warning"
    assert issues[0].severity == "warning"
    assert "NoData" in issues[0].message


def test_raster_nodata_warning_registry():
    """通过 registry 对 raster_calculator 触发。"""
    issues = preflight_for("raster_calculator", {"kwargs": {}})
    codes = [i.code for i in issues]
    assert "raster_nodata_warning" in codes


# ------------------------------------------------------------------
# reclassify_table_fields
# ------------------------------------------------------------------

def test_reclassify_fields_valid_range():
    """区间重分类：bins=3, values=4（合法）。"""
    issues = _check_reclassify_fields(
        {"kwargs": {"bins": [3, 6, 9], "values": [10, 20, 30, 40]}}
    )
    assert len(issues) == 0


def test_reclassify_fields_valid_replace():
    """逐值替换：bins=3, values=3（合法）。"""
    issues = _check_reclassify_fields(
        {"kwargs": {"bins": [1, 2, 3], "values": [100, 200, 300]}}
    )
    assert len(issues) == 0


def test_reclassify_fields_mismatch():
    """bins=3, values=2（不合法）。"""
    issues = _check_reclassify_fields(
        {"kwargs": {"bins": [1, 2, 3], "values": [10, 20]}}
    )
    assert len(issues) == 1
    assert issues[0].code == "reclassify_length_mismatch"
    assert issues[0].severity == "error"


def test_reclassify_fields_bins_1_values_3():
    """bins=1, values=3（不合法）。"""
    issues = _check_reclassify_fields(
        {"kwargs": {"bins": [5], "values": [10, 20, 30]}}
    )
    assert len(issues) == 1
    assert issues[0].code == "reclassify_length_mismatch"


def test_reclassify_missing_bins():
    """缺少 bins。"""
    issues = _check_reclassify_fields(
        {"kwargs": {"values": [10, 20]}}
    )
    assert len(issues) == 1
    assert issues[0].code == "reclassify_missing_bins"


def test_reclassify_missing_values():
    """缺少 values。"""
    issues = _check_reclassify_fields(
        {"kwargs": {"bins": [1, 2]}}
    )
    assert len(issues) == 1
    assert issues[0].code == "reclassify_missing_values"


def test_reclassify_both_missing():
    """bins 和 values 都未传（由函数默认值处理）。"""
    issues = _check_reclassify_fields({"kwargs": {}})
    assert len(issues) == 0


def test_reclassify_bins_not_list():
    """bins 不是列表。"""
    issues = _check_reclassify_fields(
        {"kwargs": {"bins": "not_list", "values": [1, 2]}}
    )
    assert len(issues) == 1
    assert issues[0].code == "reclassify_bins_not_list"


def test_reclassify_values_not_list():
    """values 不是列表。"""
    issues = _check_reclassify_fields(
        {"kwargs": {"bins": [1, 2], "values": 42}}
    )
    assert len(issues) == 1
    assert issues[0].code == "reclassify_values_not_list"


# ------------------------------------------------------------------
# reclassify_io_types
# ------------------------------------------------------------------

def test_reclassify_io_types_raster_ok():
    """raster 类型输入应通过。"""
    ws = WorkspaceState({})
    ws.add_layer(name="dem", kind="raster", metadata={"kind": "raster"})
    issues = _check_reclassify_io_types({
        "workspace": ws,
        "kwargs": {"input_ref": "dem"},
    })
    assert len(issues) == 0


def test_reclassify_io_types_vector_blocked():
    """vector 类型输入应阻止。"""
    ws = WorkspaceState({})
    ws.add_layer(name="roads", kind="vector", metadata={"kind": "vector"})
    issues = _check_reclassify_io_types({
        "workspace": ws,
        "kwargs": {"input_ref": "roads"},
    })
    assert len(issues) == 1
    assert issues[0].code == "reclassify_not_raster_input"
    assert issues[0].severity == "error"
    assert "raster" in issues[0].message.lower()


def test_reclassify_io_types_no_workspace():
    """无 workspace 应跳过。"""
    issues = _check_reclassify_io_types({
        "workspace": None,
        "kwargs": {"input_ref": "dem"},
    })
    assert len(issues) == 0


def test_reclassify_io_types_no_input_ref():
    """无 input_ref 应跳过。"""
    ws = WorkspaceState({})
    issues = _check_reclassify_io_types({
        "workspace": ws,
        "kwargs": {},
    })
    assert len(issues) == 0


def test_reclassify_io_types_missing_layer():
    """不存在的图层应跳过（layer_exists 处理）。"""
    ws = WorkspaceState({})
    issues = _check_reclassify_io_types({
        "workspace": ws,
        "kwargs": {"input_ref": "nonexistent"},
    })
    assert len(issues) == 0


def test_reclassify_io_types_no_kind_metadata():
    """无 kind metadata 的图层应跳过。"""
    ws = WorkspaceState({})
    ws.add_layer(name="unknown", kind="unknown", metadata={})
    issues = _check_reclassify_io_types({
        "workspace": ws,
        "kwargs": {"input_ref": "unknown"},
    })
    assert len(issues) == 0
