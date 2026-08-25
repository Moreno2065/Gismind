"""layer_exists + field_exists rules：检查图层与字段引用有效性。

规则：
- layer_exists: 作用于需要引用图层的多个 semantic_action。
- field_exists: 作用于 "extract_by_attribute" / "field_calculator"。
"""
from __future__ import annotations

from typing import Any

from app.agents.preflight.registry import register_preflight_rule
from app.agents.preflight.validation import ValidationIssue, RepairProposal

_KEY_REF_FIELDS = ("input_ref", "overlay_ref", "geom_ref", "layer_ref")


@register_preflight_rule(
    "layer_exists",
    "buffer_layer",
    "intersect_layer",
    "difference_layer",
    "clip_layer",
    "query_poi",
    "extract_by_attribute",
)
def _check_layer_exists(ctx: dict[str, Any]) -> list[ValidationIssue]:
    """检查所有引用的图层别名是否存在于 WorkspaceState 中。"""
    workspace = ctx.get("workspace")
    kwargs = ctx.get("kwargs", {})
    if not workspace:
        return []
    for key in _KEY_REF_FIELDS:
        ref = kwargs.get(key)
        # Native Schema tools use integer indexes into their runtime DAG
        # reference catalog. They are not WorkspaceState layer names.
        if isinstance(ref, int) and not isinstance(ref, bool):
            continue
        if ref is not None and not workspace.has_layer(str(ref)):
            return [
                ValidationIssue(
                    code="layer_not_found",
                    stage="preflight",
                    severity="error",
                    message=f"图层 {ref} 不存在，请检查变量名。",
                    repair=RepairProposal(kind="ask_user"),
                )
            ]
    return []


@register_preflight_rule("field_exists", "extract_by_attribute", "field_calculator")
def _check_field_exists(ctx: dict[str, Any]) -> list[ValidationIssue]:
    """检查表达式中引用的字段是否存在于目标图层的 metadata.fields 中。"""
    workspace = ctx.get("workspace")
    kwargs = ctx.get("kwargs", {})
    if not workspace:
        return []
    field_name = kwargs.get("field") or kwargs.get("formula") or kwargs.get("expression") or kwargs.get("attribute")
    layer_ref = kwargs.get("input_ref")
    if not field_name or not layer_ref:
        return []
    try:
        record = workspace.resolve(str(layer_ref))
    except KeyError:
        return []
    raw_fields = record.metadata.get("fields") or []
    available: set[str] = set()
    for f in raw_fields:
        if isinstance(f, str):
            available.add(f.lower())
        elif isinstance(f, dict) and "name" in f:
            available.add(f["name"].lower())
    if str(field_name).lower() not in available:
        return [
            ValidationIssue(
                code="field_not_found",
                stage="preflight",
                severity="error",
                message=(
                    f"字段 {field_name} 不存在于图层 {record.name}。"
                    f"可用字段：{', '.join(sorted(available)) or '(无字段信息)'}"
                ),
                repair=RepairProposal(kind="ask_user"),
            )
        ]
    return []
