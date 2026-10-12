"""Follow-up regressions for empty-result handling and baseline reconciliation."""

from __future__ import annotations

import json
from pathlib import Path

from app.runtime import ToolContractRegistry
from app.runtime.contracts import OutputSpec


def test_success_empty_payloads_are_classified_by_declared_artifact_type():
    from app.runtime.executor import RuntimeExecutor

    assert RuntimeExecutor._is_empty_payload({"type": "FeatureCollection", "features": []}, OutputSpec(port="v", artifact_type="Vector")) is True
    assert RuntimeExecutor._is_empty_payload([], OutputSpec(port="t", artifact_type="Table")) is True
    assert RuntimeExecutor._is_empty_payload([], OutputSpec(port="r", artifact_type="Raster", cardinality="many", min_items=0, max_items=None)) is True
    assert RuntimeExecutor._is_empty_payload([{"id": 1}], OutputSpec(port="t", artifact_type="Table")) is False
    assert RuntimeExecutor._is_empty_payload({"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [0, 0]}, "properties": {}}]}, OutputSpec(port="v", artifact_type="Vector")) is False


def test_independent_runtime_baseline_reconciles_with_shipped_manifest():
    registry = ToolContractRegistry.with_builtins()
    manifest = json.loads((Path(__file__).resolve().parents[2] / "app" / "runtime" / "contracts" / "public_operations_manifest.json").read_text(encoding="utf-8"))
    baseline = json.loads((Path(__file__).resolve().parents[3] / "docs" / "legacy_public_operations_baseline.json").read_text(encoding="utf-8"))
    assert registry.reconcile_public_operations_manifest(manifest, baseline=baseline) is True
