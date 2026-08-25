"""Raster 相关 preflight guardrail 规则。

规则列表：
- raster_band_exists: 检查 band 参数 >= 1。
- raster_nodata_warning: 注入 NoData 关注提示。
- reclassify_table_fields: 检查 bins/values 长度匹配。
- reclassify_io_types: 检查输入图层 kind 是否为 "raster"。
"""
from __future__ import annotations

from typing import Any

from app.agents.preflight.registry import register_preflight_rule
from app.agents.preflight.validation import ValidationIssue, RepairProposal


# ------------------------------------------------------------------
# 1. raster_band_exists
# ------------------------------------------------------------------

@register_preflight_rule(
    "raster_band_exists",
    "slope",
    "aspect",
    "hillshade",
    "zonal_statistics",
    "raster_sampling",
)
def _check_raster_band(ctx: dict[str, Any]) -> list[ValidationIssue]:
    """检查 kwargs 中的 band 参数是否为合理范围（band >= 1）。

    preflight 阶段无法读取栅格文件获取实际波段数，仅校验 band 参数合法性。
    """
    kwargs = ctx.get("kwargs", {})
    band = kwargs.get("band")
    if band is None:
        # 未传 band 参数 → 方法使用默认值 1，不报错
        return []

    try:
        band_int = int(band)
    except (TypeError, ValueError):
        return [
            ValidationIssue(
                code="raster_band_invalid",
                stage="preflight",
                severity="error",
                message=f"band 参数必须为整数，当前值: {band}",
                repair=RepairProposal(kind="ask_user"),
            )
        ]

    if band_int < 1:
        return [
            ValidationIssue(
                code="raster_band_invalid",
                stage="preflight",
                severity="error",
                message=f"band 参数不能为负数或零，当前值: {band_int}（波段号从 1 开始）",
                repair=RepairProposal(kind="ask_user"),
            )
        ]

    return []


# ------------------------------------------------------------------
# 2. raster_nodata_warning
# ------------------------------------------------------------------

_NODATA_WARNING_MSG = (
    "栅格计算建议检查并处理 NoData 值。"
    "如输入栅格含有 NoData 区域，计算结果可能在这些区域产生意外值（NaN/Inf），"
    "请考虑使用 np.nan_to_num、np.where 或在计算前对 NoData 做掩膜处理。"
)


@register_preflight_rule(
    "raster_nodata_warning",
    "raster_calculator",
    "zonal_statistics",
    "reclassify_raster",
)
def _check_raster_nodata(ctx: dict[str, Any]) -> list[ValidationIssue]:
    """注入 warning 提示关注 NoData。

    不检查实际数据（preflight 阶段无文件），仅注入 warning。
    """
    return [
        ValidationIssue(
            code="raster_nodata_warning",
            stage="preflight",
            severity="warning",
            message=_NODATA_WARNING_MSG,
        )
    ]


# ------------------------------------------------------------------
# 3. reclassify_table_fields
# ------------------------------------------------------------------

@register_preflight_rule("reclassify_table_fields", "reclassify_raster")
def _check_reclassify_fields(ctx: dict[str, Any]) -> list[ValidationIssue]:
    """检查 bins/values 是否存在且长度匹配。

    len(values) == len(bins) + 1（区间重分类）或 len(values) == len(bins)（逐值替换）。
    """
    kwargs = ctx.get("kwargs", {})
    bins = kwargs.get("bins")
    values = kwargs.get("values")

    if bins is None and values is None:
        return []  # 两个都没传，由函数默认值处理
    if bins is None:
        return [
            ValidationIssue(
                code="reclassify_missing_bins",
                stage="preflight",
                severity="error",
                message="reclassify_raster 缺少 bins 参数",
                repair=RepairProposal(kind="ask_user"),
            )
        ]
    if values is None:
        return [
            ValidationIssue(
                code="reclassify_missing_values",
                stage="preflight",
                severity="error",
                message="reclassify_raster 缺少 values 参数",
                repair=RepairProposal(kind="ask_user"),
            )
        ]

    # bins 和 values 都提供了，检查长度
    if not isinstance(bins, (list, tuple)):
        return [
            ValidationIssue(
                code="reclassify_bins_not_list",
                stage="preflight",
                severity="error",
                message=f"bins 必须为列表，当前类型: {type(bins).__name__}",
                repair=RepairProposal(kind="ask_user"),
            )
        ]
    if not isinstance(values, (list, tuple)):
        return [
            ValidationIssue(
                code="reclassify_values_not_list",
                stage="preflight",
                severity="error",
                message=f"values 必须为列表，当前类型: {type(values).__name__}",
                repair=RepairProposal(kind="ask_user"),
            )
        ]

    n_bins = len(bins)
    n_values = len(values)

    valid = (n_values == n_bins) or (n_values == n_bins + 1)
    if not valid:
        return [
            ValidationIssue(
                code="reclassify_length_mismatch",
                stage="preflight",
                severity="error",
                message=(
                    f"bins/values 长度不匹配: bins 有 {n_bins} 个元素，"
                    f"values 有 {n_values} 个元素。"
                    f"期望 values 长度 = bins 长度（逐值替换）或 bins 长度 + 1（区间重分类）。"
                ),
                repair=RepairProposal(kind="ask_user"),
            )
        ]

    return []


# ------------------------------------------------------------------
# 4. reclassify_io_types
# ------------------------------------------------------------------

@register_preflight_rule("reclassify_io_types", "reclassify_raster")
def _check_reclassify_io_types(ctx: dict[str, Any]) -> list[ValidationIssue]:
    """检查 input_ref 是否为 raster 类型（metadata.kind == "raster"）。"""
    workspace = ctx.get("workspace")
    kwargs = ctx.get("kwargs", {})
    input_ref = kwargs.get("input_ref")
    if not workspace or not input_ref:
        return []

    try:
        record = workspace.resolve(str(input_ref))
    except KeyError:
        # layer_exists 规则会处理缺失图层，这里不做二次报错
        return []

    kind = record.metadata.get("kind", "")
    if kind and kind != "raster":
        return [
            ValidationIssue(
                code="reclassify_not_raster_input",
                stage="preflight",
                severity="error",
                message=(
                    f"reclassify_raster 需要 raster 类型的输入图层，"
                    f"当前输入 {record.name} 类型为 {kind}"
                ),
                repair=RepairProposal(kind="ask_user"),
            )
        ]

    return []
