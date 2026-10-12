"""Raster bytes must remain readable across Runtime Artifact boundaries."""

from __future__ import annotations

import json

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app.runtime import ArtifactStore, RuntimeDB, RuntimeStore
from app.runtime.tool_handlers import handle_io_load_raster, handle_raster_slope, handle_raster_tri


def _runtime(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    return runtime, ArtifactStore(tmp_path / "data", runtime)


def _write_dem(path):
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=3,
        width=3,
        count=1,
        dtype="float32",
        crs="EPSG:3857",
        transform=from_origin(0, 3, 1, 1),
    ) as dataset:
        dataset.write(np.arange(9, dtype="float32").reshape(1, 3, 3))


def test_load_raster_publishes_geotiff_bytes_that_next_raster_operation_can_open(tmp_path):
    source = tmp_path / "input.tif"
    _write_dem(source)
    runtime, artifacts = _runtime(tmp_path)

    loaded = handle_io_load_raster(file={"path": str(source)})

    assert loaded["status"] == "success"
    assert isinstance(loaded["data"], bytes)
    staged = artifacts.stage_bytes(loaded["data"], filename="loaded.tif")
    artifact = artifacts.publish_staged(
        staged,
        origin="upload",
        artifact_type="Raster",
        crs={"kind": "EPSG", "code": 3857},
    )
    slope = handle_raster_slope(input={"path": str(artifacts.path_for(artifact["artifact_id"]))})

    assert artifact["validation_status"] == "valid"
    assert slope["status"] == "success"


def test_terrain_derivative_handler_output_is_publishable_as_raster_artifact(tmp_path):
    source = tmp_path / "input.tif"
    _write_dem(source)
    runtime, artifacts = _runtime(tmp_path)

    derived = handle_raster_tri(raster={"path": str(source)})
    assert derived["status"] == "success" and isinstance(derived["data"], bytes)
    artifact = artifacts.publish_staged(
        artifacts.stage_bytes(derived["data"], filename="tri.tif"),
        origin="upload", artifact_type="Raster", crs={"kind": "EPSG", "code": 3857},
    )
    assert artifact["validation_status"] == "valid"


def test_raster_artifact_rejects_json_or_other_non_raster_content(tmp_path):
    _runtime_store, artifacts = _runtime(tmp_path)
    staged = artifacts.stage_bytes(b'{"not":"a raster"}', filename="fake.tif")

    with pytest.raises(ValueError, match="Raster"):
        artifacts.publish_staged(
            staged,
            origin="upload",
            artifact_type="Raster",
            crs={"kind": "EPSG", "code": 3857},
        )


def test_raster_artifact_rejects_declared_crs_that_disagrees_with_geotiff(tmp_path):
    source = tmp_path / "mismatch.tif"
    _write_dem(source)
    _runtime_store, artifacts = _runtime(tmp_path)

    with pytest.raises(ValueError, match="CRS"):
        artifacts.publish_staged(
            artifacts.stage_bytes(source.read_bytes(), filename="mismatch.tif"),
            origin="upload", artifact_type="Raster",
            crs={"kind": "EPSG", "code": 4326},
        )


def test_same_cas_blob_can_back_distinct_typed_artifacts_without_metadata_aliasing(tmp_path):
    _runtime_store, artifacts = _runtime(tmp_path)
    payload = b'{"type":"FeatureCollection","features":[]}'
    first = artifacts.publish_staged(
        artifacts.stage_bytes(payload, filename="empty.geojson"),
        origin="upload",
        artifact_type="Vector",
        crs={"kind": "EPSG", "code": 4326},
    )

    second = artifacts.publish_staged(
        artifacts.stage_bytes(payload, filename="same-bytes.bin"),
        origin="upload",
        artifact_type="File",
        crs=None,
    )

    assert first["artifact_id"] != second["artifact_id"]
    assert first["content_hash"] == second["content_hash"]
    assert artifacts.runtime.get_artifact(first["artifact_id"])["type"] == "Vector"
    assert artifacts.runtime.get_artifact(second["artifact_id"])["type"] == "File"
    assert artifacts.path_for(first["artifact_id"]) == artifacts.path_for(second["artifact_id"])
    assert artifacts.runtime.get_artifact(first["content_hash"]) is None


def test_maplayer_artifact_requires_frontend_consumable_layers_and_bbox(tmp_path):
    _runtime_store, artifacts = _runtime(tmp_path)
    valid = {
        "layers": [{"type": "FeatureCollection", "features": []}],
        "bbox": [116.4, 39.9, 116.6, 40.1],
        "crs": {"kind": "EPSG", "code": 4326},
    }
    artifact = artifacts.publish_staged(
        artifacts.stage_bytes(json.dumps(valid).encode(), filename="layer.json"),
        origin="upload", artifact_type="MapLayer", crs={"kind": "EPSG", "code": 4326},
    )

    assert artifact["type"] == "MapLayer"
    with pytest.raises(ValueError, match="MapLayer"):
        artifacts.publish_staged(
            artifacts.stage_bytes(b"{}", filename="invalid-layer.json"),
            origin="upload", artifact_type="MapLayer", crs={"kind": "EPSG", "code": 4326},
        )
