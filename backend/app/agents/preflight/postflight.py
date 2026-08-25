"""Postflight 检查：空结果 / feature_count 异常检测 / CSV 行数验证。"""
from __future__ import annotations

from typing import Any

from app.agents.preflight.validation import ValidationIssue

# 需要附空间原因提示的工具集合
_SPATIAL_EMPTY_TOOLS = {
    "clip_layer",
    "intersect_layer",
    "extract_by_location",
    "count_points_in_polygon",
}

# 空间连接类工具（需检查 feature_count_explosion）
_JOIN_TOOLS = {"join_by_location", "join_by_nearest"}

_SPATIAL_EMPTY_HINT = "可能原因：CRS 不一致、输入范围无交集、筛选条件过严。"


def run_postflight(tool_name: str, result, ctx: dict[str, Any]) -> list[ValidationIssue]:
    """对工具执行结果做 postflight 检查。

    检查项：
    - 结果为空（features=[] 或 data 为空）。
    - 空空间结果（spatial tools 特定原因提示）。
    - feature_count 异常（输出 >> 输入）。
    - feature_count_explosion（空间连接导致要素膨胀）。
    - csv_row_vs_feature（CSV 转点行数差异过大）。

    Args:
        tool_name: 工具名称（含 semantic_action 名）。
        result: 工具执行结果（应有 .data 属性）。
        ctx: 上下文 dict，含 "workspace"、"kwargs" 等。

    Returns:
        postflight ValidationIssue 列表（不阻断，仅 warning）。
    """
    issues: list[ValidationIssue] = []

    if not hasattr(result, "data"):
        return issues

    data = result.data or {}

    # --- 空结果检测 ---
    features = data.get("features")
    if features is not None and isinstance(features, list) and len(features) == 0:
        issues.append(
            ValidationIssue(
                code="empty_result",
                stage="postflight",
                severity="warning",
                message="结果为空，可能输入范围过大或筛选条件过严。",
            )
        )

    # --- 空空间结果（附加 GIS 专用原因提示） ---
    _check_empty_spatial_result(tool_name, data, issues)

    # --- feature_count 异常检测 ---
    fc = data.get("feature_count")
    if fc is None and features is not None:
        fc = len(features)
    if fc is not None and isinstance(fc, (int, float)) and fc > 0:
        input_count = _get_input_feature_count(ctx)
        if input_count is not None and fc > input_count * 10:
            issues.append(
                ValidationIssue(
                    code="feature_count_anomaly",
                    stage="postflight",
                    severity="warning",
                    message=(
                        f"输出要素数量 ({fc}) 远超输入 ({input_count})，"
                        f"可能存在一对多展开或重复。"
                    ),
                )
            )

        # --- feature_count_explosion（空间连接膨胀） ---
        _check_feature_count_explosion(tool_name, fc, ctx, issues)

    # --- csv_row_vs_feature（CSV 转点行数差异） ---
    _check_csv_row_vs_feature(tool_name, data, ctx, issues)

    return issues


def _check_empty_spatial_result(
    tool_name: str, data: dict, issues: list[ValidationIssue],
) -> None:
    """当 spatial tools 结果为空时，附加 GIS 专用原因提示。"""
    if tool_name not in _SPATIAL_EMPTY_TOOLS:
        return
    features = data.get("features")
    if features is not None and isinstance(features, list) and len(features) == 0:
        # 检查是否已有 empty_result issue，补充消息
        for issue in issues:
            if issue.code == "empty_result":
                issue.message = f"{issue.message} {_SPATIAL_EMPTY_HINT}"
                return
        # 如果没有已有的 empty_result issue，单独创建
        issues.append(
            ValidationIssue(
                code="empty_spatial_result",
                stage="postflight",
                severity="warning",
                message=_SPATIAL_EMPTY_HINT,
            )
        )


def _check_feature_count_explosion(
    tool_name: str, fc: int, ctx: dict[str, Any], issues: list[ValidationIssue],
) -> None:
    """检查空间连接是否导致要素数量膨胀（输出 > max(input_a, input_b) * 5）。"""
    if tool_name not in _JOIN_TOOLS:
        return

    workspace = ctx.get("workspace")
    kwargs = ctx.get("kwargs", {})
    if not workspace:
        return

    counts: list[int] = []
    for ref_key in ("input_ref", "overlay_ref"):
        ref = kwargs.get(ref_key)
        if not ref:
            continue
        try:
            record = workspace.resolve(str(ref))
            c = record.metadata.get("feature_count")
            if isinstance(c, (int, float)):
                counts.append(int(c))
        except KeyError:
            continue

    if not counts:
        return

    max_input = max(counts)
    if fc > max_input * 5:
        issues.append(
            ValidationIssue(
                code="feature_count_explosion",
                stage="postflight",
                severity="warning",
                message=(
                    f"空间连接导致要素数量膨胀：输出 {fc} 要素，"
                    f"最大输入为 {max_input}（膨胀 {fc / max_input:.1f}x）。"
                ),
            )
        )


def _check_csv_row_vs_feature(
    tool_name: str, data: dict, ctx: dict[str, Any], issues: list[ValidationIssue],
) -> None:
    """比较 CSV 输入行数与 csv_to_points 输出要素数，差异大时 warning。"""
    if tool_name != "csv_to_points":
        return

    workspace = ctx.get("workspace")
    kwargs = ctx.get("kwargs", {})
    if not workspace:
        return

    # 获取输出 feature_count
    fc = data.get("feature_count")
    features = data.get("features")
    if fc is None and features is not None:
        fc = len(features)
    if fc is None:
        return

    # 获取输入 row_count（从 workspace 中引用的图层元数据）
    input_ref = kwargs.get("csv_df_or_path")
    if not isinstance(input_ref, str):
        return

    try:
        record = workspace.resolve(str(input_ref))
        row_count = record.metadata.get("row_count")
        if row_count is None:
            # fallback: 用 feature_count 作为近似
            row_count = record.metadata.get("feature_count")
    except KeyError:
        return

    if row_count is None:
        return

    if isinstance(row_count, (int, float)) and row_count > 0:
        diff_ratio = float(fc) / float(row_count)
        if diff_ratio < 0.5 or diff_ratio > 2.0:
            issues.append(
                ValidationIssue(
                    code="csv_row_vs_feature",
                    stage="postflight",
                    severity="warning",
                    message=(
                        f"CSV 输入 {row_count} 行，输出 {fc} 个要素 "
                        f"（比例 {diff_ratio:.2f}x），差异较大，"
                        f"请检查坐标字段是否正确或是否有大量无效坐标被过滤。"
                    ),
                )
            )


def _get_input_feature_count(ctx: dict[str, Any]) -> int | None:
    """从上下文获取输入图层的 feature_count。"""
    workspace = ctx.get("workspace")
    input_ref = ctx.get("kwargs", {}).get("input_ref")
    if not workspace or not input_ref:
        return None
    try:
        record = workspace.resolve(str(input_ref))
        return record.metadata.get("feature_count")
    except KeyError:
        return None
