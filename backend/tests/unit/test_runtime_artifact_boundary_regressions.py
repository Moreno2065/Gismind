"""Regressions for physical Vector metadata and interrupted upload staging."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import UploadFile
from starlette.requests import Request

from app.runtime import ArtifactStore, RuntimeDB, RuntimeStore


@pytest.fixture
def artifacts(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    return ArtifactStore(tmp_path / "data", runtime)


def _vector(**metadata):
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature", "properties": {},
            "geometry": {"type": "Point", "coordinates": [116.4, 39.9]},
        }],
        **metadata,
    }


def _publish_vector(artifacts, payload, crs):
    return artifacts.publish_staged(
        artifacts.stage_bytes(json.dumps(payload).encode(), filename="vector.geojson"),
        origin="upload", artifact_type="Vector", crs=crs,
    )


@pytest.mark.parametrize("metadata", [
    {"crs": {"type": "name", "properties": {"name": "EPSG:3857"}}},
    {"_physical_crs": "EPSG:3857"},
    {"_crs_label": "GCJ02"},
])
def test_vector_publication_rejects_conflicting_json_crs_evidence(artifacts, metadata):
    with pytest.raises(ValueError, match="CRS"):
        _publish_vector(artifacts, _vector(**metadata), {"kind": "EPSG", "code": 4326})
    assert artifacts.runtime._read(lambda c: c.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]) == 0
    assert not any(path.is_file() for path in artifacts.artifacts_dir.rglob("*"))


@pytest.mark.parametrize("label", ["WGS84", "PROJECTED"])
def test_gcj02_artifact_rejects_an_explicit_different_datum_label(artifacts, label):
    with pytest.raises(ValueError, match="CRS"):
        _publish_vector(artifacts, _vector(_physical_crs="EPSG:4326", _crs_label=label), {"kind": "GCJ02"})
    assert artifacts.runtime._read(lambda c: c.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]) == 0


@pytest.mark.parametrize("crs", [{"kind": "invalid"}, {"kind": "EPSG", "code": True}])
def test_vector_publication_rejects_unsupported_crs_metadata(artifacts, crs):
    with pytest.raises(ValueError, match="CRS"):
        _publish_vector(artifacts, _vector(), crs)


@pytest.mark.parametrize("metadata,crs", [
    ({"crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3857"}}, "_physical_crs": "EPSG:3857"}, {"kind": "EPSG", "code": 3857}),
    ({"_physical_crs": "EPSG:4326"}, {"kind": "WGS84"}),
    ({"_physical_crs": "EPSG:4326", "_crs_label": "GCJ02"}, {"kind": "GCJ02"}),
    ({}, {"kind": "EPSG", "code": 3857}),
    ({}, {"kind": "GCJ02"}),
])
def test_vector_publication_preserves_existing_crs_representations(artifacts, metadata, crs):
    artifact = _publish_vector(artifacts, _vector(**metadata), crs)
    assert artifact["validation_status"] == "valid"
    assert artifact["crs"] == crs
    assert json.loads(artifacts.path_for(artifact["artifact_id"]).read_bytes()) == _vector(**metadata)


def _deny_staging_cleanup(artifacts, monkeypatch):
    original_unlink = Path.unlink

    def denied_unlink(path, *args, **kwargs):
        if path.parent == artifacts.staging_dir:
            raise PermissionError("staging cleanup denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied_unlink)


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_cancelled_upload_preserves_cancellation_and_attempts_cleanup(artifacts, monkeypatch, caplog, cleanup_fails):
    from app.api.runtime import upload_runtime_artifact

    if cleanup_fails:
        _deny_staging_cleanup(artifacts, monkeypatch)
    upload = UploadFile(file=io.BytesIO(b"name,value\nA,1\n"), filename="input.csv")
    state = SimpleNamespace(runtime_store=artifacts.runtime, artifact_store=artifacts, runtime_supervisor=object())
    request = Request({"type": "http", "app": SimpleNamespace(state=state), "headers": []})

    async def cancel_after_first_chunk():
        waiting = asyncio.Event()
        resume = asyncio.Event()
        original_read = upload.read

        async def paused_read(size):
            if upload.file.tell() == 0:
                return await original_read(size)
            waiting.set()
            await resume.wait()
            return b""

        upload.read = paused_read
        task = asyncio.create_task(upload_runtime_artifact(request, upload, None, None))
        await asyncio.wait_for(waiting.wait(), timeout=2)
        assert len(list(artifacts.staging_dir.iterdir())) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(cancel_after_first_chunk())
    finally:
        upload.file.close()
    if cleanup_fails:
        assert len(list(artifacts.staging_dir.iterdir())) == 1
        assert any("upload staging" in record.getMessage().lower() and record.exc_info for record in caplog.records)
    else:
        assert list(artifacts.staging_dir.iterdir()) == []
    assert artifacts.runtime._read(lambda c: c.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]) == 0


@pytest.mark.parametrize("payload,status", [(b"", 422), (b"x", 413)])
def test_rejected_upload_preserves_error_response_when_staging_cleanup_fails(artifacts, monkeypatch, caplog, payload, status):
    from app.api.runtime import settings, upload_runtime_artifact

    _deny_staging_cleanup(artifacts, monkeypatch)
    monkeypatch.setattr(settings, "UPLOAD_MAX_SIZE", 0)
    upload = UploadFile(file=io.BytesIO(payload), filename="input.csv")
    state = SimpleNamespace(runtime_store=artifacts.runtime, artifact_store=artifacts, runtime_supervisor=object())
    request = Request({"type": "http", "app": SimpleNamespace(state=state), "headers": []})
    try:
        response = asyncio.run(upload_runtime_artifact(request, upload, None, None))
    finally:
        upload.file.close()
    assert response.status_code == status
    assert len(list(artifacts.staging_dir.iterdir())) == 1
    assert any("upload staging" in record.getMessage().lower() and record.exc_info for record in caplog.records)
    assert artifacts.runtime._read(lambda c: c.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]) == 0
