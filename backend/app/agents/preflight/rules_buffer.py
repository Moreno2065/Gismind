"""buffer_crs rule：检查输入图层 CRS 是否为地理坐标系。

注册为 "buffer_requires_projected_crs" 规则，作用于 "buffer_layer" semantic_action。

当输入图层 CRS 属于 {EPSG:4326, EPSG:4490, CRS:84, WGS84} 时阻断，
建议先重投影到投影坐标系再做米制缓冲。
"""
from __future__ import annotations

from typing import Any

from app.agents.preflight.registry import register_preflight_rule
from app.agents.preflight.validation import ValidationIssue, RepairProposal

# 地理坐标系 CRS 前缀（单位是度，不能直接做米制缓冲）
_GEOGRAPHIC_CRS_PREFIXES = frozenset({
    "EPSG:4326", "EPSG:4490", "CRS:84", "WGS84", "OGC:CRS84",
})


@register_preflight_rule("buffer_requires_projected_crs", "buffer_layer")
def _check_buffer_crs(ctx: dict[str, Any]) -> list[ValidationIssue]:
    workspace = ctx.get("workspace")
    input_ref = ctx.get("kwargs", {}).get("input_ref") or ctx.get("kwargs", {}).get("geom_ref")
    if not workspace or not input_ref:
        return []
    try:
        record = workspace.resolve(str(input_ref))
    except KeyError:
        return [
            ValidationIssue(
                code="layer_not_found",
                stage="preflight",
                severity="error",
                message=f"图层 {input_ref} 不存在",
                repair=RepairProposal(kind="ask_user"),
            )
        ]
    crs = str(record.metadata.get("crs", "")).upper().strip()
    if any(crs.startswith(prefix) for prefix in _GEOGRAPHIC_CRS_PREFIXES):
        return [
            ValidationIssue(
                code="buffer_crs_mismatch",
                stage="preflight",
                severity="error",
                message=(
                    f"输入图层 CRS 是 {crs}（地理坐标系），不能直接按米缓冲。"
                    f"请先重投影到投影坐标系。"
                ),
                repair=RepairProposal(
                    kind="confirm_action",
                    action="reproject_layer",
                    patch={"input_ref": f"{record.name}_projected"},
                ),
            )
        ]
    return []
