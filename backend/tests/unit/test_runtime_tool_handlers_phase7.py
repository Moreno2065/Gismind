"""Runtime-owned deterministic tool coverage for the phase-seven cutover."""

import json


def test_vector_buffer_is_runtime_owned():
    from app.runtime.tool_handlers import handle_vector_buffer

    result = handle_vector_buffer(
        vector={"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [116.4, 39.9]}}]},
        distance_m=100,
    )
    assert result["status"] == "success"
    assert result["data"]["features"][0]["geometry"]["type"] == "Polygon"


def test_csv_bytes_are_read_without_external_cache():
    from app.runtime.tool_handlers import handle_io_load_csv, handle_io_csv_to_points

    table = handle_io_load_csv(file={"data": b"lon,lat,name\n1,2,a\n"})
    assert table["status"] == "success"
    assert table["data"]["row_count"] == 1
    points = handle_io_csv_to_points(input=table["data"], x_col="lon", y_col="lat")
    assert points["status"] == "success"
    assert points["data"]["features"][0]["geometry"]["coordinates"] == [1.0, 2.0]


def test_vector_loader_rejects_declared_crs_that_disagrees_with_file_metadata(tmp_path):
    from app.runtime.tool_handlers import handle_io_load_vector

    source = tmp_path / "points.geojson"
    source.write_text(json.dumps({
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
        "features": [{"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [1, 2]}}],
    }), encoding="utf-8")
    result = handle_io_load_vector(file={
        "path": str(source),
        "schema": {"file": {"declared_crs": {"value": {"kind": "EPSG", "code": 3857}, "source": "user"}}},
    })
    assert result["status"] == "error"
    assert "DECLARED_METADATA_MISMATCH" in result["message"]


def test_vector_loader_rejects_declared_bbox_that_disagrees_with_file_bounds(tmp_path):
    from app.runtime.tool_handlers import handle_io_load_vector

    source = tmp_path / "points.geojson"
    source.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [1, 2]}}],
    }), encoding="utf-8")
    result = handle_io_load_vector(file={
        "path": str(source),
        "schema": {"file": {"declared_bbox": {"value": [0, 0, 3, 3], "source": "user"}}},
    })
    assert result["status"] == "error"
    assert "DECLARED_METADATA_MISMATCH" in result["message"]


def test_code_task_returns_explicit_result_from_restricted_process():
    from app.runtime.tool_handlers import handle_code_execute

    result = handle_code_execute(source_code="x = 2\n__result__ = {'value': x + 1}")
    assert result["status"] == "success"
    assert result["data"] == {"value": 3}


def test_code_task_binds_declared_file_artifacts_without_exposing_host_paths():
    from app.runtime.tool_handlers import handle_code_execute

    result = handle_code_execute(
        source_code="__result__ = {'value': inputs['document'].decode(), 'id': artifact_metadata['document']['artifact_id']}",
        input=[{"artifact_id": "art_document", "content_hash": "b" * 64, "type": "File", "data": b"hello"}],
        declared_inputs=["document"],
    )

    assert result["status"] == "success"
    assert result["data"] == {"value": "hello", "id": "art_document"}


def test_raster_slope_uses_runtime_raster_handler(tmp_path):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from app.runtime.tool_handlers import handle_raster_slope

    source = tmp_path / "dem.tif"
    with rasterio.open(source, "w", driver="GTiff", height=3, width=3, count=1, dtype="float32", crs="EPSG:4326", transform=from_origin(0, 3, 1, 1)) as dataset:
        dataset.write(np.arange(9, dtype="float32").reshape(1, 3, 3))
    result = handle_raster_slope(input={"path": str(source)})
    assert result["status"] == "success"
    assert isinstance(result["data"], bytes)


def test_poi_handler_uses_runtime_provider_factory(monkeypatch):
    import app.runtime.tool_handlers as handlers

    class Provider:
        def search_poi_tool(self, query, location, radius):
            return {"status": "success", "source": "deterministic", "data": {"pois": [{"name": query, "location": list(location)}]}}

    monkeypatch.setattr(handlers, "get_poi_query_instance", lambda: Provider())
    result = handlers.handle_poi_search(query="school", location=[1, 2], radius=100)
    assert result == {"status": "success", "data": {"pois": [{"name": "school", "location": [1.0, 2.0]}]}, "source": "deterministic"}


def test_map_build_returns_the_maplayer_artifact_shape_consumed_by_the_frontend():
    from app.runtime.tool_handlers import handle_map_build

    result = handle_map_build(
        vector={
            "crs": {"kind": "EPSG", "code": 4326},
            "data": {
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature", "properties": {"name": "a"}, "geometry": {"type": "Point", "coordinates": [116.4, 39.9]}},
                    {"type": "Feature", "properties": {"name": "b"}, "geometry": {"type": "Point", "coordinates": [116.6, 40.1]}},
                ],
            },
        }
    )

    assert result["status"] == "success"
    assert result["data"]["bbox"] == [116.4, 39.9, 116.6, 40.1]
    assert result["data"]["crs"] == {"kind": "EPSG", "code": 4326}
    assert result["data"]["layers"] == [{
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"name": "a"}, "geometry": {"type": "Point", "coordinates": [116.4, 39.9]}},
            {"type": "Feature", "properties": {"name": "b"}, "geometry": {"type": "Point", "coordinates": [116.6, 40.1]}},
        ],
    }]
