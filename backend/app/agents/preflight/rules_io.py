"""IO guardrail 规则：坐标字段存在性 / 坐标值范围 / 输出路径覆盖检查。

规则：
- coordinate_field_exists: 作用于 csv_to_points，检查 x_field / y_field 是否存在于输入 CSV 的字段中。
- coordinate_value_range: 作用于 csv_to_points（postflight），检查坐标字段命名是否疑似颠倒（lat/lon 语义）。
- output_path_overwrite: 作用于 export_result，检查输出路径是否已存在。
"""
from __future__ import annotations

import os
from typing import Any

from app.agents.preflight.registry import register_preflight_rule
from app.agents.preflight.validation import ValidationIssue, RepairProposal

# x_field / y_field 命名提示：疑似颠倒的字段名模式
_LAT_LIKE = {"lat", "latitude", "y", "ycoord", "y_coord", "northing", "n"}
_LON_LIKE = {"lon", "lng", "long", "longitude", "x", "xcoord", "x_coord", "easting", "e"}


@register_preflight_rule("coordinate_field_exists", "csv_to_points")
def _check_coordinate_field_exists(ctx: dict[str, Any]) -> list[ValidationIssue]:
    """检查 x_field / y_field 是否存在于输入图层的元数据字段中。

    从 ctx.kwargs 取 x_field / y_field，从 workspace.resolve(input_ref).metadata.fields
    查字段。不存在时 severity=error + ask_user。
    """
    workspace = ctx.get("workspace")
    kwargs = ctx.get("kwargs", {})
    if not workspace:
        return []

    x_field = kwargs.get("x_field")
    y_field = kwargs.get("y_field")
    input_ref = kwargs.get("csv_df_or_path")

    # 如果 csv_df_or_path 是 DataFrame/dict（非引用），跳过字段检查
    if not isinstance(input_ref, str):
        return []

    if not x_field or not y_field:
        return []

    try:
        record = workspace.resolve(str(input_ref))
    except KeyError:
        return []

    raw_fields = record.metadata.get("fields") or []
    available: set[str] = set()
    for f in raw_fields:
        if isinstance(f, str):
            available.add(f.lower())
        elif isinstance(f, dict) and "name" in f:
            available.add(f["name"].lower())

    issues: list[ValidationIssue] = []

    if str(x_field).lower() not in available:
        issues.append(
            ValidationIssue(
                code="coordinate_field_not_found",
                stage="preflight",
                severity="error",
                message=(
                    f"X 坐标字段 '{x_field}' 不存在于输入数据中。"
                    f"可用字段：{', '.join(sorted(available)) or '(无字段信息)'}"
                ),
                repair=RepairProposal(kind="ask_user"),
            )
        )

    if str(y_field).lower() not in available:
        issues.append(
            ValidationIssue(
                code="coordinate_field_not_found",
                stage="preflight",
                severity="error",
                message=(
                    f"Y 坐标字段 '{y_field}' 不存在于输入数据中。"
                    f"可用字段：{', '.join(sorted(available)) or '(无字段信息)'}"
                ),
                repair=RepairProposal(kind="ask_user"),
            )
        )

    return issues


@register_preflight_rule("coordinate_value_range", "csv_to_points")
def _check_coordinate_value_range(ctx: dict[str, Any]) -> list[ValidationIssue]:
    """检查 x_field / y_field 命名是否疑似颠倒（postflight 概念）。

    当 x_field 名称含纬度特征词（lat / y / northing）或 y_field 含经度特征词
    （lon / x / easting）时，warning 提示可能选反了字段。
    severity=warning + RepairProposal(kind="auto_repair", action="swap_x_y")。
    """
    kwargs = ctx.get("kwargs", {})
    x_field = str(kwargs.get("x_field", "")).lower().strip()
    y_field = str(kwargs.get("y_field", "")).lower().strip()

    if not x_field or not y_field:
        return []

    x_is_lat = x_field in _LAT_LIKE
    y_is_lon = y_field in _LON_LIKE

    if x_is_lat and y_is_lon:
        return [
            ValidationIssue(
                code="coordinate_field_swapped",
                stage="preflight",
                severity="warning",
                message=(
                    f"x_field='{x_field}' 疑似纬度字段，y_field='{y_field}' "
                    f"疑似经度字段——坐标可能选反。"
                ),
                repair=RepairProposal(
                    kind="confirm_action",
                    action="swap_x_y",
                    patch={"x_field": kwargs.get("y_field"), "y_field": kwargs.get("x_field")},
                ),
            )
        ]

    return []


@register_preflight_rule("output_path_overwrite", "export_result")
def _check_output_path_overwrite(ctx: dict[str, Any]) -> list[ValidationIssue]:
    """检查导出路径是否已存在文件。

    从 ctx.kwargs 取 output_path，用 os.path.exists 检查。
    存在时 severity=warning + RepairProposal(kind="confirm_overwrite")。
    """
    output_path = ctx.get("kwargs", {}).get("path", "")
    if not output_path or not os.path.exists(str(output_path)):
        return []
    return [
        ValidationIssue(
            code="output_path_exists",
            stage="preflight",
            severity="warning",
            message=f"输出路径 {output_path} 已存在，执行将覆盖。",
            repair=RepairProposal(
                kind="confirm_overwrite",
                patch={"path": output_path},
            ),
        )
    ]
