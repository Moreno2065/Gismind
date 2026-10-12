"""Artifact type labels must be proved by their physical payloads."""

from __future__ import annotations

import json

import pytest

from app.runtime import ArtifactStore, RuntimeDB, RuntimeStore


def _store(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    return ArtifactStore(tmp_path / "data", runtime)


def _publish(store, payload, artifact_type, *, crs=None):
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=True).encode("utf-8")
    return store.publish_staged(
        store.stage_bytes(encoded, filename=f"{artifact_type.lower()}.json"),
        origin="upload", artifact_type=artifact_type, crs=crs,
    )


def test_vector_requires_a_real_geojson_feature_collection(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="Vector"):
        _publish(store, {"not": "vector"}, "Vector", crs={"kind": "EPSG", "code": 4326})

    artifact = _publish(
        store,
        {"type": "FeatureCollection", "features": [{
            "type": "Feature", "properties": {"name": "A"},
            "geometry": {"type": "Point", "coordinates": [118.78, 32.04]},
        }]},
        "Vector", crs={"kind": "EPSG", "code": 4326},
    )
    assert artifact["validation_status"] == "valid"


def test_vector_geoparquet_rejects_declared_crs_that_disagrees_with_file(tmp_path):
    import geopandas as gpd
    from shapely.geometry import Point

    store = _store(tmp_path)
    path = tmp_path / "source.parquet"
    gpd.GeoDataFrame({"name": ["p"]}, geometry=[Point(1, 2)], crs="EPSG:3857").to_parquet(path, index=False)

    with pytest.raises(ValueError, match="CRS"):
        store.publish_staged(
            store.stage_bytes(path.read_bytes(), filename="source.parquet"),
            origin="upload", artifact_type="Vector", crs={"kind": "EPSG", "code": 4326}, schema={},
        )


def test_coordinate_rejects_non_finite_or_out_of_range_geographic_values(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="Coordinate"):
        _publish(store, {"location": [float("nan"), 32.04]}, "Coordinate", crs={"kind": "WGS84"})
    with pytest.raises(ValueError, match="Coordinate"):
        _publish(store, {"location": [218.78, 32.04]}, "Coordinate", crs={"kind": "WGS84"})

    assert _publish(
        store, {"location": [118.78, 32.04]}, "Coordinate", crs={"kind": "WGS84"},
    )["validation_status"] == "valid"


def test_table_requires_records_or_a_mapping_not_a_scalar(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="Table"):
        _publish(store, "not-a-table", "Table")
    assert _publish(store, [{"name": "A", "value": 1}], "Table")["validation_status"] == "valid"


def test_poiset_requires_rows_with_finite_geographic_locations(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="POISet"):
        _publish(store, [{"name": "A"}], "POISet", crs={"kind": "GCJ02"})
    assert _publish(
        store, [{"name": "A", "location": [118.78, 32.04]}],
        "POISet", crs={"kind": "GCJ02"},
    )["validation_status"] == "valid"
