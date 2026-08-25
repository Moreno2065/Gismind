"""Vector/attribute guardrail rules。

规则列表：
1. crs_consistency          — 双图层输入 CRS 一致性检查
2. crs_geographic_for_metric_op — 米制运算前检查输入 CRS 是否为地理坐标系
3. geometry_type_match      — 双图层几何类型匹配检查
4. geometry_validity        — 输入图层几何有效性检查
5. extent_overlap           — 双图层 bbox 重叠检查
6. field_type_compatibility — 属性字段类型兼容性检查
7. keep_fields_downstream   — keep_fields 后 geometry 列保留检查
"""
from __future__ import annotations

from typing import Any

from app.agents.preflight.registry import register_preflight_rule
from app.agents.preflight.validation import ValidationIssue, RepairProposal

# 地理坐标系 CRS 前缀（单位是度，不能直接做米制运算）
_GEOGRAPHIC_CRS_PREFIXES = frozenset({
    "EPSG:4326", "EPSG:4490", "CRS:84", "WGS84", "OGC:CRS84",
})


def _get_two_refs(ctx: dict[str, Any]) -> tuple:
    """从 ctx 中提取两个图层引用 (input_ref, overlay_ref)。"""
    kwargs = ctx.get("kwargs", {})
    ref_a = kwargs.get("input_ref") or kwargs.get("geom_a_ref")
    ref_b = kwargs.get("overlay_ref") or kwargs.get("geom_b_ref")
    return ref_a, ref_b


def _is_geographic_crs(crs_label: str) -> bool:
    """检查 CRS 字符串或其 crs_label 是否为地理坐标系/GCJ02。"""
    crs_upper = crs_label.upper().strip()
    if crs_upper == "GCJ02":
        return True
    return any(crs_upper.startswith(prefix) for prefix in _GEOGRAPHIC_CRS_PREFIXES)


# ====================================================================
# 规则 1：crs_consistency
# ====================================================================


@register_preflight_rule(
    "crs_consistency",
    "join_by_location",
    "join_by_nearest",
    "clip_layer",
    "intersect_layer",
    "difference_layer",
    "union_layer",
    "extract_by_location",
    "spatial_join",
    "count_points_in_polygon",
)
def _check_crs_consistency(ctx: dict[str, Any]) -> list[ValidationIssue]:
    """检查两个输入图层的 CRS 是否一致。

    不一致时 severity=error，建议 reproject_layer 自动修复。
    """
    workspace = ctx.get("workspace")
    ref_a, ref_b = _get_two_refs(ctx)
    if not workspace or not ref_a or not ref_b:
        return []
    try:
        a = workspace.resolve(str(ref_a))
        b = workspace.resolve(str(ref_b))
    except KeyError:
        return []
    crs_a = str(a.metadata.get("crs", ""))
    crs_b = str(b.metadata.get("crs", ""))
    if crs_a and crs_b and crs_a != crs_b:
        return [
            ValidationIssue(
                code="crs_consistency_mismatch",
                stage="preflight",
                severity="error",
                message=(
                    f"两个图层 CRS 不一致：{a.name}={crs_a}, {b.name}={crs_b}。"
                    f"建议统一重投影到 {crs_a}，是否执行？"
                ),
                repair=RepairProposal(
                    kind="confirm_action",
                    action="reproject_layer",
                    patch={"overlay_ref": f"{b.name}_to_{crs_a.replace(':', '_')}"},
                ),
            )
        ]
    return []


# ====================================================================
# 规则 2：crs_geographic_for_metric_op
# ====================================================================


@register_preflight_rule(
    "crs_geographic_for_metric_op",
    "buffer_layer",
    "dissolve_layer",
    "area_calculation",
    "field_calculator",
    "simplify_geometry",
    "fix_geometries",
)
def _check_crs_geographic_for_metric(ctx: dict[str, Any]) -> list[ValidationIssue]:
    """检查输入图层 CRS 是否为地理坐标系/GCJ02。

    地理坐标系下米制运算结果严重变形，需先重投影到投影坐标系。
    """
    workspace = ctx.get("workspace")
    input_ref = ctx.get("kwargs", {}).get("input_ref") or ctx.get("kwargs", {}).get("geom_ref")
    if not workspace or not input_ref:
        return []
    try:
        record = workspace.resolve(str(input_ref))
    except KeyError:
        return []
    crs = str(record.metadata.get("crs", "")).strip()
    crs_label = str(record.metadata.get("crs_label", "")).strip()
    if _is_geographic_crs(crs) or _is_geographic_crs(crs_label):
        display_crs = crs_label or crs
        return [
            ValidationIssue(
                code="geographic_crs_for_metric_op",
                stage="preflight",
                severity="error",
                message=(
                    f"图层 {record.name} CRS 是 {display_crs}（地理坐标系），"
                    f"米制运算前建议重投影到投影坐标系（如 EPSG:4548），是否执行？"
                ),
                repair=RepairProposal(
                    kind="confirm_action",
                    action="reproject_layer",
                    patch={"input_ref": str(input_ref), "suggested_crs": "EPSG:4548"},
                ),
            )
        ]
    return []


# ====================================================================
# 规则 3：geometry_type_match
# ====================================================================


@register_preflight_rule(
    "geometry_type_match",
    "join_by_location",
    "count_points_in_polygon",
    "join_by_nearest",
)
def _check_geometry_type_match(ctx: dict[str, Any]) -> list[ValidationIssue]:
    """检查两个图层的 geometry_type 是否匹配。

    常见期望：点对点、面对点等。不匹配时 severity=warning。
    """
    workspace = ctx.get("workspace")
    ref_a, ref_b = _get_two_refs(ctx)
    if not workspace or not ref_a or not ref_b:
        return []
    try:
        a = workspace.resolve(str(ref_a))
        b = workspace.resolve(str(ref_b))
    except KeyError:
        return []
    gtype_a = str(a.metadata.get("geometry_type", "")).strip()
    gtype_b = str(b.metadata.get("geometry_type", "")).strip()
    if gtype_a and gtype_b and gtype_a != gtype_b:
        return [
            ValidationIssue(
                code="geometry_type_mismatch",
                stage="preflight",
                severity="warning",
                message=(
                    f"两个图层几何类型不一致：{a.name}={gtype_a}, {b.name}={gtype_b}。"
                    f"请确认空间操作是否合理。"
                ),
            )
        ]
    return []


# ====================================================================
# 规则 4：geometry_validity
# ====================================================================


@register_preflight_rule(
    "geometry_validity",
    "clip_layer",
    "intersect_layer",
    "difference_layer",
    "union_layer",
    "buffer_layer",
    "dissolve_layer",
)
def _check_geometry_validity(ctx: dict[str, Any]) -> list[ValidationIssue]:
    """检查输入图层是否有无效几何标记。

    metadata 中有 invalid_geometries 标记时 severity=warning，建议自动修复。
    """
    workspace = ctx.get("workspace")
    kwargs = ctx.get("kwargs", {})
    input_ref = kwargs.get("input_ref") or kwargs.get("geom_ref")
    if not workspace or not input_ref:
        return []
    try:
        record = workspace.resolve(str(input_ref))
    except KeyError:
        return []
    has_invalid = record.metadata.get("has_invalid_geometries", False)
    invalid_count = record.metadata.get("invalid_count", 0)
    if has_invalid or invalid_count > 0:
        return [
            ValidationIssue(
                code="geometry_has_invalid",
                stage="preflight",
                severity="warning",
                message=(
                    f"图层 {record.name} 包含 {invalid_count or '未知数量'} 个无效几何，"
                    f"可能导致计算异常。建议先执行 fix_geometries。"
                ),
                repair=RepairProposal(
                    kind="confirm_action",
                    action="fix_geometries",
                    patch={"input_ref": str(input_ref)},
                ),
            )
        ]
    return []


# ====================================================================
# 规则 5：extent_overlap
# ====================================================================


@register_preflight_rule(
    "extent_overlap",
    "clip_layer",
    "intersect_layer",
    "extract_by_location",
)
def _check_extent_overlap(ctx: dict[str, Any]) -> list[ValidationIssue]:
    """检查两个图层的 bbox 是否有重叠。

    无重叠时 severity=warning，提示可能产生空结果。
    """
    workspace = ctx.get("workspace")
    ref_a, ref_b = _get_two_refs(ctx)
    if not workspace or not ref_a or not ref_b:
        return []
    try:
        a = workspace.resolve(str(ref_a))
        b = workspace.resolve(str(ref_b))
    except KeyError:
        return []
    bbox_a = a.metadata.get("bbox")
    bbox_b = b.metadata.get("bbox")
    if not bbox_a or not bbox_b:
        return []
    try:
        from shapely.geometry import box as shapely_box

        box_a = shapely_box(*bbox_a)
        box_b = shapely_box(*bbox_b)
        if not box_a.intersects(box_b):
            return [
                ValidationIssue(
                    code="extent_no_overlap",
                    stage="preflight",
                    severity="warning",
                    message=(
                        f"图层 {a.name} 与 {b.name} 的 bbox 无重叠，"
                        f"空间操作可能产生空结果。"
                    ),
                )
            ]
    except Exception:
        # bbox 格式异常时不阻断
        pass
    return []


# ====================================================================
# 规则 6：field_type_compatibility
# ====================================================================


_NUMERIC_DTYPES = frozenset({
    "int64", "int32", "int16", "int8",
    "uint64", "uint32", "uint16", "uint8",
    "float64", "float32", "float16",
    "int", "float",
})


@register_preflight_rule(
    "field_type_compatibility",
    "extract_by_attribute",
    "field_calculator",
)
def _check_field_type_compatibility(ctx: dict[str, Any]) -> list[ValidationIssue]:
    """检查字段类型是否与操作兼容。

    字符串字段做数值比较时 severity=error + ask_user。
    """
    workspace = ctx.get("workspace")
    kwargs = ctx.get("kwargs", {})
    input_ref = kwargs.get("input_ref")
    if not workspace or not input_ref:
        return []
    try:
        record = workspace.resolve(str(input_ref))
    except KeyError:
        return []
    fields_meta = record.metadata.get("fields") or []
    # 构建字段名 -> dtype 映射（支持 str 和 dict 两种格式）
    field_dtypes: dict[str, str] = {}
    for f in fields_meta:
        if isinstance(f, str):
            pass  # 仅有字段名，无 dtype
        elif isinstance(f, dict):
            name = f.get("name", "")
            dtype = str(f.get("dtype", "")).lower()
            if name and dtype:
                field_dtypes[name] = dtype

    # 检查 extract_by_attribute 场景
    field_name = kwargs.get("field") or kwargs.get("attribute")
    operator = kwargs.get("operator", "")
    if field_name and operator in (">", ">=", "<", "<="):
        dtype = field_dtypes.get(str(field_name), "")
        if dtype and dtype not in _NUMERIC_DTYPES:
            return [
                ValidationIssue(
                    code="field_type_not_numeric",
                    stage="preflight",
                    severity="error",
                    message=(
                        f"字段 {field_name} 类型为 {dtype}（非数值），"
                        f"不支持 {operator} 数值比较。"
                    ),
                    repair=RepairProposal(kind="ask_user"),
                )
            ]

    return []


# ====================================================================
# 规则 7：keep_fields_downstream
# ====================================================================


@register_preflight_rule(
    "keep_fields_downstream",
    "keep_fields",
)
def _check_keep_fields_downstream(ctx: dict[str, Any]) -> list[ValidationIssue]:
    """检查 keep_fields 是否保留了 geometry 列。

    geometry 列缺失会导致下游空间操作失败，severity=warning。
    """
    kwargs = ctx.get("kwargs", {})
    fields = kwargs.get("fields") or []
    if not fields:
        return [
            ValidationIssue(
                code="keep_fields_no_fields",
                stage="preflight",
                severity="warning",
                message="keep_fields 未指定任何字段，将仅保留 geometry 列。",
            )
        ]
    # 检查是否包含 geometry
    has_geom = any(str(f).lower() == "geometry" for f in fields)
    if not has_geom:
        return [
            ValidationIssue(
                code="keep_fields_missing_geometry",
                stage="preflight",
                severity="warning",
                message=(
                    "keep_fields 未包含 geometry 列，"
                    "结果图层将无空间信息，无法进行后续空间操作。"
                ),
            )
        ]
    return []
