"""Blocking semantic checks for GIS tool outputs at the executor boundary.

These checks protect the contract that downstream agents, SSE traces and the
map renderer consume.  They deliberately inspect concrete tool data rather
than planner prose: a handler that reports ``success`` but violates a geometric
invariant is converted to a failed step before Dispatcher can publish it.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from statistics import median
from typing import Any, Mapping

from app.tools.geo_transform import gcj02_to_wgs84, haversine_m, wgs84_to_gcj02


@dataclass(frozen=True)
class PostconditionIssue:
    """A deterministic, user-visible semantic failure for one tool result."""

    code: str
    message: str


def validate_tool_postconditions(
    tool_name: str,
    data: Any,
    *,
    params: Mapping[str, Any],
    dependencies: Mapping[int, Any],
) -> list[PostconditionIssue]:
    """Return blocking semantic issues for the supported GIS result contracts.

    The function is intentionally no-op for tools without a declared invariant.
    Empty results are handled by the normal tool status contract; this function
    only rejects a *successful* result that contradicts its input or metadata.
    """

    issues: list[PostconditionIssue] = []
    scoped_dependencies = dependencies
    if tool_name in _POSTCONDITION_REFERENCE_FIELDS:
        scoped_dependencies, dependency_issues = _resolve_postcondition_dependencies(
            tool_name,
            params,
            dependencies,
        )
        issues.extend(dependency_issues)
        if dependency_issues:
            return _dedupe_issues(issues)
    if tool_name == "query_poi":
        issues.extend(_validate_poi(data))
    elif tool_name == "buffer":
        issues.extend(_validate_buffer(data, params, scoped_dependencies))
    elif tool_name == "overlay":
        issues.extend(_validate_overlay(data, params, scoped_dependencies))
    elif tool_name == "clip_layer":
        issues.extend(_validate_clip(data, scoped_dependencies))
    elif tool_name == "reclassify_raster":
        issues.extend(_validate_reclassify(data))
    elif tool_name == "export_result":
        issues.extend(_validate_export(data, scoped_dependencies))

    if tool_name in {"buffer", "overlay", "clip_layer"}:
        issues.extend(_validate_crs_contract(tool_name, data, scoped_dependencies))
    return _dedupe_issues(issues)


def postcondition_failure_message(issues: list[PostconditionIssue]) -> str:
    """Compact message retained in the tool trace and supplied to the agent."""

    return "; ".join(f"{issue.code}: {issue.message}" for issue in issues)


def _issue(code: str, message: str) -> PostconditionIssue:
    return PostconditionIssue(code=code, message=message)


_POSTCONDITION_REFERENCE_FIELDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "buffer": (("geometry_from", "points_from"),),
    "overlay": (("geometry_a_from",), ("geometry_b_from",)),
    "clip_layer": (("input_ref", "geometry_from"), ("overlay_ref", "mask_from")),
    "export_result": (("data_from", "input_ref"),),
}


def _resolve_postcondition_dependencies(
    tool_name: str,
    params: Mapping[str, Any],
    dependencies: Mapping[int, Any],
) -> tuple[dict[int, Any], list[PostconditionIssue]]:
    """Resolve the exact inputs named by the successful tool call.

    The runtime catalog can contain dependency products plus prior retry data.
    Positional iteration over that whole table is therefore not an execution
    contract.  Numeric ``*_from`` values select catalog entries; code-mode may
    pass a direct object.  The positional fallback only preserves legacy
    checkpoints that predate explicit server-bound references.
    """

    selected: dict[int, Any] = {}
    issues: list[PostconditionIssue] = []
    for position, field_names in enumerate(_POSTCONDITION_REFERENCE_FIELDS[tool_name]):
        value: Any = None
        declared = False
        for field_name in field_names:
            if field_name not in params or params.get(field_name) is None:
                continue
            declared = True
            reference = params.get(field_name)
            if isinstance(reference, int) and not isinstance(reference, bool):
                value = dependencies.get(reference)
            else:
                value = reference
            break
        if not declared:
            value = dependencies.get(position)
        if value is None:
            issues.append(_issue(
                "POSTCONDITION_DEPENDENCY_MISSING",
                f"{tool_name} 无法解析第 {position + 1} 个已执行输入，拒绝验证成功。",
            ))
        else:
            selected[position] = value
    return selected, issues


def _as_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dumped if isinstance(dumped, dict) else None
    return None


def _feature_collection(value: Any) -> dict[str, Any] | None:
    """Find a FeatureCollection without treating arbitrary nested maps as one."""

    item = _as_mapping(value)
    if item is None:
        return None
    if item.get("type") == "FeatureCollection" and isinstance(item.get("features"), list):
        return item
    for key in ("data", "result", "geojson"):
        nested = item.get(key)
        if nested is not item:
            found = _feature_collection(nested)
            if found is not None:
                return found
    return None


def _features(value: Any) -> list[dict[str, Any]]:
    collection = _feature_collection(value)
    if collection is None:
        return []
    return [feature for feature in collection.get("features", []) if isinstance(feature, dict)]


def _declared_crs(value: Any) -> str | None:
    item = _as_mapping(value)
    if item is None:
        return None
    for key in ("_crs_label", "crs"):
        candidate = item.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip().upper()
    for key in ("data", "result", "geojson"):
        nested = item.get(key)
        if nested is not item:
            crs = _declared_crs(nested)
            if crs:
                return crs
    return None


def _geometry_list(value: Any) -> tuple[list[Any], list[PostconditionIssue]]:
    features = _features(value)
    if not features:
        return [], []
    try:
        from shapely.geometry import shape
    except ImportError:
        return [], [_issue("GEOMETRY_VALIDATION_UNAVAILABLE", "Shapely is required for geometry postconditions.")]

    geometries: list[Any] = []
    issues: list[PostconditionIssue] = []
    for feature in features:
        geometry = feature.get("geometry")
        try:
            parsed = shape(geometry)
        except Exception:
            issues.append(_issue("GEOMETRY_INVALID", "输出包含无法解析的 GeoJSON 几何。"))
            continue
        if parsed.is_empty or not parsed.is_valid:
            issues.append(_issue("GEOMETRY_INVALID", "输出包含空或无效几何。"))
            continue
        geometries.append(parsed)
    return geometries, issues


def _validate_poi(data: Any) -> list[PostconditionIssue]:
    item = _as_mapping(data)
    if item is None:
        return [_issue("POI_CONTRACT_MISSING", "POI 成功结果必须是包含查询元数据的对象。")]
    pois = item.get("pois")
    center = item.get("center")
    radius = item.get("radius_m")
    tolerance = item.get("radius_tolerance_m", 0.0)
    crs = item.get("crs")
    if not isinstance(pois, list) or not _coordinate_pair(center) or not _positive_number(radius) or not isinstance(crs, str):
        return [_issue("POI_CONTRACT_MISSING", "POI 成功结果缺少 pois、center、radius_m 或 crs。")]
    try:
        tolerance_m = max(0.0, float(tolerance))
        center_pair = (float(center[0]), float(center[1]))
        max_distance = float(radius) + tolerance_m
    except (TypeError, ValueError):
        return [_issue("POI_CONTRACT_MISSING", "POI 半径元数据不是有效数值。")]

    issues: list[PostconditionIssue] = []
    expected_crs = crs.upper()
    for poi in pois:
        if not isinstance(poi, dict) or not _coordinate_pair(poi.get("location")):
            issues.append(_issue("POI_LOCATION_INVALID", "POI 缺少可计算距离的 location。"))
            continue
        location = (float(poi["location"][0]), float(poi["location"][1]))
        distance = haversine_m(center_pair, location)
        reported = poi.get("distance")
        if not isinstance(reported, (int, float)) or isinstance(reported, bool):
            issues.append(_issue("POI_DISTANCE_MISSING", "POI 必须携带服务端重算的 distance。"))
        elif not math.isclose(float(reported), distance, abs_tol=max(1.0, tolerance_m)):
            issues.append(_issue("POI_DISTANCE_MISMATCH", "POI 的 distance 与球面重算距离不一致。"))
        if distance > max_distance:
            issues.append(_issue("POI_OUTSIDE_RADIUS", "POI 超出请求半径，不能进入地图或计数。"))
        poi_crs = poi.get("crs")
        if not isinstance(poi_crs, str) or poi_crs.upper() != expected_crs:
            issues.append(_issue("POI_CRS_MISMATCH", "POI 坐标系与查询中心坐标系不一致。"))
    return _dedupe_issues(issues)


def _validate_buffer(
    data: Any,
    params: Mapping[str, Any],
    dependencies: Mapping[int, Any],
) -> list[PostconditionIssue]:
    output_geometries, issues = _geometry_list(data)
    if not output_geometries:
        return issues or [_issue("BUFFER_OUTPUT_INVALID", "buffer 成功结果缺少有效面几何。")]
    input_geometries, input_issues = _geometry_list(_first_dependency(dependencies))
    issues.extend(input_issues)
    if not input_geometries:
        issues.append(_issue("BUFFER_INPUT_INVALID", "buffer 输入缺少可验证的有效几何。"))
        return _dedupe_issues(issues)
    input_geometries, output_geometries, conversion_issues = _geometries_in_metric_crs(
        input_geometries,
        input_crs=_declared_crs(_first_dependency(dependencies)),
        output_geometries=output_geometries,
        output_crs=_declared_crs(data),
    )
    issues.extend(conversion_issues)
    if conversion_issues:
        return _dedupe_issues(issues)
    if sum(geometry.area for geometry in output_geometries) <= sum(geometry.area for geometry in input_geometries):
        issues.append(_issue("BUFFER_AREA_INVALID", "buffer 输出面积没有大于输入几何。"))

    radius = params.get("radius_m", params.get("radius"))
    if not _positive_number(radius):
        return _dedupe_issues(issues)
    distances: list[float] = []
    try:
        from shapely.geometry import Point
        from shapely.ops import unary_union

        source_union = unary_union(input_geometries)
        output_boundary = unary_union(output_geometries).boundary
        boundary_parts = list(getattr(output_boundary, "geoms", [])) or [output_boundary]
        for boundary in boundary_parts:
            for coordinate in getattr(boundary, "coords", []):
                distances.append(Point(coordinate).distance(source_union))
    except Exception:
        distances = []
    if not distances:
        issues.append(_issue("BUFFER_OUTPUT_INVALID", "buffer 输出不包含可验证的面边界。"))
    else:
        expected = float(radius)
        allowed = max(5.0, expected * 0.15)
        if abs(median(distances) - expected) > allowed:
            issues.append(_issue("BUFFER_RADIUS_MISMATCH", "buffer 边界与请求距离不一致。"))
    return _dedupe_issues(issues)


def _validate_overlay(
    data: Any,
    params: Mapping[str, Any],
    dependencies: Mapping[int, Any],
) -> list[PostconditionIssue]:
    output, issues = _geometry_list(data)
    first, first_issues = _geometry_list(_first_dependency(dependencies))
    second, second_issues = _geometry_list(_second_dependency(dependencies))
    issues.extend(first_issues + second_issues)
    if not output:
        issues.append(_issue("OVERLAY_OUTPUT_INVALID", "overlay 成功结果缺少可验证的有效几何。"))
    if not first or not second:
        issues.append(_issue("OVERLAY_INPUT_INVALID", "overlay 输入缺少可验证的有效几何。"))
    if not output or not first or not second:
        return _dedupe_issues(issues)
    output, conversion_issues = _geometries_in_crs(
        output,
        source_crs=_declared_crs(data),
        target_crs=_declared_crs(_first_dependency(dependencies)),
    )
    issues.extend(conversion_issues)
    if conversion_issues:
        return _dedupe_issues(issues)
    try:
        from shapely.ops import unary_union

        left = unary_union(first)
        right = unary_union(second)
        operation = str(params.get("how", "intersection"))
        expected_by_operation = {
            "intersection": lambda: left.intersection(right),
            "union": lambda: left.union(right),
            "difference": lambda: left.difference(right),
            "symmetric_difference": lambda: left.symmetric_difference(right),
            "identity": lambda: left,
        }
        expected_factory = expected_by_operation.get(operation)
        if expected_factory is None:
            issues.append(_issue("OVERLAY_OPERATION_UNVERIFIED", f"无法验证 overlay 模式 {operation!r}。"))
        else:
            expected = expected_factory()
            actual = unary_union(output)
            topology_tolerance = _topology_tolerance(_declared_crs(_first_dependency(dependencies)))
            if not _geometries_equivalent(expected, actual, topology_tolerance):
                issues.append(_issue("OVERLAY_TOPOLOGY_INVALID", f"{operation} 输出与两个输入的确定性拓扑结果不一致。"))
            property_inputs = (
                [_first_dependency(dependencies)]
                if operation == "difference"
                else [_first_dependency(dependencies), _second_dependency(dependencies)]
            )
            issues.extend(_validate_property_preservation(data, property_inputs, "OVERLAY_PROPERTY_LOST"))
    except Exception:
        issues.append(_issue("OVERLAY_TOPOLOGY_INVALID", "无法验证 overlay 输出拓扑。"))
    return _dedupe_issues(issues)


def _validate_clip(data: Any, dependencies: Mapping[int, Any]) -> list[PostconditionIssue]:
    output, issues = _geometry_list(data)
    source, source_issues = _geometry_list(_first_dependency(dependencies))
    mask, mask_issues = _geometry_list(_second_dependency(dependencies))
    issues.extend(source_issues + mask_issues)
    if not output:
        issues.append(_issue("CLIP_OUTPUT_INVALID", "clip 成功结果缺少可验证的有效几何。"))
    if not source or not mask:
        issues.append(_issue("CLIP_INPUT_INVALID", "clip 输入缺少可验证的有效几何。"))
    if not output or not source or not mask:
        return _dedupe_issues(issues)
    output, conversion_issues = _geometries_in_crs(
        output,
        source_crs=_declared_crs(data),
        target_crs=_declared_crs(_second_dependency(dependencies)),
    )
    issues.extend(conversion_issues)
    if conversion_issues:
        return _dedupe_issues(issues)
    try:
        from shapely.ops import unary_union

        source_union = unary_union(source)
        mask_union = unary_union(mask)
        topology_tolerance = _topology_tolerance(_declared_crs(_second_dependency(dependencies)))
        expected = source_union.intersection(mask_union)
        actual = unary_union(output)
        if not _geometries_equivalent(expected, actual, topology_tolerance):
            issues.append(_issue("CLIP_TOPOLOGY_INVALID", "clip 输出与输入和掩膜的完整交集不一致。"))
        issues.extend(_validate_property_preservation(data, [_first_dependency(dependencies)], "CLIP_PROPERTY_LOST"))
    except Exception:
        issues.append(_issue("CLIP_TOPOLOGY_INVALID", "无法验证 clip 输出拓扑。"))
    return _dedupe_issues(issues)


def _validate_property_preservation(
    output: Any,
    inputs: list[Any],
    code: str,
) -> list[PostconditionIssue]:
    output_properties = [
        feature.get("properties") or {}
        for feature in _features(output)
    ]
    output_fields = {
        str(field)
        for properties in output_properties
        for field in properties
    }
    input_values: dict[str, set[str]] = {}
    for item in inputs:
        for feature in _features(item):
            for field, value in (feature.get("properties") or {}).items():
                field_name = str(field)
                input_values.setdefault(field_name, set())
                if value is not None:
                    input_values[field_name].add(_stable_value(value))

    missing_fields = {
        field
        for field in input_values
        if field not in output_fields
        and not any(candidate.startswith(f"{field}_") for candidate in output_fields)
    }
    if missing_fields:
        return [_issue(code, "输出缺少输入图层应保留的属性字段。")]

    for properties in output_properties:
        for output_field, value in properties.items():
            if value is None:
                continue
            field_name = str(output_field)
            source_field = next(
                (
                    candidate
                    for candidate in input_values
                    if field_name == candidate or field_name.startswith(f"{candidate}_")
                ),
                None,
            )
            if source_field is None:
                continue
            allowed_values = input_values[source_field]
            if allowed_values and _stable_value(value) not in allowed_values:
                return [_issue(code, "输出属性值无法追溯到输入图层。")]
    return []


def _validate_reclassify(data: Any) -> list[PostconditionIssue]:
    item = _as_mapping(data)
    if item is None:
        return [_issue("RASTER_CLASS_COUNTS_MISSING", "栅格重分类成功结果缺少统计元数据。")]
    class_counts = item.get("class_counts")
    valid_count = item.get("valid_pixel_count")
    nodata_count = item.get("nodata_pixel_count")
    total_count = item.get("total_pixel_count")
    issues: list[PostconditionIssue] = []
    if not isinstance(class_counts, dict) or not isinstance(valid_count, int) or isinstance(valid_count, bool):
        issues.append(_issue("RASTER_CLASS_COUNTS_MISSING", "栅格重分类必须报告 class_counts 和 valid_pixel_count。"))
        return issues
    if not isinstance(nodata_count, int) or isinstance(nodata_count, bool) or nodata_count < 0:
        issues.append(_issue("RASTER_NODATA_COUNT_MISSING", "栅格重分类必须显式报告未参与统计的 nodata_pixel_count。"))
    if not isinstance(total_count, int) or isinstance(total_count, bool) or total_count < 0:
        issues.append(_issue("RASTER_TOTAL_COUNT_MISSING", "栅格重分类必须显式报告 total_pixel_count。"))
    try:
        counted = sum(int(count) for count in class_counts.values())
    except (TypeError, ValueError):
        return issues + [_issue("RASTER_CLASS_COUNTS_MISSING", "class_counts 必须全部为非负整数。")]
    if any(not isinstance(count, int) or isinstance(count, bool) or count < 0 for count in class_counts.values()):
        issues.append(_issue("RASTER_CLASS_COUNTS_MISSING", "class_counts 必须全部为非负整数。"))
    if counted != valid_count:
        issues.append(_issue("RASTER_CLASS_COUNT_MISMATCH", "class_counts 总和必须等于参与统计的有效像元数。"))
    if (
        isinstance(nodata_count, int)
        and not isinstance(nodata_count, bool)
        and isinstance(total_count, int)
        and not isinstance(total_count, bool)
        and valid_count + nodata_count != total_count
    ):
        issues.append(_issue("RASTER_PIXEL_PARTITION_MISMATCH", "有效像元与 nodata 数量之和必须等于栅格总像元数。"))
    return _dedupe_issues(issues)


def _validate_export(data: Any, dependencies: Mapping[int, Any]) -> list[PostconditionIssue]:
    item = _as_mapping(data)
    if item is None:
        return [_issue("EXPORT_CONTRACT_MISSING", "导出成功结果缺少 path 和 feature_count。")]
    path = item.get("path")
    reported_count = item.get("feature_count")
    if not isinstance(path, str) or not path or not isinstance(reported_count, int) or isinstance(reported_count, bool):
        return [_issue("EXPORT_CONTRACT_MISSING", "导出成功结果缺少 path 或有效 feature_count。")]
    output_path = Path(path)
    if not output_path.is_file():
        return [_issue("EXPORT_FILE_MISSING", "导出结果文件不存在或不可读。")]
    actual_count = _read_export_feature_count(output_path)
    if actual_count is None:
        return [_issue("EXPORT_UNREADABLE", "导出结果无法重新读取以验证。")]
    issues: list[PostconditionIssue] = []
    if actual_count != reported_count:
        issues.append(_issue("EXPORT_COUNT_MISMATCH", "导出文件要素数与返回的 feature_count 不一致。"))
    source = _first_dependency(dependencies)
    source_collection = _feature_collection(source)
    if source_collection is None:
        issues.append(_issue("EXPORT_SOURCE_INVALID", "导出输入缺少可重新计数的 FeatureCollection。"))
    elif actual_count != len(source_collection.get("features") or []):
        issues.append(_issue("EXPORT_SOURCE_COUNT_MISMATCH", "导出文件要素数与输入结果不一致。"))
    declared_source_crs = _canonical_export_crs(_declared_crs(source))
    reported_source_crs = _canonical_export_crs(item.get("source_crs"))
    exported_crs = _canonical_export_crs(item.get("crs"))
    if declared_source_crs is None or reported_source_crs is None or exported_crs is None:
        issues.append(_issue("EXPORT_CRS_METADATA_MISSING", "导出结果必须声明输入 CRS 和输出 CRS。"))
    elif declared_source_crs != reported_source_crs:
        issues.append(_issue("EXPORT_SOURCE_CRS_MISMATCH", "导出结果报告的 source_crs 与输入图层不一致。"))
    elif declared_source_crs == "GCJ02":
        if exported_crs != "WGS84" or item.get("coordinate_transform") != "GCJ02_TO_WGS84":
            issues.append(_issue(
                "EXPORT_CRS_TRANSFORM_MISSING",
                "GCJ02 数据导出为标准 GIS 文件前必须转换为 WGS84 并记录转换来源。",
            ))
    elif exported_crs != declared_source_crs:
        issues.append(_issue("EXPORT_CRS_MISMATCH", "导出文件 CRS 与输入图层 CRS 不一致。"))
    return issues


def _canonical_export_crs(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().upper().replace(" ", "")
    if normalized == "GCJ02":
        return "GCJ02"
    if normalized in {"WGS84", "EPSG:4326", "OGC:CRS84", "CRS84"}:
        return "WGS84"
    try:
        from pyproj import CRS

        crs = CRS.from_user_input(value)
        if crs.to_epsg() == 4326:
            return "WGS84"
        return crs.to_string().upper()
    except Exception:
        return normalized


def _read_export_feature_count(path: Path) -> int | None:
    suffix = path.suffix.lower()
    if suffix in {".json", ".geojson"}:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            features = payload.get("features") if isinstance(payload, dict) else None
            return len(features) if isinstance(features, list) else None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
    try:
        import geopandas as gpd

        return int(len(gpd.read_file(path)))
    except Exception:
        return None


def _validate_crs_contract(
    tool_name: str,
    data: Any,
    dependencies: Mapping[int, Any],
) -> list[PostconditionIssue]:
    issues: list[PostconditionIssue] = []
    if _declared_crs(data) is None:
        issues.append(_issue("CRS_METADATA_MISSING", f"{tool_name} 输出未声明 CRS，无法排除坐标系混用。"))
    missing_inputs = [index for index, value in dependencies.items() if _declared_crs(value) is None]
    if missing_inputs:
        issues.append(_issue("CRS_METADATA_MISSING", f"{tool_name} 输入未完整声明 CRS，无法排除坐标系混用。"))
    issues.extend(_validate_input_crs(tool_name, dependencies))
    return _dedupe_issues(issues)


def _validate_input_crs(tool_name: str, dependencies: Mapping[int, Any]) -> list[PostconditionIssue]:
    crs_values = {crs for value in dependencies.values() if (crs := _declared_crs(value))}
    if len(crs_values) > 1:
        return [_issue("CRS_MISMATCH", f"{tool_name} 的输入图层坐标系不一致：{', '.join(sorted(crs_values))}。")]
    return []


def _geometries_in_crs(
    geometries: list[Any],
    *,
    source_crs: str | None,
    target_crs: str | None,
) -> tuple[list[Any], list[PostconditionIssue]]:
    """Convert only the explicit GCJ02/WGS84 display-coordinate boundary.

    SpatialAnalyzer intentionally returns GCJ02 for domestic map rendering,
    even when an uploaded source was WGS84.  Topology/radius validation must
    compare one coordinate space, otherwise a correct result is falsely
    rejected by the several-hundred-metre GCJ offset.
    """

    if not source_crs or not target_crs or source_crs == target_crs:
        return geometries, []
    conversion: Any | None = None
    if source_crs == "GCJ02" and target_crs == "WGS84":
        conversion = gcj02_to_wgs84
    elif source_crs == "WGS84" and target_crs == "GCJ02":
        conversion = wgs84_to_gcj02
    else:
        return geometries, [_issue(
            "CRS_COMPARISON_UNSUPPORTED",
            f"无法在 {source_crs} 与 {target_crs} 之间验证几何拓扑。",
        )]
    try:
        from shapely.ops import transform

        def transform_coordinate(x: float, y: float, z: float | None = None):
            converted_x, converted_y = conversion(float(x), float(y))
            return (converted_x, converted_y) if z is None else (converted_x, converted_y, z)

        return [transform(transform_coordinate, geometry) for geometry in geometries], []
    except Exception:
        return geometries, [_issue(
            "CRS_COMPARISON_UNSUPPORTED",
            f"无法将 {source_crs} 转换为 {target_crs} 以验证几何结果。",
        )]


def _geometries_in_metric_crs(
    input_geometries: list[Any],
    *,
    input_crs: str | None,
    output_geometries: list[Any],
    output_crs: str | None,
) -> tuple[list[Any], list[Any], list[PostconditionIssue]]:
    """Project both sides into one local metre CRS for distance/area checks."""

    try:
        from pyproj import CRS, Transformer
        from shapely.ops import transform, unary_union

        def to_wgs84(geometries: list[Any], crs_label: str | None) -> list[Any]:
            normalized = str(crs_label or "").upper()
            if normalized == "GCJ02":
                def gcj_to_wgs(x: float, y: float, z: float | None = None):
                    converted_x, converted_y = gcj02_to_wgs84(float(x), float(y))
                    return (converted_x, converted_y) if z is None else (converted_x, converted_y, z)

                return [transform(gcj_to_wgs, geometry) for geometry in geometries]
            if normalized in {"WGS84", "EPSG:4326"}:
                return geometries
            source = CRS.from_user_input(crs_label)
            transformer = Transformer.from_crs(source, "EPSG:4326", always_xy=True)
            return [transform(transformer.transform, geometry) for geometry in geometries]

        input_wgs84 = to_wgs84(input_geometries, input_crs)
        output_wgs84 = to_wgs84(output_geometries, output_crs)
        anchor = unary_union(input_wgs84).centroid
        zone = max(1, min(60, int((float(anchor.x) + 180.0) // 6.0) + 1))
        metric_epsg = 32600 + zone if float(anchor.y) >= 0 else 32700 + zone
        projector = Transformer.from_crs("EPSG:4326", f"EPSG:{metric_epsg}", always_xy=True)
        return (
            [transform(projector.transform, geometry) for geometry in input_wgs84],
            [transform(projector.transform, geometry) for geometry in output_wgs84],
            [],
        )
    except Exception:
        return input_geometries, output_geometries, [_issue(
            "CRS_COMPARISON_UNSUPPORTED",
            f"无法把 {input_crs or 'unknown'} 与 {output_crs or 'unknown'} 投影到统一米制坐标系。",
        )]


def _topology_tolerance(crs: str | None) -> float:
    # GCJ02↔WGS84 inverse conversion is iterative and GeoJSON serialization
    # also rounds coordinates.  About 11 cm in geographic display space avoids
    # treating a numerical sliver as a topology escape; projected data gets a
    # sub-millimetre tolerance instead.
    return 1e-6 if crs in {"GCJ02", "WGS84"} else 1e-4


def _covers_with_tolerance(container: Any, geometry: Any, tolerance: float) -> bool:
    return container.covers(geometry) or container.buffer(tolerance).covers(geometry)


def _geometries_equivalent(expected: Any, actual: Any, tolerance: float) -> bool:
    """Bidirectional coverage prevents a correct-looking partial subset passing."""

    return (
        _covers_with_tolerance(expected, actual, tolerance)
        and _covers_with_tolerance(actual, expected, tolerance)
    )


def _coordinate_pair(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value)
    )


def _positive_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) > 0


def _first_dependency(dependencies: Mapping[int, Any]) -> Any:
    return next(iter(dependencies.values()), None)


def _second_dependency(dependencies: Mapping[int, Any]) -> Any:
    iterator = iter(dependencies.values())
    next(iterator, None)
    return next(iterator, None)


def _stable_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _dedupe_issues(issues: list[PostconditionIssue]) -> list[PostconditionIssue]:
    seen: set[str] = set()
    unique: list[PostconditionIssue] = []
    for issue in issues:
        if issue.code not in seen:
            seen.add(issue.code)
            unique.append(issue)
    return unique
