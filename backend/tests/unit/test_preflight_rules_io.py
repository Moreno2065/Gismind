"""Preflight rules_io 规则单元测试。

覆盖：
- coordinate_field_exists: x_field / y_field 存在于 input 元数据字段中。
- coordinate_value_range: 坐标字段命名疑似颠倒检测。
- output_path_overwrite: 输出路径已存在检查。
"""
from __future__ import annotations

import os as os_module
from unittest.mock import patch

import pytest

from app.agents.workspace.state import WorkspaceState


# ============================================================
# coordinate_field_exists
# ============================================================

class TestCoordinateFieldExists:
    """coordinate_field_exists preflight 规则测试。"""

    @pytest.fixture(autouse=True)
    def _register_rules(self):
        """导入 rules_io 以注册规则。"""
        from app.agents.preflight import rules_io  # noqa: F401

    def test_fields_exist_no_issue(self):
        """x_field / y_field 都存在于 input metadata.fields 中 → 无 issue。"""
        from app.agents.preflight.registry import preflight_for

        ws = WorkspaceState({})
        ws.add_layer(
            name="my_csv",
            kind="vector",
            metadata={"fields": ["name", "lon", "lat", "value"]},
        )
        issues = preflight_for("csv_to_points", {
            "workspace": ws,
            "kwargs": {
                "x_field": "lon",
                "y_field": "lat",
                "csv_df_or_path": "my_csv",
            },
        })
        field_issues = [i for i in issues if i.code == "coordinate_field_not_found"]
        assert len(field_issues) == 0

    def test_x_field_missing(self):
        """x_field 不存在 → error + ask_user。"""
        from app.agents.preflight.registry import preflight_for

        ws = WorkspaceState({})
        ws.add_layer(
            name="my_csv",
            kind="vector",
            metadata={"fields": ["name", "lat"]},
        )
        issues = preflight_for("csv_to_points", {
            "workspace": ws,
            "kwargs": {
                "x_field": "lon",
                "y_field": "lat",
                "csv_df_or_path": "my_csv",
            },
        })
        field_issues = [i for i in issues if i.code == "coordinate_field_not_found"]
        assert len(field_issues) >= 1
        assert field_issues[0].severity == "error"
        assert field_issues[0].repair is not None
        assert field_issues[0].repair.kind == "ask_user"
        assert "lon" in field_issues[0].message

    def test_y_field_missing(self):
        """y_field 不存在 → error + ask_user。"""
        from app.agents.preflight.registry import preflight_for

        ws = WorkspaceState({})
        ws.add_layer(
            name="my_csv",
            kind="vector",
            metadata={"fields": ["name", "lon"]},
        )
        issues = preflight_for("csv_to_points", {
            "workspace": ws,
            "kwargs": {
                "x_field": "lon",
                "y_field": "lat",
                "csv_df_or_path": "my_csv",
            },
        })
        field_issues = [i for i in issues if i.code == "coordinate_field_not_found"]
        assert len(field_issues) >= 1
        assert "lat" in field_issues[0].message

    def test_no_workspace(self):
        """无 workspace 时不检查。"""
        from app.agents.preflight.registry import preflight_for

        issues = preflight_for("csv_to_points", {
            "workspace": None,
            "kwargs": {"x_field": "lon", "y_field": "lat"},
        })
        field_issues = [i for i in issues if i.code == "coordinate_field_not_found"]
        assert len(field_issues) == 0

    def test_input_not_string_ref(self):
        """csv_df_or_path 不为字符串时不检查（可能是 DataFrame/dict 直接传入）。"""
        from app.agents.preflight.registry import preflight_for

        ws = WorkspaceState({})
        issues = preflight_for("csv_to_points", {
            "workspace": ws,
            "kwargs": {
                "x_field": "lon",
                "y_field": "lat",
                "csv_df_or_path": {"sample": []},
            },
        })
        field_issues = [i for i in issues if i.code == "coordinate_field_not_found"]
        assert len(field_issues) == 0

    def test_layer_not_found_graceful(self):
        """引用不存在的 layer 时不崩溃。"""
        from app.agents.preflight.registry import preflight_for

        ws = WorkspaceState({})
        issues = preflight_for("csv_to_points", {
            "workspace": ws,
            "kwargs": {
                "x_field": "lon",
                "y_field": "lat",
                "csv_df_or_path": "nonexistent",
            },
        })
        field_issues = [i for i in issues if i.code == "coordinate_field_not_found"]
        assert len(field_issues) == 0


# ============================================================
# coordinate_value_range
# ============================================================

class TestCoordinateValueRange:
    """coordinate_value_range preflight 规则测试。"""

    @pytest.fixture(autouse=True)
    def _register_rules(self):
        """导入 rules_io 以注册规则。"""
        from app.agents.preflight import rules_io  # noqa: F401

    def test_fields_swapped_detection(self):
        """x_field='lat' / y_field='lon' → warning + swap_x_y。"""
        from app.agents.preflight.registry import preflight_for

        issues = preflight_for("csv_to_points", {
            "kwargs": {"x_field": "lat", "y_field": "lon"},
        })
        swap_issues = [i for i in issues if i.code == "coordinate_field_swapped"]
        assert len(swap_issues) == 1
        assert swap_issues[0].severity == "warning"
        assert swap_issues[0].repair is not None
        assert swap_issues[0].repair.kind == "confirm_action"
        assert swap_issues[0].repair.action == "swap_x_y"

    def test_normal_fields_no_warning(self):
        """x_field='lon' / y_field='lat'（正常）→ 无 warning。"""
        from app.agents.preflight.registry import preflight_for

        issues = preflight_for("csv_to_points", {
            "kwargs": {"x_field": "lon", "y_field": "lat"},
        })
        swap_issues = [i for i in issues if i.code == "coordinate_field_swapped"]
        assert len(swap_issues) == 0

    def test_unrelated_field_names(self):
        """非坐标语义字段名 → 无 issue。"""
        from app.agents.preflight.registry import preflight_for

        issues = preflight_for("csv_to_points", {
            "kwargs": {"x_field": "col_a", "y_field": "col_b"},
        })
        swap_issues = [i for i in issues if i.code == "coordinate_field_swapped"]
        assert len(swap_issues) == 0

    def test_missing_kwargs(self):
        """无 kwargs 时不崩溃。"""
        from app.agents.preflight.registry import preflight_for

        issues = preflight_for("csv_to_points", {})
        assert len(issues) == 0


# ============================================================
# output_path_overwrite
# ============================================================

class TestOutputPathOverwrite:
    """output_path_overwrite preflight 规则测试。"""

    @pytest.fixture(autouse=True)
    def _register_rules(self):
        """导入 rules_io 以注册规则。"""
        from app.agents.preflight import rules_io  # noqa: F401

    def test_path_exists_warning(self):
        """输出路径已存在 → warning + confirm_overwrite。"""
        from app.agents.preflight.registry import preflight_for

        with patch.object(os_module.path, "exists", return_value=True):
            issues = preflight_for("export_result", {
                "kwargs": {"path": "/tmp/test.gpkg"},
            })
        overwrite = [i for i in issues if i.code == "output_path_exists"]
        assert len(overwrite) == 1
        assert overwrite[0].severity == "warning"
        assert overwrite[0].repair is not None
        assert overwrite[0].repair.kind == "confirm_overwrite"

    def test_path_not_exists_no_issue(self):
        """输出路径不存在 → 无 issue。"""
        from app.agents.preflight.registry import preflight_for

        with patch.object(os_module.path, "exists", return_value=False):
            issues = preflight_for("export_result", {
                "kwargs": {"path": "/tmp/new.gpkg"},
            })
        overwrite = [i for i in issues if i.code == "output_path_exists"]
        assert len(overwrite) == 0

    def test_no_path_no_issue(self):
        """无 path kwarg → 无 issue。"""
        from app.agents.preflight.registry import preflight_for

        issues = preflight_for("export_result", {"kwargs": {}})
        overwrite = [i for i in issues if i.code == "output_path_exists"]
        assert len(overwrite) == 0
