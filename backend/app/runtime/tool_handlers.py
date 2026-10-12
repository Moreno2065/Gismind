"""Deterministic GIS handlers owned by :mod:`app.runtime`.

Handlers receive resolved Runtime artifact envelopes and return small
``{status, data, message}`` dictionaries. They have no planner, graph,
database, or external-cache dependencies.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import tempfile
from pathlib import Path
from typing import Any

from app.config import settings
from app.tools.data_io import DataIO
from app.tools.geo_code import GeoCoder
from app.tools.map_layer import MapLayerBuilder
from app.tools.poi_query import POIQuery
from app.tools.raster_analysis import RasterAnalyzer
from app.tools.spatial_analysis import SpatialAnalyzer
from .tool_context import ToolContext
from .output_protocol import ArtifactBatch


def get_poi_query_instance() -> POIQuery:
    return POIQuery(amap_key=settings.AMAP_KEY, amap_timeout=settings.AMAP_TIMEOUT,
                     osm_timeout=settings.OSM_TIMEOUT, osm_endpoint=settings.OSM_ENDPOINT,
                     osm_backup_endpoints=settings.OSM_BACKUP_ENDPOINTS)


def _instances() -> dict[str, Any]:
    return {"poi": get_poi_query_instance(),
            "geo_coder": GeoCoder(amap_key=settings.AMAP_KEY, timeout=settings.AMAP_TIMEOUT),
            "analyzer": SpatialAnalyzer(amap_key=settings.AMAP_KEY, amap_timeout=settings.AMAP_TIMEOUT),
            "data_io": DataIO(), "layer_builder": MapLayerBuilder(), "raster_analyzer": RasterAnalyzer()}


def _value(value: Any) -> Any:
    return value.get("data") if isinstance(value, dict) and "data" in value else value


def _path(value: Any) -> str | None:
    if isinstance(value, (str, Path)):
        return str(value)
    if isinstance(value, dict):
        for key in ("path", "raster_path", "src_path", "dst_path"):
            if value.get(key):
                return str(value[key])
        return _path(value.get("data"))
    return None


def _consume_generated_file(value: Any) -> Any:
    """Read a generated temporary file and release it after CAS handoff."""
    path = _path(value)
    if not path or not Path(path).is_file():
        return value
    generated = Path(path)
    try:
        return generated.read_bytes()
    finally:
        try:
            generated.unlink()
        except OSError:
            pass


def _crs_name(value: Any) -> str:
    """Normalize only Runtime's documented CRS representations.

    ``GCJ02`` is a datum-label rather than a pyproj CRS, so it remains a
    distinct value.  EPSG values are kept explicit; callers must transform
    coordinates rather than merely changing this label.
    """
    if isinstance(value, dict):
        kind = str(value.get("kind") or value.get("name") or "").upper()
        if kind == "EPSG":
            code = value.get("code")
            return f"EPSG:{code}" if isinstance(code, int) and not isinstance(code, bool) and code > 0 else ""
        if kind in {"WGS84", "GCJ02"}:
            return kind
        properties = value.get("properties")
        if isinstance(properties, dict):
            return _crs_name(properties.get("name"))
    text = str(value or "").strip().upper()
    if text in {"WGS84", "WGS 84", "EPSG:4326", "4326"}:
        return "WGS84" if text in {"WGS84", "WGS 84"} else "EPSG:4326"
    if text == "GCJ02":
        return "GCJ02"
    if text.startswith("EPSG:") and text[5:].isdigit() and int(text[5:]) > 0:
        return text
    return ""


def _gdf_crs(envelope: Any, value: dict[str, Any]) -> tuple[str, str | None]:
    """Return the physical GeoPandas CRS and an optional GCJ02 datum label."""
    declared = _crs_name(envelope.get("crs")) if isinstance(envelope, dict) else ""
    if not declared:
        declared = _crs_name(value.get("crs"))
    if declared == "GCJ02":
        return "EPSG:4326", "GCJ02"
    if declared == "WGS84":
        return "EPSG:4326", None
    if declared:
        return declared, None
    label = value.get("_crs_label")
    if label == "GCJ02":
        return "EPSG:4326", "GCJ02"
    # GeoJSON without a CRS is conventionally WGS84.  This default is only
    # used for unlabelled data, never to overwrite Artifact metadata.
    return "EPSG:4326", None


def _gdf(value: Any):
    import geopandas as gpd
    from shapely.geometry import Point
    envelope = value
    value = _value(value)
    if isinstance(value, gpd.GeoDataFrame):
        return value
    if isinstance(value, list):
        if value and isinstance(value[0], dict) and "location" in value[0]:
            declared, label = _gdf_crs(envelope, {})
            result = gpd.GeoDataFrame(value, geometry=[Point(item["location"]) for item in value], crs=declared)
            if label:
                result.attrs["crs_label"] = label
            return result
        value = {"type": "FeatureCollection", "features": value}
    if not isinstance(value, dict):
        return None
    if value.get("type") == "FeatureCollection" or "features" in value:
        try:
            crs, label = _gdf_crs(envelope, value)
            gdf = gpd.GeoDataFrame.from_features(value.get("features", []), crs=crs)
            if label:
                gdf.attrs["crs_label"] = label
            return gdf
        except Exception:
            return None
    for key in ("result", "geojson", "vector"):
        if key in value and value[key] is not value:
            parsed = _gdf(value[key])
            if parsed is not None:
                return parsed
    return None


def _vector_for_raster(value: Any, raster_path: str):
    """Resolve a Vector Artifact and align its physical coordinates to a raster.

    Vector handlers already normalize the Runtime's GCJ02 label through
    ``SpatialAnalyzer._ensure_wgs84``.  Rasterio operations, however, consume
    physical coordinates directly and do not know about that datum label.
    Normalize the datum first, then reproject to the raster CRS so sampling,
    masking and zonal statistics never silently use mismatched coordinates.
    """
    gdf = _gdf(value)
    if gdf is None or not raster_path:
        return None
    try:
        normalized = _instances()["analyzer"]._ensure_wgs84(gdf)
        import rasterio

        with rasterio.open(raster_path) as dataset:
            target_crs = dataset.crs
        if target_crs is None:
            raise ValueError("raster is missing CRS metadata")
        if normalized.crs is None:
            normalized = normalized.set_crs("EPSG:4326", allow_override=True)
        if normalized.crs != target_crs:
            normalized = normalized.to_crs(target_crs)
        return normalized
    except Exception:
        return None


def _declared_file_metadata(value: Any) -> dict[str, Any]:
    """Return validated user declarations attached to a File Artifact."""
    if not isinstance(value, dict):
        return {}
    schema = value.get("schema")
    file_schema = schema.get("file") if isinstance(schema, dict) else None
    return file_schema if isinstance(file_schema, dict) else {}


def _crs_identity(value: Any) -> str:
    name = _crs_name(value)
    return "EPSG:4326" if name == "WGS84" else name


def _validate_declared_file_metadata(value: Any, *, actual_crs: Any = None, actual_bbox: Any = None) -> str | None:
    """Cross-check optional upload declarations against parsed file metadata."""
    file_schema = _declared_file_metadata(value)
    declaration = file_schema.get("declared_crs")
    if declaration is not None:
        if not isinstance(declaration, dict) or set(declaration) != {"value", "source"} or declaration.get("source") != "user":
            return "DECLARED_METADATA_MISMATCH: declared_crs must be wrapped with value and source=user"
        try:
            expected = _crs_identity(declaration["value"])
        except Exception:
            expected = ""
        observed = _crs_identity(actual_crs)
        if actual_crs is not None and (not expected or not observed or expected != observed):
            return f"DECLARED_METADATA_MISMATCH: declared CRS {expected} does not match file CRS {observed}"
    bbox_declaration = file_schema.get("declared_bbox")
    if bbox_declaration is not None:
        if not isinstance(bbox_declaration, dict) or set(bbox_declaration) != {"value", "source"} or bbox_declaration.get("source") != "user":
            return "DECLARED_METADATA_MISMATCH: declared_bbox must be wrapped with value and source=user"
        expected_bbox = bbox_declaration["value"]
        if actual_bbox is not None:
            try:
                expected_values = [float(item) for item in expected_bbox]
                observed_values = [float(item) for item in actual_bbox]
            except (TypeError, ValueError):
                return "DECLARED_METADATA_MISMATCH: declared_bbox is not numeric"
            if len(expected_values) != 4 or len(observed_values) != 4 or any(
                not math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)
                for left, right in zip(expected_values, observed_values)
            ):
                return "DECLARED_METADATA_MISMATCH: declared bbox does not match file bounds"
    return None


def _jsonable(value: Any) -> Any:
    import geopandas as gpd
    if isinstance(value, gpd.GeoDataFrame):
        data = json.loads(value.to_json())
        if value.crs is not None:
            data["_physical_crs"] = value.crs.to_string()
        if value.attrs.get("crs_label"):
            data["_crs_label"] = value.attrs["crs_label"]
        return data
    if isinstance(value, Path):
        return str(value)
    return value


def _result(status: str, data: Any = None, message: str | None = None, source: str | None = None) -> dict[str, Any]:
    result = {"status": status, "data": _jsonable(data)}
    if message:
        result["message"] = message
    if source:
        result["source"] = source
    return result


def _ctx(operation: str, kwargs: dict[str, Any]) -> ToolContext:
    return ToolContext(f"runtime_{operation}", operation, 0, kwargs, instances=_instances())


def _vector_crs(value: Any, gdf: Any) -> str:
    """Return the semantic CRS to preserve for a spatial transformation."""
    declared = _crs_name(value.get("crs")) if isinstance(value, dict) else ""
    if declared:
        return declared
    label = getattr(gdf, "attrs", {}).get("crs_label") if gdf is not None else None
    if label:
        return str(label)
    physical = getattr(gdf, "crs", None)
    return str(physical) if physical is not None else "WGS84"


def _coerce_vector_output(value: Any, target_crs: str | None) -> Any:
    """Make a spatial result's coordinates and CRS marker agree.

    SpatialAnalyzer's historical methods return display-ready GCJ02 values.
    Runtime operations instead preserve the input CRS unless an operation
    explicitly requests a target CRS (for example ``vector.reproject``).
    """
    if not target_crs:
        return value
    try:
        import geopandas as gpd
        if not isinstance(value, gpd.GeoDataFrame):
            return value
        analyzer = _instances()["analyzer"]
        current_label = (value.attrs or {}).get("crs_label")
        if target_crs == "GCJ02":
            if current_label != "GCJ02":
                value = analyzer._to_gcj02_output(analyzer._ensure_wgs84(value))
            else:
                value = value.set_crs("EPSG:4326", allow_override=True)
                value.attrs["crs_label"] = "GCJ02"
            return value
        if current_label == "GCJ02":
            value = analyzer._ensure_wgs84(value)
        if target_crs in {"WGS84", "EPSG:4326"}:
            value = value.set_crs("EPSG:4326", allow_override=True)
            value.attrs.pop("crs_label", None)
            return value
        if value.crs is None:
            value = value.set_crs("EPSG:4326", allow_override=True)
        value = value.to_crs(target_crs)
        value.attrs.pop("crs_label", None)
        return value
    except Exception:
        # Let ArtifactStore/Executor surface an invalid physical result with
        # its normal typed validation error instead of hiding the operation's
        # original payload.
        return value


def _call(ctx: ToolContext, method: str, *args: Any, output_crs: str | None = None, **kwargs: Any) -> dict[str, Any]:
    try:
        value = getattr(ctx.analyzer, method)(*args, **kwargs)
        if isinstance(value, dict) and "status" in value:
            return _result(value.get("status", "error"), _coerce_vector_output(value.get("data"), output_crs), value.get("message"), "computed")
        return _result("success", _coerce_vector_output(value, output_crs), source="computed")
    except Exception as exc:
        return _result("error", message=str(exc))


def handle_geo_geocode(**kwargs):
    try:
        raw = asyncio.run(_instances()["geo_coder"].geocode(str(kwargs.get("address") or kwargs.get("query") or kwargs.get("location") or "")))
        return _result(raw.get("status", "empty"), raw, raw.get("message"), raw.get("source", "Amap"))
    except Exception as exc:
        return _result("error", message=str(exc))


def handle_geo_transform(**kwargs):
    from app.tools.geo_transform import gcj02_to_wgs84, wgs84_to_gcj02
    source = _crs_name(kwargs.get("source_crs"))
    target = _crs_name(kwargs.get("target_crs"))
    value = _value(kwargs.get("coordinates")) or {"lng": kwargs.get("lng"), "lat": kwargs.get("lat")}
    pair = value.get("location", [value.get("lng"), value.get("lat")]) if isinstance(value, dict) else value
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        return _result("error", message="需要有效坐标")
    try:
        lng, lat = float(pair[0]), float(pair[1])
        if not source or not target:
            return _result("error", message="source_crs 和 target_crs 必须是受支持的 CRS")
        if source == target or {source, target} <= {"WGS84", "EPSG:4326"}:
            transformed = (lng, lat)
        elif (source, target) in {("WGS84", "GCJ02"), ("EPSG:4326", "GCJ02")}:
            transformed = wgs84_to_gcj02(lng, lat)
        elif (source, target) in {("GCJ02", "WGS84"), ("GCJ02", "EPSG:4326")}:
            transformed = gcj02_to_wgs84(lng, lat)
        elif source != "GCJ02" and target != "GCJ02":
            from pyproj import Transformer
            transformed = Transformer.from_crs(source, target, always_xy=True).transform(lng, lat)
        else:
            return _result("error", message="GCJ02 与投影 CRS 的转换必须经 WGS84 显式拆分")
        return _result("success", {"location": [float(transformed[0]), float(transformed[1])], "source_crs": source, "target_crs": target}, source="computed")
    except Exception as exc:
        return _result("error", message=str(exc))


def handle_poi_search(**kwargs):
    location = _value(kwargs.get("location"))
    if isinstance(location, dict):
        location = location.get("location") or [location.get("lng"), location.get("lat")]
    if not isinstance(location, (list, tuple)) or len(location) != 2:
        return _result("error", message="需要 location")
    try:
        raw = get_poi_query_instance().search_poi_tool(str(kwargs.get("query", "")), (float(location[0]), float(location[1])), int(kwargs.get("radius", 1000)))
        return _result(raw.get("status", "empty"), raw.get("data", raw), raw.get("message"), raw.get("source"))
    except Exception as exc:
        return _result("error", message=str(exc))


def handle_vector_buffer(**kwargs):
    source = kwargs.get("vector", kwargs.get("input"))
    gdf = _gdf(source)
    distance = kwargs.get("distance_m", kwargs.get("radius", kwargs.get("radius_m")))
    if gdf is None or not isinstance(distance, (int, float)) or isinstance(distance, bool) or not math.isfinite(float(distance)) or distance < 0:
        return _result("error", message="需要 vector 和非负 distance_m")
    return _call(_ctx("vector.buffer", kwargs), "buffer", gdf, float(distance), output_crs=_vector_crs(source, gdf))


def handle_vector_reproject(**kwargs):
    gdf = _gdf(kwargs.get("vector", kwargs.get("input")))
    if gdf is None:
        return _result("error", message="需要 Vector Artifact")
    target = kwargs.get("target_crs")
    if isinstance(target, dict) and target.get("kind") == "EPSG":
        target = f"EPSG:{target['code']}"
    return _call(_ctx("vector.reproject", kwargs), "reproject_layer", gdf, target_crs=target, output_crs=_crs_name(target))


def handle_vector_reproject_many(**kwargs):
    values = kwargs.get("vector", kwargs.get("input"))
    if not isinstance(values, list):
        return _result("error", message="需要 Vector Artifact 列表")
    target = kwargs.get("target_crs")
    if isinstance(target, dict) and target.get("kind") == "EPSG":
        target = f"EPSG:{target['code']}"
    outputs = []
    for item in values:
        result = handle_vector_reproject(vector=item, target_crs=target)
        if result.get("status") != "success":
            return result
        outputs.append(result.get("data"))
    return _result("success", ArtifactBatch(outputs), source="computed")


def handle_coordinate_convert(**kwargs):
    return handle_geo_transform(**kwargs)


def handle_vector_convert_datum(**kwargs):
    gdf = _gdf(kwargs.get("vector", kwargs.get("input")))
    source = _crs_name(kwargs.get("source_crs"))
    target = _crs_name(kwargs.get("target_crs"))
    if gdf is None:
        return _result("error", message="需要 Vector Artifact")
    if not source or not target:
        return _result("error", message="source_crs 和 target_crs 必须是受支持的 CRS")
    try:
        if source == target or {source, target} <= {"WGS84", "EPSG:4326"}:
            result = gdf.copy()
        elif (source, target) in {("WGS84", "GCJ02"), ("EPSG:4326", "GCJ02")}:
            result = _instances()["analyzer"]._to_gcj02_output(gdf.set_crs("EPSG:4326", allow_override=True))
        elif (source, target) in {("GCJ02", "WGS84"), ("GCJ02", "EPSG:4326")}:
            source_gdf = gdf.set_crs("EPSG:4326", allow_override=True)
            source_gdf.attrs["crs_label"] = "GCJ02"
            result = _instances()["analyzer"]._ensure_wgs84(source_gdf)
        elif source != "GCJ02" and target != "GCJ02":
            result = gdf.set_crs(source, allow_override=True).to_crs(target)
        else:
            return _result("error", message="GCJ02 与投影 CRS 的转换必须经 WGS84 显式拆分")
        return _result("success", result, source="computed")
    except Exception as exc:
        return _result("error", message=str(exc))


def _map_bbox(features: Any) -> list[float] | None:
    """Compute a finite GeoJSON bbox without inspecting arbitrary properties."""
    coordinates: list[tuple[float, float]] = []

    def visit(value: Any) -> None:
        if isinstance(value, (list, tuple)):
            if len(value) >= 2 and isinstance(value[0], (int, float)) and not isinstance(value[0], bool) and isinstance(value[1], (int, float)) and not isinstance(value[1], bool):
                x, y = float(value[0]), float(value[1])
                if math.isfinite(x) and math.isfinite(y):
                    coordinates.append((x, y))
                return
            for item in value:
                visit(item)

    for feature in features if isinstance(features, list) else []:
        if isinstance(feature, dict):
            geometry = feature.get("geometry")
            if isinstance(geometry, dict):
                visit(geometry.get("coordinates"))
    if not coordinates:
        return None
    xs, ys = zip(*coordinates)
    return [min(xs), min(ys), max(xs), max(ys)]


def handle_map_build(**kwargs):
    try:
        envelope = kwargs.get("vector", kwargs.get("input"))
        value = _value(envelope)
        features = value.get("features", []) if isinstance(value, dict) else value
        bbox = _map_bbox(features)
        if bbox is None:
            return _result("empty", {"layers": [], "bbox": None}, "Vector Artifact has no finite geometries", "computed")
        crs = envelope.get("crs") if isinstance(envelope, dict) else None
        if crs is None and isinstance(value, dict):
            crs = value.get("crs")
        return _result(
            "success",
            {
                "layers": [_instances()["layer_builder"].build_feature_collection(features)],
                "bbox": bbox,
                "crs": crs,
            },
            source="computed",
        )
    except Exception as exc:
        return _result("error", message=str(exc))


def handle_file_export(**kwargs):
    value = _value(kwargs.get("input"))
    if isinstance(value, bytes):
        return _result("success", value, source="computed")
    return _result("success", json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode(), source="computed")


def _code_artifact_inputs(value: Any, declarations: Any) -> dict[str, dict[str, Any]]:
    supplied = value if isinstance(value, list) else ([] if value is None else [value])
    if not supplied:
        if declarations not in (None, []):
            raise ValueError("declared_inputs must be empty when no Artifact inputs are bound")
        return {}
    if not isinstance(declarations, list) or len(declarations) != len(supplied):
        raise ValueError("declared_inputs must name every bound Artifact input exactly once")
    names: list[str] = []
    for declaration in declarations:
        name = declaration.get("name") if isinstance(declaration, dict) else declaration
        if not isinstance(name, str) or not name or len(name) > 64 or not name.replace("_", "a").isalnum():
            raise ValueError("declared input names must be alphanumeric identifiers")
        names.append(name)
    if len(set(names)) != len(names):
        raise ValueError("declared input names must be unique")
    resolved: dict[str, dict[str, Any]] = {}
    for name, artifact in zip(names, supplied):
        if not isinstance(artifact, dict) or not isinstance(artifact.get("data"), bytes):
            raise ValueError("code.execute only accepts resolved File Artifact bytes")
        resolved[name] = {
            "data": artifact["data"],
            "artifact_id": artifact.get("artifact_id"),
            "content_hash": artifact.get("content_hash"),
            "media_type": artifact.get("media_type"),
            "type": artifact.get("type"),
        }
    return resolved


def handle_code_execute(**kwargs):
    from app.runtime.code_mode.executor import execute_source
    source = kwargs.get("source_code", "")
    if not isinstance(source, str) or not source.strip():
        return _result("error", message="source_code 不能为空")
    supplied = kwargs.get("source_code_hash")
    if supplied and supplied != hashlib.sha256(source.encode()).hexdigest():
        return _result("error", message="source_code_hash 不匹配")
    try:
        inputs = _code_artifact_inputs(kwargs.get("input"), kwargs.get("declared_inputs"))
        return execute_source(source, inputs=inputs)
    except ValueError as exc:
        return _result("error", message=str(exc))


def _io(name: str, **kwargs):
    io = DataIO()
    file_value = kwargs.get("file", kwargs.get("input"))
    path = _path(file_value) or kwargs.get("path")
    schema = file_value.get("schema", {}) if isinstance(file_value, dict) else {}
    inspected = schema.get("inspection") if isinstance(schema, dict) else None
    if isinstance(inspected, dict) and path:
        try:
            from app.runtime.upload_inspection import read_table, read_vector
            if name == "load_csv":
                if inspected.get("artifact_type") != "Table":
                    raise ValueError("上传文件不是表格")
                return _result("success", read_table(Path(path), inspected), source="computed")
            if name == "load_vector":
                if inspected.get("artifact_type") != "Vector":
                    raise ValueError("上传文件不是矢量数据")
                declaration = schema.get("file", {}).get("declared_crs", {})
                frame = read_vector(Path(path), inspected, declaration.get("value"))
                return _result("success", frame, source="computed")
        except Exception as exc:
            return _result("error", message=str(exc))
    temporary_path: Path | None = None
    if name == "load_csv" and isinstance(file_value, dict) and isinstance(file_value.get("data"), (bytes, bytearray)):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as stream:
            stream.write(bytes(file_value["data"]))
            path = stream.name
        temporary_path = Path(path)
    if not path:
        return _result("error", message="需要文件路径")
    try:
        raw = getattr(io, name)(str(path))
        if raw.get("status") == "success" and name == "load_vector":
            metadata = raw.get("metadata", {})
            mismatch = _validate_declared_file_metadata(
                file_value,
                actual_crs=metadata.get("crs") if isinstance(metadata, dict) else None,
                actual_bbox=metadata.get("bbox") if isinstance(metadata, dict) else None,
            )
            if mismatch:
                return _result("error", message=mismatch)
        return _result(raw.get("status", "error"), raw.get("data"), raw.get("message"), "computed")
    except Exception as exc:
        return _result("error", message=str(exc))
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def handle_io_load_vector(**kwargs): return _io("load_vector", **kwargs)


def handle_io_load_raster(**kwargs):
    file_input = kwargs.get("file", kwargs.get("input"))
    file_schema = file_input.get("schema", {}) if isinstance(file_input, dict) else {}
    inspection = file_schema.get("inspection") if isinstance(file_schema, dict) else None
    if isinstance(inspection, dict) and _path(file_input):
        try:
            from app.runtime.upload_inspection import read_raster
            declaration = file_schema.get("file", {}).get("declared_crs", {})
            return _result("success", read_raster(Path(_path(file_input)), inspection, declaration.get("value")), source="computed")
        except Exception as exc:
            return _result("error", message=str(exc))
    """Validate a raster once, then retain its original binary representation.

    Metadata is useful to the UI, but JSON metadata cannot be passed to the
    next Rasterio operation.  CAS stores the readable source bytes instead.
    """
    file_value = kwargs.get("file", kwargs.get("input"))
    path = _path(file_value) or kwargs.get("path")
    if not path:
        return _result("error", message="需要栅格文件路径")
    try:
        inspected = DataIO().load_raster(str(path))
        if inspected.get("status") != "success":
            return _result(inspected.get("status", "error"), inspected.get("data"), inspected.get("message"), "computed")
        metadata = inspected.get("data", {}).get("metadata", {}) if isinstance(inspected.get("data"), dict) else {}
        bounds = metadata.get("bounds") if isinstance(metadata, dict) else None
        actual_bbox = None
        if isinstance(bounds, dict):
            actual_bbox = [bounds.get("left"), bounds.get("bottom"), bounds.get("right"), bounds.get("top")]
        mismatch = _validate_declared_file_metadata(
            file_value,
            actual_crs=metadata.get("crs") if isinstance(metadata, dict) else None,
            actual_bbox=actual_bbox,
        )
        if mismatch:
            return _result("error", message=mismatch)
        return _result("success", Path(path).read_bytes(), source="computed")
    except Exception as exc:
        return _result("error", message=str(exc))


def handle_io_load_csv(**kwargs): return _io("load_csv", **kwargs)


def handle_io_csv_to_points(**kwargs):
    try:
        crs = _crs_name(kwargs.get("crs")) or "EPSG:4326"
        raw = DataIO().csv_to_points(_value(kwargs.get("input", kwargs.get("data_from"))), str(kwargs.get("x_col", "lon")), str(kwargs.get("y_col", "lat")), crs)
        result = _result(raw.get("status", "error"), raw.get("data"), raw.get("message"), "computed")
        if raw.get("error_code") in {"COORDINATE_OUT_OF_RANGE", "INVALID_COORDINATE"}:
            result["error_code"] = raw["error_code"]
            result["invalid_row_positions"] = raw.get("invalid_row_positions", [])
        return result
    except Exception as exc:
        return _result("error", message=str(exc))


def handle_io_summarize(**kwargs):
    try:
        value = _value(kwargs.get("input", kwargs.get("vector")))
        parsed = _gdf(value)
        raw = DataIO().summarize_layer(parsed if parsed is not None else value)
        return _result(raw.get("status", "error"), raw.get("data"), raw.get("message"), "computed")
    except Exception as exc:
        return _result("error", message=str(exc))


def handle_io_read_upload(**kwargs):
    """Identity-read an already published File Artifact without legacy upload state."""
    source = kwargs.get("file", kwargs.get("input"))
    if isinstance(source, dict) and "data" in source:
        return _result("success", source["data"], source="runtime_artifact")
    return _result("error", message="io.read_upload requires a published File Artifact")
def handle_publish_file(**kwargs): return _result("error", message="publish_file 由 ArtifactStore 负责")


_METHODS = {
    "vector.voronoi": ("voronoi", ("input",)), "vector.isochrone": ("isochrone", ("input",)),
    "vector.overlay": ("overlay", ("input", "other")), "vector.clip": ("clip", ("input", "mask")),
    "vector.dissolve": ("dissolve", ("input",)), "vector.merge": ("merge_layers", ("input",)),
    "vector.join_location": ("join_by_location", ("input", "other")), "vector.join_nearest": ("join_by_nearest", ("input", "other")),
    "vector.count_points": ("count_points_in_polygon", ("points", "polygons")), "vector.extract_location": ("extract_by_location", ("input", "mask")),
    "vector.convex_hull": ("convex_hull", ("input",)), "vector.bounding_boxes": ("bounding_boxes", ("input",)),
    "vector.centroid": ("centroid_layer", ("input",)), "vector.point_on_surface": ("point_on_surface", ("input",)),
    "vector.simplify": ("simplify_geometry", ("input",)), "vector.fix_geometries": ("fix_geometries", ("input",)),
    "vector.check_validity": ("check_validity", ("input",)), "vector.multipart_to_singlepart": ("multipart_to_singlepart", ("input",)),
    "vector.delete_duplicates": ("delete_duplicate_geometries", ("input",)), "vector.snap": ("snap_geometries", ("input", "other")),
    "attribute.filter": ("extract_by_attribute", ("input",)), "attribute.keep_fields": ("keep_fields", ("input",)),
    "attribute.rename_field": ("rename_field", ("input",)), "attribute.calculate": ("field_calculator", ("input",)),
}


def _make_method(operation: str, method: str, keys: tuple[str, ...]):
    def handler(**kwargs):
        values = [_gdf(kwargs.get(key)) for key in keys]
        if any(item is None for item in values):
            return _result("error", message="无法解析输入 Vector Artifact")
        # Only named operation parameters are forwarded.  Input ports are
        # positional GeoDataFrames; all other compiler-approved fields must
        # reach the GIS implementation unchanged.
        parameters = {name: value for name, value in kwargs.items() if name not in keys}
        return _call(
            _ctx(operation, kwargs), method, *values,
            output_crs=_vector_crs(kwargs.get(keys[0]), values[0]),
            **parameters,
        )
    handler.__name__ = f"handle_{operation.replace('.', '_')}"
    return handler


for _op, (_method, _keys) in _METHODS.items():
    globals()[f"handle_{_op.replace('.', '_')}"] = _make_method(_op, _method, _keys)


def handle_vector_merge(*, input=None, **_kwargs):
    if not isinstance(input, list) or not input:
        return _result("error", message="vector.merge requires one or more Vector Artifacts")
    vectors = [_gdf(item) for item in input]
    if any(item is None for item in vectors):
        return _result("error", message="vector.merge could not read every Vector Artifact")
    crs_values = {str(item.crs) for item in vectors}
    if len(crs_values) != 1:
        return _result("error", message="vector.merge inputs must use the same CRS")
    return _call(
        _ctx("vector.merge", {"input": input}), "merge_layers", vectors,
        output_crs=_vector_crs(input[0], vectors[0]),
    )


def handle_vector_isochrone(*, origin=None, mode=None, time_min=None, **_kwargs):
    value = _value(origin)
    pair = value.get("location") if isinstance(value, dict) else value
    declared = _crs_name(origin.get("crs")) if isinstance(origin, dict) else ""
    if declared and declared != "GCJ02":
        return _result("error", message="vector.isochrone origin must use GCJ02")
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        return _result("error", message="vector.isochrone requires a Coordinate origin")
    try:
        point = (float(pair[0]), float(pair[1]))
        if not all(math.isfinite(item) for item in point):
            raise ValueError("origin coordinates must be finite")
        raw = SpatialAnalyzer(amap_key=settings.AMAP_KEY, amap_timeout=settings.AMAP_TIMEOUT).isochrone(
            point, mode=str(mode), time_min=int(time_min),
        )
        data = raw.get("data")
        if isinstance(data, dict) and data.get("geometry") is not None:
            from shapely.geometry import mapping
            data = {
                "type": "FeatureCollection",
                "features": [{"type": "Feature", "properties": {}, "geometry": mapping(data["geometry"])}],
            }
        return _result(raw.get("status", "error"), data, raw.get("message"), "computed")
    except Exception as exc:
        return _result("error", message=str(exc))


_RASTER_METHODS = {
    "raster.reproject": "reproject_raster", "raster.clip_mask": "clip_raster_by_mask", "raster.clip_extent": "clip_raster_by_extent",
    "raster.calculate": "raster_calculator", "raster.zonal_statistics": "zonal_statistics", "raster.sample": "raster_sampling",
    "raster.vectorize": "rasterize_vector", "raster.polygonize": "polygonize_raster", "raster.slope": "slope", "raster.aspect": "aspect",
    "raster.hillshade": "hillshade", "raster.contour": "contour", "raster.reclassify": "reclassify_raster", "raster.tri": "terrain_ruggedness_index",
    "raster.tpi": "topographic_position_index", "raster.roughness": "roughness",
}

PUBLIC_OPERATIONS = frozenset({
    "geo.geocode", "geo.transform", "poi.search", "vector.buffer", "vector.overlay", "vector.voronoi",
    "vector.isochrone", "vector.clip", "vector.dissolve", "vector.merge", "vector.join_location",
    "vector.join_nearest", "vector.count_points", "vector.extract_location", "vector.convex_hull",
    "vector.bounding_boxes", "vector.centroid", "vector.point_on_surface", "vector.simplify",
    "vector.fix_geometries", "vector.check_validity", "vector.multipart_to_singlepart", "vector.delete_duplicates",
    "vector.snap", "vector.reproject", "vector.reproject_many", "vector.convert_datum", "attribute.filter",
    "attribute.keep_fields", "attribute.rename_field", "attribute.calculate", "io.read_upload", "io.load_vector",
    "io.load_raster", "io.load_csv", "io.csv_to_points", "io.summarize", "io.export", "map.build", "file.export",
    "publish_file", "raster.reproject", "raster.reproject_many", "raster.clip_mask", "raster.clip_extent",
    "raster.calculate", "raster.zonal_statistics", "raster.sample", "raster.vectorize", "raster.polygonize",
    "raster.slope", "raster.aspect", "raster.hillshade", "raster.contour", "raster.reclassify", "raster.tri",
    "raster.tpi", "raster.roughness", "code.execute",
})


def _make_raster(operation: str, method: str):
    def handler(**kwargs):
        source = _path(kwargs.get("raster", kwargs.get("input")))
        if not source:
            return _result("error", message="需要 Raster Artifact")
        args = dict(kwargs); args.pop("raster", None); args.pop("input", None)
        if method == "reproject_raster":
            requested = args.pop("target_crs", args.get("dst_crs", "EPSG:4326"))
            normalized = _crs_name(requested)
            if normalized == "WGS84":
                normalized = "EPSG:4326"
            if not normalized or normalized == "GCJ02":
                return _result("error", message="Raster reproject requires a physical EPSG CRS")
            args["dst_crs"] = normalized
        try:
            raw = getattr(RasterAnalyzer(), method)(source, **args)
            data = raw.get("data")
            # Raster-producing algorithms write a temporary GeoTIFF.  Keep
            # that file as the typed output rather than serializing its UI
            # preview/metadata dictionary to JSON.
            if raw.get("status") == "success":
                data = _consume_generated_file(data)
            return _result(raw.get("status", "error"), data, raw.get("message"), "computed")
        except Exception as exc:
            return _result("error", message=str(exc))
    handler.__name__ = f"handle_{operation.replace('.', '_')}"
    return handler


for _op, _method in _RASTER_METHODS.items():
    globals()[f"handle_{_op.replace('.', '_')}"] = _make_raster(_op, _method)


def handle_raster_contour(*, raster=None, interval=None, **_kwargs):
    """Return contour isolines as a typed GeoJSON FeatureCollection.

    ``RasterAnalyzer.contour`` returns a GeoDataFrame, unlike the raster
    transforms which return a temporary GeoTIFF.  Keep this conversion
    explicit so the result can be validated and committed as Vector bytes.
    """
    source = _path(raster)
    if not source:
        return _result("error", message="raster.contour requires a Raster Artifact")
    if interval is not None:
        try:
            interval = float(interval)
        except (TypeError, ValueError):
            return _result("error", message="interval must be a positive number")
        if not math.isfinite(interval) or interval <= 0:
            return _result("error", message="interval must be a positive number")
    try:
        raw = RasterAnalyzer().contour(source, interval=interval)
        data = raw.get("data")
        if hasattr(data, "to_json"):
            data = json.loads(data.to_json())
        return _result(raw.get("status", "error"), data, raw.get("message"), "computed")
    except Exception as exc:
        return _result("error", message=str(exc))


def handle_raster_calculate(*, raster=None, expression=None, **_kwargs):
    source = _path(raster)
    if not source:
        return _result("error", message="raster.calculate requires a Raster Artifact")
    if not isinstance(expression, str) or not expression.strip():
        return _result("error", message="raster.calculate requires an expression")
    try:
        raw = RasterAnalyzer().raster_calculator({"raster": source}, expression)
        data = raw.get("data")
        if raw.get("status") == "success":
            data = _consume_generated_file(data)
        return _result(raw.get("status", "error"), data, raw.get("message"), "computed")
    except Exception as exc:
        return _result("error", message=str(exc))


def handle_raster_reproject_many(*, raster=None, target_crs=None, **_kwargs):
    if not isinstance(raster, list) or not raster:
        return _result("error", message="raster.reproject_many requires one or more Raster Artifacts")
    outputs = []
    for item in raster:
        result = handle_raster_reproject(raster=item, target_crs=target_crs)
        if result.get("status") != "success":
            return result
        outputs.append(result.get("data"))
    return _result("success", ArtifactBatch(outputs), source="computed")


def handle_raster_clip_mask(*, raster=None, mask=None, **_kwargs):
    source = _path(raster)
    mask_gdf = _vector_for_raster(mask, source) if source else None
    if not source or mask_gdf is None:
        return _result("error", message="raster.clip_mask requires Raster and Vector mask Artifacts")
    try:
        raw = RasterAnalyzer().clip_raster_by_mask(source, mask_gdf)
        data = raw.get("data")
        if raw.get("status") == "success":
            data = _consume_generated_file(data)
        return _result(raw.get("status", "error"), data, raw.get("message"), "computed")
    except Exception as exc:
        return _result("error", message=str(exc))


def handle_raster_zonal_statistics(*, raster=None, zones=None, stats=None, **_kwargs):
    source = _path(raster)
    zones_gdf = _vector_for_raster(zones, source) if source else None
    if not source or zones_gdf is None:
        return _result("error", message="raster.zonal_statistics requires Raster and Vector zones Artifacts")
    try:
        raw = RasterAnalyzer().zonal_statistics(source, zones_gdf, stats=stats)
        data = raw.get("data")
        # rasterstats returns GeoJSON Features while the ToolContract declares
        # a Table. Flatten only that documented representation so consumers see
        # the requested statistic columns directly rather than backend-specific
        # Feature wrappers.
        if isinstance(data, list) and all(isinstance(row, dict) and row.get("type") == "Feature" for row in data):
            data = [dict(row.get("properties") or {}) for row in data]
        return _result(raw.get("status", "error"), data, raw.get("message"), "computed")
    except Exception as exc:
        return _result("error", message=str(exc))


def handle_raster_sample(*, raster=None, points=None, **_kwargs):
    source = _path(raster)
    points_gdf = _vector_for_raster(points, source) if source else None
    if not source or points_gdf is None:
        return _result("error", message="raster.sample requires Raster and Vector point Artifacts")
    try:
        raw = RasterAnalyzer().raster_sampling(source, points_gdf)
        return _result(raw.get("status", "error"), raw.get("data"), raw.get("message"), "computed")
    except Exception as exc:
        return _result("error", message=str(exc))


def handle_raster_vectorize(*, vector=None, out_shape=None, transform=None, crs=None, **_kwargs):
    vector_gdf = _gdf(vector)
    if vector_gdf is None:
        return _result("error", message="raster.vectorize requires a Vector Artifact")
    try:
        from affine import Affine
        shape = tuple(out_shape or ())
        if len(shape) != 2 or any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in shape):
            raise ValueError("out_shape must be [height, width]")
        values = tuple(transform or ())
        if len(values) != 6:
            raise ValueError("transform must contain six affine coefficients")
        target_crs = _crs_name(crs) or str(crs or "")
        if not target_crs:
            raise ValueError("crs is required")
        # Rasterio burns physical coordinates; normalize Runtime datum labels
        # and reproject vectors before rasterization instead of merely tagging
        # the output with a different CRS.  GCJ02 has no physical CRS and is
        # therefore normalized to WGS84 by _ensure_wgs84 first.
        normalized = _instances()["analyzer"]._ensure_wgs84(vector_gdf)
        if normalized.crs is None:
            normalized = normalized.set_crs("EPSG:4326", allow_override=True)
        pyproj_target = "EPSG:4326" if target_crs == "WGS84" else target_crs
        if target_crs == "GCJ02":
            raise ValueError("raster.vectorize target CRS GCJ02 is not a physical raster CRS")
        if str(normalized.crs) != pyproj_target:
            normalized = normalized.to_crs(pyproj_target)
        raw = RasterAnalyzer().rasterize_vector(normalized, shape, Affine(*values), pyproj_target)
        data = raw.get("data")
        if raw.get("status") == "success":
            data = _consume_generated_file(data)
        return _result(raw.get("status", "error"), data, raw.get("message"), "computed")
    except Exception as exc:
        return _result("error", message=str(exc))


def handle_raster_polygonize(*, raster=None, band=1, **_kwargs):
    source = _path(raster)
    if not source:
        return _result("error", message="raster.polygonize requires a Raster Artifact")
    try:
        raw = RasterAnalyzer().polygonize_raster(source, band=band)
        data = raw.get("data")
        if raw.get("status") == "success" and hasattr(data, "to_json"):
            data = json.loads(data.to_json())
        return _result(raw.get("status", "error"), data, raw.get("message"), "computed")
    except Exception as exc:
        return _result("error", message=str(exc))


def validate_code_execute(**kwargs):
    source = kwargs.get("source_code")
    if not isinstance(source, str) or not source.strip(): return False
    if kwargs.get("source_code_hash") and kwargs["source_code_hash"] != hashlib.sha256(source.encode()).hexdigest(): return False
    declarations = kwargs.get("declared_inputs")
    bound = kwargs.get("input")
    if bound is not None and not isinstance(declarations, list):
        return False
    if isinstance(declarations, list):
        names = [item.get("name") if isinstance(item, dict) else item for item in declarations]
        if any(not isinstance(name, str) or not name or len(name) > 64 or not name.replace("_", "a").isalnum() for name in names) or len(set(names)) != len(names):
            return False
    return not any(token in source for token in ("tool_fns", "socket", "rpc", "__import__"))


__all__ = [name for name in globals() if name.startswith("handle_")] + ["get_poi_query_instance", "validate_code_execute"]
