"""Executable semantic postconditions for public Runtime operations.

Handlers are deliberately allowed to return provider/UI-shaped dictionaries,
but a successful Task may only be published after this module proves that the
payload still has the semantics promised by its ToolContract.  The functions
share the executor's small callable contract::

    check(result=..., inputs=..., params=..., empty_policy=...) -> bool

They return ``False`` instead of raising for malformed provider output.  A
raised exception is reserved for an unavailable validation dependency (for
example a broken Rasterio installation), which the executor records as a
durable Task failure.
"""

from __future__ import annotations

import io
import json
import math
import os
import sqlite3
import tempfile
from collections import defaultdict
from contextlib import closing
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable


def _data(result: Any) -> Any:
    if isinstance(result, dict) and "data" in result:
        return result["data"]
    return None


def _status_ok(result: Any, *, empty_policy: str | None = None) -> bool:
    if not isinstance(result, dict):
        return False
    status = str(result.get("status") or "").lower()
    if status in {"success", "ok"}:
        return "data" in result and result.get("data") is not None
    return status == "empty" and empty_policy == "allow" and "data" in result and result.get("data") is not None


def _unwrap(value: Any) -> Any:
    if isinstance(value, dict) and "data" in value:
        return value["data"]
    return value


def _features(value: Any) -> list[dict[str, Any]] | None:
    value = _unwrap(value)
    if isinstance(value, dict) and isinstance(value.get("features"), list):
        features = value["features"]
        if all(isinstance(item, dict) and item.get("type") == "Feature" for item in features):
            return features
    if isinstance(value, list) and all(isinstance(item, dict) and item.get("type") == "Feature" for item in value):
        return value
    return None


def _geometry_valid(features: list[dict[str, Any]]) -> bool:
    try:
        from shapely.geometry import shape
        for feature in features:
            geometry = feature.get("geometry")
            if not isinstance(geometry, dict):
                return False
            parsed = shape(geometry)
            if parsed.is_empty or not parsed.is_valid:
                return False
        return True
    except Exception:
        return False


def _property_key(value: Any) -> tuple[Any, ...]:
    """Hash JSON properties while keeping booleans distinct from numbers."""
    if isinstance(value, dict):
        return ("object", tuple(sorted((key, _property_key(item)) for key, item in value.items())))
    if isinstance(value, list):
        return ("array", tuple(_property_key(item) for item in value))
    if isinstance(value, bool):
        return ("boolean", value)
    if isinstance(value, (int, float)):
        return ("number", Fraction(value))
    if isinstance(value, str):
        return ("string", value)
    if value is None:
        return ("null",)
    raise ValueError("feature property is not a JSON value")


def _same_coordinate_tree(left: Any, right: Any, tolerance: float) -> bool:
    """Compare every ordinate and dimension, including Z in nested geometries."""
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _same_coordinate_tree(left[key], right[key], tolerance) for key in left
        )
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _same_coordinate_tree(a, b, tolerance) for a, b in zip(left, right)
        )
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isfinite(left) and math.isfinite(right) and abs(left - right) <= tolerance
    return left == right


def _same_geometry(expected: Any, actual: Any, tolerance: float) -> bool:
    return actual.equals_exact(expected, tolerance) and _same_coordinate_tree(
        expected.__geo_interface__, actual.__geo_interface__, tolerance
    )


def _same_records(expected: list[tuple[dict[str, Any], Any]],
                  actual: list[tuple[dict[str, Any], Any]], *, tolerance: float) -> bool:
    """Match records in any order; index the common exact case and spatially bound near matches."""
    if len(expected) != len(actual):
        return False
    if all(_property_key(source_properties) == _property_key(output_properties)
           and _same_geometry(source_geometry, output_geometry, tolerance)
           for (source_properties, source_geometry), (output_properties, output_geometry) in zip(expected, actual)):
        return True

    exact: dict[tuple[Any, bytes], list[int]] = defaultdict(list)
    for index, (properties, geometry) in enumerate(actual):
        exact[(_property_key(properties), geometry.wkb)].append(index)
    used = [False] * len(actual)
    pending: list[tuple[dict[str, Any], Any]] = []
    for properties, geometry in expected:
        indices = exact.get((_property_key(properties), geometry.wkb))
        if indices:
            index = indices.pop()
            used[index] = True
        else:
            pending.append((properties, geometry))
    if not pending:
        return True

    from shapely.geometry import box
    from shapely.strtree import STRtree

    groups: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    for index, (properties, _) in enumerate(actual):
        if not used[index]:
            groups[_property_key(properties)].append(index)
    trees = {key: (indices, STRtree([actual[index][1] for index in indices]))
             for key, indices in groups.items()}
    for properties, geometry in pending:
        group = trees.get(_property_key(properties))
        if group is None:
            return False
        indices, tree = group
        min_x, min_y, max_x, max_y = geometry.bounds
        search = box(min_x - tolerance, min_y - tolerance, max_x + tolerance, max_y + tolerance)
        for local_index in tree.query(search):
            index = indices[int(local_index)]
            if not used[index] and _property_key(actual[index][0]) == _property_key(properties) \
                    and _same_geometry(geometry, actual[index][1], tolerance):
                used[index] = True
                break
        else:
            return False
    return all(used)


def _crs_name(value: Any) -> str:
    if isinstance(value, dict):
        kind = str(value.get("kind") or value.get("name") or "").upper()
        if kind == "EPSG" and isinstance(value.get("code"), int) and not isinstance(value.get("code"), bool):
            return f"EPSG:{value['code']}"
        if kind in {"WGS84", "GCJ02"}:
            return "EPSG:4326" if kind == "WGS84" else kind
    text = str(value or "").strip().upper().replace(" ", "")
    if text in {"WGS84", "EPSG:4326", "4326"}:
        return "EPSG:4326"
    if text == "GCJ02":
        return "GCJ02"
    return text if text.startswith("EPSG:") else ""


def _physical_crs(value: Any) -> str:
    value = _unwrap(value)
    return str(value.get("_physical_crs") or "").upper().replace(" ", "") if isinstance(value, dict) else ""


def _declared_crs(envelope: Any) -> str:
    return _crs_name(envelope.get("crs")) if isinstance(envelope, dict) else ""


def _expected_target(params: dict[str, Any]) -> str:
    return _crs_name(params.get("target_crs"))


def _vector_result(result: Any, *, empty_policy: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    if not _status_ok(result, empty_policy=empty_policy):
        return None
    payload = _data(result)
    features = _features(payload)
    if features is None or not _geometry_valid(features):
        return None
    return features, _unwrap(payload) if isinstance(_unwrap(payload), dict) else {}


def validate_vector_generic(result: Any, inputs: dict[str, Any], params: dict[str, Any], *, empty_policy: str | None = None) -> bool:
    parsed = _vector_result(result, empty_policy=empty_policy)
    return parsed is not None


def validate_vector_buffer(result: Any, inputs: dict[str, Any], params: dict[str, Any], *, empty_policy: str | None = None) -> bool:
    parsed = _vector_result(result, empty_policy=empty_policy)
    if parsed is None:
        return False
    features, payload = parsed
    source = _features(inputs.get("vector") or inputs.get("input"))
    if source is None:
        return False
    # A buffer is one output geometry per source feature (unless the source is
    # genuinely empty), so a handler that silently drops features is invalid.
    if source and len(features) < len(source):
        return False
    expected = params.get("distance_m", params.get("radius_m", params.get("radius")))
    if isinstance(expected, bool) or not isinstance(expected, (int, float)) or not math.isfinite(float(expected)) or expected < 0:
        return False
    for key in ("buffer_distance_m", "distance_m", "radius_m"):
        if key in payload:
            observed = payload[key]
            if isinstance(observed, bool) or not isinstance(observed, (int, float)) or not math.isfinite(float(observed)):
                return False
            if not math.isclose(float(observed), float(expected), rel_tol=1e-9, abs_tol=1e-6):
                return False
    source_crs = _declared_crs(inputs.get("vector") or inputs.get("input"))
    physical = _physical_crs(payload)
    if source_crs == "GCJ02" and physical not in {"", "EPSG:4326"}:
        return False
    if source_crs not in {"", "GCJ02"} and physical and physical != source_crs.replace(" ", ""):
        return False
    return True


def validate_vector_reproject(result: Any, inputs: dict[str, Any], params: dict[str, Any], *, empty_policy: str | None = None) -> bool:
    parsed = _vector_result(result, empty_policy=empty_policy)
    if parsed is None:
        return False
    features, payload = parsed
    target = _expected_target(params)
    physical = _physical_crs(payload)
    if target == "GCJ02":
        if physical not in {"", "EPSG:4326"} or payload.get("_crs_label") != "GCJ02":
            return False
    elif not target or physical != target.replace(" ", ""):
        return False

    source = inputs.get("vector") or inputs.get("input")
    source_features = _features(source)
    source_crs = _crs_name(params.get("source_crs")) or _declared_crs(source)
    if source_features is None or not source_crs or len(features) != len(source_features):
        return False
    try:
        from pyproj import Transformer
        from shapely.geometry import shape
        from shapely.ops import transform

        from app.tools.geo_transform import gcj02_to_wgs84, wgs84_to_gcj02

        def datum_transform(geometry, converter):
            def convert(x, y, z=None):
                longitude, latitude = converter(x, y)
                return (longitude, latitude) if z is None else (longitude, latitude, z)
            return transform(convert, geometry)

        physical_source = "EPSG:4326" if source_crs == "GCJ02" else source_crs
        physical_target = "EPSG:4326" if target == "GCJ02" else target
        projection = None if physical_source == physical_target else Transformer.from_crs(
            physical_source, physical_target, always_xy=True
        ).transform
        actual = [(feature.get("properties") or {}, shape(feature["geometry"])) for feature in features]
        tolerance = 1e-5 if "GCJ02" in {source_crs, target} else 1e-7 if physical_target == "EPSG:4326" else 1e-4
        expected = []
        for feature in source_features:
            properties = feature.get("properties") or {}
            geometry = shape(feature["geometry"])
            if source_crs == "GCJ02" and target != "GCJ02":
                geometry = datum_transform(geometry, gcj02_to_wgs84)
            if projection is not None:
                geometry = transform(projection, geometry)
            if target == "GCJ02" and source_crs != "GCJ02":
                geometry = datum_transform(geometry, wgs84_to_gcj02)
            expected.append((properties, geometry))
        return _same_records(expected, actual, tolerance=tolerance)
    except Exception:
        return False


def validate_vector_reproject_many(result: Any, inputs: dict[str, Any], params: dict[str, Any], *, empty_policy: str | None = None) -> bool:
    if not _status_ok(result, empty_policy=empty_policy):
        return False
    payload = _data(result)
    if not isinstance(payload, list):
        return False
    target = _expected_target(params)
    if not target or len(payload) != len(inputs.get("vector", [])):
        return False
    return all(validate_vector_reproject({"status": "success", "data": item}, {"vector": source}, params)
               for item, source in zip(payload, inputs.get("vector", [])))


def validate_vector_convert_datum(result: Any, inputs: dict[str, Any], params: dict[str, Any], *, empty_policy: str | None = None) -> bool:
    return validate_vector_reproject(result, inputs, params, empty_policy=empty_policy)


def validate_vector_delete_duplicates(result: Any, inputs: dict[str, Any], params: dict[str, Any], *, empty_policy: str | None = None) -> bool:
    parsed = _vector_result(result, empty_policy=empty_policy)
    if parsed is None:
        return False
    features, _ = parsed
    source = _features(inputs.get("input") or inputs.get("vector"))
    if source is None or len(features) > len(source):
        return False
    source_fields = {key for feature in source for key in (feature.get("properties") or {})}
    output_fields = {key for feature in features for key in (feature.get("properties") or {})}
    return source_fields == output_fields


def _raster_bytes(result: Any) -> bytes | None:
    payload = _data(result)
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    if isinstance(payload, dict):
        for key in ("data", "path", "dst_path", "raster_path"):
            value = payload.get(key)
            if isinstance(value, (bytes, bytearray)):
                return bytes(value)
            if isinstance(value, (str, Path)) and Path(value).is_file():
                try:
                    return Path(value).read_bytes()
                except OSError:
                    return None
    return None


def _raster_profile(result: Any):
    raw = _raster_bytes(result)
    if not raw:
        return None
    try:
        import rasterio
        with rasterio.MemoryFile(raw) as memory:
            with memory.open() as dataset:
                if dataset.count < 1 or dataset.width < 1 or dataset.height < 1 or dataset.crs is None:
                    return None
                if dataset.transform is None or not all(math.isfinite(float(item)) for item in dataset.transform):
                    return None
                return dataset.crs.to_string().upper().replace(" ", ""), dataset.width, dataset.height
    except Exception:
        return None


def validate_raster(result: Any, inputs: dict[str, Any], params: dict[str, Any], *, empty_policy: str | None = None) -> bool:
    return _status_ok(result, empty_policy=empty_policy) and _raster_profile(result) is not None


def validate_raster_reproject(result: Any, inputs: dict[str, Any], params: dict[str, Any], *, empty_policy: str | None = None) -> bool:
    profile = _raster_profile(result)
    target = _expected_target(params)
    return profile is not None and bool(target) and profile[0] == target.replace(" ", "")


def validate_raster_reproject_many(result: Any, inputs: dict[str, Any], params: dict[str, Any], *, empty_policy: str | None = None) -> bool:
    if not _status_ok(result, empty_policy=empty_policy):
        return False
    payload = _data(result)
    if not isinstance(payload, list) or len(payload) != len(inputs.get("raster", [])):
        return False
    return all(validate_raster_reproject({"status": "success", "data": item}, {}, params) for item in payload)


def validate_raster_slope(result: Any, inputs: dict[str, Any], params: dict[str, Any], *, empty_policy: str | None = None) -> bool:
    profile = _raster_profile(result)
    if profile is None:
        return False
    source = inputs.get("raster") or inputs.get("input")
    declared = _declared_crs(source)
    return not declared or declared == profile[0]


def validate_coordinate(result: Any, inputs: dict[str, Any], params: dict[str, Any], *, empty_policy: str | None = None) -> bool:
    if not _status_ok(result, empty_policy=empty_policy):
        return False
    payload = _unwrap(_data(result))
    pair = payload.get("location") if isinstance(payload, dict) else payload
    if not isinstance(pair, (list, tuple)) or len(pair) < 2:
        return False
    return all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) for value in pair[:2])


def validate_geo_geocode(result: Any, inputs: dict[str, Any], params: dict[str, Any], *, empty_policy: str | None = None) -> bool:
    if isinstance(result, dict) and str(result.get("status") or "").lower() == "empty" and empty_policy == "allow":
        # Provider adapters intentionally omit coordinates for a legitimate
        # no-match/unavailable response.  A durable explanatory message is
        # the semantic evidence separating that response from silent output.
        return isinstance(result.get("message"), str) and bool(result["message"].strip())
    if not _status_ok(result, empty_policy=empty_policy):
        return False
    payload = _unwrap(_data(result))
    if not isinstance(payload, dict):
        return False
    status = str(result.get("status") or "").lower()
    if status == "empty":
        return isinstance(payload.get("candidates"), list)
    location = payload.get("location")
    provider = result.get("source") or payload.get("source")
    if provider not in {"Amap", "OSM", "cache"}:
        return False
    if not isinstance(location, (list, tuple)) or len(location) != 2:
        return False
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) for value in location):
        return False
    if not (-180 <= float(location[0]) <= 180 and -90 <= float(location[1]) <= 90):
        return False
    candidates = payload.get("candidates", [payload])
    return isinstance(candidates, list) and all(isinstance(item, dict) and isinstance(item.get("location"), (list, tuple)) and len(item["location"]) == 2 for item in candidates)


def validate_poi_search(result: Any, inputs: dict[str, Any], params: dict[str, Any], *, empty_policy: str | None = None) -> bool:
    if isinstance(result, dict) and str(result.get("status") or "").lower() == "empty" and empty_policy == "allow":
        return isinstance(result.get("message"), str) and bool(result["message"].strip())
    if not _status_ok(result, empty_policy=empty_policy):
        return False
    payload = _unwrap(_data(result))
    if not isinstance(payload, dict) or not isinstance(payload.get("pois"), list):
        return False
    provider = result.get("source") or payload.get("source")
    if provider not in {"Amap", "OSM_CN", "OSM_Global"}:
        return False
    for poi in payload["pois"]:
        if not isinstance(poi, dict):
            return False
        pair = poi.get("location")
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            return False
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) for value in pair[:2]):
            return False
    return True


def validate_file_export(result: Any, inputs: dict[str, Any], params: dict[str, Any], *, empty_policy: str | None = None) -> bool:
    if not _status_ok(result, empty_policy=empty_policy):
        return False
    payload = _data(result)
    if not isinstance(payload, (bytes, bytearray)) or not payload:
        return False
    export_format = params.get("format")
    if export_format is None:
        # The older io.export/publish_file contracts share this validator but
        # do not promise a physical format. file.export always has one in IR.
        return True
    source = inputs.get("input")
    if not isinstance(source, dict) or source.get("type") != "Vector":
        return False
    source_data = source.get("data")
    if not isinstance(source_data, dict) or not isinstance(source_data.get("features"), list):
        return False
    expected_count = len(source_data["features"])
    source_crs = source.get("crs") or {}
    source_epsg = 4326 if source_crs.get("kind") == "WGS84" else source_crs.get("code") if source_crs.get("kind") == "EPSG" else None
    if isinstance(source_epsg, bool) or not isinstance(source_epsg, int):
        return False

    def same_records(output_features: list[dict[str, Any]], *, target_epsg: int) -> bool:
        from pyproj import Transformer
        from shapely.geometry import shape
        from shapely.ops import transform

        projector = None
        if source_epsg != target_epsg:
            projector = Transformer.from_crs(f"EPSG:{source_epsg}", f"EPSG:{target_epsg}", always_xy=True).transform
        actual = []
        for feature in output_features:
            if not isinstance(feature, dict) or feature.get("type") != "Feature":
                return False
            properties = feature.get("properties")
            geometry = feature.get("geometry")
            if not isinstance(properties, dict) or not isinstance(geometry, dict):
                return False
            parsed = shape(geometry)
            if parsed.is_empty or not parsed.is_valid:
                return False
            actual.append((properties, parsed))
        expected = []
        for feature in source_data["features"]:
            if not isinstance(feature, dict) or feature.get("type") != "Feature":
                return False
            properties = feature.get("properties")
            geometry = feature.get("geometry")
            if not isinstance(properties, dict) or not isinstance(geometry, dict):
                return False
            expected_geometry = shape(geometry)
            if projector is not None:
                expected_geometry = transform(projector, expected_geometry)
            expected.append((properties, expected_geometry))
        return _same_records(expected, actual, tolerance=1e-7)
    if export_format == "geojson":
        try:
            document = json.loads(bytes(payload).decode("utf-8"))
            if not isinstance(document, dict) or document.get("type") != "FeatureCollection" or not isinstance(document.get("features"), list):
                return False
            if len(document["features"]) != expected_count:
                return False
            return same_records(document["features"], target_epsg=4326)
        except Exception:
            return False
    if export_format == "gpkg":
        if not bytes(payload).startswith(b"SQLite format 3\x00"):
            return False
        try:
            import geopandas as gpd

            with tempfile.TemporaryDirectory(prefix="gismind-validate-export-") as directory:
                path = Path(directory) / "result.gpkg"
                path.write_bytes(payload)
                with closing(sqlite3.connect(path)) as database:
                    if database.execute("PRAGMA application_id").fetchone()[0] != 0x47504B47:
                        return False
                    if database.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        return False
                    if database.execute("SELECT COUNT(*) FROM gpkg_contents").fetchone()[0] != 1:
                        return False
                frame = gpd.read_file(path)
                if len(frame) != expected_count or frame.crs is None:
                    return False
                if frame.crs.to_epsg() != source_epsg:
                    return False
                return same_records(json.loads(frame.to_json(drop_id=True))["features"], target_epsg=source_epsg)
        except Exception:
            return False
    return False


def validate_publish_file(result: Any, inputs: dict[str, Any], params: dict[str, Any], *, empty_policy: str | None = None) -> bool:
    if not validate_file_export(result, inputs, params, empty_policy=empty_policy):
        return False
    raw_path = params.get("path")
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        return False
    destination = Path(raw_path)
    if not destination.is_absolute() or destination.name in {"", ".", ".."} or ".." in destination.parts:
        return False
    try:
        if not destination.is_file() or destination.stat().st_size == 0:
            return False
        output = bytes(_data(result))
        if destination.read_bytes() != output:
            return False
    except OSError:
        return False
    source = inputs.get("input")
    source_data = _unwrap(source)
    if isinstance(source_data, (bytes, bytearray)) and bytes(source_data) != bytes(_data(result)):
        return False
    return True


def validate_map(result: Any, inputs: dict[str, Any], params: dict[str, Any], *, empty_policy: str | None = None) -> bool:
    if not _status_ok(result, empty_policy=empty_policy):
        return False
    payload = _unwrap(_data(result))
    return isinstance(payload, dict) and isinstance(payload.get("layers"), list) and bool(payload["layers"]) and isinstance(payload.get("bbox"), list) and len(payload["bbox"]) == 4


def validate_table(result: Any, inputs: dict[str, Any], params: dict[str, Any], *, empty_policy: str | None = None) -> bool:
    if not _status_ok(result, empty_policy=empty_policy):
        return False
    payload = _unwrap(_data(result))
    if isinstance(payload, list):
        return all(isinstance(row, dict) for row in payload)
    return isinstance(payload, dict)


def validate_file(result: Any, inputs: dict[str, Any], params: dict[str, Any], *, empty_policy: str | None = None) -> bool:
    return validate_file_export(result, inputs, params, empty_policy=empty_policy)


def validate_code(result: Any, inputs: dict[str, Any], params: dict[str, Any], *, empty_policy: str | None = None) -> bool:
    return _status_ok(result, empty_policy=empty_policy) and _data(result) is not None


def validate_generic(result: Any, inputs: dict[str, Any], params: dict[str, Any], *, empty_policy: str | None = None) -> bool:
    return _status_ok(result, empty_policy=empty_policy)


_VECTOR_OPS = {
    "vector.overlay", "vector.voronoi", "vector.isochrone", "vector.clip", "vector.dissolve", "vector.merge",
    "vector.join_location", "vector.join_nearest", "vector.count_points", "vector.extract_location", "vector.convex_hull",
    "vector.bounding_boxes", "vector.centroid", "vector.point_on_surface", "vector.simplify", "vector.fix_geometries",
    "vector.multipart_to_singlepart", "vector.snap", "attribute.filter", "attribute.keep_fields", "attribute.rename_field",
    "attribute.calculate", "io.load_vector", "raster.contour", "raster.polygonize", "io.csv_to_points",
}
_RASTER_OPS = {"raster.reclassify", "raster.clip_extent", "raster.tpi", "raster.tri", "raster.clip_mask", "raster.hillshade", "raster.calculate", "raster.vectorize", "raster.roughness", "raster.aspect"}
_TABLE_OPS = {"raster.zonal_statistics", "raster.sample", "io.load_csv", "io.summarize", "vector.check_validity"}

POSTCONDITION_FUNCTIONS: dict[str, Callable[..., bool]] = {
    "vector.buffer": validate_vector_buffer,
    "vector.reproject": validate_vector_reproject,
    "vector.reproject_many": validate_vector_reproject_many,
    "vector.convert_datum": validate_vector_convert_datum,
    "vector.delete_duplicates": validate_vector_delete_duplicates,
    "raster.reproject": validate_raster_reproject,
    "raster.reproject_many": validate_raster_reproject_many,
    "raster.slope": validate_raster_slope,
    "io.load_raster": validate_raster,
    "geo.geocode": validate_geo_geocode,
    "poi.search": validate_poi_search,
    "file.export": validate_file_export,
    "io.export": validate_file_export,
    "publish_file": validate_publish_file,
    "geo.transform": validate_coordinate,
    "coordinate.convert": validate_coordinate,
    "map.build": validate_map,
    "io.read_upload": validate_file,
    "code.execute": validate_code,
}
POSTCONDITION_REGISTRY = POSTCONDITION_FUNCTIONS
for _operation in _VECTOR_OPS:
    POSTCONDITION_FUNCTIONS.setdefault(_operation, validate_vector_generic)
for _operation in _RASTER_OPS:
    POSTCONDITION_FUNCTIONS.setdefault(_operation, validate_raster)
for _operation in _TABLE_OPS:
    POSTCONDITION_FUNCTIONS.setdefault(_operation, validate_table)


def postcondition_references(operation: str) -> list[str]:
    """Return stable importable references for one public operation."""
    function = POSTCONDITION_FUNCTIONS.get(operation, validate_generic)
    return [f"app.runtime.postconditions:{function.__name__}"]


def get_postcondition(operation: str) -> Callable[..., bool]:
    """Resolve the registered semantic checker for an operation."""
    return POSTCONDITION_FUNCTIONS.get(operation, validate_generic)


def resolve_postcondition(reference: str) -> Callable[..., bool]:
    if not isinstance(reference, str) or reference.count(":") != 1:
        raise ValueError("postcondition must be a module:callable reference")
    module_name, callable_name = reference.split(":", 1)
    if module_name != __name__:
        raise ValueError("postcondition is outside the Runtime registry")
    function = globals().get(callable_name)
    if not callable(function) or callable_name.startswith("_"):
        raise ValueError("postcondition is not registered")
    return function


__all__ = ["POSTCONDITION_FUNCTIONS", "POSTCONDITION_REGISTRY", "postcondition_references", "get_postcondition", "resolve_postcondition"] + sorted({fn.__name__ for fn in POSTCONDITION_FUNCTIONS.values()})
