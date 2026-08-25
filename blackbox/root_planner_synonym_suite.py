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


def _validate(case: dict[str, Any], events: list[dict[str, Any]], http_status: int) -> tuple[list[str], dict[str, Any]]:
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
    return reasons, {
        "planner_source": source,
        "event_sequence": event_names,
        "plan_tasks": tasks,
        "tool_starts": starts,
        "terminal_tools": terminal,
        "final_answer": _answer(events),
        "map_feature_count": feature_count,
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

                evaluated = turn_records[int(case.get("evaluation_turn", len(turn_records) - 1))]
                reasons, evidence = _validate(case, evaluated["events"], int(evaluated["http_status"]))
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
