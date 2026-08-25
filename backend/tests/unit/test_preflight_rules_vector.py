"""Unit tests for vector/attribute preflight rules.

Seven rules tested:
1. crs_consistency
2. crs_geographic_for_metric_op
3. geometry_type_match
4. geometry_validity
5. extent_overlap
6. field_type_compatibility
7. keep_fields_downstream
"""
from __future__ import annotations

import pytest

from app.agents.workspace.state import WorkspaceState
from app.agents.preflight.validation import ValidationIssue, RepairProposal


# ============================================================
# 1. crs_consistency
# ============================================================


class TestCRSConsistency:
    """crs_consistency: 两个图层 CRS 不一致时 error + auto_repair."""

    def test_crs_mismatch_blocked(self):
        from app.agents.preflight.rules_vector import _check_crs_consistency

        ws = WorkspaceState({})
        ws.add_layer(name="layer_a", kind="vector", metadata={"crs": "EPSG:4326"})
        ws.add_layer(name="layer_b", kind="vector", metadata={"crs": "EPSG:4548"})
        issues = _check_crs_consistency({
            "workspace": ws,
            "kwargs": {"input_ref": "layer_a", "overlay_ref": "layer_b"},
        })
        assert len(issues) == 1
        assert issues[0].code == "crs_consistency_mismatch"
        assert issues[0].severity == "error"
        assert issues[0].repair.kind == "confirm_action"
        assert issues[0].repair.action == "reproject_layer"

    def test_crs_match_passes(self):
        from app.agents.preflight.rules_vector import _check_crs_consistency

        ws = WorkspaceState({})
        ws.add_layer(name="a", kind="vector", metadata={"crs": "EPSG:4548"})
        ws.add_layer(name="b", kind="vector", metadata={"crs": "EPSG:4548"})
        issues = _check_crs_consistency({
            "workspace": ws,
            "kwargs": {"input_ref": "a", "overlay_ref": "b"},
        })
        assert len(issues) == 0

    def test_missing_layer_skipped(self):
        from app.agents.preflight.rules_vector import _check_crs_consistency

        ws = WorkspaceState({})
        ws.add_layer(name="a", kind="vector", metadata={"crs": "EPSG:4326"})
        issues = _check_crs_consistency({
            "workspace": ws,
            "kwargs": {"input_ref": "a", "overlay_ref": "nonexistent"},
        })
        assert len(issues) == 0

    def test_no_crs_metadata_passes(self):
        from app.agents.preflight.rules_vector import _check_crs_consistency

        ws = WorkspaceState({})
        ws.add_layer(name="a", kind="vector", metadata={})
        ws.add_layer(name="b", kind="vector", metadata={})
        issues = _check_crs_consistency({
            "workspace": ws,
            "kwargs": {"input_ref": "a", "overlay_ref": "b"},
        })
        assert len(issues) == 0

    def test_geom_a_b_ref_fallback(self):
        from app.agents.preflight.rules_vector import _check_crs_consistency

        ws = WorkspaceState({})
        ws.add_layer(name="a", kind="vector", metadata={"crs": "EPSG:4326"})
        ws.add_layer(name="b", kind="vector", metadata={"crs": "EPSG:4548"})
        issues = _check_crs_consistency({
            "workspace": ws,
            "kwargs": {"geom_a_ref": "a", "geom_b_ref": "b"},
        })
        assert len(issues) == 1
        assert issues[0].code == "crs_consistency_mismatch"

    def test_registry_semantic_action(self):
        from app.agents.preflight.registry import preflight_for

        ws = WorkspaceState({})
        ws.add_layer(name="a", kind="vector", metadata={"crs": "EPSG:4326"})
        ws.add_layer(name="b", kind="vector", metadata={"crs": "EPSG:4548"})
        issues = preflight_for("clip_layer", {
            "workspace": ws,
            "kwargs": {"input_ref": "a", "overlay_ref": "b"},
        })
        assert len(issues) >= 1
        codes = {i.code for i in issues}
        assert "crs_consistency_mismatch" in codes


# ============================================================
# 2. crs_geographic_for_metric_op
# ============================================================


class TestCRSGeographicForMetricOp:
    """crs_geographic_for_metric_op: 地理坐标系输入时 error + auto_repair."""

    def test_epsg4326_blocked(self):
        from app.agents.preflight.rules_vector import _check_crs_geographic_for_metric

        ws = WorkspaceState({})
        ws.add_layer(name="roads", kind="vector", metadata={"crs": "EPSG:4326"})
        issues = _check_crs_geographic_for_metric({
            "workspace": ws,
            "kwargs": {"input_ref": "roads"},
        })
        assert len(issues) == 1
        assert issues[0].code == "geographic_crs_for_metric_op"
        assert issues[0].severity == "error"
        assert issues[0].repair.kind == "confirm_action"
        assert issues[0].repair.action == "reproject_layer"

    def test_epsg4490_blocked(self):
        from app.agents.preflight.rules_vector import _check_crs_geographic_for_metric

        ws = WorkspaceState({})
        ws.add_layer(name="parcels", kind="polygon", metadata={"crs": "EPSG:4490"})
        issues = _check_crs_geographic_for_metric({
            "workspace": ws,
            "kwargs": {"input_ref": "parcels"},
        })
        assert len(issues) == 1
        assert issues[0].code == "geographic_crs_for_metric_op"

    def test_gcj02_crs_label_blocked(self):
        from app.agents.preflight.rules_vector import _check_crs_geographic_for_metric

        ws = WorkspaceState({})
        ws.add_layer(name="gcj_data", kind="vector", metadata={
            "crs": "EPSG:4326",
            "crs_label": "GCJ02",
        })
        issues = _check_crs_geographic_for_metric({
            "workspace": ws,
            "kwargs": {"input_ref": "gcj_data"},
        })
        assert len(issues) == 1

    def test_projected_crs_passes(self):
        from app.agents.preflight.rules_vector import _check_crs_geographic_for_metric

        ws = WorkspaceState({})
        ws.add_layer(name="buildings", kind="vector", metadata={"crs": "EPSG:4548"})
        issues = _check_crs_geographic_for_metric({
            "workspace": ws,
            "kwargs": {"input_ref": "buildings"},
        })
        assert len(issues) == 0

    def test_utm_passes(self):
        from app.agents.preflight.rules_vector import _check_crs_geographic_for_metric

        ws = WorkspaceState({})
        ws.add_layer(name="roads", kind="vector", metadata={"crs": "EPSG:32650"})
        issues = _check_crs_geographic_for_metric({
            "workspace": ws,
            "kwargs": {"input_ref": "roads"},
        })
        assert len(issues) == 0

    def test_no_workspace_passes(self):
        from app.agents.preflight.rules_vector import _check_crs_geographic_for_metric

        issues = _check_crs_geographic_for_metric({"workspace": None, "kwargs": {}})
        assert len(issues) == 0

    def test_missing_layer(self):
        from app.agents.preflight.rules_vector import _check_crs_geographic_for_metric

        ws = WorkspaceState({})
        issues = _check_crs_geographic_for_metric({
            "workspace": ws,
            "kwargs": {"input_ref": "nonexistent"},
        })
        assert len(issues) == 0


# ============================================================
# 3. geometry_type_match
# ============================================================


class TestGeometryTypeMatch:
    """geometry_type_match: 几何类型不匹配时 warning."""

    def test_type_mismatch_warning(self):
        from app.agents.preflight.rules_vector import _check_geometry_type_match

        ws = WorkspaceState({})
        ws.add_layer(name="points", kind="point", metadata={"geometry_type": "Point"})
        ws.add_layer(name="polygons", kind="polygon", metadata={"geometry_type": "Polygon"})
        issues = _check_geometry_type_match({
            "workspace": ws,
            "kwargs": {"input_ref": "points", "overlay_ref": "polygons"},
        })
        assert len(issues) == 1
        assert issues[0].code == "geometry_type_mismatch"
        assert issues[0].severity == "warning"

    def test_type_match_passes(self):
        from app.agents.preflight.rules_vector import _check_geometry_type_match

        ws = WorkspaceState({})
        ws.add_layer(name="a", kind="polygon", metadata={"geometry_type": "Polygon"})
        ws.add_layer(name="b", kind="polygon", metadata={"geometry_type": "Polygon"})
        issues = _check_geometry_type_match({
            "workspace": ws,
            "kwargs": {"input_ref": "a", "overlay_ref": "b"},
        })
        assert len(issues) == 0

    def test_no_geometry_type_metadata_passes(self):
        from app.agents.preflight.rules_vector import _check_geometry_type_match

        ws = WorkspaceState({})
        ws.add_layer(name="a", kind="vector", metadata={})
        ws.add_layer(name="b", kind="vector", metadata={})
        issues = _check_geometry_type_match({
            "workspace": ws,
            "kwargs": {"input_ref": "a", "overlay_ref": "b"},
        })
        assert len(issues) == 0


# ============================================================
# 4. geometry_validity
# ============================================================


class TestGeometryValidity:
    """geometry_validity: 有无无效几何标记时 warning + auto_repair."""

    def test_invalid_marked_warning(self):
        from app.agents.preflight.rules_vector import _check_geometry_validity

        ws = WorkspaceState({})
        ws.add_layer(name="data", kind="vector", metadata={
            "has_invalid_geometries": True,
            "invalid_count": 3,
        })
        issues = _check_geometry_validity({
            "workspace": ws,
            "kwargs": {"input_ref": "data"},
        })
        assert len(issues) == 1
        assert issues[0].code == "geometry_has_invalid"
        assert issues[0].severity == "warning"
        assert issues[0].repair.kind == "confirm_action"
        assert issues[0].repair.action == "fix_geometries"

    def test_no_invalid_mark_passes(self):
        from app.agents.preflight.rules_vector import _check_geometry_validity

        ws = WorkspaceState({})
        ws.add_layer(name="data", kind="vector", metadata={})
        issues = _check_geometry_validity({
            "workspace": ws,
            "kwargs": {"input_ref": "data"},
        })
        assert len(issues) == 0

    def test_invalid_count_zero_passes(self):
        from app.agents.preflight.rules_vector import _check_geometry_validity

        ws = WorkspaceState({})
        ws.add_layer(name="data", kind="vector", metadata={
            "has_invalid_geometries": False,
            "invalid_count": 0,
        })
        issues = _check_geometry_validity({
            "workspace": ws,
            "kwargs": {"input_ref": "data"},
        })
        assert len(issues) == 0

    def test_geom_ref_fallback(self):
        from app.agents.preflight.rules_vector import _check_geometry_validity

        ws = WorkspaceState({})
        ws.add_layer(name="geom", kind="vector", metadata={
            "has_invalid_geometries": True,
        })
        issues = _check_geometry_validity({
            "workspace": ws,
            "kwargs": {"geom_ref": "geom"},
        })
        assert len(issues) == 1
        assert issues[0].code == "geometry_has_invalid"


# ============================================================
# 5. extent_overlap
# ============================================================


class TestExtentOverlap:
    """extent_overlap: bbox 无重叠时 warning."""

    def test_no_overlap_warning(self):
        from app.agents.preflight.rules_vector import _check_extent_overlap

        ws = WorkspaceState({})
        ws.add_layer(name="a", kind="vector", metadata={
            "bbox": [0, 0, 10, 10],
        })
        ws.add_layer(name="b", kind="vector", metadata={
            "bbox": [100, 100, 110, 110],
        })
        issues = _check_extent_overlap({
            "workspace": ws,
            "kwargs": {"input_ref": "a", "overlay_ref": "b"},
        })
        assert len(issues) == 1
        assert issues[0].code == "extent_no_overlap"
        assert issues[0].severity == "warning"

    def test_overlap_passes(self):
        from app.agents.preflight.rules_vector import _check_extent_overlap

        ws = WorkspaceState({})
        ws.add_layer(name="a", kind="vector", metadata={
            "bbox": [0, 0, 10, 10],
        })
        ws.add_layer(name="b", kind="vector", metadata={
            "bbox": [5, 5, 15, 15],
        })
        issues = _check_extent_overlap({
            "workspace": ws,
            "kwargs": {"input_ref": "a", "overlay_ref": "b"},
        })
        assert len(issues) == 0

    def test_no_bbox_metadata_passes(self):
        from app.agents.preflight.rules_vector import _check_extent_overlap

        ws = WorkspaceState({})
        ws.add_layer(name="a", kind="vector", metadata={})
        ws.add_layer(name="b", kind="vector", metadata={})
        issues = _check_extent_overlap({
            "workspace": ws,
            "kwargs": {"input_ref": "a", "overlay_ref": "b"},
        })
        assert len(issues) == 0

    def test_one_bbox_missing_passes(self):
        from app.agents.preflight.rules_vector import _check_extent_overlap

        ws = WorkspaceState({})
        ws.add_layer(name="a", kind="vector", metadata={"bbox": [0, 0, 10, 10]})
        ws.add_layer(name="b", kind="vector", metadata={})
        issues = _check_extent_overlap({
            "workspace": ws,
            "kwargs": {"input_ref": "a", "overlay_ref": "b"},
        })
        assert len(issues) == 0


# ============================================================
# 6. field_type_compatibility
# ============================================================


class TestFieldTypeCompatibility:
    """field_type_compatibility: 字符串字段做数值比较时 error + ask_user."""

    def test_string_field_numeric_compare_blocked(self):
        from app.agents.preflight.rules_vector import _check_field_type_compatibility

        ws = WorkspaceState({})
        ws.add_layer(name="data", kind="vector", metadata={
            "fields": [
                {"name": "name", "dtype": "object"},
                {"name": "population", "dtype": "int64"},
            ],
        })
        issues = _check_field_type_compatibility({
            "workspace": ws,
            "kwargs": {"input_ref": "data", "field": "name", "operator": ">"},
        })
        assert len(issues) == 1
        assert issues[0].code == "field_type_not_numeric"
        assert issues[0].severity == "error"
        assert issues[0].repair.kind == "ask_user"

    def test_numeric_field_numeric_compare_passes(self):
        from app.agents.preflight.rules_vector import _check_field_type_compatibility

        ws = WorkspaceState({})
        ws.add_layer(name="data", kind="vector", metadata={
            "fields": [
                {"name": "population", "dtype": "int64"},
            ],
        })
        issues = _check_field_type_compatibility({
            "workspace": ws,
            "kwargs": {"input_ref": "data", "field": "population", "operator": ">"},
        })
        assert len(issues) == 0

    def test_eq_operator_on_string_passes(self):
        from app.agents.preflight.rules_vector import _check_field_type_compatibility

        ws = WorkspaceState({})
        ws.add_layer(name="data", kind="vector", metadata={
            "fields": [{"name": "name", "dtype": "object"}],
        })
        # == 操作对字符串是合理的，不应拦截
        issues = _check_field_type_compatibility({
            "workspace": ws,
            "kwargs": {"input_ref": "data", "field": "name", "operator": "=="},
        })
        assert len(issues) == 0

    def test_no_dtype_metadata_passes(self):
        from app.agents.preflight.rules_vector import _check_field_type_compatibility

        ws = WorkspaceState({})
        ws.add_layer(name="data", kind="vector", metadata={
            "fields": ["name", "population"],
        })
        issues = _check_field_type_compatibility({
            "workspace": ws,
            "kwargs": {"input_ref": "data", "field": "name", "operator": ">"},
        })
        assert len(issues) == 0

    def test_no_field_in_kwargs_passes(self):
        from app.agents.preflight.rules_vector import _check_field_type_compatibility

        ws = WorkspaceState({})
        ws.add_layer(name="data", kind="vector", metadata={
            "fields": [{"name": "name", "dtype": "object"}],
        })
        issues = _check_field_type_compatibility({
            "workspace": ws,
            "kwargs": {"input_ref": "data"},
        })
        assert len(issues) == 0


# ============================================================
# 7. keep_fields_downstream
# ============================================================


class TestKeepFieldsDownstream:
    """keep_fields_downstream: 未保留 geometry 列时 warning."""

    def test_missing_geometry_warning(self):
        from app.agents.preflight.rules_vector import _check_keep_fields_downstream

        issues = _check_keep_fields_downstream({
            "workspace": WorkspaceState({}),
            "kwargs": {"fields": ["name", "population"]},
        })
        assert len(issues) == 1
        assert issues[0].code == "keep_fields_missing_geometry"
        assert issues[0].severity == "warning"

    def test_includes_geometry_passes(self):
        from app.agents.preflight.rules_vector import _check_keep_fields_downstream

        issues = _check_keep_fields_downstream({
            "workspace": WorkspaceState({}),
            "kwargs": {"fields": ["geometry", "name"]},
        })
        assert len(issues) == 0

    def test_empty_fields_warning(self):
        from app.agents.preflight.rules_vector import _check_keep_fields_downstream

        issues = _check_keep_fields_downstream({
            "workspace": WorkspaceState({}),
            "kwargs": {"fields": []},
        })
        assert len(issues) == 1
        assert issues[0].code == "keep_fields_no_fields"

    def test_no_fields_kwarg(self):
        from app.agents.preflight.rules_vector import _check_keep_fields_downstream

        issues = _check_keep_fields_downstream({
            "workspace": WorkspaceState({}),
            "kwargs": {},
        })
        assert len(issues) == 1
        assert issues[0].code == "keep_fields_no_fields"
