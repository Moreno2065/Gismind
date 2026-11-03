"""Real missing-CRS uploads resume through immutable inputs and isolated Workers."""
from __future__ import annotations

import json

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from fastapi.testclient import TestClient
from rasterio.transform import from_origin

from app.runtime import (
    ArtifactRef, ArtifactStore, PlannerDraft, PlannerStepDraft, RuntimeDB,
    RuntimeStore, RuntimeSupervisor, TaskOutputRef,
)


class FileLoaderPlanner:
    def create_draft(self, request):
        tasks = []
        for index, artifact in enumerate(request.input_artifacts):
            raster = artifact["schema"]["inspection"]["artifact_type"] == "Raster"
            key = f"load_{index}"
            tasks.append(PlannerStepDraft(
                task_key=key, op="io.load_raster" if raster else "io.load_vector",
                inputs={"file": ArtifactRef(artifact_id=artifact["artifact_id"], type="File",
                                            crs=artifact.get("crs"))},
            ))
            if not raster:
                tasks.append(PlannerStepDraft(
                    task_key=f"export_{index}", op="file.export",
                    inputs={"input": TaskOutputRef(task_key=key, port="vector")},
                    params={"format": "gpkg"},
                ))
        return PlannerDraft(run_id=request.run_id, input_revision=request.input_revision,
                            user_goal=request.user_goal, tasks=tasks)


@pytest.fixture
def client(tmp_path):
    from app.main import create_app

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("crs-resume")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    supervisor = RuntimeSupervisor(runtime, artifacts, planner=FileLoaderPlanner(),
                                   isolate=True, autostart=False)
    with TestClient(create_app(runtime_store=runtime, artifact_store=artifacts,
                              runtime_supervisor=supervisor)) as result:
        yield result


def _upload(client, tmp_path, kind, *, name="source"):
    if kind == "vector":
        content = json.dumps({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"record_id": 17, "名称": "甲"},
             "geometry": {"type": "Point", "coordinates": [1000, 2000]}}
        ]}, ensure_ascii=False).encode("utf-8")
        filename = f"{name}.geojson"
    else:
        source = tmp_path / f"{name}.tif"
        with rasterio.open(source, "w", driver="GTiff", width=2, height=2, count=1,
                           dtype="int16", transform=from_origin(1000, 2000, 10, 10),
                           nodata=-9999) as dataset:
            dataset.write(np.array([[1, 2], [3, 4]], dtype="int16"), 1)
            dataset.update_tags(source="missing-crs-upload")
        content = source.read_bytes()
        filename = f"{name}.tif"
    response = client.post("/api/artifacts/uploads", files={"file": (filename, content)})
    assert response.status_code == 201, response.text
    artifact = response.json()
    assert artifact["crs"] is None
    assert artifact["schema"]["inspection"]["crs_status"] == "missing"
    return artifact, content


def _start(client, artifacts, *, raster=False):
    runtime = client.app.state.runtime_store
    runtime.create_run("run-crs", "crs-resume")
    runtime.add_input_revision("run-crs", 1, {
        "prompt": "读取上传栅格" if raster else "读取上传矢量并导出GPKG",
        "input_artifact_ids": [item["artifact_id"] for item in artifacts],
    })
    supervisor = client.app.state.runtime_supervisor
    assert supervisor.run_once("worker-crs", run_id="run-crs")["status"] == "awaiting_input"
    snapshot = client.get("/api/runs/run-crs").json()
    assert snapshot["diagnostic"]["code"] == "CRS_REQUIRED"
    return runtime, supervisor, snapshot


def _resume(client, snapshot, artifact_id=None, *, command="declare-crs", code=3857):
    file_patch = {"declared_crs": {
        "value": {"kind": "EPSG", "code": code}, "source": "user",
    }}
    if artifact_id is not None:
        file_patch["artifact_id"] = artifact_id
    return client.post("/api/runs/run-crs/resume", json={
        "command_id": command, "expected_run_version": snapshot["run_version"],
        "patch": {"file": file_patch},
    })


@pytest.mark.parametrize("kind", ["vector", "raster"])
def test_missing_crs_resume_reaches_isolated_worker_without_mutating_upload(client, tmp_path, kind):
    source, content = _upload(client, tmp_path, kind)
    runtime, supervisor, snapshot = _start(client, [source], raster=kind == "raster")
    original = runtime.get_artifact(source["artifact_id"])

    response = _resume(client, snapshot)
    assert response.status_code == 202, response.text
    repeated = _resume(client, snapshot)
    assert repeated.status_code == 202 and repeated.json() == response.json()
    assert supervisor.run_once("worker-crs", run_id="run-crs")["status"] == "planned"
    assert runtime._read(lambda conn: conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]) == 2

    plan = runtime._read(lambda conn: json.loads(conn.execute(
        "SELECT ir_json FROM plans WHERE run_id='run-crs'").fetchone()[0]))
    loader = next(task for task in plan["tasks"] if task["op"].startswith("io.load_"))
    resolved_id = loader["inputs"]["file"]["artifact_id"]
    assert resolved_id != source["artifact_id"]
    resolved = runtime.get_artifact(resolved_id)
    assert resolved["content_hash"] == original["content_hash"]
    assert resolved["storage_uri"] == original["storage_uri"]
    assert resolved["schema"]["file"]["declared_crs"] == {
        "value": {"kind": "EPSG", "code": 3857}, "source": "user"}
    assert [row["parent_artifact_id"] for row in runtime.list_lineage(resolved_id)] == [source["artifact_id"]]

    result = None
    for _ in plan["tasks"]:
        result = supervisor.run_once("worker-crs", run_id="run-crs")
        assert result is not None
    assert runtime.get_run("run-crs")["status"] == "completed", result
    assert result["artifact"]["validation_status"] == "valid", result
    output = supervisor.artifacts.path_for(result["artifact"]["artifact_id"])
    if kind == "vector":
        downloaded = tmp_path / "result.gpkg"
        downloaded.write_bytes(output.read_bytes())
        actual = gpd.read_file(downloaded)
        assert actual.crs.to_epsg() == 3857
        assert len(actual) == 1 and actual.iloc[0]["名称"] == "甲"
        assert (actual.geometry.iloc[0].x, actual.geometry.iloc[0].y) == (1000, 2000)
    else:
        with rasterio.open(output) as actual:
            assert actual.crs.to_epsg() == 3857
            assert actual.transform == from_origin(1000, 2000, 10, 10)
            np.testing.assert_array_equal(actual.read(1), [[1, 2], [3, 4]])
            assert actual.nodata == -9999 and actual.tags()["source"] == "missing-crs-upload"
    attempts = runtime._read(lambda conn: [dict(row) for row in conn.execute(
        "SELECT status,executor_identity FROM task_attempts")])
    assert all(row["status"] == "succeeded" and json.loads(row["executor_identity"])["pid"] for row in attempts)
    assert runtime.get_artifact(source["artifact_id"]) == original
    assert client.get(source["content_url"]).content == content


def test_multiple_missing_crs_files_bind_each_answer_to_the_current_question(client, tmp_path):
    first, _ = _upload(client, tmp_path, "vector", name="first")
    second, _ = _upload(client, tmp_path, "vector", name="second")
    runtime, supervisor, snapshot = _start(client, [first, second])
    assert _resume(client, snapshot).status_code == 202
    assert supervisor.run_once("worker-crs", run_id="run-crs")["status"] == "awaiting_input"
    assert runtime._read(lambda conn: conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]) == 3
    snapshot = client.get("/api/runs/run-crs").json()
    assert snapshot["diagnostic"]["artifact_id"] == second["artifact_id"]
    assert runtime.get_artifact(second["artifact_id"])["schema"]["file"].get("declared_crs") is None
    assert _resume(client, snapshot, command="declare-second").status_code == 202
    supervisor = RuntimeSupervisor(runtime, supervisor.artifacts, planner=FileLoaderPlanner(),
                                   isolate=True, autostart=False)
    assert supervisor.run_once("worker-crs", run_id="run-crs")["status"] == "planned"
    assert runtime._read(lambda conn: conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]) == 4


@pytest.mark.parametrize("bad_target,bad_code", [("foreign", 3857), (None, 999999999)])
def test_crs_resume_never_publishes_a_declaration_for_an_unavailable_target_or_invalid_epsg(
    client, tmp_path, bad_target, bad_code,
):
    source, _ = _upload(client, tmp_path, "vector")
    if bad_target == "foreign":
        unavailable, _ = _upload(client, tmp_path, "vector", name="unavailable")
        bad_target = unavailable["artifact_id"]
    runtime, supervisor, snapshot = _start(client, [source])
    before = runtime._read(lambda conn: conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0])
    response = _resume(client, snapshot, bad_target or source["artifact_id"], code=bad_code)
    assert response.status_code == 202, response.text
    assert supervisor.run_once("worker-crs", run_id="run-crs")["status"] == "awaiting_input"
    assert runtime._read(lambda conn: conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]) == before
    assert runtime._read(lambda conn: conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]) == 0
    snapshot = client.get("/api/runs/run-crs").json()
    assert _resume(client, snapshot, command="correct-answer").status_code == 202
    assert supervisor.run_once("worker-crs", run_id="run-crs")["status"] == "planned"
