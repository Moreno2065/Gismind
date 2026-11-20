"""Regression tests for repository audit security, runtime, and GIS tool fixes."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import httpx
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Point, Polygon

from app.config import Settings
from app.main import _JsonLogFormatter, create_app
from app.runtime.artifacts import ArtifactStore
from app.runtime.code_mode.ast_guard import ASTBannedNodeError, inspect as inspect_code
from app.runtime.code_mode.executor import execute_source
from app.runtime.code_mode.runner import _build_sandbox_env
from app.runtime.compiler import CompileError, Compiler
from app.runtime.contracts import (
    ArtifactRef,
    PlannerDraft,
    PlannerStepDraft,
    ToolContractRegistry,
)
from app.runtime.db import RuntimeDB
from app.runtime.postconditions import validate_attribute_filter, validate_geo_geocode
from app.runtime.process import check_resource_budget
from app.runtime.queue import RuntimeSupervisor
from app.runtime.store import RuntimeStore
from app.runtime.tool_handlers import (
    handle_attribute_calculate,
    handle_attribute_filter,
    handle_code_execute,
    handle_io_csv_to_points,
    handle_vector_reproject,
)
from app.runtime.upload_inspection import vector_source
from app.tools.geo_code import GeoCoder
from app.tools.poi_query import POIQuery
from app.tools.raster_analysis import RasterAnalyzer


def test_sandbox_blocks_os_mod_escape_and_host_open_and_env_secrets(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-super-secret-value")
    monkeypatch.setenv("AMAP_KEY", "amap-super-secret-value")
    env = _build_sandbox_env("/tmp/sandbox")
    assert "LLM_API_KEY" not in env
    assert "AMAP_KEY" not in env

    res_escape = execute_source("__result__ = sitecustomize_gismind._os_mod.environ")
    assert res_escape["status"] == "error"

    res_open = execute_source("__result__ = open(__file__).read()")
    assert res_open["status"] == "error"


def test_sandbox_handles_large_input_payload_beyond_argv_limit():
    large_bytes = b"A" * 60000
    res = execute_source(
        "__result__ = {'length': len(inputs['big']), 'head': inputs['big'][:4].decode('ascii')}",
        inputs={
            "big": {
                "data": large_bytes,
                "artifact_id": "art_big",
                "content_hash": "b" * 64,
                "media_type": "application/octet-stream",
                "type": "File",
            }
        },
    )
    assert res["status"] == "success"
    assert res["data"] == {"length": 60000, "head": "AAAA"}


def test_code_execute_accepts_json_file_artifact_raw_bytes():
    raw = json.dumps({"type": "FeatureCollection", "features": []}).encode("utf-8")
    res = handle_code_execute(
        source_code="__result__ = len(inputs['src'])",
        declared_inputs=["src"],
        input=[{
            "artifact_id": "art_json",
            "content_hash": "c" * 64,
            "media_type": "application/geo+json",
            "type": "File",
            "data": {"type": "FeatureCollection", "features": []},
            "raw_bytes": raw,
        }],
    )
    assert res["status"] == "success"
    assert res["data"] == len(raw)


@pytest.mark.parametrize("source", [
    "open('secret.txt').read()",
    "getattr(1, 'real')",
    "globals()",
    "sitecustomize_gismind",
])
def test_ast_guard_and_compiler_reject_expanded_unsafe_names(source):
    with pytest.raises(ASTBannedNodeError):
        inspect_code(source)
    draft = PlannerDraft(
        user_goal="run code",
        tasks=[PlannerStepDraft(task_key="c1", op="code.execute", params={"source_code": source})],
    )
    with pytest.raises(CompileError):
        Compiler(ToolContractRegistry.with_builtins()).compile(draft)


def test_compiler_enforces_exclusive_minimum_on_raster_contour():
    digest = "a" * 64
    raster_meta = {
        "art_dem": {
            "artifact_id": "art_dem",
            "artifact_type": "Raster",
            "type": "Raster",
            "crs": {"kind": "EPSG", "code": 3857},
            "schema": {},
            "content_hash": digest,
            "storage_uri": f"artifacts/sha256/{digest[:2]}/{digest}",
            "size_bytes": 128,
            "validation_status": "valid",
        }
    }
    compiler = Compiler(ToolContractRegistry.with_builtins(), artifact_lookup=raster_meta.get)
    bad = PlannerDraft(
        user_goal="contours",
        tasks=[PlannerStepDraft(
            task_key="contour",
            op="raster.contour",
            inputs={"raster": ArtifactRef(artifact_id="art_dem", type="Raster", crs={"kind": "EPSG", "code": 3857})},
            params={"interval": 0},
        )],
    )
    with pytest.raises(CompileError) as exc_info:
        compiler.compile(bad)
    assert exc_info.value.code == "INVALID_PARAMETER"


def test_retryable_handler_error_schedules_retry_instead_of_stranding_attempt(tmp_path, monkeypatch):
    db = RuntimeDB(tmp_path / "runtime.db")
    runtime = RuntimeStore(db)
    runtime.initialize()
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.update_run_status("r1", "planning")
    registry = ToolContractRegistry.with_builtins()
    draft = PlannerDraft(
        user_goal="geocode",
        tasks=[PlannerStepDraft(task_key="g1", op="geo.geocode", params={"address": "天安门"})],
    )
    plan = Compiler(registry).compile(draft)
    runtime.create_plan("p1", "r1", plan_version=1, status="compiled", plan=plan.model_dump(mode="json"))
    runtime.create_task("t1", "p1", "g1", tool_name="geo.geocode", task=plan.tasks[0].model_dump(mode="json"))
    runtime.update_run_status("r1", "ready")

    from app.runtime import tool_handlers

    monkeypatch.setattr(
        tool_handlers,
        "handle_geo_geocode",
        lambda **kwargs: {"status": "error", "error_code": "TIMEOUT", "message": "upstream timed out"},
    )
    supervisor = RuntimeSupervisor(runtime, artifacts, registry=registry, isolate=False, autostart=False)
    outcome = supervisor.run_once("worker-1", run_id="r1")
    assert outcome["status"] == "retry_wait"
    task_status = runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='t1'").fetchone()[0])
    assert task_status == "retry_wait"
    attempts = runtime._read(lambda c: [dict(r) for r in c.execute("SELECT status, error_code FROM task_attempts WHERE task_id='t1'").fetchall()])
    assert attempts == [{"status": "retry_wait", "error_code": "TIMEOUT"}]


def test_get_run_snapshot_uses_read_transaction_without_write_lock(tmp_path):
    db = RuntimeDB(tmp_path / "runtime.db")
    runtime = RuntimeStore(db)
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")

    # Hold a write lock on another connection; read_transaction snapshot must still succeed.
    writer = db._connect()
    try:
        writer.execute("BEGIN IMMEDIATE")
        snapshot = runtime.get_run_snapshot("r1")
        assert snapshot is not None
        assert snapshot["run"]["run_id"] == "r1"
    finally:
        writer.rollback()
        writer.close()


def test_settings_default_host_loopback_and_reset_refuses_missing_manifest(tmp_path, monkeypatch):
    cfg = Settings(_env_file=None, APP_PROJECT_ROOT=str(tmp_path), APP_DATA_ROOT=str(tmp_path / "data"))
    assert cfg.APP_HOST == "127.0.0.1"
    from app.runtime import reset

    monkeypatch.setattr(reset, "get_settings", lambda: cfg)
    with pytest.raises(RuntimeError, match="runtime path manifest is missing"):
        reset._managed_paths()
    assert not (tmp_path / "data" / "runtime_paths.json").exists()


def test_vector_source_rejects_actual_extracted_size_over_limit(tmp_path):
    archive_path = tmp_path / "bomb.zip"
    payload = b"0" * 4096
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("layer.shp", payload)
        zf.writestr("layer.shx", b"1" * 100)
        zf.writestr("layer.dbf", b"2" * 100)
    # Bypass header check with a smaller extraction budget via monkeypatching validate_zip
    from app.runtime import upload_inspection

    original_validate = upload_inspection.validate_zip
    try:
        upload_inspection.validate_zip = lambda p, **kw: original_validate(p, max_total=10_000_000, max_count=10)
        with pytest.raises(ValueError, match="exceeds configured limit"):
            with vector_source(archive_path, "shapefile_zip", max_total=512, max_count=10):
                pass
    finally:
        upload_inspection.validate_zip = original_validate


def test_geo_code_timeout_falls_back_to_nominatim_and_postcondition_accepts_source(monkeypatch):
    coder = GeoCoder(amap_key="dummy", timeout=1)
    from app.tools import geo_code as gc_mod

    gc_mod._CACHE.clear()

    async def fake_get(self, url, **kwargs):
        if "amap.com" in url:
            raise httpx.TimeoutException("amap timeout")
        return httpx.Response(
            200,
            json=[{"lat": "39.9087", "lon": "116.3975", "display_name": "Tiananmen", "type": "attraction"}],
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    result = asyncio.run(coder.geocode("天安门"))
    assert result["status"] == "success"
    assert result["source"] == "OSM_Nominatim"
    envelope = {"status": "success", "data": result, "source": result["source"]}
    assert validate_geo_geocode(envelope, {}, {}) is True


def test_vector_reproject_wgs84_and_csv_to_points_gcj02():
    vec = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [116.4, 39.9]}, "properties": {"id": 1}}],
        "crs": {"type": "name", "properties": {"name": "EPSG:3857"}},
    }
    res_rep = handle_vector_reproject(vector=vec, target_crs={"kind": "WGS84"})
    assert res_rep["status"] == "success"

    table = {"rows": [{"lon": 116.397, "lat": 39.908, "name": "A"}]}
    res_gcj = handle_io_csv_to_points(input=table, x_col="lon", y_col="lat", crs={"kind": "GCJ02"})
    assert res_gcj["status"] == "success"
    assert res_gcj["data"]["_crs_label"] == "GCJ02"


def test_attribute_filter_contains_handles_invalid_regex_and_calculate_honors_field_type():
    fc = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[116.0, 39.0], [116.01, 39.0], [116.01, 39.01], [116.0, 39.01], [116.0, 39.0]]]}, "properties": {"label": "C++ [unclosed"}},
            {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[116.0, 39.0], [116.01, 39.0], [116.01, 39.01], [116.0, 39.01], [116.0, 39.0]]]}, "properties": {"label": "Python"}},
        ],
    }
    filtered = handle_attribute_filter(input=fc, field="label", operator="contains", value="C++ [unclosed")
    assert filtered["status"] == "success"
    assert len(filtered["data"]["features"]) == 1
    assert validate_attribute_filter(filtered, {"input": fc}, {"field": "label", "operator": "contains", "value": "C++ [unclosed"}) is True

    calc = handle_attribute_calculate(input=fc, field_name="area_int", expression="$area", field_type="int")
    assert calc["status"] == "success"
    val = calc["data"]["features"][0]["properties"]["area_int"]
    assert isinstance(val, int)


def test_raster_hillshade_zenith_and_vectorized_terrain_indices_and_contour_cap(tmp_path):
    dem_path = tmp_path / "flat.tif"
    arr = np.full((10, 10), 100.0, dtype=np.float32)
    profile = {
        "driver": "GTiff",
        "height": 10,
        "width": 10,
        "count": 1,
        "dtype": "float32",
        "crs": "EPSG:3857",
        "transform": from_origin(0, 100, 10, 10),
    }
    with rasterio.open(dem_path, "w", **profile) as dst:
        dst.write(arr, 1)

    analyzer = RasterAnalyzer()
    hs = analyzer.hillshade(str(dem_path), azimuth=315, altitude=90)
    assert hs["status"] == "success"
    with rasterio.open(hs["data"]["dst_path"]) as src:
        vals = src.read(1)
    assert int(np.mean(vals)) == 255
    os.unlink(hs["data"]["dst_path"])

    tri = analyzer.terrain_ruggedness_index(str(dem_path))
    assert tri["status"] == "success"
    with rasterio.open(tri["data"]["dst_path"]) as src:
        assert float(np.nanmax(src.read(1))) == 0.0
    os.unlink(tri["data"]["dst_path"])

    # Contour should reject absurdly tiny interval that would OOM
    ramp_path = tmp_path / "ramp.tif"
    ramp = np.linspace(0, 10000, 100, dtype=np.float32).reshape(10, 10)
    with rasterio.open(ramp_path, "w", **profile) as dst:
        dst.write(ramp, 1)
    too_many = analyzer.contour(str(ramp_path), interval=0.01)
    assert too_many["status"] == "error"


def test_json_log_formatter_and_request_id_sanitization_and_process_available():
    formatter = _JsonLogFormatter()
    rec = logging.LogRecord("test", logging.INFO, __file__, 1, 'hello "world"\nline2', (), None)
    parsed = json.loads(formatter.format(rec))
    assert parsed["message"] == 'hello "world"\nline2'

    budget = check_resource_budget(
        SimpleNamespace(is_alive=lambda: True),
        memory_mb=128,
        cpu_weight=1,
        sampler=lambda proc, **kw: {"memory_mb": 10.0, "cpu_percent": 1.0},
    )
    assert budget["available"] is True

    escaped = POIQuery._sanitize_overpass_tag('cafe "A+B"\n[test]')
    assert "\n" not in escaped
    assert '\\"' in escaped
    assert "\\+" in escaped
