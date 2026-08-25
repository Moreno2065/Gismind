"""Black-box real-LLM prompt suite for the public Gismind HTTP API.

This client imports no Gismind backend modules and never injects an LLM.  It
only uses GET /api/health, POST /api/upload and POST /api/chat, then validates
the returned SSE stream against docs/MANUAL_TESTING.md and
docs/04_testing_strategy.md.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx


@dataclass
class Case:
    id: str
    prompt: str
    expected_tools: dict[str, int] = field(default_factory=dict)
    upload_names: list[str] = field(default_factory=list)
    session_group: str | None = None
    expect_map: bool = False
    answer_must_include_coordinates: bool = False
    prerequisite: str = ""


CASES = [
    Case("01", "南京新街口的经纬度是多少", {"geo_code": 1}, answer_must_include_coordinates=True),
    Case("02", "南京新街口 500 米内有多少蜜雪冰城", {"query_poi": 1}, expect_map=True),
    Case("03", "找出南京夫子庙 1km 内的所有地铁站，然后把 1km 缓冲区画出来", {"query_poi": 1, "buffer": 1, "map_layer_build": 1}, expect_map=True),
    Case("04", "南京新街口 500m 蜜雪冰城覆盖区与夫子庙 500m 蜜雪冰城覆盖区，求交集并标出来", {"query_poi": 2, "buffer": 2, "overlay": 1, "map_layer_build": 1}, expect_map=True),
    Case("05", "把这 4 个 POI 做泰森多边形：中山陵、夫子庙、新街口、玄武湖", {"geo_code": 4, "voronoi": 1}, expect_map=True),
    Case("06", "画一个上海人民广场步行 15 分钟可达范围", {"geo_code": 1, "isochrone": 1}, expect_map=True),
    Case("07", "把这个文件按字段 class 分级设色显示", {"data_io_read": 1, "map_layer_build": 1}, ["sample_points"], "style", True),
    Case("08", "再加一层，把所有 class 是 poi 的点放大 2 倍", {"map_layer_build": 1}, [], "style", True),
    Case("09", "用南京市行政区划裁剪这个 POI 图层", {"data_io_read": 1, "clip_layer": 1}, ["sample_points", "nanjing_admin"], expect_map=True),
    Case("10", "按区域字段融合相邻地块", {"data_io_read": 1, "dissolve_layer": 1}, ["parcels"], expect_map=True),
    Case("11", "把玄武湖图层和紫金山图层合并成一个", {"data_io_read": 2, "merge_layers": 1}, ["xuanwuhu", "zijinshan"], expect_map=True),
    Case("12", "统计每个街道里有多少个 POI", {"data_io_read": 1, "count_points_in_polygon": 1}, ["streets", "sample_points"], expect_map=True),
    Case("13", "给每个 POI 关联最近的公交站点", {"data_io_read": 2, "join_by_nearest": 1}, ["sample_points", "bus_stations"], expect_map=True),
    Case("14", "加载 DEM，计算坡度、坡向、山体阴影，并叠加显示", {"slope": 1, "aspect": 1, "hillshade": 1}, ["dem"], expect_map=True, prerequisite="GeoTIFF upload"),
    Case("15", "用行政区分区统计 DEM 的平均海拔", {"zonal_statistics": 1}, ["dem", "nanjing_admin"], expect_map=True, prerequisite="GeoTIFF upload"),
    Case("16", "添加一个面积字段 area_km2", {"field_calculator": 1}, ["parcels"], expect_map=True),
    Case("17", "把这个图层从 GCJ02 转为 WGS84", {"reproject_layer": 1}, ["sample_points"], expect_map=True),
    Case("18", "筛选出 class 是 station 的所有要素", {"extract_by_attribute": 1}, ["sample_points"], expect_map=True),
    Case("19", "计算这组 POI 的外包凸包：中山陵、夫子庙、新街口、玄武湖", {"geo_code": 4, "convex_hull": 1}, expect_map=True),
    Case("20", "把坡度分成 0-15° / 15-30° / >30° 三档", {"slope": 1, "reclassify_raster": 1}, ["dem"], expect_map=True, prerequisite="GeoTIFF upload"),
    Case("21", "这个区有多少蜜雪冰城", {"data_io_read": 1, "query_poi": 1}, ["nanjing_admin"], expect_map=True),
    Case("22a", "南京新街口500米内蜜雪冰城", {"query_poi": 1}, session_group="multi", expect_map=True),
    Case("22b", "再查下茶百道，对比密度", {"query_poi": 1}, session_group="multi", expect_map=True),
    Case(
        "23",
        "修复上传图层几何，重投影到 EPSG:4548，做 500 米缓冲，融合后导出 GeoJSON",
        {"data_io_read": 1, "fix_geometries": 1, "reproject_layer": 1, "buffer": 1, "dissolve_layer": 1, "export_result": 1},
        ["parcels"],
    ),
]


def feature_collection(features: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": features}


def point(name: str, cls: str, x: float, y: float) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {"name": name, "class": cls},
        "geometry": {"type": "Point", "coordinates": [x, y]},
    }


def polygon(name: str, region: str, ring: list[list[float]]) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {"name": name, "region": region},
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def build_fixtures(root: Path) -> dict[str, Path]:
    sample = feature_collection([
        point("蜜雪A", "poi", 118.7800, 32.0420),
        point("蜜雪B", "poi", 118.7950, 32.0570),
        point("站点C", "station", 118.8050, 32.0420),
        point("站点D", "station", 118.8200, 32.0520),
    ])
    admin_ring = [[118.70, 31.95], [118.90, 31.95], [118.90, 32.15], [118.70, 32.15], [118.70, 31.95]]
    admin = feature_collection([polygon("南京测试行政区", "南京", admin_ring)])
    parcels = feature_collection([
        polygon("A1", "A", [[118.76, 32.00], [118.78, 32.00], [118.78, 32.02], [118.76, 32.02], [118.76, 32.00]]),
        polygon("A2", "A", [[118.78, 32.00], [118.80, 32.00], [118.80, 32.02], [118.78, 32.02], [118.78, 32.00]]),
        polygon("B1", "B", [[118.76, 32.02], [118.78, 32.02], [118.78, 32.04], [118.76, 32.04], [118.76, 32.02]]),
        polygon("B2", "B", [[118.78, 32.02], [118.80, 32.02], [118.80, 32.04], [118.78, 32.04], [118.78, 32.02]]),
    ])
    # Self-intersecting bow-tie polygon for real check_validity/fix_geometries
    # paths.  It is intentionally invalid rather than merely labelled invalid.
    invalid_parcels = feature_collection([
        polygon("bow_tie", "invalid", [
            [118.76, 32.00], [118.80, 32.04], [118.76, 32.04],
            [118.80, 32.00], [118.76, 32.00],
        ]),
    ])
    xuanwuhu = feature_collection([polygon("玄武湖", "lake", [[118.78, 32.06], [118.81, 32.06], [118.81, 32.09], [118.78, 32.09], [118.78, 32.06]])])
    zijinshan = feature_collection([polygon("紫金山", "mountain", [[118.82, 32.05], [118.88, 32.05], [118.88, 32.10], [118.82, 32.10], [118.82, 32.05]])])
    streets = feature_collection([
        polygon("街道甲", "甲", [[118.76, 32.00], [118.80, 32.00], [118.80, 32.08], [118.76, 32.08], [118.76, 32.00]]),
        polygon("街道乙", "乙", [[118.80, 32.00], [118.84, 32.00], [118.84, 32.08], [118.80, 32.08], [118.80, 32.00]]),
    ])
    bus_stations = feature_collection([
        # Intentionally co-located with 蜜雪A.  It exercises the closed 0 m
        # nearest-neighbour boundary without accepting any non-zero match.
        point("公交站甲", "bus_station", 118.7800, 32.0420),
        point("公交站乙", "bus_station", 118.8060, 32.0430),
        point("公交站丙", "bus_station", 118.8210, 32.0530),
    ])
    values = {
        "sample_points": sample,
        "nanjing_admin": admin,
        "parcels": parcels,
        "invalid_parcels": invalid_parcels,
        "xuanwuhu": xuanwuhu,
        "zijinshan": zijinshan,
        "streets": streets,
        "bus_stations": bus_stations,
    }
    paths: dict[str, Path] = {}
    for name, value in values.items():
        path = root / f"{name}.geojson"
        write_json(path, value)
        paths[name] = path

    dem_path = root / "nanjing_dem.tif"
    try:
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin

        # Four long, planar slope strips (10°, 15°, 30°, 45°).  The 15° and
        # 30° interiors exercise both reclassification boundaries while the
        # complete fixture still has values below/between/above the two bins.
        pixel_size = 0.002
        slope_degrees = np.repeat(np.array([10.0, 15.0, 30.0, 45.0]), 25)
        elevation_steps = np.tan(np.deg2rad(slope_degrees)) * pixel_size
        profile = np.cumsum(elevation_steps, dtype=np.float64).astype("float32")
        data = np.repeat(profile.reshape(1, 100), 10, axis=0)
        with rasterio.open(
            dem_path,
            "w",
            driver="GTiff",
            width=100,
            height=10,
            count=1,
            dtype="float32",
            crs="EPSG:4326",
            transform=from_origin(118.70, 32.15, pixel_size, pixel_size),
            nodata=-9999,
        ) as dataset:
            dataset.write(data, 1)
        paths["dem"] = dem_path
    except Exception as exc:  # noqa: BLE001
        (root / "dem_fixture_error.txt").write_text(str(exc), encoding="utf-8")
    return paths


def parse_sse(text: str) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    for block in re.split(r"\r?\n\r?\n", text):
        event_name = ""
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if not event_name or not data_lines:
            continue
        raw = "\n".join(data_lines)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"_raw": raw}
        parsed.append({"event": event_name, "data": data})
    return parsed


def tool_plan(events: list[dict[str, Any]]) -> list[str]:
    for item in events:
        if item["event"] == "run.plan":
            return [str(task.get("tool_name") or "") for task in item["data"].get("tasks") or []]
    return []


def planner_source(events: list[dict[str, Any]]) -> str:
    """Read the explicit source emitted with the public ``run.plan`` event.

    Reports produced before planner provenance existed deliberately return an
    empty value; callers must not infer Root-LLM planning from a non-empty DAG.
    """
    for item in events:
        if item["event"] == "run.plan":
            value = item["data"].get("planner_source")
            return str(value) if value in {"guardrail", "root_llm", "fallback"} else ""
    return ""


def tool_completions(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item["data"] for item in events if item["event"] == "tool.call.complete"]


def terminal_tool_completions(completions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the last attempt for each atomic DAG task.

    Native steps intentionally retry transient empty/error provider responses;
    an earlier failed attempt is not a workflow failure when the terminal
    attempt for that task succeeds.
    """
    terminal: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(completions):
        key = str(item.get("task_id") or f"{item.get('tool_name')}:{index}")
        terminal[key] = item
    return list(terminal.values())


def map_feature_count(events: list[dict[str, Any]]) -> int:
    total = 0
    for item in events:
        if item["event"] != "map":
            continue
        for layer in item["data"].get("layers") or []:
            layer_type = layer.get("type")
            if layer_type == "FeatureCollection":
                total += len(layer.get("features") or [])
            elif layer_type in {"point", "polygon", "polyline", "heatmap"}:
                total += len(layer.get("coordinates") or [])
            elif layer_type == "raster":
                total += 1
    return total


def count_tools(tools: list[str]) -> dict[str, int]:
    return {name: tools.count(name) for name in sorted(set(tools)) if name}


def validate(case: Case, response_status: int, events: list[dict[str, Any]], upload_errors: list[str]) -> tuple[str, list[str], dict[str, Any]]:
    reasons: list[str] = []
    plan = tool_plan(events)
    completions = tool_completions(events)
    terminal_completions = terminal_tool_completions(completions)
    token_text = "".join(str(item["data"].get("content") or "") for item in events if item["event"] == "token")
    event_names = [item["event"] for item in events]
    feature_count = map_feature_count(events)

    if upload_errors:
        reasons.extend(upload_errors)
    if response_status != 200:
        reasons.append(f"HTTP {response_status}")
    if "done" not in event_names:
        reasons.append("missing done event")
    if "error" in event_names or "run.failed" in event_names:
        reasons.append("workflow emitted error/run.failed")
    if "judge.awaiting_input" in event_names:
        reasons.append("workflow is awaiting user input")

    for name, expected_count in case.expected_tools.items():
        actual = plan.count(name)
        if actual < expected_count:
            reasons.append(f"plan {name} count {actual} < {expected_count}")

    failed_tools = [
        f"{item.get('tool_name')}:{item.get('status')}:{item.get('error_code') or ''}"
        for item in terminal_completions
        if item.get("status") != "success"
    ]
    if failed_tools:
        reasons.append("tool failures: " + ", ".join(failed_tools))
    if case.expect_map and feature_count <= 0:
        reasons.append("map missing or empty")
    if not token_text.strip():
        reasons.append("empty final answer")
    if case.answer_must_include_coordinates:
        numbers = [float(value) for value in re.findall(r"-?\d{2,3}\.\d+", token_text)]
        has_lng = any(70 <= value <= 140 for value in numbers)
        has_lat = any(0 <= value <= 60 for value in numbers)
        if not (has_lng and has_lat):
            reasons.append("final answer omitted numeric coordinates")
        if re.search(r"未(?:直接)?提供.{0,12}(?:经纬度|坐标)", token_text):
            reasons.append("final answer contradicts the geo_code result by claiming coordinates were not provided")

    status = "passed" if not reasons else "failed"
    evidence = {
        "event_names": event_names,
        "planner_source": planner_source(events),
        "plan_tools": plan,
        "plan_tool_counts": count_tools(plan),
        "tool_completions": completions,
        "map_feature_count": feature_count,
        "answer": token_text,
    }
    return status, reasons, evidence


def stream_chat(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    deadline_s: float,
) -> dict[str, Any]:
    """Consume SSE incrementally and enforce a wall-clock deadline."""
    started = time.monotonic()
    lines: list[str] = []
    run_id = ""
    timed_out = False
    status_code = 0
    read_error = ""
    try:
        with client.stream(
            "POST",
            f"{base_url}/api/chat",
            json=payload,
            headers={**headers, "Accept": "text/event-stream"},
        ) as response:
            status_code = response.status_code
            for line in response.iter_lines():
                lines.append(line)
                if line.startswith("data:"):
                    try:
                        item = json.loads(line[5:].strip())
                        if isinstance(item, dict) and item.get("run_id"):
                            run_id = str(item["run_id"])
                    except json.JSONDecodeError:
                        pass
                if time.monotonic() - started > deadline_s:
                    timed_out = True
                    break
    except (httpx.ReadError, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
        read_error = f"{type(exc).__name__}: {exc}"

    cancel_result: dict[str, Any] | None = None
    if (timed_out or read_error) and run_id:
        try:
            with httpx.Client(timeout=20, trust_env=False) as cancel_client:
                cancel_response = cancel_client.post(
                    f"{base_url}/api/runs/{run_id}/cancel",
                    headers=headers,
                )
            cancel_result = {
                "status_code": cancel_response.status_code,
                "body": cancel_response.json() if cancel_response.content else None,
            }
        except Exception as exc:  # noqa: BLE001
            cancel_result = {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "status_code": status_code,
        "text": "\n".join(lines),
        "timed_out": timed_out,
        "run_id": run_id,
        "cancel_result": cancel_result,
        "read_error": read_error,
        "elapsed_s": round(time.monotonic() - started, 2),
    }


def upload_fixture(client: httpx.Client, base_url: str, headers: dict[str, str], path: Path) -> dict[str, Any]:
    mime = "image/tiff" if path.suffix.lower() in {".tif", ".tiff"} else "application/geo+json"
    try:
        with path.open("rb") as handle:
            response = client.post(
                f"{base_url}/api/upload",
                files={"file": (path.name, handle, mime)},
                headers=headers,
            )
    except httpx.HTTPError as exc:
        return {
            "status_code": 0,
            "body": {"client_error": f"{type(exc).__name__}: {exc}"},
        }
    try:
        body: Any = response.json()
    except Exception:  # noqa: BLE001
        body = response.text[:1000]
    return {"status_code": response.status_code, "body": body}


def selected_cases(spec: str) -> list[Case]:
    if not spec or spec.lower() == "all":
        return CASES
    wanted: set[str] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part and part.replace("-", "").isdigit():
            start, end = part.split("-", 1)
            wanted.update(f"{value:02d}" for value in range(int(start), int(end) + 1))
        else:
            wanted.add(part.zfill(2) if part.isdigit() else part)
    return [case for case in CASES if case.id in wanted]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--cases", default="all", help="all, comma list, or numeric range such as 1-6")
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--case-deadline", type=float, default=300.0)
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    base_url = args.base_url.rstrip("/")
    user_id = f"codex-real-blackbox-{uuid.uuid4().hex[:8]}"
    headers = {"X-User-Id": user_id}
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cases = selected_cases(args.cases)
    session_ids: dict[str, str] = {}
    results: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="gismind-real-blackbox-") as temp_dir:
        fixtures = build_fixtures(Path(temp_dir))
        timeout = httpx.Timeout(connect=20.0, read=min(args.timeout, 60.0), write=60.0, pool=20.0)
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            health_response = client.get(f"{base_url}/api/health", headers=headers)
            health = {"status_code": health_response.status_code, "body": health_response.json()}
            print("HEALTH", json.dumps(health, ensure_ascii=False))

            upload_cache: dict[str, dict[str, Any]] = {}
            for case in cases:
                started = time.time()
                upload_ids: list[str] = []
                upload_errors: list[str] = []
                upload_evidence: dict[str, Any] = {}
                for name in case.upload_names:
                    if name not in fixtures:
                        upload_errors.append(f"fixture unavailable: {name}")
                        continue
                    if name not in upload_cache:
                        upload_cache[name] = upload_fixture(client, base_url, headers, fixtures[name])
                    upload_result = upload_cache[name]
                    upload_evidence[name] = upload_result
                    body = upload_result.get("body")
                    file_id = body.get("file_id") if isinstance(body, dict) else None
                    if upload_result["status_code"] == 200 and file_id:
                        upload_ids.append(str(file_id))
                    else:
                        upload_errors.append(f"upload {name} failed: HTTP {upload_result['status_code']} {body}")

                group = case.session_group or f"case-{case.id}"
                session_id = session_ids.setdefault(group, f"real-{group}-{uuid.uuid4().hex[:10]}")
                payload: dict[str, Any] = {"session_id": session_id, "message": case.prompt}
                if upload_ids:
                    payload["upload_file_ids"] = upload_ids
                try:
                    stream = stream_chat(client, base_url, headers, payload, args.case_deadline)
                    events = parse_sse(stream["text"])
                    stream_errors = list(upload_errors)
                    if stream["timed_out"]:
                        stream_errors.append(
                            f"wall-clock timeout after {stream['elapsed_s']}s; "
                            f"cancel={stream['cancel_result']}"
                        )
                    if stream["read_error"]:
                        stream_errors.append(f"SSE read failed: {stream['read_error']}")
                    status, reasons, evidence = validate(
                        case,
                        stream["status_code"],
                        events,
                        stream_errors,
                    )
                    record = {
                        "id": case.id,
                        "prompt": case.prompt,
                        "status": status,
                        "reasons": reasons,
                        "duration_s": round(time.time() - started, 2),
                        "session_id": session_id,
                        "upload_ids": upload_ids,
                        "uploads": upload_evidence,
                        "prerequisite": case.prerequisite,
                        "http_status": stream["status_code"],
                        "stream": {key: value for key, value in stream.items() if key != "text"},
                        "evidence": evidence,
                        "raw_sse": stream["text"],
                    }
                except Exception as exc:  # noqa: BLE001
                    record = {
                        "id": case.id,
                        "prompt": case.prompt,
                        "status": "failed",
                        "reasons": [f"client exception: {type(exc).__name__}: {exc}", *upload_errors],
                        "duration_s": round(time.time() - started, 2),
                        "session_id": session_id,
                        "upload_ids": upload_ids,
                        "uploads": upload_evidence,
                        "prerequisite": case.prerequisite,
                    }
                results.append(record)
                print(
                    f"CASE {case.id} {record['status'].upper()} {record['duration_s']}s",
                    "; ".join(record.get("reasons") or []) or "ok",
                    flush=True,
                )
                output_path.write_text(
                    json.dumps({"health": health, "user_id": user_id, "cases": results}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

    passed = sum(item["status"] == "passed" for item in results)
    print(f"SUMMARY passed={passed} failed={len(results) - passed} total={len(results)} output={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
