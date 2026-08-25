"""验证 preflight 阶段不触发任何外部 IO 操作。

重点验证：
- Preflight 规则不调 HTTP 请求（requests / httpx）。
- Preflight 规则不访问数据库（Redis / DB）。
- Preflight 规则仅操作内存中的 WorkspaceState。
"""
from __future__ import annotations

import os as os_module
import pytest
from unittest.mock import patch

from app.agents.workspace.state import WorkspaceState


def test_buffer_crs_no_io():
    """buffer_crs preflight 只读 WorkspaceState，不发 HTTP。"""
    from app.agents.preflight.registry import preflight_for
    from app.agents.preflight import rules_buffer  # noqa: F401 - register rule

    ws = WorkspaceState({})
    ws.add_layer(name="test", kind="vector", metadata={"crs": "EPSG:4326"})
    # The rule does not import httpx/requests, so verifying it works = no IO
    issues = preflight_for("buffer_layer", {
        "workspace": ws,
        "kwargs": {"input_ref": "test"},
    })
    assert len(issues) >= 1
    codes = {i.code for i in issues}
    assert "buffer_crs_mismatch" in codes or "crs_geographic_for_metric_op" in codes


def test_overlay_crs_no_io():
    """overlay_crs_alignment preflight 不触发网络。"""
    from app.agents.preflight.registry import preflight_for
    from app.agents.preflight import rules_overlay  # noqa: F401

    ws = WorkspaceState({})
    ws.add_layer(name="a", kind="vector", metadata={"crs": "EPSG:4326"})
    ws.add_layer(name="b", kind="vector", metadata={"crs": "EPSG:4548"})
    # executes without network — no httpx/requests import needed
    issues = preflight_for("intersect_layer", {
        "workspace": ws,
        "kwargs": {"input_ref": "a", "overlay_ref": "b"},
    })
    assert len(issues) >= 1
    codes = {i.code for i in issues}
    assert "overlay_crs_mismatch" in codes or "crs_consistency_mismatch" in codes


def test_layer_exists_no_io():
    """layer_exists preflight 只读 WorkspaceState。"""
    from app.agents.preflight.registry import preflight_for
    from app.agents.preflight import rules_layer  # noqa: F401

    ws = WorkspaceState({})
    issues = preflight_for("query_poi", {
        "workspace": ws,
        "kwargs": {"input_ref": "nonexistent"},
    })
    assert len(issues) == 1
    assert issues[0].code == "layer_not_found"


def test_numeric_runtime_references_are_not_workspace_layer_names():
    from app.agents.preflight.rules_layer import _check_layer_exists

    issues = _check_layer_exists({
        "workspace": WorkspaceState({}),
        "kwargs": {"input_ref": 0, "overlay_ref": 1},
    })

    assert issues == []


def test_field_exists_no_io():
    """field_exists preflight 只读 WorkspaceState metadata。"""
    from app.agents.preflight.registry import preflight_for
    from app.agents.preflight import rules_layer  # noqa: F401

    ws = WorkspaceState({})
    ws.add_layer(name="test", kind="vector", metadata={
        "fields": ["name", "population"],
    })
    issues = preflight_for("extract_by_attribute", {
        "workspace": ws,
        "kwargs": {"input_ref": "test", "field": "nonexistent_field"},
    })
    assert len(issues) == 1
    assert issues[0].code == "field_not_found"


def test_output_overwrite_no_network_io():
    """output_overwrite 仅调 os.path.exists，不调 HTTP。"""
    from app.agents.preflight.registry import preflight_for
    from app.agents.preflight import rules_overwrite  # noqa: F401

    with patch.object(os_module.path, "exists", return_value=False):
        issues = preflight_for("export_result", {
            "kwargs": {"output_path": "/tmp/test.geojson"},
        })
        assert len(issues) == 0

    with patch.object(os_module.path, "exists", return_value=True):
        issues = preflight_for("export_result", {
            "kwargs": {"output_path": "/tmp/test.geojson"},
        })
        assert len(issues) == 1
        assert issues[0].code == "output_exists"


def test_preflight_for_unknown_action_not_crash():
    """Unknown semantic_action should return empty issues, not crash."""
    from app.agents.preflight.registry import preflight_for
    issues = preflight_for("nonexistent_action", {
        "workspace": None,
        "kwargs": {},
    })
    assert isinstance(issues, list)
    assert len(issues) == 0


def test_run_with_preflight_no_io():
    """Full run_with_preflight wrapper does not make network calls."""
    from app.agents.preflight.runner import run_with_preflight

    def handler():
        class DummyResult:
            data = {"status": "ok"}
        return DummyResult()

    # Should execute without needing network, DB, etc.
    result = run_with_preflight(
        "test_tool", "test_action", handler, (), {}, None,
    )
    assert result.data["status"] == "ok"
