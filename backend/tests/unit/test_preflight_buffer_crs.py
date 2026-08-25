"""Unit tests for buffer_crs preflight rule."""
from __future__ import annotations

import pytest

from app.agents.workspace.state import WorkspaceState
from app.agents.preflight.rules_buffer import _check_buffer_crs


def test_buffer_geographic_crs_blocked():
    """EPSG:4326 should be blocked with buffer_crs_mismatch."""
    ws = WorkspaceState({})
    ws.add_layer(name="roads", kind="vector", metadata={"crs": "EPSG:4326"})
    issues = _check_buffer_crs({"workspace": ws, "kwargs": {"input_ref": "roads"}})
    assert len(issues) == 1
    assert issues[0].code == "buffer_crs_mismatch"
    assert issues[0].severity == "error"
    assert "reproject" in issues[0].message.lower() or "重投影" in issues[0].message


def test_buffer_epsg4490_blocked():
    """EPSG:4490 should also be blocked."""
    ws = WorkspaceState({})
    ws.add_layer(name="parcels", kind="polygon", metadata={"crs": "EPSG:4490"})
    issues = _check_buffer_crs({"workspace": ws, "kwargs": {"input_ref": "parcels"}})
    assert len(issues) == 1
    assert issues[0].code == "buffer_crs_mismatch"


def test_buffer_projected_crs_passes():
    """EPSG:4548 (projected) should pass."""
    ws = WorkspaceState({})
    ws.add_layer(name="roads", kind="vector", metadata={"crs": "EPSG:4548"})
    issues = _check_buffer_crs({"workspace": ws, "kwargs": {"input_ref": "roads"}})
    assert len(issues) == 0


def test_buffer_utm_projected_passes():
    """UTM zones (32650, 32750 etc.) should pass."""
    ws = WorkspaceState({})
    ws.add_layer(name="buildings", kind="vector", metadata={"crs": "EPSG:32650"})
    issues = _check_buffer_crs({"workspace": ws, "kwargs": {"input_ref": "buildings"}})
    assert len(issues) == 0


def test_buffer_missing_layer():
    """Non-existent layer should return layer_not_found."""
    ws = WorkspaceState({})
    issues = _check_buffer_crs({"workspace": ws, "kwargs": {"input_ref": "nonexistent"}})
    assert len(issues) == 1
    assert issues[0].code == "layer_not_found"


def test_buffer_empty_workspace():
    """No workspace should yield no issues."""
    issues = _check_buffer_crs({"workspace": None, "kwargs": {}})
    assert len(issues) == 0


def test_buffer_no_input_ref():
    """Missing input_ref kwarg should yield no issues."""
    ws = WorkspaceState({})
    issues = _check_buffer_crs({"workspace": ws, "kwargs": {}})
    assert len(issues) == 0


def test_buffer_uses_geom_ref_fallback():
    """geom_ref should work as fallback when input_ref is not present."""
    ws = WorkspaceState({})
    ws.add_layer(name="geom", kind="vector", metadata={"crs": "EPSG:4326"})
    issues = _check_buffer_crs({"workspace": ws, "kwargs": {"geom_ref": "geom"}})
    assert len(issues) == 1
    assert issues[0].code == "buffer_crs_mismatch"


def test_buffer_no_crs_metadata_passes():
    """Layer without CRS metadata should pass (no CRS to check)."""
    ws = WorkspaceState({})
    ws.add_layer(name="unknown_crs", kind="vector", metadata={})
    issues = _check_buffer_crs({"workspace": ws, "kwargs": {"input_ref": "unknown_crs"}})
    assert len(issues) == 0
