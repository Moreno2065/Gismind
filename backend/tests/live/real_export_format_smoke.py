"""Opt-in REAL DeepSeek Flash export smoke through public upload/run APIs.

Run from ``backend`` with ``python -m tests.live.real_export_format_smoke --help``.
Each invocation needs an explicit USD cap and current provider token prices.
It keeps the SQLite DB, CAS, downloaded bytes, and a report for each format.
This module never substitutes a planner response or counts offline tests as live.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlparse
import uuid

import geopandas as gpd
from fastapi.testclient import TestClient

from app.main import create_app
from app.runtime import ArtifactStore, RuntimeDB, RuntimeStore, RuntimeSupervisor
from app.runtime.llm_planner import OpenAICompatiblePlanner, _digest


FORMATS = {
    "geojson": (".geojson", "application/geo+json"),
    "gpkg": (".gpkg", "application/geopackage+sqlite3"),
}
CASES = ("geojson", "gpkg", "csv")
SOURCE = {
    "type": "FeatureCollection",
    "features": [{
        "type": "Feature",
        "properties": {"name": "南京医院", "patients": 7},
        "geometry": {"type": "Point", "coordinates": [118.78, 32.04]},
    }],
}


def require_real_provider(*, api_key: str, base_url: str, model: str) -> None:
    """Fail closed before constructing evidence or making any network call."""
    if not api_key.strip():
        raise ValueError("LLM_API_KEY is required for REAL DeepSeek Flash smoke")
    endpoint = urlparse(base_url)
    if endpoint.scheme != "https" or endpoint.hostname != "api.deepseek.com" or endpoint.username or endpoint.password:
        raise ValueError("LLM_BASE_URL must use https://api.deepseek.com (no local/mock gateway)")
    if model != "deepseek-flash":
        raise ValueError("LLM_MODEL must be deepseek-flash")


class CostBudget:
    """Reserve a conservative token-price upper bound before each planner call.

    The provider's ``max_tokens`` caps generated tokens. Input UTF-8 bytes plus
    8192 overhead conservatively bound this small prompt's tokenized input.
    With transport retries disabled, a planner call can issue at most two HTTP
    requests (the built-in compatibility fallback), so both are reserved.
    Price rates are supplied by the caller because provider prices can change.
    """

    def __init__(self, *, max_usd: float, input_usd_per_million: float,
                 output_usd_per_million: float, max_planner_calls: int = 6):
        if not all(math.isfinite(value) and value > 0 for value in
                   (max_usd, input_usd_per_million, output_usd_per_million)):
            raise ValueError("cost cap and both token prices must be positive")
        if max_planner_calls < 1:
            raise ValueError("max_planner_calls must be positive")
        self.max_usd = float(max_usd)
        self.input_rate = float(input_usd_per_million)
        self.output_rate = float(output_usd_per_million)
        self.max_planner_calls = max_planner_calls
        self.reserved_usd = 0.0
        self.planner_calls = 0

    def reserve(self, messages: list[dict[str, str]], *, max_tokens: int, max_http_calls: int) -> None:
        if max_tokens < 1 or max_http_calls < 1:
            raise ValueError("max_tokens and max_http_calls must be positive")
        if self.planner_calls >= self.max_planner_calls:
            raise RuntimeError("planner call cap reached before provider request")
        raw_bytes = len(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        input_tokens_bound = raw_bytes + 8192
        reserve_usd = max_http_calls * (
            input_tokens_bound * self.input_rate + max_tokens * self.output_rate
        ) / 1_000_000
        if self.reserved_usd + reserve_usd > self.max_usd:
            raise RuntimeError(
                f"cost cap reached before provider request: reserving ${reserve_usd:.6f} "
                f"would exceed ${self.max_usd:.6f}"
            )
        self.reserved_usd += reserve_usd
        self.planner_calls += 1


class MeteredRealPlanner(OpenAICompatiblePlanner):
    budget: CostBudget
    observed_responses: list[dict[str, Any]]

    def _call(self, messages: list[dict[str, str]]):
        self.budget.reserve(messages, max_tokens=self.max_tokens, max_http_calls=2)
        raw, latency_ms = super()._call(messages)
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        self.observed_responses.append({
            "response_hash": _digest(raw), "usage": usage,
            "http_calls": usage.get("planner_http_calls"), "latency_ms": latency_ms,
        })
        return raw, latency_ms


def assert_export_bytes(artifact: dict[str, Any], content: bytes, physical_path: Path,
                        *, requested_format: str) -> None:
    """Verify actual spatial file bytes independently with a GIS reader."""
    extension, media_type = FORMATS[requested_format]
    assert artifact.get("type") == "File", "export output is not a File Artifact"
    assert artifact.get("validation_status") == "valid", "export Artifact is not valid"
    assert artifact.get("media_type") == media_type, "top-level export media_type mismatch"
    file_meta = artifact.get("schema", {}).get("file", {})
    assert file_meta.get("format") == requested_format, "export format metadata mismatch"
    assert file_meta.get("extension") == extension, "export extension metadata mismatch"
    assert str(file_meta.get("filename", "")).lower().endswith(extension), "export filename extension mismatch"
    assert file_meta.get("media_type") == media_type, "export file media_type mismatch"
    assert file_meta.get("download_disposition") == "attachment", "export must be a download"
    assert hashlib.sha256(content).hexdigest() == artifact.get("content_hash"), "export content hash mismatch"
    assert physical_path.read_bytes() == content, "downloaded bytes differ from retained file"
    if requested_format == "gpkg":
        assert content.startswith(b"SQLite format 3\x00"), "output is not a GeoPackage SQLite file"
    else:
        decoded = json.loads(content)
        assert decoded.get("type") == "FeatureCollection", "output is not GeoJSON"
    frame = gpd.read_file(physical_path)
    assert len(frame) == 1, "export feature count changed"
    assert frame.crs is not None and frame.crs.to_epsg() == 4326, "export CRS changed"
    row = frame.iloc[0]
    assert row["name"] == "南京医院" and int(row["patients"]) == 7, "export properties changed"
    point = row.geometry
    assert point.geom_type == "Point" and abs(point.x - 118.78) < 1e-6 and abs(point.y - 32.04) < 1e-6, "export coordinates changed"


def assert_rejected_csv_outcome(snapshot: dict[str, Any]) -> None:
    assert snapshot.get("status") in {"awaiting_input", "failed"}, "unsupported CSV was not rejected"
    assert not any(item.get("type") == "File" for item in snapshot.get("output_artifacts", [])), "unsupported CSV produced a File Artifact"


def assert_csv_planning_evidence(run_status: str, attempts: list[dict[str, Any]],
                                 observed_hashes: set[str]) -> None:
    assert any(row.get("response_hash") in observed_hashes for row in attempts), "CSV rejection lacks a matching real provider response"
    if run_status == "awaiting_input":
        assert any(row.get("error_code") == "CLARIFICATION_REQUIRED" for row in attempts), "CSV did not produce a concrete clarification"
        return
    assert run_status == "failed", "CSV run did not fail closed"
    def is_format_rejection(row: dict[str, Any]) -> bool:
        if row.get("error_code") != "INVALID_PARAMETER":
            return False
        try:
            diagnostic = json.loads(row.get("diagnostics_json") or "{}")
        except (TypeError, ValueError):
            return False
        return (str(diagnostic.get("path", "")).endswith(".params.format")
                and "csv" in diagnostic.get("details", {}).get("requested_formats", []))
    assert any(is_format_rejection(row) for row in attempts), "CSV failure lacks the unsupported format compile diagnostic"


def _source_bytes() -> bytes:
    return json.dumps(SOURCE, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _rows(runtime: RuntimeStore, query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return runtime._read(lambda connection: [dict(row) for row in connection.execute(query, params).fetchall()])


def _assert_live_provenance(runtime: RuntimeStore, run_id: str, planner: MeteredRealPlanner,
                            requested_format: str) -> None:
    attempts = _rows(runtime, "SELECT * FROM planner_attempts WHERE run_id=? ORDER BY created_at", (run_id,))
    responses = planner.observed_responses
    assert attempts and responses, "no durable planner attempt with observed provider response"
    assert all(row["provider"] == "openai-compatible" and row["model"] == "deepseek-flash" for row in attempts), "planner did not use configured real DeepSeek Flash"
    observed_hashes = {row["response_hash"] for row in responses if row.get("http_calls", 0) >= 1}
    assert observed_hashes, "no actual provider HTTP response observed"
    assert all(row.get("http_calls", 0) in (1, 2) for row in responses), "provider HTTP call count is outside budgeted bound"
    if requested_format == "csv":
        run = runtime.get_run(run_id)
        assert_csv_planning_evidence(run["status"], attempts, observed_hashes)
    else:
        assert any(row["status"] == "succeeded" and row["response_hash"] in observed_hashes for row in attempts), "durable planner response does not match provider response"
    tasks = _rows(runtime, "SELECT t.task_id,t.tool_name,t.status,a.executor_identity,a.status AS attempt_status FROM tasks t JOIN plans p ON p.plan_id=t.plan_id JOIN task_attempts a ON a.task_id=t.task_id WHERE p.run_id=? ORDER BY t.task_key,a.started_at", (run_id,))
    if requested_format == "csv":
        assert not any(row["tool_name"] == "file.export" and row["status"] == "succeeded" for row in tasks), "unsupported CSV executed file.export"
        return
    assert any(row["tool_name"] == "io.load_vector" and row["status"] == "succeeded" for row in tasks), "uploaded File was not loaded by worker"
    exports = [row for row in tasks if row["tool_name"] == "file.export"]
    assert exports and any(row["status"] == "succeeded" and row["attempt_status"] == "succeeded" for row in exports), "file.export did not execute successfully"
    for row in tasks:
        identity = json.loads(row["executor_identity"])
        assert isinstance(identity.get("pid"), int) and identity["pid"] != os.getpid(), "GIS task did not run in an isolated child process"
    plans = _rows(runtime, "SELECT ir_json FROM plans WHERE run_id=? ORDER BY created_at", (run_id,))
    assert plans, "compiled PlanIR is absent"
    plan = json.loads(plans[-1]["ir_json"])
    export_tasks = [task for task in plan["tasks"] if task["op"] == "file.export"]
    assert len(export_tasks) == 1 and export_tasks[0]["params"].get("format") == requested_format, "compiled export format differs from request"


def run_case(format_name: str, root: Path, budget: CostBudget, *, timeout_seconds: float) -> dict[str, Any]:
    case_root = root / format_name
    case_root.mkdir()
    runtime = RuntimeStore(RuntimeDB(case_root / "runtime.db"))
    runtime.initialize()
    artifacts = ArtifactStore(case_root / "cas", runtime)
    planner = MeteredRealPlanner.from_settings()
    require_real_provider(api_key=planner.api_key, base_url=planner.base_url, model=planner.model)
    planner.max_retries = 0
    planner.max_tokens = min(planner.max_tokens, 1536)
    planner.budget = budget
    planner.observed_responses = []
    supervisor = RuntimeSupervisor(runtime, artifacts, planner=planner, isolate=True, autostart=False,
                                   timeout_seconds=timeout_seconds)
    report: dict[str, Any] = {
        "format": format_name, "ok": False, "provider": planner.provider, "model": planner.model,
        "endpoint_host": urlparse(planner.base_url).hostname,
        "database": str(case_root / "runtime.db"), "cas": str(case_root / "cas"),
    }
    try:
        with TestClient(create_app(runtime_store=runtime, artifact_store=artifacts,
                                   runtime_supervisor=supervisor)) as client:
            session_response = client.post("/api/sessions")
            assert session_response.status_code == 201, session_response.text
            session_id = session_response.json()["id"]
            source = _source_bytes()
            uploaded_response = client.post(
                "/api/artifacts/uploads",
                files={"file": ("nanjing_hospital.geojson", source, "application/geo+json")},
            )
            assert uploaded_response.status_code == 201, uploaded_response.text
            uploaded = uploaded_response.json()
            assert uploaded["content_hash"] == hashlib.sha256(source).hexdigest()
            report["upload_artifact_id"] = uploaded["artifact_id"]
            report["upload_content_hash"] = uploaded["content_hash"]
            label = {"geojson": "GeoJSON", "gpkg": "GeoPackage (GPKG)", "csv": "CSV"}[format_name]
            prompt = f"请把上传的南京医院点文件明确导出为{label}格式供下载，保留全部属性和坐标。"
            run_response = client.post("/api/runs", json={
                "session_id": session_id, "message": prompt,
                "input_artifact_ids": [uploaded["artifact_id"]],
                "client_request_id": f"export-format-{format_name}-{uuid.uuid4().hex}",
            })
            assert run_response.status_code == 202, run_response.text
            run_id = run_response.json()["run_id"]
            report["run_id"] = run_id
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                snapshot_response = client.get(f"/api/runs/{run_id}")
                assert snapshot_response.status_code == 200, snapshot_response.text
                snapshot = snapshot_response.json()
                if snapshot["status"] in {"completed", "failed", "cancelled", "awaiting_input"}:
                    break
                supervisor.queue.recover_expired_leases(run_id=run_id)
                try:
                    supervisor.run_once(f"real-export-{format_name}", run_id=run_id)
                except Exception:
                    snapshot = client.get(f"/api/runs/{run_id}").json()
                    if snapshot["status"] not in {"failed", "awaiting_input"}:
                        raise
                time.sleep(0.01)
            else:
                client.post(f"/api/runs/{run_id}/cancel", json={"command_id": f"timeout-{uuid.uuid4().hex}"})
                raise TimeoutError(f"{format_name} export exceeded {timeout_seconds}s")
            report["run_status"] = snapshot["status"]
            if format_name == "csv":
                assert_rejected_csv_outcome(snapshot)
                _assert_live_provenance(runtime, run_id, planner, format_name)
                report.update(ok=True, expected_rejection=True, diagnostic=snapshot.get("diagnostic"))
                return report
            assert snapshot["status"] == "completed", f"export run ended as {snapshot['status']}: {snapshot.get('diagnostic')}"
            _assert_live_provenance(runtime, run_id, planner, format_name)
            files = [item for item in snapshot["output_artifacts"] if item.get("type") == "File"]
            assert len(files) == 1, f"expected one exported File Artifact, got {len(files)}"
            output = files[0]
            public_meta = client.get(f"/api/artifacts/{output['artifact_id']}")
            assert public_meta.status_code == 200 and public_meta.json()["content_hash"] == output["content_hash"]
            download = client.get(output["content_url"])
            assert download.status_code == 200, download.text[:300]
            assert download.headers.get("content-disposition", "").startswith("attachment"), "download is not an attachment"
            assert download.headers.get("x-content-type-options") == "nosniff", "download lacks nosniff"
            assert download.headers.get("content-type", "").startswith("application/octet-stream"), "download MIME is unsafe"
            assert artifacts.path_for(output["artifact_id"]).read_bytes() == download.content, "CAS and public download differ"
            extension, _ = FORMATS[format_name]
            physical = case_root / f"download{extension}"
            physical.write_bytes(download.content)
            assert_export_bytes(output, download.content, physical, requested_format=format_name)
            report.update(ok=True, output_artifact_id=output["artifact_id"],
                          content_hash=output["content_hash"], byte_size=len(download.content),
                          downloaded_file=str(physical), file_schema=output.get("schema", {}).get("file"),
                          media_type=output.get("media_type"))
    except Exception as exc:
        report.update(error_type=type(exc).__name__, error=str(exc)[:1000])
    finally:
        report["provider_responses"] = planner.observed_responses
        report["reserved_usd"] = budget.reserved_usd
        if report.get("run_id"):
            run_id = report["run_id"]
            report["planner_attempts"] = [
                {key: row.get(key) for key in ("attempt_id", "phase", "status", "provider", "model", "response_hash", "error_code")}
                for row in _rows(runtime, "SELECT * FROM planner_attempts WHERE run_id=? ORDER BY created_at", (run_id,))
            ]
            report["task_attempts"] = _rows(runtime,
                "SELECT a.attempt_id,t.tool_name,a.status,a.executor_identity,a.error_code FROM task_attempts a JOIN tasks t ON t.task_id=a.task_id JOIN plans p ON p.plan_id=t.plan_id WHERE p.run_id=? ORDER BY a.started_at",
                (run_id,))
            report["run_events"] = _rows(runtime,
                "SELECT event_id,event_type FROM events WHERE run_id=? ORDER BY event_id", (run_id,))
        (case_root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="required opt-in to contact the real provider")
    parser.add_argument("--out", type=Path, required=True, help="new directory for retained DB/CAS/report evidence")
    parser.add_argument("--format", choices=CASES, action="append", dest="formats",
                        help="case to check; repeat for several (default: GeoJSON, GPKG, rejected CSV)")
    parser.add_argument("--max-cost-usd", type=float, required=True, help="hard reservation cap in USD")
    parser.add_argument("--input-usd-per-million", type=float, required=True, help="current DeepSeek input token price")
    parser.add_argument("--output-usd-per-million", type=float, required=True, help="current DeepSeek output token price")
    parser.add_argument("--timeout-seconds", type=float, default=180)
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("--live is required; offline tests do not count as REAL evidence")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    formats = tuple(dict.fromkeys(args.formats or CASES))
    budget = CostBudget(max_usd=args.max_cost_usd,
                        input_usd_per_million=args.input_usd_per_million,
                        output_usd_per_million=args.output_usd_per_million)
    probe = OpenAICompatiblePlanner.from_settings()
    require_real_provider(api_key=probe.api_key, base_url=probe.base_url, model=probe.model)
    if args.out.exists():
        parser.error("--out must be a new directory; retained evidence is never overwritten")
    args.out.mkdir(parents=True)
    reports = []
    for format_name in formats:
        report = run_case(format_name, args.out, budget, timeout_seconds=args.timeout_seconds)
        reports.append(report)
        print(json.dumps({"format": format_name, "ok": report["ok"], "run_id": report.get("run_id"),
                          "error": report.get("error"), "report": str(args.out / format_name / "report.json")},
                         ensure_ascii=False))
        if not report["ok"]:
            break
    summary = {"suite": "real_export_format_smoke", "configured_endpoint_host": "api.deepseek.com",
               "formats_requested": formats, "formats_passed": [r["format"] for r in reports if r["ok"]],
               "max_cost_usd": args.max_cost_usd, "reserved_usd": budget.reserved_usd,
               "provider_http_calls_observed": sum(
                   int(response["http_calls"] or 0)
                   for report in reports for response in report["provider_responses"]
               ),
               "ok": len(reports) == len(formats) and all(r["ok"] for r in reports)}
    summary["evidence_valid"] = bool(summary["ok"] and summary["provider_http_calls_observed"] >= len(formats))
    (args.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
