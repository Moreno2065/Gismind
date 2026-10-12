"""Raster ToolContracts expose the actual typed GIS operation signatures."""

from app.runtime import ToolContractRegistry
from app.runtime.tool_handlers import (
    handle_raster_calculate,
    handle_raster_reproject_many,
    handle_raster_sample,
    handle_raster_vectorize,
    handle_raster_tri,
    handle_raster_tpi,
    handle_raster_roughness,
    handle_raster_zonal_statistics,
)

import numpy as np
import rasterio
from rasterio.transform import from_origin


def test_raster_multi_input_and_conversion_contracts_have_real_ports_and_result_types():
    registry = ToolContractRegistry.with_builtins()

    clip = registry.get("raster.clip_mask")
    zonal = registry.get("raster.zonal_statistics")
    sample = registry.get("raster.sample")
    vectorize = registry.get("raster.vectorize")
    polygonize = registry.get("raster.polygonize")
    calculate = registry.get("raster.calculate")

    assert set(clip.input_ports) == {"raster", "mask"}
    assert clip.input_ports["raster"].artifact_type == "Raster"
    assert clip.input_ports["mask"].artifact_type == "Vector"
    assert set(zonal.input_ports) == {"raster", "zones"}
    assert zonal.output_ports["table"].artifact_type == "Table"
    assert set(sample.input_ports) == {"raster", "points"}
    assert sample.output_ports["table"].artifact_type == "Table"
    assert vectorize.input_ports["vector"].artifact_type == "Vector"
    assert vectorize.output_ports["raster"].artifact_type == "Raster"
    assert {"out_shape", "transform", "crs"} <= set(vectorize.parameter_schema["required"])
    assert polygonize.input_ports["raster"].artifact_type == "Raster"
    assert polygonize.output_ports["vector"].artifact_type == "Vector"
    assert set(calculate.input_ports) == {"raster"}
    assert list(calculate.parameter_schema["required"]) == ["expression"]
    assert registry.get("raster.contour").output_ports["vector"].artifact_type == "Vector"
    assert registry.get("vector.check_validity").output_ports["result"].artifact_type == "Table"


def test_raster_sample_and_zonal_handlers_consume_named_vector_ports(tmp_path):
    raster_path = tmp_path / "values.tif"
    with rasterio.open(
        raster_path, "w", driver="GTiff", height=2, width=2, count=1,
        dtype="float32", crs="EPSG:4326", transform=from_origin(0, 2, 1, 1),
    ) as dataset:
        dataset.write(np.array([[1, 2], [3, 4]], dtype="float32"), 1)
    points = {
        "data": {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [0.5, 1.5]}}],
        },
        "crs": {"kind": "EPSG", "code": 4326},
    }
    zones = {
        "data": {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {"name": "all"}, "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]]}}],
        },
        "crs": {"kind": "EPSG", "code": 4326},
    }

    sampled = handle_raster_sample(raster={"path": str(raster_path)}, points=points)
    zonal = handle_raster_zonal_statistics(raster={"path": str(raster_path)}, zones=zones, stats=["mean"])

    assert sampled["status"] == "success"
    assert sampled["data"][0]["value"] == 1
    assert zonal["status"] == "success"
    assert zonal["data"][0]["mean"] == 2.5


def test_raster_sample_reprojects_vector_coordinates_to_raster_crs(tmp_path):
    raster_path = tmp_path / "projected.tif"
    with rasterio.open(
        raster_path, "w", driver="GTiff", height=2, width=2, count=1,
        dtype="float32", crs="EPSG:3857", transform=from_origin(0, 200000, 100000, 100000),
    ) as dataset:
        dataset.write(np.array([[1, 2], [3, 4]], dtype="float32"), 1)
    # 0.5°E, 1.5°N falls in the upper-left 100 km cell after WGS84 -> 3857.
    points = {
        "data": {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [0.5, 1.5]}}
        ]},
        "crs": {"kind": "WGS84"},
    }
    result = handle_raster_sample(raster={"path": str(raster_path)}, points=points)
    assert result["status"] == "success"
    assert result["data"][0]["value"] == 1


def test_raster_vectorize_reprojects_vector_coordinates_to_declared_output_crs():
    vector = {
        "data": {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {}, "geometry": {
                "type": "Polygon", "coordinates": [[[0.1, 1.0], [0.8, 1.0], [0.8, 1.7], [0.1, 1.7], [0.1, 1.0]]]
            }}
        ]},
        "crs": {"kind": "WGS84"},
    }
    result = handle_raster_vectorize(
        vector=vector,
        out_shape=[2, 2],
        transform=[100000, 0, 0, 0, -100000, 200000],
        crs={"kind": "EPSG", "code": 3857},
    )

    assert result["status"] == "success"
    assert isinstance(result["data"], bytes)
    with rasterio.MemoryFile(result["data"]) as memory:
        with memory.open() as dataset:
            assert dataset.crs.to_epsg() == 3857
            assert int(dataset.read(1).sum()) > 0


def test_raster_calculator_preserves_geotiff_and_applies_expression(tmp_path):
    raster_path = tmp_path / "source.tif"
    with rasterio.open(
        raster_path, "w", driver="GTiff", height=2, width=2, count=1,
        dtype="float32", crs="EPSG:3857", transform=from_origin(0, 2, 1, 1),
    ) as dataset:
        dataset.write(np.array([[1, 2], [3, 4]], dtype="float32"), 1)

    result = handle_raster_calculate(raster={"path": str(raster_path)}, expression="raster * 2")

    assert result["status"] == "success"
    assert isinstance(result["data"], bytes)
    with rasterio.MemoryFile(result["data"]) as memory:
        with memory.open() as dataset:
            assert dataset.crs.to_epsg() == 3857
            assert dataset.read(1).tolist() == [[2.0, 4.0], [6.0, 8.0]]


def test_raster_reproject_many_consumes_many_named_inputs(tmp_path):
    paths = []
    for index in range(2):
        path = tmp_path / f"source-{index}.tif"
        with rasterio.open(
            path, "w", driver="GTiff", height=1, width=1, count=1,
            dtype="float32", crs="EPSG:4326", transform=from_origin(0, 1, 1, 1),
        ) as dataset:
            dataset.write(np.array([[index + 1]], dtype="float32"), 1)
        paths.append({"path": str(path)})

    result = handle_raster_reproject_many(raster=paths, target_crs={"kind": "EPSG", "code": 3857})

    assert result["status"] == "success"
    assert isinstance(result["data"], list) and len(result["data"]) == 2
    with rasterio.MemoryFile(result["data"][0]) as memory:
        with memory.open() as dataset:
            assert dataset.crs.to_epsg() == 3857


def test_raster_contour_returns_typed_vector_geojson(tmp_path):
    raster_path = tmp_path / "dem.tif"
    with rasterio.open(
        raster_path, "w", driver="GTiff", height=4, width=4, count=1,
        dtype="float32", crs="EPSG:3857", transform=from_origin(0, 4, 1, 1),
    ) as dataset:
        dataset.write(np.arange(16, dtype="float32").reshape(4, 4), 1)

    from app.runtime.tool_handlers import handle_raster_contour
    result = handle_raster_contour(raster={"path": str(raster_path)}, interval=2)

    assert result["status"] == "success"
    assert result["data"]["type"] == "FeatureCollection"
    assert result["data"]["features"]


def test_raster_terrain_derivatives_return_readable_geotiff_bytes(tmp_path):
    raster_path = tmp_path / "dem.tif"
    with rasterio.open(
        raster_path, "w", driver="GTiff", height=3, width=3, count=1,
        dtype="float32", crs="EPSG:4326", transform=from_origin(0, 3, 1, 1),
    ) as dataset:
        dataset.write(np.arange(9, dtype="float32").reshape(3, 3), 1)

    for handler in (handle_raster_tri, handle_raster_tpi, handle_raster_roughness):
        result = handler(raster={"path": str(raster_path)})
        assert result["status"] == "success"
        assert isinstance(result["data"], bytes)
        with rasterio.MemoryFile(result["data"]) as memory:
            with memory.open() as dataset:
                assert dataset.crs.to_epsg() == 4326
                assert dataset.read(1).shape == (3, 3)
