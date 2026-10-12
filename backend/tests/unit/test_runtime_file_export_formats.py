"""The managed export contract must describe bytes that GIS readers can open."""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.runtime.compiler import CompileError, Compiler
from app.runtime.contracts import ArtifactRef, PlannerDraft, PlannerStepDraft, ToolContractRegistry
from app.runtime.postconditions import validate_file_export


_CRS = {"kind": "EPSG", "code": 4326}
_VECTOR = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [118.7, 32.0]}, "properties": {"name": "南京"}},
    ],
}


def _draft(*, format: str | None = None, user_goal: str = "导出结果文件", artifact_id: str = "source") -> PlannerDraft:
    params = {} if format is None else {"format": format}
    return PlannerDraft(
        run_id="run-export", input_revision=1, user_goal=user_goal, origin="test",
        tasks=[PlannerStepDraft(
            task_key="export", op="file.export",
            inputs={"input": ArtifactRef(artifact_id=artifact_id, type="Vector", crs=_CRS)},
            params=params,
        )],
    )


def test_compiler_binds_export_format_and_immutable_file_metadata():
    compiler = Compiler(ToolContractRegistry.with_builtins())
    for format, extension, media_type in (
        ("geojson", ".geojson", "application/geo+json"),
        ("gpkg", ".gpkg", "application/geopackage+sqlite3"),
    ):
        task = compiler.compile(_draft(format=format)).tasks[0]
        assert task.params["format"] == format
        assert task.outputs[0].schema["file"] == {
            "format": format,
            "filename": f"export-file{extension}",
            "extension": extension,
            "media_type": media_type,
            "download_disposition": "attachment",
        }


def test_unspecified_export_is_explicit_geojson_and_unsupported_csv_fails_closed():
    compiler = Compiler(ToolContractRegistry.with_builtins())
    assert compiler.compile(_draft()).tasks[0].params["format"] == "geojson"
    with pytest.raises(CompileError) as exc:
        compiler.compile(_draft(format="csv"))
    assert exc.value.code == "INVALID_PARAMETER"


def test_trusted_explicit_request_format_cannot_be_changed_by_draft():
    compiler = Compiler(ToolContractRegistry.with_builtins(), policy={"trusted_user_goal": "请导出为 GeoPackage 文件"})
    with pytest.raises(CompileError) as exc:
        compiler.compile(_draft(format="geojson", user_goal="导出结果文件"))
    assert exc.value.code == "INVALID_PARAMETER"
    assert compiler.compile(_draft(format="gpkg", user_goal="导出结果文件")).tasks[0].params["format"] == "gpkg"
    with pytest.raises(CompileError) as exc:
        compiler.compile(_draft(user_goal="导出结果文件"))
    assert exc.value.code == "INVALID_PARAMETER"


def test_trusted_unsupported_csv_request_does_not_fall_back_to_geojson():
    compiler = Compiler(ToolContractRegistry.with_builtins(), policy={"trusted_user_goal": "将分析结果导出为 CSV 文件"})
    with pytest.raises(CompileError) as exc:
        compiler.compile(_draft(user_goal="导出结果文件"))
    assert exc.value.code == "INVALID_PARAMETER"
    with pytest.raises(CompileError) as exc:
        compiler.compile(_draft(format="gpkg", user_goal="导出结果文件"))
    assert exc.value.code == "INVALID_PARAMETER"


def test_input_filename_is_not_treated_as_requested_export_format():
    compiler = Compiler(ToolContractRegistry.with_builtins(), policy={"trusted_user_goal": "读取 shops.csv，然后导出为 GPKG 文件"})
    assert compiler.compile(_draft(format="gpkg")).tasks[0].params["format"] == "gpkg"


@pytest.mark.parametrize("goal", ["请导出为 GPKG 文件", "将结果导出为 CSV 文件"])
def test_explicit_requested_export_cannot_be_satisfied_by_omitting_export_task(goal):
    compiler = Compiler(ToolContractRegistry.with_builtins(), policy={"trusted_user_goal": goal})
    draft = PlannerDraft(
        run_id="run-export", input_revision=1, user_goal="只要缓冲区", origin="test",
        tasks=[PlannerStepDraft(
            task_key="buffer", op="vector.buffer",
            inputs={"vector": ArtifactRef(artifact_id="source", type="Vector", crs=_CRS)},
            params={"distance_m": 50},
        )],
    )
    with pytest.raises(CompileError) as exc:
        compiler.compile(draft)
    assert exc.value.code == "INVALID_PARAMETER"


def test_source_geojson_in_conversion_goal_is_not_mistaken_for_target_format():
    compiler = Compiler(ToolContractRegistry.with_builtins(), policy={"trusted_user_goal": "把上传的 GeoJSON 转换为 GPKG 并导出"})
    assert compiler.compile(_draft(format="gpkg")).tasks[0].params["format"] == "gpkg"


@pytest.mark.parametrize("goal", ["导出为 DXF 文件", "导出为 .dxf 文件", "导出为 PNG 图片", "导出为 DXF", "export as DXF"])
def test_unlisted_explicit_target_format_fails_closed(goal):
    compiler = Compiler(ToolContractRegistry.with_builtins(), policy={"trusted_user_goal": goal})
    with pytest.raises(CompileError) as exc:
        compiler.compile(_draft(format="geojson"))
    assert exc.value.code == "INVALID_PARAMETER"


def test_export_serializer_and_postcondition_validate_actual_geojson():
    from app.runtime.contracts import _handle_file_export

    source = {"data": _VECTOR, "crs": _CRS, "type": "Vector"}
    result = _handle_file_export(input=source, format="geojson")
    payload = json.loads(result["data"].decode("utf-8"))
    assert payload["type"] == "FeatureCollection"
    assert payload["features"][0]["properties"]["name"] == "南京"
    assert validate_file_export(result, {"input": source}, {"format": "geojson"}) is True
    assert validate_file_export({"status": "success", "data": b"{}"}, {"input": source}, {"format": "geojson"}) is False


def test_export_serializer_and_postcondition_validate_real_gpkg(tmp_path):
    from app.runtime.contracts import _handle_file_export

    source = {"data": _VECTOR, "crs": _CRS, "type": "Vector"}
    result = _handle_file_export(input=source, format="gpkg")
    payload = result["data"]
    assert payload.startswith(b"SQLite format 3\x00")
    path = tmp_path / "out.gpkg"
    path.write_bytes(payload)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA application_id").fetchone()[0] == 0x47504B47
        assert db.execute("SELECT COUNT(*) FROM gpkg_contents").fetchone()[0] == 1
    import geopandas as gpd
    frame = gpd.read_file(path)
    assert frame.crs.to_epsg() == 4326
    assert frame.loc[0, "name"] == "南京"
    assert validate_file_export(result, {"input": source}, {"format": "gpkg"}) is True
    assert validate_file_export({"status": "success", "data": b"SQLite format 3\x00not a gpkg"}, {"input": source}, {"format": "gpkg"}) is False


@pytest.mark.parametrize("format_name", ["geojson", "gpkg"])
@pytest.mark.parametrize("corruption", ["coordinate", "property", "swapped_properties"])
def test_export_postcondition_rejects_same_count_record_corruption(tmp_path, format_name, corruption):
    import geopandas as gpd
    from shapely.geometry import Point

    from app.runtime.contracts import _handle_file_export

    source = {
        "type": "Vector", "crs": _CRS,
        "data": {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"record_id": 1, "name": "东"},
             "geometry": {"type": "Point", "coordinates": [118.7, 32.0]}},
            {"type": "Feature", "properties": {"record_id": 2, "name": "西"},
             "geometry": {"type": "Point", "coordinates": [118.8, 32.1]}},
        ]},
    }
    original = _handle_file_export(input=source, format=format_name)
    assert original["status"] == "success"
    source_path = tmp_path / f"original.{format_name}"
    source_path.write_bytes(original["data"])
    frame = gpd.read_file(source_path)
    if corruption == "coordinate":
        frame.at[0, "geometry"] = Point(119.7, 32.0)
    elif corruption == "property":
        frame.at[0, "name"] = "被改写"
    else:
        first_name = frame.at[0, "name"]
        frame.at[0, "name"] = frame.at[1, "name"]
        frame.at[1, "name"] = first_name
    if format_name == "geojson":
        changed_bytes = frame.to_json(drop_id=True).encode("utf-8")
    else:
        changed_path = tmp_path / "changed.gpkg"
        frame.to_file(changed_path, driver="GPKG", layer="result", index=False)
        changed_bytes = changed_path.read_bytes()
    assert validate_file_export(original, {"input": source}, {"format": format_name}) is True
    assert validate_file_export(
        {"status": "success", "data": changed_bytes}, {"input": source}, {"format": format_name}
    ) is False


def test_projected_source_geojson_export_has_wgs84_coordinates_and_bound_records(tmp_path):
    import geopandas as gpd
    from pyproj import Transformer

    from app.runtime.contracts import _handle_file_export

    points = [(1, "东", 13210000.0, 3760000.0), (2, "西", 13220000.0, 3770000.0)]
    source = {"type": "Vector", "crs": {"kind": "EPSG", "code": 3857}, "data": {
        "type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"record_id": record_id, "name": name},
             "geometry": {"type": "Point", "coordinates": [x, y]}}
            for record_id, name, x, y in points
        ],
    }}
    result = _handle_file_export(input=source, format="geojson")
    assert result["status"] == "success", result
    path = tmp_path / "projected.geojson"
    path.write_bytes(result["data"])
    frame = gpd.read_file(path)
    assert frame.crs.to_epsg() == 4326
    transformer = Transformer.from_crs(3857, 4326, always_xy=True)
    for record_id, name, x, y in points:
        row = frame.loc[frame["record_id"] == record_id].iloc[0]
        expected_x, expected_y = transformer.transform(x, y)
        assert row["name"] == name
        assert row.geometry.x == pytest.approx(expected_x, abs=1e-8)
        assert row.geometry.y == pytest.approx(expected_y, abs=1e-8)
    assert validate_file_export(result, {"input": source}, {"format": "geojson"}) is True
    mislabeled = {"type": "FeatureCollection", "features": source["data"]["features"]}
    assert validate_file_export(
        {"status": "success", "data": json.dumps(mislabeled).encode()}, {"input": source}, {"format": "geojson"}
    ) is False


@pytest.mark.parametrize("format_name", ["geojson", "gpkg"])
def test_export_postcondition_accepts_nullable_fields_and_changed_feature_order(tmp_path, format_name):
    import geopandas as gpd

    from app.runtime.contracts import _handle_file_export

    source = {"type": "Vector", "crs": _CRS, "data": {
        "type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"record_id": 1, "name": None},
             "geometry": {"type": "Point", "coordinates": [118.7, 32.0]}},
            {"type": "Feature", "properties": {"record_id": 2, "name": "西"},
             "geometry": {"type": "Point", "coordinates": [118.8, 32.1]}},
        ],
    }}
    result = _handle_file_export(input=source, format=format_name)
    assert result["status"] == "success", result
    assert validate_file_export(result, {"input": source}, {"format": format_name}) is True
    if format_name == "geojson":
        payload = json.loads(result["data"])
        payload["features"].reverse()
        reordered = json.dumps(payload).encode("utf-8")
    else:
        source_path = tmp_path / "source.gpkg"
        reordered_path = tmp_path / "reordered.gpkg"
        source_path.write_bytes(result["data"])
        frame = gpd.read_file(source_path).iloc[::-1].reset_index(drop=True)
        frame.to_file(reordered_path, driver="GPKG", layer="result", index=False)
        reordered = reordered_path.read_bytes()
    assert validate_file_export(
        {"status": "success", "data": reordered}, {"input": source}, {"format": format_name}
    ) is True


def test_empty_export_uses_defined_geojson_and_rejects_undefined_gpkg_geometry():
    from app.runtime.contracts import _handle_file_export

    source = {"type": "Vector", "crs": _CRS, "data": {"type": "FeatureCollection", "features": []}}
    geojson = _handle_file_export(input=source, format="geojson")
    assert geojson["status"] == "success", geojson
    assert json.loads(geojson["data"]) == {"type": "FeatureCollection", "features": []}
    assert validate_file_export(geojson, {"input": source}, {"format": "geojson"}) is True

    gpkg = _handle_file_export(input=source, format="gpkg")
    assert gpkg["status"] == "error"
    assert "geometry schema" in gpkg["message"]


def test_export_rejects_unrepresentable_gcj02_geopackage():
    from app.runtime.contracts import _handle_file_export

    result = _handle_file_export(input={"data": _VECTOR, "crs": {"kind": "GCJ02"}, "type": "Vector"}, format="gpkg")
    assert result["status"] == "error"


@pytest.mark.parametrize("format,extension", [("geojson", ".geojson"), ("gpkg", ".gpkg")])
def test_export_task_publishes_true_file_bytes_and_safe_download_headers(tmp_path, format, extension):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.runtime import ArtifactStore, RuntimeDB, RuntimeStore, RuntimeSupervisor

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("session-export")
    runtime.create_run("run-export", "session-export")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    source = artifacts.publish_staged(
        artifacts.stage_bytes(json.dumps(_VECTOR).encode("utf-8"), filename="source.geojson"),
        origin="upload", artifact_type="Vector", crs=_CRS, schema={},
    )
    draft = _draft(format=format, artifact_id=source["artifact_id"])
    registry = ToolContractRegistry.with_builtins()
    plan = Compiler(registry, artifact_lookup=runtime.get_artifact).compile(draft)
    runtime.create_plan("plan-export", "run-export", plan=plan)
    runtime.create_task("export", "plan-export", "export", tool_name="file.export")
    supervisor = RuntimeSupervisor(runtime, artifacts, registry=registry, isolate=False, autostart=False)
    assert supervisor.worker_loop("worker-export", run_id="run-export", max_tasks=1, poll_interval=0.001) == 1
    assert runtime._read(lambda db: db.execute("SELECT status FROM tasks WHERE task_id='export'").fetchone()[0]) == "succeeded"
    artifact_id = runtime._read(lambda db: db.execute("SELECT artifact_id FROM task_outputs WHERE task_id='export'").fetchone()[0])
    output = runtime.get_artifact(artifact_id)
    assert output["schema"]["file"]["filename"] == f"export-file{extension}"
    assert output["media_type"] == output["schema"]["file"]["media_type"]
    payload = artifacts.path_for(artifact_id).read_bytes()
    if format == "gpkg":
        assert payload.startswith(b"SQLite format 3\x00")
    else:
        assert json.loads(payload)["type"] == "FeatureCollection"
    with TestClient(create_app(runtime_store=runtime, artifact_store=artifacts, runtime_supervisor=supervisor)) as client:
        for headers, status in (({}, 200), ({"Range": "bytes=0-12"}, 206)):
            response = client.get(f"/api/artifacts/{artifact_id}/content", headers=headers)
            assert response.status_code == status
            assert response.headers["content-type"] == "application/octet-stream"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["content-disposition"] == f'attachment; filename="export-file{extension}"'
