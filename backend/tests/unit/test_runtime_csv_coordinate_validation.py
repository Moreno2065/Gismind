"""Coordinate input rejection must be complete, CRS-aware and durable."""

import json
import os

import pytest

from app.runtime.tool_handlers import handle_io_csv_to_points
from app.tools.data_io import DataIO


@pytest.mark.parametrize("x,y,code", [(181, 32, "COORDINATE_OUT_OF_RANGE"), (118, 91, "COORDINATE_OUT_OF_RANGE"), (-181, 32, "COORDINATE_OUT_OF_RANGE"), (118, -91, "COORDINATE_OUT_OF_RANGE"), (float("inf"), 32, "INVALID_COORDINATE"), (118, float("-inf"), "INVALID_COORDINATE"), (float("nan"), 32, "INVALID_COORDINATE"), (None, 32, "INVALID_COORDINATE"), ("invalid", 32, "INVALID_COORDINATE"), (True, 32, "INVALID_COORDINATE")])
def test_invalid_coordinate_rejects_entire_table_without_dropping_rows(x, y, code):
    rows = [{"x": 118, "y": 32, "id": 1}, {"x": x, "y": y, "id": 2}]
    raw = DataIO().csv_to_points({"rows": rows, "row_count": 2}, x_field="x", y_field="y", crs="EPSG:4326")
    assert raw["status"] == "error"
    assert raw["error_code"] == code
    assert raw["invalid_row_positions"] == [1]
    assert "data" not in raw
    result = handle_io_csv_to_points(input={"rows": rows, "row_count": 2}, x_col="x", y_col="y", crs="EPSG:4326")
    assert result["status"] == "error"
    assert result["error_code"] == code
    assert code in result["message"]


@pytest.mark.parametrize("crs,x,y", [("EPSG:3857", 13000000, 3700000), ("EPSG:32650", 500000, 3541000), ("EPSG:4326", 180, 90), ("EPSG:4326", -180, -90)])
def test_valid_coordinates_respect_crs_units_and_keep_every_row(crs, x, y):
    result = handle_io_csv_to_points(input={"rows": [{"x": x, "y": y, "id": 7}], "row_count": 1}, x_col="x", y_col="y", crs=crs)
    assert result["status"] == "success", result
    assert len(result["data"]["features"]) == 1
    assert result["data"]["features"][0]["geometry"]["coordinates"] == [float(x), float(y)]
    assert result["data"]["features"][0]["properties"]["id"] == 7


def test_invalid_coordinate_error_code_survives_isolated_worker(tmp_path):
    from app.runtime import ArtifactRef, ArtifactStore, Compiler, PlannerDraft, PlannerStepDraft, RuntimeDB, RuntimeStore, RuntimeSupervisor

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    table = {"rows": [{"x": 118, "y": 100}], "row_count": 1, "columns": ["x", "y"]}
    artifact = artifacts.publish_staged(artifacts.stage_bytes(json.dumps(table).encode()), origin="upload", artifact_type="Table", schema={})
    draft = PlannerDraft(run_id="r1", tasks=[PlannerStepDraft(task_key="points", op="io.csv_to_points", inputs={"input": ArtifactRef(artifact_id=artifact["artifact_id"], type="Table")}, params={"x_col": "x", "y_col": "y", "crs": "EPSG:4326"})])
    plan = Compiler(artifact_lookup=runtime.get_artifact).compile(draft)
    runtime.create_plan("p1", "r1", plan=plan)
    runtime.create_task("points", "p1", "points", tool_name="io.csv_to_points")
    with pytest.raises(RuntimeError, match="COORDINATE_OUT_OF_RANGE"):
        RuntimeSupervisor(runtime, artifacts, isolate=True).run_once("csv-worker", run_id="r1")
    attempt = runtime._read(lambda conn: dict(conn.execute("SELECT * FROM task_attempts WHERE task_id='points'").fetchone()))
    assert attempt["status"] == "failed"
    assert attempt["error_code"] == "COORDINATE_OUT_OF_RANGE"
    assert json.loads(attempt["executor_identity"])["pid"] != os.getpid()
    assert not runtime._read(lambda conn: conn.execute("SELECT * FROM task_outputs WHERE task_id='points'").fetchall())
