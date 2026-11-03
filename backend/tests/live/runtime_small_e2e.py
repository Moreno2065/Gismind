"""Four opt-in real-model Runtime cases; fresh evidence, shared durable budget."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Point
from fastapi.testclient import TestClient

from app.main import create_app
from app.runtime import ArtifactStore, RuntimeDB, RuntimeStore, RuntimeSupervisor
from tests.browser.runtime_chinese_browser_server import DurableRecordingPlanner
from tests.browser.runtime_browser_budget import _validated_policy, budget_summary


BACKEND = Path(__file__).resolve().parents[2]
BUDGET_DB = BACKEND / ".gismind/zh-browser-2000-20261014-budget.sqlite"
CASES = {
    "vector_crs_resume": "读取附件中的点，保留属性和坐标数值，导出为GeoPackage文件。",
    "raster_crs_resume": "读取附件中的栅格，保留原像元数值和网格，提供GeoTIFF结果。",
    "filter_buffer": "筛选附件中 population 大于100的点，然后对筛选结果做100米缓冲，保留属性，导出为GeoPackage文件。",
    "raster_slope": "计算附件DEM的坡度，单位为度，输出GeoTIFF。",
}


class LimitedRealPlanner(DurableRecordingPlanner):
    def _call(self, messages):
        if self.local_counter["used"] >= self.local_counter["limit"]:
            raise RuntimeError("LOCAL_PLANNER_GENERATION_CAP")
        self.local_counter["used"] += 1
        return super()._call(messages)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def source_manifest():
    paths = [Path(__file__), BACKEND / "tests/browser/runtime_chinese_browser_server.py",
             BACKEND / "tests/browser/runtime_browser_budget.py", BACKEND / "tests/live/real_export_format_smoke.py"]
    paths += [path for path in (BACKEND / "app").rglob("*") if path.is_file() and path.suffix in {".py", ".json"}]
    return {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(paths)}


def configuration(output):
    # Reopen only the already-authorized ledger; never initialize, upgrade, or
    # replace it. Exact existing rates go back to DurableCostBudget unchanged.
    with closing(sqlite3.connect(BUDGET_DB.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        policy = _validated_policy(conn)
    if policy["max_nanos"] != 20_000_000_000 or policy["max_planner_calls"] != 8767:
        raise ValueError("shared ledger must retain the authorized USD 20 / 8767 policy")
    return {"output": str(output), "budget_db": str(BUDGET_DB), "max_cost_usd": 20,
            "max_planner_calls": 8767, "input_usd_per_million": policy["input_rate"],
            "output_usd_per_million": policy["output_rate"],
            "cache_hit_usd_per_million": policy["cache_rate"], "source_manifest": source_manifest()}


def fixture(case_id, root):
    missing = case_id.endswith("crs_resume")
    if case_id.startswith("vector") or case_id == "filter_buffer":
        points = [(1, 50, 1000, 2000)] if missing else [(1, 50, 500000, 2000), (2, 150, 501000, 2500), (3, 250, 503000, 4000)]
        payload = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"record_id": key, "population": population},
             "geometry": {"type": "Point", "coordinates": [x, y]}} for key, population, x, y in points]}
        if not missing:
            payload["crs"] = {"type": "name", "properties": {"name": "EPSG:32631"}}
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        path = root / "source.geojson"
        path.write_bytes(content)
        return path, content
    path = root / "source.tif"
    with rasterio.open(path, "w", driver="GTiff", width=3, height=3, count=1,
                       dtype="float32", transform=from_origin(1000, 2000, 10, 10),
                       crs=None if missing else "EPSG:3857", nodata=-9999) as dataset:
        dataset.write(np.array([[0, 10, 20]] * 3, dtype="float32"), 1)
    return path, path.read_bytes()


def advance(client, supervisor, run_id, deadline):
    while time.monotonic() < deadline:
        snapshot = client.get(f"/api/runs/{run_id}").json()
        if snapshot["status"] in {"completed", "failed", "cancelled", "awaiting_input"}:
            return snapshot
        supervisor.run_once("small-live-worker", run_id=run_id)
        time.sleep(0.01)
    client.post(f"/api/runs/{run_id}/cancel", json={"command_id": uuid.uuid4().hex})
    raise TimeoutError("case exceeded its wall-time limit")


def physical_oracle(case_id, path):
    if case_id in {"vector_crs_resume", "filter_buffer"}:
        actual = gpd.read_file(path).sort_values("record_id")
        epsg = 3857 if case_id == "vector_crs_resume" else 32631
        assert actual.crs.to_epsg() == epsg, "output CRS differs"
        expected = [(1, 50, 1000, 2000)] if case_id == "vector_crs_resume" else [(2, 150, 501000, 2500), (3, 250, 503000, 4000)]
        assert list(actual.record_id) == [row[0] for row in expected], "selected records differ"
        assert list(actual.population) == [row[1] for row in expected], "attributes differ"
        errors = []
        for geometry, (_, _, x, y) in zip(actual.geometry, expected):
            wanted = Point(x, y) if case_id == "vector_crs_resume" else Point(x, y).buffer(100)
            errors.append(float(geometry.hausdorff_distance(wanted)))
        assert max(errors) <= 1e-6, "physical coordinates/buffer differ from independent Shapely oracle"
        return {"reader": "GeoPandas/GDAL + independent Shapely geometry", "epsg": epsg,
                "record_ids": list(map(int, actual.record_id)), "max_hausdorff_m": max(errors)}
    with rasterio.open(path) as actual:
        assert actual.crs.to_epsg() == 3857, "output raster CRS differs"
        assert (actual.width, actual.height, actual.count) == (3, 3, 1), "output grid shape differs"
        assert actual.transform == from_origin(1000, 2000, 10, 10), "output grid moved"
        expected = np.array([[0, 10, 20]] * 3) if case_id == "raster_crs_resume" else np.full((3, 3), 45.0)
        np.testing.assert_allclose(actual.read(1), expected, rtol=0, atol=1e-5)
        return {"reader": "Rasterio", "epsg": 3857, "shape": [3, 3],
                "oracle": "unchanged pixels" if case_id.endswith("crs_resume") else "analytic plane: rise 10 m / run 10 m gives 45 degrees"}


def run_case(case_id, root, base_configuration, counter, timeout):
    root.mkdir()
    planner = LimitedRealPlanner.from_settings()
    planner.local_counter = counter
    planner.configure_evidence({**base_configuration, "output": str(root)})
    runtime = RuntimeStore(RuntimeDB(root / "runtime.db"))
    runtime.initialize()
    artifacts = ArtifactStore(root / "cas", runtime)
    supervisor = RuntimeSupervisor(runtime, artifacts, planner=planner, isolate=True,
                                   autostart=False, timeout_seconds=timeout)
    report = {"case_id": case_id, "prompt": CASES[case_id], "ok": False, "resumes": []}
    try:
        source_path, source_bytes = fixture(case_id, root)
        with TestClient(create_app(runtime_store=runtime, artifact_store=artifacts,
                                   runtime_supervisor=supervisor)) as client:
            session = client.post("/api/sessions")
            assert session.status_code == 201, session.text
            uploaded = client.post("/api/artifacts/uploads", files={"file": (source_path.name, source_bytes)})
            assert uploaded.status_code == 201, uploaded.text
            source = uploaded.json()
            original = runtime.get_artifact(source["artifact_id"])
            submitted = client.post("/api/runs", json={"session_id": session.json()["id"],
                "message": CASES[case_id], "input_artifact_ids": [source["artifact_id"]],
                "client_request_id": uuid.uuid4().hex})
            assert submitted.status_code == 202, submitted.text
            run_id = submitted.json()["run_id"]
            report["run_id"] = run_id
            deadline = time.monotonic() + timeout
            snapshot = advance(client, supervisor, run_id, deadline)
            report["first_status"] = snapshot["status"]
            if case_id.endswith("crs_resume"):
                assert snapshot["status"] == "awaiting_input" and not snapshot.get("active_plan"), "missing CRS did not pause safely"
                used_shapes = set()
                for _ in range(2):
                    schema = snapshot.get("patch_schema") or {}
                    properties = schema.get("properties", {})
                    diagnostic = snapshot.get("diagnostic") or {}
                    if "file" in properties and diagnostic.get("code") == "CRS_REQUIRED":
                        shape = "file"
                        patch = {"file": {"declared_crs": {"value": {"kind": "EPSG", "code": 3857}, "source": "user"}}}
                    elif "clarification" in properties and any(word in str(diagnostic.get("message", "")).lower()
                                                               for word in ("坐标", "crs", "epsg", "投影")):
                        shape = "clarification"
                        patch = {"clarification": "这个附件的原始坐标系是 EPSG:3857；保留原始坐标数值，继续完成原要求。"}
                    else:
                        raise AssertionError("public clarification is not an answerable source-CRS question")
                    assert shape not in used_shapes, "repeated clarification after the supplied answer"
                    used_shapes.add(shape)
                    response = client.post(f"/api/runs/{run_id}/resume", json={"command_id": uuid.uuid4().hex,
                        "expected_run_version": snapshot["run_version"], "patch": patch})
                    assert response.status_code == 202, response.text
                    report["resumes"].append({"diagnostic": diagnostic, "patch_schema": schema, "patch": patch})
                    snapshot = advance(client, supervisor, run_id, deadline)
                    if snapshot["status"] != "awaiting_input":
                        break
            report["terminal_status"] = snapshot["status"]
            assert snapshot["status"] == "completed", str(snapshot.get("diagnostic") or snapshot["status"])
            outputs = runtime._read(lambda conn: [dict(row) for row in conn.execute(
                "SELECT t.task_id,t.task_key,t.task_json,o.artifact_id FROM tasks t JOIN task_outputs o ON o.task_id=t.task_id "
                "JOIN plans p ON p.plan_id=t.plan_id WHERE p.run_id=?", (run_id,))])
            plan = runtime._read(lambda conn: json.loads(conn.execute("SELECT ir_json FROM plans WHERE run_id=? ORDER BY plan_version DESC LIMIT 1", (run_id,)).fetchone()[0]))
            consumed = {ref["task_key"] for task in plan["tasks"] for value in task["inputs"].values()
                        for ref in (value if isinstance(value, list) else [value]) if ref.get("kind") == "task_output"}
            sinks = {task["task_key"] for task in plan["tasks"]} - consumed
            terminal = [row for row in outputs if row["task_key"] in sinks]
            assert len(terminal) == 1, "ambiguous final artifact"
            artifact = runtime.get_artifact(terminal[0]["artifact_id"])
            vector_case = case_id in {"vector_crs_resume", "filter_buffer"}
            assert artifact["type"] in ({"File"} if vector_case else {"File", "Raster"})
            downloaded = client.get(f"/api/artifacts/{artifact['artifact_id']}/content")
            assert downloaded.status_code == 200
            assert hashlib.sha256(downloaded.content).hexdigest() == artifact["content_hash"]
            destination = root / ("result.gpkg" if vector_case else "result.tif")
            destination.write_bytes(downloaded.content)
            report["physical_oracle"] = physical_oracle(case_id, destination)
            workers = runtime._read(lambda conn: [dict(row) for row in conn.execute("SELECT status,executor_identity FROM task_attempts")])
            assert workers and all(row["status"] == "succeeded" and json.loads(row["executor_identity"])["pid"] != os.getpid() for row in workers), "isolated Worker evidence absent"
            responses = planner.budget.responses(planner.cohort)
            hashes = {row["response_hash"] for row in responses}
            attempts = runtime._read(lambda conn: [dict(row) for row in conn.execute("SELECT input_revision,phase,status,response_hash,error_code FROM planner_attempts")])
            assert hashes and all(row["response_hash"] in hashes for row in attempts if row["status"] == "succeeded"), "real provider provenance missing"
            assert runtime.get_artifact(source["artifact_id"]) == original, "original upload metadata changed"
            assert client.get(source["content_url"]).content == source_bytes, "original upload bytes changed"
            if case_id.endswith("crs_resume"):
                declared = [ref["artifact_id"] for task in plan["tasks"] for value in task["inputs"].values()
                            for ref in (value if isinstance(value, list) else [value]) if ref.get("kind") == "artifact"]
                assert source["artifact_id"] not in declared, "Worker still used undeclared upload"
                assert any(any(edge["parent_artifact_id"] == source["artifact_id"] for edge in runtime.list_lineage(key)) for key in declared), "new declaration lineage absent"
            report.update(ok=True, artifact_id=artifact["artifact_id"], result_path=str(destination),
                          content_hash=artifact["content_hash"], planner_attempts=attempts)
    except Exception as exc:
        detail = str(exc).replace(planner.api_key, "[REDACTED]") if planner.api_key else str(exc)
        report.update(error_type=type(exc).__name__, error=detail[:2000])
    finally:
        planner._write_evidence()
        report["local_generations_admitted"] = counter["used"]
        write_json(root / "result.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-generations", type=int, default=12)
    parser.add_argument("--case-ids", nargs="+", choices=tuple(CASES))
    parser.add_argument("--timeout-seconds", type=float, default=180)
    args = parser.parse_args()
    scheduled = args.case_ids or list(CASES)
    if len(set(scheduled)) != len(scheduled):
        parser.error("duplicate case IDs are not allowed")
    if args.inventory:
        print(json.dumps(CASES, ensure_ascii=False, indent=2))
        return
    if os.environ.get("GISMIND_LIVE_LLM") != "1":
        parser.error("explicit GISMIND_LIVE_LLM=1 is required")
    if not 1 <= args.max_generations <= 12 or not math.isfinite(args.timeout_seconds) or not 1 <= args.timeout_seconds <= 300:
        parser.error("require 1..12 generations and 1..300 seconds per case")
    default_name = "small-live-e2e-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    root = (args.output or BACKEND / ".gismind" / default_name).resolve()
    if not root.is_relative_to((BACKEND / ".gismind").resolve()) or root.exists():
        parser.error("output must be a new directory within backend/.gismind")
    root.mkdir(parents=True)
    config = configuration(root)
    write_json(root / "configuration.json", {**config, "local_max_generations": args.max_generations})
    summary = {"suite": "small-real-runtime-e2e-v1", "output": str(root), "scheduled": scheduled,
               "budget_before": budget_summary(BUDGET_DB), "results": []}
    counter = {"used": 0, "limit": args.max_generations}
    for case_id in scheduled:
        ledger = budget_summary(BUDGET_DB)
        if counter["used"] >= counter["limit"] or ledger["remaining_usd"] <= 0 or ledger["planner_calls_reserved"] >= ledger["max_planner_calls"]:
            summary["stopped"] = "local generation cap or shared external budget exhausted"
            break
        result = run_case(case_id, root / case_id, config, counter, args.timeout_seconds)
        summary["results"].append(result)
        summary.update(budget_after=budget_summary(BUDGET_DB), local_generations_admitted=counter["used"])
        write_json(root / "summary.json", summary)
        print(json.dumps({"case_id": case_id, "ok": result["ok"], "terminal_status": result.get("terminal_status"), "error": result.get("error")}, ensure_ascii=False), flush=True)
        if result.get("error") and any(term in result["error"] for term in ("global cost cap", "global planner call cap", "LOCAL_PLANNER_GENERATION_CAP", "source drift")):
            summary["stopped"] = result["error"]
            break
    summary["ok"] = len(summary["results"]) == len(scheduled) and all(result["ok"] for result in summary["results"])
    write_json(root / "summary.json", summary)
    print(json.dumps({"ok": summary["ok"], "output": str(root), "local_generations_admitted": counter["used"]}, ensure_ascii=False), flush=True)
    raise SystemExit(0 if summary["ok"] else 1)


if __name__ == "__main__":
    main()
