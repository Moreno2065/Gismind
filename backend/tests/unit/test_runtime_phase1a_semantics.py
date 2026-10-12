"""Phase 1A semantic regressions for Runtime-owned GIS execution."""

import json

import pytest


def test_load_csv_to_points_reconciles_full_rows_not_preview(tmp_path):
    from app.runtime.tool_handlers import handle_io_csv_to_points, handle_io_load_csv

    source = tmp_path / "points.csv"
    source.write_text(
        "lon,lat,name\n" + "".join(f"{100 + i / 100},{30 + i / 100},p{i}\n" for i in range(12)),
        encoding="utf-8",
    )

    table = handle_io_load_csv(file={"path": str(source)})
    assert table["status"] == "success"
    assert table["data"]["row_count"] == 12
    assert len(table["data"]["sample"]) == 5

    points = handle_io_csv_to_points(input=table["data"], x_col="lon", y_col="lat")

    assert points["status"] == "success"
    assert len(points["data"]["features"]) == 12
    assert points["data"]["features"][-1]["properties"]["name"] == "p11"


def test_executor_distinguishes_table_row_list_from_artifact_batch():
    from app.runtime.contracts import OutputSpec
    from app.runtime.executor import RuntimeExecutor
    from app.runtime.output_protocol import ArtifactBatch

    table_output = OutputSpec(port="table", artifact_type="Table")
    rows = [{"value": 1}, {"value": 2}]
    assert RuntimeExecutor._output_items(table_output, rows) == [rows]

    many_output = OutputSpec(port="vectors", artifact_type="Vector", cardinality="many", min_items=0, max_items=None)
    with pytest.raises(ValueError, match="ArtifactBatch"):
        RuntimeExecutor._output_items(many_output, [{"type": "FeatureCollection", "features": []}])
    assert RuntimeExecutor._output_items(many_output, ArtifactBatch(["a", "b"])) == ["a", "b"]


def test_vector_buffer_payload_crs_matches_inherited_wgs84_metadata(tmp_path):
    from app.runtime import ArtifactStore, Compiler, PlannerDraft, PlannerStepDraft, RuntimeDB, RuntimeStore, RuntimeSupervisor, ToolContractRegistry

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    source = artifacts.publish_staged(
        artifacts.stage_bytes(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [116.4, 39.9]}}],
            }).encode("utf-8"),
            filename="source.geojson",
        ),
        origin="upload",
        artifact_type="Vector",
        crs={"kind": "EPSG", "code": 4326},
        schema={},
    )
    draft = PlannerDraft(
        run_id="r1",
        input_revision=1,
        tasks=[PlannerStepDraft(
            task_key="buffer",
            op="vector.buffer",
            inputs={"vector": {"artifact_id": source["artifact_id"], "type": "Vector", "crs": {"kind": "EPSG", "code": 4326}}},
            params={"distance_m": 100},
        )],
    )
    plan = Compiler(ToolContractRegistry.with_builtins(), artifact_lookup=runtime.get_artifact).compile(draft)
    runtime.create_plan("p1", "r1", plan=plan)
    runtime.create_task("buffer", "p1", "buffer", tool_name="vector.buffer")

    result = RuntimeSupervisor(runtime, artifacts).run_once("worker-a", run_id="r1")

    assert result is not None
    output = runtime.get_artifact(result["artifact"]["artifact_id"])
    assert output["crs"] == {"kind": "EPSG", "code": 4326}
    payload = json.loads(artifacts.path_for(output["artifact_id"]).read_text(encoding="utf-8"))
    assert payload.get("_crs_label") in (None, "WGS84")
    assert payload["_physical_crs"] == "EPSG:4326"


def test_explicit_datum_conversion_still_publishes_gcj02_marker():
    from app.runtime.tool_handlers import handle_vector_convert_datum

    result = handle_vector_convert_datum(
        vector={"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [116.4, 39.9]}}]},
        source_crs={"kind": "WGS84"},
        target_crs={"kind": "GCJ02"},
    )

    assert result["status"] == "success"
    assert result["data"]["_crs_label"] == "GCJ02"
    assert result["data"]["_physical_crs"] == "EPSG:4326"
