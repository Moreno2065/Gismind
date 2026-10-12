"""Actual upload bytes -> persisted IR -> isolated worker -> independent readers.

These transport/data tests build an explicit plan; they do not score LLM intent.
"""
import io
import json
import zipfile

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Point

from app.runtime import ArtifactRef, Compiler, PlannerDraft, PlannerStepDraft, RuntimeSupervisor, TaskOutputRef
from tests.integration.test_runtime_upload_formats import client, _upload


def _vector_bytes(tmp_path, format_name):
    frame = gpd.GeoDataFrame({"record_id": [17, 29], "label": ["甲", "乙"], "value": [3, 11]},
                             geometry=[Point(116.4, 39.9), Point(118.8, 32.1)], crs="EPSG:4326")
    if format_name == "geojson":
        return frame.to_json().encode("utf-8"), frame
    if format_name == "gpkg":
        path = tmp_path / "source.gpkg"
        frame.to_file(path, layer="points", driver="GPKG")
        return path.read_bytes(), frame
    if format_name == "kml":
        placemarks = ''.join(f'<Placemark><name>{label}</name><Point><coordinates>{x},{y},0</coordinates></Point></Placemark>'
                             for label, x, y in [("甲", 116.4, 39.9), ("乙", 118.8, 32.1)])
        return f'<kml xmlns="http://www.opengis.net/kml/2.2"><Document>{placemarks}</Document></kml>'.encode(), frame
    directory = tmp_path / "shapefile"
    directory.mkdir()
    frame.to_file(directory / "points.shp", encoding="UTF-8")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        for path in sorted(directory.iterdir()):
            output.write(path, "nested/" + path.name.upper())
    return archive.getvalue(), frame


def _execute_load(client, artifact, *, raster=False):
    runtime = client.app.state.runtime_store
    artifacts = client.app.state.artifact_store
    runtime.create_run("roundtrip", "uploads")
    steps = [PlannerStepDraft(
        task_key="load", op="io.load_raster" if raster else "io.load_vector",
        inputs={"file": ArtifactRef(artifact_id=artifact["artifact_id"], type="File", crs=artifact.get("crs"))},
    )]
    if not raster:
        steps.append(PlannerStepDraft(task_key="export", op="file.export",
            inputs={"input": TaskOutputRef(task_key="load", port="vector")}, params={"format": "gpkg"}))
    draft = PlannerDraft(run_id="roundtrip", input_revision=1, tasks=steps)
    plan = Compiler(artifact_lookup=runtime.get_artifact).compile(draft)
    runtime.create_plan("roundtrip-plan", "roundtrip", plan=plan)
    for task in plan.tasks:
        runtime.create_task(task.task_key, "roundtrip-plan", task.task_key, tool_name=task.op,
                            status="blocked" if task.task_key == "export" else "ready")
    if not raster:
        runtime.add_task_dependency("export", "load")
        runtime.bind_task_input("export-input", "export", "input", source_task_id="load", source_port="vector")
    worker = RuntimeSupervisor(runtime, artifacts, isolate=True, autostart=False)
    result = None
    for _ in plan.tasks:
        result = worker.run_once("upload-roundtrip", run_id="roundtrip")
        assert result is not None, runtime.get_run("roundtrip")
    assert runtime.get_run("roundtrip")["status"] == "completed"
    assert result and result["artifact"]["validation_status"] == "valid"
    attempts = runtime._read(lambda conn: [dict(row) for row in conn.execute("SELECT executor_identity,status FROM task_attempts")])
    assert all(row["status"] == "succeeded" and json.loads(row["executor_identity"])["pid"] for row in attempts)
    return artifacts.path_for(result["artifact"]["artifact_id"])


@pytest.mark.parametrize("format_name", ["geojson", "gpkg", "shapefile_zip", "kml"])
@pytest.mark.parametrize("name_mode", ["standard", "extensionless", "misleading"])
def test_content_identity_survives_suffix_and_isolated_export(client, tmp_path, format_name, name_mode):
    content, expected = _vector_bytes(tmp_path, format_name)
    suffix = {"geojson": "GEOJSON", "gpkg": "GPKG", "shapefile_zip": "ZIP", "kml": "KML"}[format_name]
    name = f"中文附件.{suffix}" if name_mode == "standard" else "无后缀附件" if name_mode == "extensionless" else "伪装.ZIP" if format_name != "shapefile_zip" else "伪装.geojson"
    response = _upload(client, name, content)
    assert response.status_code == 201, response.text
    artifact = response.json()
    assert artifact["schema"]["inspection"]["format"] == format_name
    assert client.get(artifact["content_url"]).content == content
    output = _execute_load(client, artifact)
    downloaded = tmp_path / "downloaded.gpkg"
    downloaded.write_bytes(output.read_bytes())
    actual = gpd.read_file(downloaded)
    assert actual.crs.to_epsg() == 4326 and len(actual) == 2
    if format_name == "kml":
        actual = actual.sort_values("Name")
        expected = expected.sort_values("label")
        assert list(actual.Name) == list(expected.label)
    else:
        actual = actual.sort_values("record_id")
        expected = expected.sort_values("record_id")
        assert actual[["record_id", "label", "value"]].to_dict("records") == expected[["record_id", "label", "value"]].to_dict("records")
    for left, right in zip(actual.geometry, expected.geometry):
        assert left.equals_exact(right, 1e-9)


def test_declared_crs_preserves_multiband_mask_grid_tags_and_original_cas(client, tmp_path):
    path = tmp_path / "multiband.tif"
    values = np.array([[[1, 2], [3, 4]], [[101, 102], [103, 104]]], dtype="int16")
    mask = np.array([[255, 0], [255, 255]], dtype="uint8")
    transform = from_origin(1000, 2000, 10, 10)
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True):
        with rasterio.open(path, "w", driver="GTiff", count=2, width=2, height=2, dtype="int16", transform=transform, nodata=-9999) as dataset:
            dataset.write(values)
            dataset.write_mask(mask)
            dataset.set_band_description(1, "温度")
            dataset.set_band_description(2, "降雨")
            dataset.update_tags(source="two-band")
            dataset.update_tags(2, unit="mm")
    content = path.read_bytes()
    response = _upload(client, "没有后缀的栅格", content, declared_crs=json.dumps({"kind": "EPSG", "code": 3857}))
    assert response.status_code == 201, response.text
    artifact = response.json()
    output = _execute_load(client, artifact, raster=True)
    with rasterio.open(output) as actual:
        np.testing.assert_array_equal(actual.read(), values)
        np.testing.assert_array_equal(actual.dataset_mask(), mask)
        assert actual.count == 2 and actual.nodata == -9999
        assert actual.crs.to_epsg() == 3857 and actual.transform == transform
        assert actual.descriptions == ("温度", "降雨")
        assert actual.tags()["source"] == "two-band" and actual.tags(2)["unit"] == "mm"
    assert client.get(artifact["content_url"]).content == content


def test_multilayer_geopackage_is_rejected_without_publishing(client, tmp_path):
    path = tmp_path / "two-layers.gpkg"
    source = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(0, 0)], crs="EPSG:4326")
    source.to_file(path, layer="first", driver="GPKG")
    source.to_file(path, layer="second", driver="GPKG")
    before = client.app.state.runtime_store._read(lambda c: c.execute("SELECT count(*) FROM artifacts").fetchone()[0])
    result = _upload(client, "含两个图层.gpkg", path.read_bytes())
    assert result.status_code == 422 and "多个图层" in result.json()["error"]["message"]
    assert client.app.state.runtime_store._read(lambda c: c.execute("SELECT count(*) FROM artifacts").fetchone()[0]) == before


@pytest.mark.parametrize("bounds,accepted", [([116.4, 39.9, 118.8, 32.1], False), ([116.4, 32.1, 118.8, 39.9], True)])
def test_declared_bbox_matches_physical_bounds(client, tmp_path, bounds, accepted):
    content, _ = _vector_bytes(tmp_path, "geojson")
    result = _upload(client, "边界.geojson", content, declared_bbox=json.dumps(bounds))
    assert result.status_code == (201 if accepted else 422), result.text
