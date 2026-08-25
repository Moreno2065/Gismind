"""overlay_crs_alignment rule：检查两个输入图层 CRS 是否一致。

注册为 "overlay_crs_alignment" 规则，作用于 "intersect_layer"、
"difference_layer"、"clip_layer" semantic_action。

当两个图层的 CRS 都存在且不一致时阻断，建议统一 CRS。
"""
from __future__ import annotations

from typing import Any

from app.agents.preflight.registry import register_preflight_rule
from app.agents.preflight.validation import ValidationIssue, RepairProposal


@register_preflight_rule(
    "overlay_crs_alignment",
    "intersect_layer",
    "difference_layer",
    "clip_layer",
)
def _check_overlay_crs(ctx: dict[str, Any]) -> list[ValidationIssue]:
    workspace = ctx.get("workspace")
    kwargs = ctx.get("kwargs", {})
    ref_a = kwargs.get("input_ref") or kwargs.get("geom_a_ref")
    ref_b = kwargs.get("overlay_ref") or kwargs.get("geom_b_ref")
    if not workspace or not ref_a or not ref_b:
        return []
    try:
        a = workspace.resolve(str(ref_a))
        b = workspace.resolve(str(ref_b))
    except KeyError:
        # layer_exists rule 会处理缺失图层
        return []
    crs_a = str(a.metadata.get("crs", ""))
    crs_b = str(b.metadata.get("crs", ""))
    if crs_a and crs_b and crs_a != crs_b:
        return [
            ValidationIssue(
                code="overlay_crs_mismatch",
                stage="preflight",
                severity="error",
                message=(
                    f"两个图层 CRS 不一致：{a.name}={crs_a}, {b.name}={crs_b}。"
                    f"请先统一 CRS。"
                ),
                repair=RepairProposal(
                    kind="confirm_action",
                    action="reproject_layer",
                    patch={
                        "overlay_ref": (
                            f"{b.name}_to_{crs_a.replace(':', '_')}"
                        )
                    },
                ),
            )
        ]
    return []
