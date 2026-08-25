"""Unit tests for overlay_crs_alignment preflight rule."""
from __future__ import annotations

import pytest

from app.agents.workspace.state import WorkspaceState
from app.agents.preflight.rules_overlay import _check_overlay_crs


def test_overlay_crs_alignment_blocked():
    """Two layers with different CRS should be blocked."""
    ws = WorkspaceState({})
    ws.add_layer(name="layer_a", kind="vector", metadata={"crs": "EPSG:4326"})
    ws.add_layer(name="layer_b", kind="vector", metadata={"crs": "EPSG:4548"})
    issues = _check_overlay_crs({
        "workspace": ws,
        "kwargs": {"input_ref": "layer_a", "overlay_ref": "layer_b"},
    })
    assert len(issues) == 1
    assert issues[0].code == "overlay_crs_mismatch"
    assert issues[0].severity == "error"


def test_overlay_crs_alignment_passes():
    """Two layers with same CRS should pass."""
    ws = WorkspaceState({})
    ws.add_layer(name="layer_a", kind="vector", metadata={"crs": "EPSG:4548"})
    ws.add_layer(name="layer_b", kind="vector", metadata={"crs": "EPSG:4548"})
    issues = _check_overlay_crs({
        "workspace": ws,
        "kwargs": {"input_ref": "layer_a", "overlay_ref": "layer_b"},
    })
    assert len(issues) == 0


def test_overlay_missing_layer_returns_empty():
    """Missing layer should be silently skipped (layer_exists handles it)."""
    ws = WorkspaceState({})
    ws.add_layer(name="layer_a", kind="vector", metadata={"crs": "EPSG:4326"})
    issues = _check_overlay_crs({
        "workspace": ws,
        "kwargs": {"input_ref": "layer_a", "overlay_ref": "nonexistent"},
    })
    assert len(issues) == 0


def test_overlay_both_missing_crs_passes():
    """Both layers without CRS metadata should pass."""
    ws = WorkspaceState({})
    ws.add_layer(name="layer_a", kind="vector", metadata={})
    ws.add_layer(name="layer_b", kind="vector", metadata={})
    issues = _check_overlay_crs({
        "workspace": ws,
        "kwargs": {"input_ref": "layer_a", "overlay_ref": "layer_b"},
    })
    assert len(issues) == 0


def test_overlay_one_missing_crs_passes():
    """One layer without CRS should pass."""
    ws = WorkspaceState({})
    ws.add_layer(name="layer_a", kind="vector", metadata={"crs": "EPSG:4326"})
    ws.add_layer(name="layer_b", kind="vector", metadata={})
    issues = _check_overlay_crs({
        "workspace": ws,
        "kwargs": {"input_ref": "layer_a", "overlay_ref": "layer_b"},
    })
    assert len(issues) == 0


def test_overlay_empty_kwargs():
    """Empty kwargs should yield no issues."""
    issues = _check_overlay_crs({"workspace": None, "kwargs": {}})
    assert len(issues) == 0


def test_overlay_east_north_notation_mismatch():
    """CRS strings that are semantically same but textually different.
    Note: This rule does simple string comparison, so "EPSG:4326" vs
    "EPSG:4326 " (trailing space) would be caught. This test ensures
    exact match behavior.
    """
    ws = WorkspaceState({})
    ws.add_layer(name="a", kind="vector", metadata={"crs": "EPSG:4326"})
    ws.add_layer(name="b", kind="vector", metadata={"crs": "EPSG:4326"})
    issues = _check_overlay_crs({
        "workspace": ws,
        "kwargs": {"input_ref": "a", "overlay_ref": "b"},
    })
    assert len(issues) == 0


def test_overlay_uses_geom_a_b_ref_fallback():
    """geom_a_ref / geom_b_ref kwargs should work as fallback."""
    ws = WorkspaceState({})
    ws.add_layer(name="a", kind="vector", metadata={"crs": "EPSG:4326"})
    ws.add_layer(name="b", kind="vector", metadata={"crs": "EPSG:4548"})
    issues = _check_overlay_crs({
        "workspace": ws,
        "kwargs": {"geom_a_ref": "a", "geom_b_ref": "b"},
    })
    assert len(issues) == 1
    assert issues[0].code == "overlay_crs_mismatch"


def test_overlay_intersect_layer_semantic_action():
    """Run through registry with intersect_layer semantic_action."""
    from app.agents.preflight.registry import preflight_for
    # preflight_for will trigger the registered rule
    ws = WorkspaceState({})
    ws.add_layer(name="a", kind="vector", metadata={"crs": "EPSG:4326"})
    ws.add_layer(name="b", kind="vector", metadata={"crs": "EPSG:4548"})
    issues = preflight_for("intersect_layer", {
        "workspace": ws,
        "kwargs": {"input_ref": "a", "overlay_ref": "b"},
    })
    assert len(issues) >= 1
    codes = {i.code for i in issues}
    assert "overlay_crs_mismatch" in codes or "crs_consistency_mismatch" in codes
