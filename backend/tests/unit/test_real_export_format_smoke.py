"""Offline checks for the opt-in real export smoke harness."""

from __future__ import annotations

import hashlib
import json

import geopandas as gpd
import pytest
from shapely.geometry import Point

from tests.live.real_export_format_smoke import (
    CostBudget, assert_csv_planning_evidence, assert_export_bytes,
    assert_rejected_csv_outcome, require_real_provider,
)


def test_real_provider_gate_rejects_missing_key_and_non_deepseek_endpoint():
    with pytest.raises(ValueError, match="LLM_API_KEY"):
        require_real_provider(api_key="", base_url="https://api.deepseek.com/v1", model="deepseek-flash")
    with pytest.raises(ValueError, match="api.deepseek.com"):
        require_real_provider(api_key="present", base_url="http://127.0.0.1:9000/v1", model="deepseek-flash")
    with pytest.raises(ValueError, match="deepseek-flash"):
        require_real_provider(api_key="present", base_url="https://api.deepseek.com/v1", model="test-model")


def test_cost_budget_reserves_worst_case_before_a_provider_call():
    budget = CostBudget(max_usd=0.02, input_usd_per_million=1.0, output_usd_per_million=1.0)
    budget.reserve([{"role": "user", "content": "export"}], max_tokens=100, max_http_calls=2)
    assert budget.reserved_usd > 0
    with pytest.raises(RuntimeError, match="cost cap"):
        budget.reserve([{"role": "user", "content": "export" * 10_000}], max_tokens=100, max_http_calls=2)
    assert budget.planner_calls == 1


@pytest.mark.parametrize("format_name,extension,media_type,driver", [
    ("geojson", ".geojson", "application/geo+json", "GeoJSON"),
    ("gpkg", ".gpkg", "application/geopackage+sqlite3", "GPKG"),
])
def test_export_verifier_reopens_real_gis_bytes_and_checks_metadata(tmp_path, format_name, extension, media_type, driver):
    source = gpd.GeoDataFrame({"name": ["南京医院"], "patients": [7]}, geometry=[Point(118.78, 32.04)], crs="EPSG:4326")
    physical = tmp_path / f"actual{extension}"
    source.to_file(physical, driver=driver)
    payload = physical.read_bytes()
    artifact = {
        "type": "File", "validation_status": "valid", "content_hash": hashlib.sha256(payload).hexdigest(),
        "media_type": media_type,
        "schema": {"file": {"format": format_name, "filename": f"export-file{extension}",
                            "extension": extension, "media_type": media_type, "download_disposition": "attachment"}},
    }
    assert_export_bytes(artifact, payload, physical, requested_format=format_name)
    bad = dict(artifact, schema={"file": dict(artifact["schema"]["file"], extension=".json")})
    with pytest.raises(AssertionError, match="extension"):
        assert_export_bytes(bad, payload, physical, requested_format=format_name)


def test_export_verifier_rejects_json_bytes_falsely_labeled_gpkg(tmp_path):
    payload = json.dumps({"type": "FeatureCollection", "features": []}).encode()
    artifact = {
        "type": "File", "validation_status": "valid", "content_hash": hashlib.sha256(payload).hexdigest(),
        "media_type": "application/geopackage+sqlite3",
        "schema": {"file": {"format": "gpkg", "filename": "export-file.gpkg", "extension": ".gpkg",
                            "media_type": "application/geopackage+sqlite3", "download_disposition": "attachment"}},
    }
    (tmp_path / "export.gpkg").write_bytes(payload)
    with pytest.raises(AssertionError, match="GeoPackage"):
        assert_export_bytes(artifact, payload, tmp_path / "export.gpkg", requested_format="gpkg")


def test_csv_negative_requires_terminal_rejection_and_no_file_artifact():
    assert_rejected_csv_outcome({"status": "awaiting_input", "output_artifacts": []})
    assert_rejected_csv_outcome({"status": "failed", "output_artifacts": []})
    with pytest.raises(AssertionError, match="rejected"):
        assert_rejected_csv_outcome({"status": "completed", "output_artifacts": []})
    with pytest.raises(AssertionError, match="File"):
        assert_rejected_csv_outcome({"status": "awaiting_input", "output_artifacts": [{"type": "File"}]})


def test_csv_negative_requires_specific_clarification_or_format_compile_diagnostic():
    response_hash = "a" * 64
    base = {"response_hash": response_hash, "status": "failed"}
    assert_csv_planning_evidence("awaiting_input", [base | {"error_code": "CLARIFICATION_REQUIRED"}], {response_hash})
    assert_csv_planning_evidence("failed", [base | {
        "error_code": "INVALID_PARAMETER",
        "diagnostics_json": json.dumps({"path": "tasks.export.params.format", "details": {"requested_formats": ["csv"]}}),
    }], {response_hash})
    with pytest.raises(AssertionError, match="format"):
        assert_csv_planning_evidence("failed", [base | {"error_code": "PLAN_COMPILE_FAILED"}], {response_hash})
