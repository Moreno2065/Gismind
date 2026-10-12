"""Real upload bytes, metadata and CAS loading regressions (no LLM calls)."""

import io
import json
import zipfile

import geopandas as gpd
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import Point

from app.runtime import ArtifactRef, ArtifactStore, Compiler, PlannerDraft, PlannerStepDraft, RuntimeDB, RuntimeStore, RuntimeSupervisor
from app.runtime.compiler import AwaitingInputDiagnostic
from app.runtime.tool_handlers import handle_io_load_csv, handle_io_load_vector, handle_io_load_raster


@pytest.fixture
def client(tmp_path):
    from app.main import create_app

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("uploads")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    supervisor = RuntimeSupervisor(runtime, artifacts, isolate=False, autostart=False)
    with TestClient(create_app(runtime_store=runtime, artifact_store=artifacts, runtime_supervisor=supervisor)) as value:
        yield value


def _geojson(*, crs=True):
    value = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"名称": "医院", "人数": 7}, "geometry": {"type": "Point", "coordinates": [116.4, 39.9]}}]}
    if crs:
        value["crs"] = {"type": "name", "properties": {"name": "EPSG:4326"}}
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _upload(client, name, payload, **data):
    return client.post("/api/artifacts/uploads", files={"file": (name, payload, "application/octet-stream")}, data=data)


def _envelope(client, artifact):
    stored = client.app.state.runtime_store.get_artifact(artifact["artifact_id"])
    path = client.app.state.artifact_store.path_for(artifact["artifact_id"])
    return {**stored, "path": str(path), "data": path.read_bytes()}


def _compile(client, artifact, op="io.load_vector"):
    stored = client.app.state.runtime_store.get_artifact(artifact["artifact_id"])
    ref = ArtifactRef(artifact_id=stored["artifact_id"], type="File", crs=stored["crs"], content_hash=stored["content_hash"])
    return Compiler(artifact_lookup=client.app.state.runtime_store.get_artifact).compile(PlannerDraft(tasks=[PlannerStepDraft(task_key="load", op=op, inputs={"file": ref})]))


@pytest.mark.parametrize("name", ["医院.GEOJSON", "医院.JsOn", "医院", "医院.dat"])
def test_upload_detects_geojson_content_and_exposes_compilable_metadata(client, name):
    response = _upload(client, name, _geojson())
    assert response.status_code == 201, response.text
    artifact = response.json()
    assert artifact["crs"] == {"kind": "EPSG", "code": 4326}
    facts = artifact["schema"]["inspection"]
    assert facts["format"] == "geojson"
    assert facts["artifact_type"] == "Vector"
    assert facts["feature_count"] == 1
    assert facts["geometry_types"] == ["Point"]
    assert set(artifact["schema"]["fields"]) == {"名称", "人数"}
    assert facts["source"] == "physical_file"
    assert _compile(client, artifact).tasks[0].outputs[0].crs == artifact["crs"]
    loaded = handle_io_load_vector(file=_envelope(client, artifact))
    assert loaded["status"] == "success", loaded
    assert loaded["data"]["features"][0]["properties"]["名称"] == "医院"
    assert client.get(artifact["content_url"]).content == _geojson()


def test_standard_geojson_uses_rfc7946_wgs84_without_user_declaration(client):
    response = _upload(client, "标准.geojson", _geojson(crs=False))
    assert response.status_code == 201, response.text
    artifact = response.json()
    assert artifact["crs"] == {"kind": "EPSG", "code": 4326}
    assert artifact["schema"]["inspection"]["crs_source"] == "RFC7946"
    assert _compile(client, artifact).tasks[0].outputs[0].crs == artifact["crs"]
    assert handle_io_load_vector(file=_envelope(client, artifact))["status"] == "success"


def _projected_geojson_without_crs():
    value = json.loads(_geojson(crs=False))
    value["features"][0]["geometry"]["coordinates"] = [1000, 2000]
    return json.dumps(value).encode("utf-8")


def test_non_geographic_geojson_without_crs_requires_clarification(client):
    response = _upload(client, "未声明.geojson", _projected_geojson_without_crs())
    assert response.status_code == 201, response.text
    artifact = response.json()
    assert artifact["crs"] is None
    assert artifact["schema"]["inspection"]["crs_status"] == "missing"
    with pytest.raises(AwaitingInputDiagnostic, match="CRS_REQUIRED"):
        _compile(client, artifact)
    result = handle_io_load_vector(file=_envelope(client, artifact))
    assert result["status"] == "error"
    assert "CRS_REQUIRED" in result["message"]


def test_missing_crs_user_declaration_is_used_without_reprojecting_coordinates(client):
    response = _upload(client, "已声明.geojson", _projected_geojson_without_crs(), declared_crs=json.dumps({"kind": "EPSG", "code": 3857}))
    assert response.status_code == 201, response.text
    artifact = response.json()
    assert artifact["crs"] is None
    assert artifact["schema"]["inspection"]["crs_status"] == "missing"
    assert _compile(client, artifact).tasks[0].outputs[0].crs == {"kind": "EPSG", "code": 3857}
    loaded = handle_io_load_vector(file=_envelope(client, artifact))
    assert loaded["status"] == "success", loaded
    assert loaded["data"]["_physical_crs"] == "EPSG:3857"
    assert loaded["data"]["features"][0]["geometry"]["coordinates"] == [1000.0, 2000.0]


def test_explicit_projected_geojson_crs_is_preserved(client):
    value = json.loads(_projected_geojson_without_crs())
    value["crs"] = {"type": "name", "properties": {"name": "EPSG:3857"}}
    response = _upload(client, "投影.geojson", json.dumps(value).encode())
    assert response.status_code == 201, response.text
    artifact = response.json()
    assert artifact["crs"] == {"kind": "EPSG", "code": 3857}
    assert artifact["schema"]["inspection"]["crs_source"] == "physical_file"
    result = handle_io_load_vector(file=_envelope(client, artifact))
    assert result["data"]["_physical_crs"] == "EPSG:3857"


def test_explicit_geographic_crs_with_projected_coordinates_is_rejected(client):
    value = json.loads(_projected_geojson_without_crs())
    value["crs"] = {"type": "name", "properties": {"name": "EPSG:4326"}}
    response = _upload(client, "错误投影.geojson", json.dumps(value).encode())
    assert response.status_code == 422


def test_upload_rejects_declared_crs_conflicting_with_physical_file(client):
    response = _upload(client, "冲突.geojson", _geojson(), declared_crs=json.dumps({"kind": "EPSG", "code": 3857}))
    assert response.status_code == 422
    assert "CRS" in response.json()["error"]["message"]


@pytest.mark.parametrize("name,payload", [("表.CSV", "经度,纬度,名称\n116.4,39.9,医院\n"), ("表.TsV", "经度\t纬度\t名称\n116.4\t39.9\t医院\n"), ("表", "经度,纬度,名称\n116.4,39.9,医院\n")])
def test_delimited_upload_preserves_complete_rows_and_real_columns(client, name, payload):
    response = _upload(client, name, payload.encode("utf-8-sig"))
    assert response.status_code == 201, response.text
    artifact = response.json()
    assert artifact["schema"]["inspection"]["artifact_type"] == "Table"
    assert artifact["schema"]["inspection"]["row_count"] == 1
    assert set(artifact["schema"]["fields"]) == {"经度", "纬度", "名称"}
    loaded = handle_io_load_csv(file=_envelope(client, artifact))
    assert loaded["status"] == "success", loaded
    assert loaded["data"]["rows"] == [{"经度": 116.4, "纬度": 39.9, "名称": "医院"}]


@pytest.mark.parametrize("payload", [
    "region,code,name,name\nNA,001,A,B\n",
    "region,,name\nNA,001,A\n",
])
def test_csv_upload_rejects_duplicate_or_blank_physical_headers(client, payload):
    response = _upload(client, "unsafe.csv", payload.encode("utf-8"))
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "ARTIFACT_INVALID"


def test_csv_upload_and_worker_preserve_code_and_literal_na_without_losing_coordinates(client):
    payload = "region,code,lon,lat\nNA,001,116.4,39.9\n"
    response = _upload(client, "locations.CSV", payload.encode("utf-8"))
    assert response.status_code == 201, response.text
    artifact = response.json()
    assert set(artifact["schema"]["inspection"]["fields"]) == {"region", "code", "lon", "lat"}
    loaded = handle_io_load_csv(file=_envelope(client, artifact))
    assert loaded["status"] == "success", loaded
    assert loaded["data"]["rows"] == [{
        "region": "NA", "code": "001", "lon": 116.4, "lat": 39.9,
    }]


@pytest.mark.parametrize("name,payload", [("未知", b"unrecognized bytes"), ("假.geojson", b"not json"), ("假.tif", b"not a raster"), ("孤立.shp", b"not a complete shapefile")])
def test_unknown_or_misleading_upload_is_rejected(client, name, payload):
    response = _upload(client, name, payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ARTIFACT_INVALID"


def test_empty_upload_rejection_removes_its_staging_file(client):
    staging = client.app.state.artifact_store.staging_dir
    before = {path.name: path.stat().st_size for path in staging.iterdir() if path.is_file()}

    response = _upload(client, "empty.geojson", b"")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ARTIFACT_INVALID"
    assert response.json()["error"]["message"] == "uploaded file is empty"
    after = {path.name: path.stat().st_size for path in staging.iterdir() if path.is_file()}
    assert after == before, f"rejected empty upload left staging files: before={before}, after={after}"


@pytest.mark.parametrize("member", ["../escape.shp", "C:escape.shp", "/escape.shp", "safe/../../escape.shp", "safe\\..\\escape.shp"])
@pytest.mark.parametrize("filename", ["危险.ZIP", "危险.bin"])
def test_zip_safety_is_content_based_and_rejects_all_traversal(client, member, filename):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(member, b"data")
    response = _upload(client, filename, stream.getvalue())
    assert response.status_code == 422
    assert "ZIP" in response.json()["error"]["message"]


def _shapefile_zip(tmp_path, *, with_crs=True, missing=None):
    source = tmp_path / "shape"
    source.mkdir()
    gpd.GeoDataFrame({"name": ["hospital"], "value": [7]}, geometry=[Point(116.4, 39.9)], crs="EPSG:4326" if with_crs else None).to_file(source / "points.shp", encoding="UTF-8")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for path in sorted(source.iterdir()):
            if path.suffix != missing:
                archive.write(path, "nested/" + path.name.upper())
    return stream.getvalue()


def test_uppercase_shapefile_archive_loads_from_extensionless_cas(client, tmp_path):
    response = _upload(client, "医院.ZIP", _shapefile_zip(tmp_path))
    assert response.status_code == 201, response.text
    artifact = response.json()
    assert artifact["crs"] == {"kind": "EPSG", "code": 4326}
    assert artifact["schema"]["inspection"]["format"] == "shapefile_zip"
    assert _compile(client, artifact).tasks[0].outputs[0].crs == artifact["crs"]
    loaded = handle_io_load_vector(file=_envelope(client, artifact))
    assert loaded["status"] == "success", loaded
    assert loaded["data"]["features"][0]["properties"]["name"] == "hospital"


def test_custom_zip_member_limit_is_honored_by_upload_and_later_worker(client, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "UPLOAD_ZIP_MAX_FILE_COUNT", 120)
    source = io.BytesIO(_shapefile_zip(tmp_path))
    enlarged = io.BytesIO()
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(enlarged, "w") as archive:
        for member in original.infolist():
            archive.writestr(member, original.read(member))
        for index in range(101):
            archive.writestr(f"notes/extra-{index:03}.txt", b"x")
    response = _upload(client, "many-members.zip", enlarged.getvalue())
    assert response.status_code == 201, response.text
    artifact = response.json()
    assert artifact["schema"]["inspection"]["zip_limits"]["max_count"] == 120
    loaded = handle_io_load_vector(file=_envelope(client, artifact))
    assert loaded["status"] == "success", loaded
    assert loaded["data"]["features"][0]["properties"]["name"] == "hospital"


@pytest.mark.parametrize("missing", [".dbf", ".shx"])
def test_shapefile_missing_companion_is_rejected(client, tmp_path, missing):
    response = _upload(client, "残缺.zip", _shapefile_zip(tmp_path, missing=missing))
    assert response.status_code == 422
    assert missing in response.json()["error"]["message"].lower()


def test_shapefile_without_prj_needs_crs_clarification(client, tmp_path):
    response = _upload(client, "缺少投影.zip", _shapefile_zip(tmp_path, with_crs=False))
    assert response.status_code == 201, response.text
    artifact = response.json()
    assert artifact["crs"] is None
    assert artifact["schema"]["inspection"]["crs_status"] == "missing"
    with pytest.raises(AwaitingInputDiagnostic):
        _compile(client, artifact)


def test_geopackage_upload_reads_physical_crs_and_fields(client, tmp_path):
    source = tmp_path / "source.gpkg"
    gpd.GeoDataFrame({"name": ["hospital"]}, geometry=[Point(1000, 2000)], crs="EPSG:3857").to_file(source, layer="hospitals", driver="GPKG")
    response = _upload(client, "医院.GPKG", source.read_bytes())
    assert response.status_code == 201, response.text
    artifact = response.json()
    assert artifact["crs"] == {"kind": "EPSG", "code": 3857}
    assert artifact["schema"]["inspection"]["format"] == "gpkg"
    loaded = handle_io_load_vector(file=_envelope(client, artifact))
    assert loaded["status"] == "success", loaded
    assert loaded["data"]["_physical_crs"] == "EPSG:3857"


def test_geotiff_upload_exposes_raster_metadata_and_loads(client, tmp_path):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    source = tmp_path / "raster.tif"
    with rasterio.open(source, "w", driver="GTiff", height=2, width=3, count=1, dtype="float32", crs="EPSG:3857", transform=from_origin(1000, 2000, 10, 10)) as dataset:
        dataset.write(np.ones((2, 3), dtype="float32"), 1)
    response = _upload(client, "地形.TIFF", source.read_bytes())
    assert response.status_code == 201, response.text
    artifact = response.json()
    assert artifact["crs"] == {"kind": "EPSG", "code": 3857}
    facts = artifact["schema"]["inspection"]
    assert facts["artifact_type"] == "Raster"
    assert facts["width"] == 3 and facts["height"] == 2
    assert _compile(client, artifact, "io.load_raster").tasks[0].outputs[0].crs == artifact["crs"]
    assert handle_io_load_raster(file=_envelope(client, artifact))["status"] == "success"


def test_kml_standard_crs_and_fields_are_read_from_extensionless_cas(client):
    data = '<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><name>医院</name><Point><coordinates>116.4,39.9,0</coordinates></Point></Placemark></Document></kml>'.encode("utf-8")
    response = _upload(client, "医院.KmL", data)
    assert response.status_code == 201, response.text
    artifact = response.json()
    assert artifact["crs"] == {"kind": "EPSG", "code": 4326}
    result = handle_io_load_vector(file=_envelope(client, artifact))
    assert result["status"] == "success", result
    assert result["data"]["features"][0]["properties"]["Name"] == "医院"


def test_gb18030_csv_encoding_is_preserved_for_worker(client):
    response = _upload(client, "医院.CSV", "经度,纬度,名称\n116.4,39.9,医院\n".encode("gb18030"))
    assert response.status_code == 201, response.text
    artifact = response.json()
    assert artifact["schema"]["inspection"]["encoding"] == "gb18030"
    result = handle_io_load_csv(file=_envelope(client, artifact))
    assert result["data"]["rows"][0]["名称"] == "医院"


@pytest.mark.parametrize("declared", [False, True])
def test_raster_without_physical_crs_requires_declaration_and_normalizes_output(client, tmp_path, declared):
    import numpy as np
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.transform import from_origin

    source = tmp_path / "missing-crs.tif"
    with rasterio.open(source, "w", driver="GTiff", height=2, width=2, count=1, dtype="uint8", transform=from_origin(1000, 2000, 10, 10)) as dataset:
        dataset.write(np.array([[1, 2], [3, 4]], dtype="uint8"), 1)
    declarations = {"declared_crs": json.dumps({"kind": "EPSG", "code": 3857})} if declared else {}
    response = _upload(client, "缺少坐标系.TIF", source.read_bytes(), **declarations)
    assert response.status_code == 201, response.text
    artifact = response.json()
    assert artifact["crs"] is None
    assert artifact["schema"]["inspection"]["crs_status"] == "missing"
    result = handle_io_load_raster(file=_envelope(client, artifact))
    if not declared:
        with pytest.raises(AwaitingInputDiagnostic, match="CRS_REQUIRED"):
            _compile(client, artifact, "io.load_raster")
        assert result["status"] == "error"
        assert "CRS_REQUIRED" in result["message"]
    else:
        assert result["status"] == "success", result
        with MemoryFile(result["data"]) as memory, memory.open() as dataset:
            assert dataset.crs.to_epsg() == 3857
            assert dataset.read(1).tolist() == [[1, 2], [3, 4]]
        assert client.get(artifact["content_url"]).content == source.read_bytes()


def test_nonexistent_declared_epsg_is_rejected_before_publication(client, tmp_path):
    response = _upload(client, "待声明.zip", _shapefile_zip(tmp_path, with_crs=False), declared_crs=json.dumps({"kind": "EPSG", "code": 999999999}))
    assert response.status_code == 422


def test_gcj02_declaration_uses_explicit_datum_marker_not_crs_equivalence(client, tmp_path):
    response = _upload(client, "火星坐标.zip", _shapefile_zip(tmp_path, with_crs=False), declared_crs=json.dumps({"kind": "GCJ02"}))
    assert response.status_code == 201, response.text
    artifact = response.json()
    loaded = handle_io_load_vector(file=_envelope(client, artifact))
    assert loaded["status"] == "success", loaded
    assert loaded["data"]["_crs_label"] == "GCJ02"
    assert _compile(client, artifact).tasks[0].outputs[0].crs == {"kind": "GCJ02"}


@pytest.mark.parametrize("link", ['<NetworkLink><Link><href>https://example.com/a.kml</href></Link></NetworkLink>', '<Style><IconStyle><Icon><href>file:///C:/secret.png</href></Icon></IconStyle></Style>', '<SchemaData schemaUrl="../../private.xml"/>'])
def test_kml_external_references_are_rejected_before_native_parser(client, link):
    data = ('<kml xmlns="http://www.opengis.net/kml/2.2"><Document>' + link + '<Placemark><Point><coordinates>116.4,39.9</coordinates></Point></Placemark></Document></kml>').encode()
    response = _upload(client, "外部引用.kml", data)
    assert response.status_code == 422


def test_inspection_has_enforced_timeout_and_reaps_child(tmp_path):
    import multiprocessing
    from app.runtime.upload_inspection import inspect_upload

    source = tmp_path / "file.geojson"
    source.write_bytes(_geojson())
    before = {child.pid for child in multiprocessing.active_children()}
    with pytest.raises(ValueError, match="超时"):
        inspect_upload(source, "file.geojson", max_total=1024 * 1024, max_count=10, timeout_seconds=0.01)
    assert {child.pid for child in multiprocessing.active_children()} == before
