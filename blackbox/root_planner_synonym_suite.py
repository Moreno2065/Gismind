"""Live Root-Planner synonym evaluation through Gismind's public HTTP API.

Unlike the deterministic browser suite, this program injects nothing. It calls
the configured real LLM and real services, then checks the public ``run.plan``
and executable tool evidence against an authored semantic contract.  Closed,
well-understood requests may deliberately use a deterministic guardrail rather
than model sampling; every case declares the permitted planner source(s).
It is intentionally small: a smoke signal, not a claim that model sampling is
deterministic.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import tempfile
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

from real_llm_prompt_suite import (
    build_fixtures,
    map_feature_count,
    parse_sse,
    planner_source,
    stream_chat,
    terminal_tool_completions,
    tool_completions,
    upload_fixture,
)


CASE_PATH = Path(__file__).with_name("root_planner_synonym_cases.json")


def _load_cases() -> list[dict[str, Any]]:
    data = json.loads(CASE_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError("root planner synonym suite must be a non-empty JSON list")
    for case in data:
        if not isinstance(case, dict) or not case.get("id") or not case.get("turns"):
            raise ValueError(f"invalid root planner synonym case: {case!r}")
    return data


def _select(cases: list[dict[str, Any]], spec: str) -> list[dict[str, Any]]:
    if not spec or spec.casefold() == "all":
        return cases
    wanted = {part.strip().upper() for part in spec.split(",") if part.strip()}
    return [case for case in cases if str(case["id"]).upper() in wanted]


def _plan_tasks(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for item in events:
        if item["event"] == "run.plan":
            tasks = item["data"].get("tasks") or []
            return [task for task in tasks if isinstance(task, dict)]
    return []


def _tool_starts(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item["data"] for item in events if item["event"] == "tool.call.start"]


def _map_point_coordinates(events: list[dict[str, Any]]) -> list[list[float]]:
    """Return the actual point coordinates emitted to the map SSE event."""

    points: list[list[float]] = []
    for item in events:
        if item["event"] != "map":
            continue
        for layer in item["data"].get("layers") or []:
            if not isinstance(layer, dict):
                continue
            if layer.get("type") == "FeatureCollection":
                for feature in layer.get("features") or []:
                    geometry = feature.get("geometry") if isinstance(feature, dict) else None
                    coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
                    if geometry and geometry.get("type") == "Point" and _coordinate_pair(coordinates):
                        points.append([float(coordinates[0]), float(coordinates[1])])
            elif layer.get("type") == "point":
                for coordinates in layer.get("coordinates") or []:
                    if _coordinate_pair(coordinates):
                        points.append([float(coordinates[0]), float(coordinates[1])])
    return points


def _answer(events: list[dict[str, Any]]) -> str:
    return "".join(
        str(item["data"].get("content") or "")
        for item in events
        if item["event"] == "token"
    )


def _has_dependency_edge(tasks: list[dict[str, Any]], upstream_tool: str, downstream_tool: str) -> bool:
    by_id = {str(task.get("id") or ""): task for task in tasks}
    for task in tasks:
        if task.get("tool_name") != downstream_tool:
            continue
        for dependency_id in task.get("depends_on") or []:
            upstream = by_id.get(str(dependency_id))
            if upstream and upstream.get("tool_name") == upstream_tool:
                return True
    return False


def _haversine_m(a: list[float], b: list[float]) -> float:
    lng1, lat1, lng2, lat2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    d_lng = lng2 - lng1
    d_lat = lat2 - lat1
    value = math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lng / 2) ** 2
    return 6_371_000.0 * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def _coordinate_pair(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) >= 2
        and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value[:2])
    )


def _decode_result(value: Any) -> Any:
    """Decode the JSON-string/dict variants emitted by tool completion SSE."""
    current = value
    for _ in range(2):
        if not isinstance(current, str):
            break
        try:
            current = json.loads(current)
        except json.JSONDecodeError:
            break
    return current


def _completion_result(completions: list[dict[str, Any]], tool_name: str) -> Any:
    for completion in reversed(completions):
        if completion.get("tool_name") != tool_name:
            continue
        result = _decode_result(completion.get("result"))
        if isinstance(result, dict) and isinstance(result.get("data"), dict):
            data = _decode_result(result["data"])
            if isinstance(data, dict) and data.get("type") in {"Feature", "FeatureCollection"}:
                return data
        return result
    return None


def _features(value: Any) -> list[dict[str, Any]]:
    decoded = _decode_result(value)
    if not isinstance(decoded, dict):
        return []
    if decoded.get("type") == "Feature":
        return [decoded]
    if decoded.get("type") == "FeatureCollection":
        return [item for item in decoded.get("features") or [] if isinstance(item, dict)]
    data = decoded.get("data")
    return _features(data) if isinstance(data, (dict, str)) else []


def _ring_area_m2(ring: Any) -> float:
    points = [
        point
        for point in (ring or [])
        if isinstance(point, list)
        and len(point) >= 2
        and isinstance(point[0], (int, float))
        and isinstance(point[1], (int, float))
    ]
    if len(points) < 4:
        return 0.0
    mean_lat = math.radians(sum(float(point[1]) for point in points) / len(points))
    radius = 6_371_000.0
    projected = [
        (
            radius * math.radians(float(point[0])) * math.cos(mean_lat),
            radius * math.radians(float(point[1])),
        )
        for point in points
    ]
    twice_area = sum(
        x1 * y2 - x2 * y1
        for (x1, y1), (x2, y2) in zip(projected, projected[1:] + projected[:1])
    )
    return abs(twice_area) / 2.0


def _geometry_area_m2(geometry: Any) -> float:
    if not isinstance(geometry, dict):
        return 0.0
    coordinates = geometry.get("coordinates") or []
    if geometry.get("type") == "Polygon":
        if not coordinates:
            return 0.0
        return max(
            0.0,
            _ring_area_m2(coordinates[0])
            - sum(_ring_area_m2(hole) for hole in coordinates[1:]),
        )
    if geometry.get("type") == "MultiPolygon":
        return sum(
            _geometry_area_m2({"type": "Polygon", "coordinates": polygon})
            for polygon in coordinates
        )
    return 0.0


def _geojson_area_m2(value: Any) -> float:
    return sum(_geometry_area_m2(feature.get("geometry")) for feature in _features(value))


def _poi_payload(completions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Recover the structured query_poi contract from an SSE completion."""

    summary: dict[str, Any] | None = None
    for completion in reversed(completions):
        if completion.get("tool_name") == "query_poi" and isinstance(completion.get("semantic"), dict):
            semantic = completion["semantic"]
            if semantic.get("kind") == "poi_radius":
                summary = dict(semantic)
                break
    candidate = _completion_result(completions, "query_poi")
    structured: dict[str, Any] | None = None
    for _ in range(3):
        candidate = _decode_result(candidate)
        if not isinstance(candidate, dict):
            break
        if isinstance(candidate.get("pois"), list):
            structured = candidate
            break
        candidate = candidate.get("data") or candidate.get("result")
    if structured is None:
        return summary
    if summary is None:
        return structured
    merged = dict(summary)
    merged.update(structured)
    return merged


def _poi_radius_reasons(
    payload: dict[str, Any],
    label: str,
    *,
    rendered_points: list[list[float]] | None = None,
) -> tuple[list[str], int | None, float | None]:
    """Check provider-independent circular distance from the public tool data."""

    pois = payload.get("pois")
    center = payload.get("center")
    radius = payload.get("radius_m")
    tolerance = payload.get("radius_tolerance_m", 0.0)
    count = len(pois) if isinstance(pois, list) else payload.get("poi_count")
    if not isinstance(count, int) or isinstance(count, bool) or not isinstance(center, list) or len(center) != 2:
        return [f"{label} query_poi structured payload missing count/center"], None, None
    if not isinstance(radius, (int, float)) or not isinstance(tolerance, (int, float)):
        return [f"{label} query_poi structured payload missing numeric radius"], count, None
    max_distance = 0.0
    reasons: list[str] = []
    points = rendered_points if rendered_points else [
        poi.get("location") for poi in pois if isinstance(poi, dict)
    ] if isinstance(pois, list) else []
    if len(points) != count:
        reasons.append(f"{label} POI point evidence count {len(points)} != tool count {count}")
    for location in points:
        if not _coordinate_pair(location):
            reasons.append(f"{label} POI has no coordinate for radius verification")
            continue
        distance = _haversine_m([float(center[0]), float(center[1])], [float(location[0]), float(location[1])])
        max_distance = max(max_distance, distance)
        if distance > float(radius) + float(tolerance):
            reasons.append(
                f"{label} POI outside radius: {distance:.1f}m > {float(radius) + float(tolerance):.1f}m"
            )
    return reasons, count, max_distance


def _semantic_assertions(
    case: dict[str, Any],
    completions: list[dict[str, Any]],
    *,
    prior_completions: list[dict[str, Any]] | None = None,
    answer: str = "",
    current_map_feature_count: int | None = None,
    prior_map_feature_counts: list[int] | None = None,
    current_map_points: list[list[float]] | None = None,
    prior_map_points: list[list[list[float]]] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Return authored GIS correctness failures and auditable numeric metrics."""
    case_id = str(case.get("id") or "")
    reasons: list[str] = []
    metrics: dict[str, Any] = {}

    if case_id == "RP01":
        payload = _poi_payload(completions)
        if payload is None:
            reasons.append("query_poi structured result missing")
            return reasons, metrics
        radius_reasons, tool_count, max_distance = _poi_radius_reasons(
            payload, "current", rendered_points=current_map_points,
        )
        reasons.extend(radius_reasons)
        metrics.update(
            tool_poi_count=tool_count,
            map_feature_count=current_map_feature_count,
            poi_radius_m=payload.get("radius_m"),
            max_poi_distance_m=round(max_distance, 3) if max_distance is not None else None,
        )
        if tool_count is not None and current_map_feature_count is not None and tool_count != current_map_feature_count:
            reasons.append(f"POI tool/map count mismatch: tool={tool_count}, map={current_map_feature_count}")
        if tool_count is not None and not re.search(rf"(?<!\d){tool_count}(?!\d)", answer):
            reasons.append(f"answer omitted POI count {tool_count}")

    elif case_id == "RP03":
        selected = _features(_completion_result(completions, "extract_by_attribute"))
        names = sorted(
            str((feature.get("properties") or {}).get("name") or "")
            for feature in selected
        )
        classes = {
            str((feature.get("properties") or {}).get("class") or "")
            for feature in selected
        }
        metrics.update(selected_feature_count=len(selected), selected_names=names)
        if names != ["站点C", "站点D"] or classes != {"station"}:
            reasons.append(f"attribute semantics mismatch: names={names}, classes={sorted(classes)}")

    elif case_id == "RP04":
        overlay = _completion_result(completions, "overlay")
        features = _features(overlay)
        area_m2 = _geojson_area_m2(overlay)
        metrics.update(
            overlay_feature_count=len(features),
            overlay_area_m2=round(area_m2, 3),
        )
        property_values = {
            str(value)
            for feature in features
            for value in (feature.get("properties") or {}).values()
        }
        if len(features) != 1:
            reasons.append(f"overlay feature count {len(features)} != 1")
        if not {"南京", "lake"}.issubset(property_values):
            reasons.append(f"overlay attributes missing source fields: {sorted(property_values)}")
        if not 8_000_000 < area_m2 < 11_000_000:
            reasons.append(f"overlay area outside golden range: {area_m2:.3f}m2")

    elif case_id == "RP05":
        raster = _completion_result(completions, "reclassify_raster")
        counts_raw = raster.get("class_counts") if isinstance(raster, dict) else None
        counts = {
            str(key): int(value)
            for key, value in (counts_raw or {}).items()
            if isinstance(value, (int, float))
        }
        metrics["raster_class_counts"] = counts
        total = sum(counts.values())
        if set(counts) != {"1", "2", "3"}:
            reasons.append(f"raster classes mismatch: {sorted(counts)}")
        if total != 1000:
            reasons.append(f"raster classified pixel count {total} != 1000")
        expected_ranges = {"1": (200, 300), "2": (200, 300), "3": (450, 550)}
        for value, (lower, upper) in expected_ranges.items():
            if not lower <= counts.get(value, -1) <= upper:
                reasons.append(f"raster class {value} count outside [{lower}, {upper}]: {counts.get(value)}")

    elif case_id == "RP06":
        previous_payload = _poi_payload(prior_completions or [])
        current_payload = _poi_payload(completions)
        previous_count: int | None = None
        current_count: int | None = None
        previous_max_distance: float | None = None
        current_max_distance: float | None = None
        if previous_payload is None:
            reasons.append("previous-turn query_poi structured result missing")
        else:
            radius_reasons, previous_count, previous_max_distance = _poi_radius_reasons(
                previous_payload,
                "previous",
                rendered_points=prior_map_points[-1] if prior_map_points else None,
            )
            reasons.extend(radius_reasons)
        if current_payload is None:
            reasons.append("current-turn query_poi structured result missing")
        else:
            radius_reasons, current_count, current_max_distance = _poi_radius_reasons(
                current_payload, "current", rendered_points=current_map_points,
            )
            reasons.extend(radius_reasons)
        metrics.update(
            previous_poi_count=previous_count,
            current_poi_count=current_count,
            previous_map_feature_count=prior_map_feature_counts[-1] if prior_map_feature_counts else None,
            current_map_feature_count=current_map_feature_count,
            previous_max_poi_distance_m=round(previous_max_distance, 3) if previous_max_distance is not None else None,
            current_max_poi_distance_m=round(current_max_distance, 3) if current_max_distance is not None else None,
        )
        if previous_count is not None and prior_map_feature_counts and previous_count != prior_map_feature_counts[-1]:
            reasons.append(
                "previous POI tool/map count mismatch: "
                f"tool={previous_count}, map={prior_map_feature_counts[-1]}"
            )
        if current_count is not None and current_map_feature_count is not None and current_count != current_map_feature_count:
            reasons.append(
                "current POI tool/map count mismatch: "
                f"tool={current_count}, map={current_map_feature_count}"
            )
        if previous_count is None:
            reasons.append("previous-turn query_poi result missing")
        if current_count is None:
            reasons.append("current-turn query_poi result missing")
        if previous_count is not None and current_count is not None:
            for label, count in (("蜜雪冰城", previous_count), ("茶百道", current_count)):
                if label not in answer or not re.search(rf"(?<!\d){count}(?!\d)", answer):
                    reasons.append(f"answer omitted {label} count {count}")
            if current_count > previous_count:
                direction_words = ("更多", "较多", "更高", "较高", "大于", "密度高")
            elif current_count < previous_count:
                direction_words = ("更少", "较少", "更低", "较低", "小于", "密度低")
            else:
                direction_words = ("相同", "一样", "相等", "持平", "没有差异")
            if not any(word in answer for word in direction_words):
                reasons.append(
                    "answer comparison direction mismatch: "
                    f"previous={previous_count}, current={current_count}"
                )

    elif case_id == "RP07":
        original = _completion_result(completions, "data_io_read")
        buffered = _completion_result(completions, "buffer")
        exported = _completion_result(completions, "export_result")
        if not _features(buffered) and isinstance(exported, dict):
            export_path = exported.get("path")
            if isinstance(export_path, str) and export_path:
                try:
                    buffered = json.loads(Path(export_path).read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    pass
        original_area = _geojson_area_m2(original)
        buffered_area = _geojson_area_m2(buffered)
        growth = buffered_area - original_area
        buffer_count = len(_features(buffered))
        export_count = exported.get("feature_count") if isinstance(exported, dict) else None
        export_source_crs = exported.get("source_crs") if isinstance(exported, dict) else None
        export_crs = exported.get("crs") if isinstance(exported, dict) else None
        export_transform = exported.get("coordinate_transform") if isinstance(exported, dict) else None
        metrics.update(
            buffer_feature_count=buffer_count,
            buffer_area_growth_m2=round(growth, 3),
            export_feature_count=export_count,
            export_source_crs=export_source_crs,
            export_crs=export_crs,
            export_coordinate_transform=export_transform,
        )
        if buffer_count != 4:
            reasons.append(f"buffer feature count {buffer_count} != 4")
        if not 2_000_000 < growth < 5_000_000:
            reasons.append(f"100m buffer area growth outside golden range: {growth:.3f}m2")
        if export_count != 4:
            reasons.append(f"export feature count {export_count} != 4")
        if str(export_source_crs).upper() != "GCJ02":
            reasons.append(f"export source CRS {export_source_crs!r} != 'GCJ02'")
        if str(export_crs).upper() not in {"EPSG:4326", "WGS84"}:
            reasons.append(f"export CRS {export_crs!r} is not WGS84")
        if export_transform != "GCJ02_TO_WGS84":
            reasons.append(f"export coordinate transform {export_transform!r} != 'GCJ02_TO_WGS84'")

    return reasons, metrics


def _coordinate_semantics(case: dict[str, Any], starts: list[dict[str, Any]], completions: list[dict[str, Any]]) -> list[str]:
    """Verify the coordinate case's numeric result rather than HTTP success."""
    if case.get("id") != "RP02":
        return []
    result: dict[str, Any] | None = None
    for completion in completions:
        if completion.get("tool_name") == "geo_transform" and isinstance(completion.get("result"), dict):
            result = completion["result"]
    if not result:
        return ["geo_transform result missing"]
    output = result.get("output") if isinstance(result.get("output"), dict) else {}
    got = [output.get("lng"), output.get("lat")]
    if not all(isinstance(value, (int, float)) for value in got):
        return ["geo_transform output omitted numeric lng/lat"]
    # GPS 116.397,39.908 converted to the GCJ02 neighbourhood. A 30 m bound
    # catches wrong CRS/direction while allowing implementation rounding.
    if _haversine_m([float(got[0]), float(got[1])], [116.403374, 39.909403]) > 30:
        return [f"coordinate error exceeds 30m: got={got}"]
    return []


def _export_semantics(case: dict[str, Any], completions: list[dict[str, Any]]) -> list[str]:
    if not case.get("export_readable"):
        return []
    for completion in completions:
        if completion.get("tool_name") != "export_result" or not isinstance(completion.get("result"), dict):
            continue
        path = completion["result"].get("path")
        if not isinstance(path, str) or not path:
            return ["export_result did not return a path"]
        output = Path(path)
        if not output.is_file() or output.stat().st_size <= 0:
            return [f"export file is not readable: {path}"]
        try:
            data = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return [f"export is not readable GeoJSON: {type(exc).__name__}: {exc}"]
        if data.get("type") not in {"FeatureCollection", "Feature"}:
            return [f"export has unexpected GeoJSON type: {data.get('type')!r}"]
        return []
    return ["export_result completion missing"]


def _validate(
    case: dict[str, Any],
    events: list[dict[str, Any]],
    http_status: int,
    *,
    prior_events: list[list[dict[str, Any]]] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    reasons: list[str] = []
    tasks = _plan_tasks(events)
    starts = _tool_starts(events)
    completions = tool_completions(events)
    terminal = terminal_tool_completions(completions)
    event_names = [item["event"] for item in events]
    planned_tools = [str(task.get("tool_name") or "") for task in tasks]
    planned_roles = [str(task.get("agent_role") or "") for task in tasks]
    source = planner_source(events)

    if http_status != 200:
        reasons.append(f"HTTP {http_status}")
    allowed_sources = set(case.get("allowed_planner_sources") or ["root_llm"])
    if source not in allowed_sources:
        reasons.append(
            f"planner_source={source or 'missing'}, expected one of {sorted(allowed_sources)}"
        )
    for tool_name, count in (case.get("expected_tools") or {}).items():
        if planned_tools.count(tool_name) < int(count):
            reasons.append(f"plan {tool_name} count {planned_tools.count(tool_name)} < {count}")
    for role, count in (case.get("expected_roles") or {}).items():
        if planned_roles.count(role) < int(count):
            reasons.append(f"plan {role} role count {planned_roles.count(role)} < {count}")
    for upstream, downstream in case.get("dependency_edges") or []:
        if not _has_dependency_edge(tasks, str(upstream), str(downstream)):
            reasons.append(f"missing DAG dependency {upstream} -> {downstream}")
    for assertion in case.get("tool_args") or []:
        tool_name = str(assertion.get("tool") or "")
        required = set(assertion.get("required_keys") or [])
        matching = [item for item in starts if item.get("tool_name") == tool_name]
        if not matching:
            reasons.append(f"no executed {tool_name} call")
        elif not any(required.issubset(set((item.get("params") or {}).keys())) for item in matching):
            reasons.append(f"{tool_name} omitted required executable args {sorted(required)}")
    failed = [
        f"{item.get('tool_name')}:{item.get('status')}:{item.get('error_code') or ''}"
        for item in terminal
        if item.get("status") != "success"
    ]
    if failed:
        reasons.append("terminal tool failures: " + ", ".join(failed))
    feature_count = map_feature_count(events)
    if case.get("expect_map") and feature_count <= 0:
        reasons.append("map missing or empty")
    if "done" not in event_names:
        reasons.append("missing done event")
    if "error" in event_names or "run.failed" in event_names:
        reasons.append("workflow emitted error/run.failed")
    if not _answer(events).strip():
        reasons.append("empty final answer")
    reasons.extend(_coordinate_semantics(case, starts, completions))
    reasons.extend(_export_semantics(case, completions))
    prior_completions = [
        completion
        for turn_events in (prior_events or [])
        for completion in tool_completions(turn_events)
    ]
    semantic_reasons, semantic_metrics = _semantic_assertions(
        case,
        completions,
        prior_completions=prior_completions,
        answer=_answer(events),
        current_map_feature_count=feature_count,
        prior_map_feature_counts=[
            map_feature_count(turn_events)
            for turn_events in (prior_events or [])
        ],
        current_map_points=_map_point_coordinates(events),
        prior_map_points=[
            _map_point_coordinates(turn_events)
            for turn_events in (prior_events or [])
        ],
    )
    reasons.extend(semantic_reasons)
    return reasons, {
        "planner_source": source,
        "event_sequence": event_names,
        "plan_tasks": tasks,
        "tool_starts": starts,
        "terminal_tools": terminal,
        "final_answer": _answer(events),
        "map_feature_count": feature_count,
        "semantic_metrics": semantic_metrics,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--cases", default="all", help="all or comma-separated RP ids")
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--case-deadline", type=float, default=120.0)
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    cases = _select(_load_cases(), args.cases)
    if not cases:
        parser.error("--cases did not select any synonym case")
    base_url = args.base_url.rstrip("/")
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    user_id = f"codex-root-synonyms-{uuid.uuid4().hex[:10]}"
    headers = {"X-User-Id": user_id}
    results: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix=".root-synonym-fixtures-", dir=output.parent) as temp_dir:
        fixtures = build_fixtures(Path(temp_dir))
        timeout = httpx.Timeout(connect=20.0, read=min(args.timeout, 60.0), write=60.0, pool=20.0)
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            health = client.get(f"{base_url}/api/health", headers=headers)
            health_evidence = {"status_code": health.status_code, "body": health.json()}
            for case in cases:
                started = time.monotonic()
                uploads: list[dict[str, Any]] = []
                upload_ids: list[str] = []
                for name in case.get("uploads") or []:
                    fixture = fixtures.get(str(name))
                    if fixture is None:
                        uploads.append({"name": name, "error": "fixture unavailable"})
                        continue
                    upload = upload_fixture(client, base_url, headers, fixture)
                    uploads.append({"name": name, **upload})
                    body = upload.get("body")
                    if upload.get("status_code") == 200 and isinstance(body, dict) and body.get("file_id"):
                        upload_ids.append(str(body["file_id"]))

                session_id = f"root-synonym-{case['id']}-{uuid.uuid4().hex[:8]}"
                turn_records: list[dict[str, Any]] = []
                for index, prompt in enumerate(case["turns"]):
                    payload: dict[str, Any] = {"session_id": session_id, "message": prompt}
                    if upload_ids:
                        payload["upload_file_ids"] = upload_ids
                    stream = stream_chat(client, base_url, headers, payload, args.case_deadline)
                    turn_records.append({
                        "payload": payload,
                        "http_status": stream["status_code"],
                        "stream": {key: value for key, value in stream.items() if key != "text"},
                        "events": parse_sse(stream["text"]),
                        "raw_sse": stream["text"],
                    })

                evaluation_index = int(case.get("evaluation_turn", len(turn_records) - 1))
                evaluated = turn_records[evaluation_index]
                reasons, evidence = _validate(
                    case,
                    evaluated["events"],
                    int(evaluated["http_status"]),
                    prior_events=[record["events"] for record in turn_records[:evaluation_index]],
                )
                record = {
                    "id": case["id"],
                    "status": "passed" if not reasons else "failed",
                    "reasons": reasons,
                    "duration_s": round(time.monotonic() - started, 2),
                    "session_id": session_id,
                    "uploads": uploads,
                    "upload_ids": upload_ids,
                    "turns": turn_records,
                    "evidence": evidence,
                }
                results.append(record)
                output.write_text(json.dumps({"health": health_evidence, "user_id": user_id, "cases": results}, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"CASE {case['id']} {record['status'].upper()} {record['duration_s']}s", "; ".join(reasons) or "ok", flush=True)

    passed = sum(case["status"] == "passed" for case in results)
    print(f"SUMMARY passed={passed} failed={len(results) - passed} total={len(results)} output={output}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
