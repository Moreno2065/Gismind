"""Schema-first contracts shared by native tool-calling sub-agents."""

from __future__ import annotations

from typing import Any

from app.agents.registry import get_tool_spec


class ToolArgumentValidationError(ValueError):
    """Raised before execution when a native tool call violates its contract."""


_STRING = {"type": "string"}
_NUMBER = {"type": "number"}
_INTEGER = {"type": "integer", "minimum": 0}
_BOOLEAN = {"type": "boolean"}
_LOCATION = {
    "type": "array",
    "items": {"type": "number"},
    "minItems": 2,
    "maxItems": 2,
    "description": "[longitude, latitude]",
}
_BBOX = {
    "type": "array",
    "items": {"type": "number"},
    "minItems": 4,
    "maxItems": 4,
}
_REF = {
    **_INTEGER,
    "description": "Index from the runtime reference catalog in the system prompt.",
}


def _fields(*names: str) -> dict[str, dict[str, Any]]:
    return {name: dict(_REF) for name in names}


_TOOL_PROPERTIES: dict[str, dict[str, dict[str, Any]]] = {
    "geo_code": {"address": {**_STRING, "description": "Place name or address."}},
    "geo_transform": {
        "operation": {
            **_STRING,
            "enum": [
                "wgs84_to_gcj02", "gcj02_to_wgs84", "gcj02_to_bd09",
                "bd09_to_gcj02", "wgs84_to_bd09", "bd09_to_wgs84",
                "haversine", "out_of_china", "auto_detect_crs",
            ],
        },
        "lng": _NUMBER,
        "lat": _NUMBER,
        "p1": _LOCATION,
        "p2": _LOCATION,
        "bbox": _BBOX,
        "file_path": _STRING,
    },
    "query_poi": {
        "query": _STRING,
        "location": _LOCATION,
        "location_from": _REF,
        "radius": {**_NUMBER, "exclusiveMinimum": 0},
        "dedup_threshold_m": {**_NUMBER, "minimum": 0},
        "within_source_dedup": _BOOLEAN,
    },
    "data_io_read": {"file_id": _STRING},
    "buffer": {
        **_fields("geometry_from", "points_from"),
        "radius_m": {**_NUMBER, "exclusiveMinimum": 0},
        "radius": {**_NUMBER, "exclusiveMinimum": 0},
    },
    "overlay": {
        **_fields("geometry_a_from", "geometry_b_from"),
        "how": {**_STRING, "enum": ["intersection", "union", "difference", "symmetric_difference"]},
    },
    "voronoi": _fields("points_from"),
    "isochrone": {
        "location": _LOCATION,
        "location_from": _REF,
        "mode": {**_STRING, "enum": ["walking", "driving", "cycling"]},
        "time_min": {**_INTEGER, "minimum": 1},
    },
    "map_layer_build": _fields("geometry_from", "data_from"),
    "clip_layer": _fields("input_ref", "geometry_from", "overlay_ref", "mask_from"),
    "dissolve_layer": {**_fields("input_ref", "geometry_from"), "by": _STRING},
    "merge_layers": _fields("layers_from", "data_from"),
    "join_by_location": {
        **_fields("input_ref", "geometry_from", "other_ref", "join_from"),
        "predicate": _STRING,
    },
    "join_by_nearest": {
        **_fields("input_ref", "geometry_from", "other_ref", "join_from"),
        "max_distance": {**_NUMBER, "minimum": 0},
    },
    "count_points_in_polygon": _fields("polygons_from", "geometry_from", "points_from"),
    "extract_by_location": {
        **_fields("input_ref", "geometry_from", "mask_ref", "mask_from"),
        "predicate": _STRING,
    },
    "convex_hull": _fields("input_ref", "geometry_from"),
    "bounding_boxes": _fields("input_ref", "geometry_from"),
    "centroid_layer": _fields("input_ref", "geometry_from"),
    "point_on_surface": _fields("input_ref", "geometry_from"),
    "simplify_geometry": {
        **_fields("input_ref", "geometry_from"),
        "tolerance": {**_NUMBER, "minimum": 0},
    },
    "fix_geometries": _fields("input_ref", "geometry_from"),
    "check_validity": _fields("input_ref", "geometry_from"),
    "reproject_layer": {
        **_fields("input_ref", "geometry_from"),
        "target_crs": _STRING,
    },
    "export_result": {
        **_fields("data_from", "input_ref"),
        "format": {**_STRING, "enum": ["geojson", "json", "gpkg", "shp", "kml"]},
        "output_path": _STRING,
        "path": _STRING,
    },
    "extract_by_attribute": {
        **_fields("input_ref", "geometry_from"),
        "expression": {
            **_STRING,
            "description": "Exact expression such as class == 'station'.",
        },
        "field": {**_STRING, "description": "Attribute field to filter."},
        "operator": {
            **_STRING,
            "enum": ["==", "!=", ">", ">=", "<", "<=", "contains", "is_null"],
        },
        # Intentionally unconstrained: an attribute value can be string,
        # number, boolean, or null.  The analyzer owns type comparison.
        "value": {},
    },
    "field_calculator": {
        **_fields("input_ref", "geometry_from"),
        "field_name": _STRING,
        "expression": _STRING,
        "field_type": {**_STRING, "enum": ["double", "float", "integer", "string"]},
    },
    "slope": {
        "dem_from": _REF,
        "dem_path": _STRING,
        "degree": _BOOLEAN,
        "dst_path": _STRING,
    },
    "aspect": {
        "dem_from": _REF,
        "dem_path": _STRING,
        "degree": _BOOLEAN,
        "dst_path": _STRING,
    },
    "hillshade": {
        "dem_from": _REF,
        "dem_path": _STRING,
        "azimuth": _NUMBER,
        "altitude": _NUMBER,
        "dst_path": _STRING,
    },
    "zonal_statistics": {
        "raster_from": _REF,
        "vector_from": _REF,
        "raster_path": _STRING,
        "vector_path": _STRING,
        "stats": {"type": "array", "items": _STRING},
    },
    "reclassify_raster": {
        "src_from": _REF,
        "src_path": _STRING,
        "bins": {"type": "array", "items": _NUMBER},
        "values": {"type": "array", "items": _NUMBER},
        "dst_path": _STRING,
    },
}

_REQUIRED: dict[str, tuple[str, ...]] = {
    "geo_code": ("address",),
    "geo_transform": ("operation",),
    "query_poi": ("query",),
    "data_io_read": ("file_id",),
    "field_calculator": ("field_name", "expression"),
}


def build_native_tool_schema(tool_name: str) -> dict[str, Any]:
    """Build a closed OpenAI-compatible function schema for one registered tool."""
    spec = get_tool_spec(tool_name)
    properties = _TOOL_PROPERTIES.get(tool_name)
    if properties is None:
        raise KeyError(f"No native tool schema registered for {tool_name!r}")
    return {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": spec.description or f"Run {tool_name}.",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(_REQUIRED.get(tool_name, ())),
                "additionalProperties": False,
            },
        },
    }


def build_native_tool_schemas(tool_names: list[str]) -> list[dict[str, Any]]:
    """Build schemas for the tools visible to one sub-agent."""
    return [build_native_tool_schema(name) for name in tool_names]


def validate_tool_arguments(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Apply the deterministic subset of JSON Schema needed at the executor boundary."""
    if not isinstance(arguments, dict):
        raise ToolArgumentValidationError(f"{tool_name} arguments must be an object")
    schema = build_native_tool_schema(tool_name)["function"]["parameters"]
    properties = schema["properties"]
    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        raise ToolArgumentValidationError(
            f"{tool_name} received unexpected arguments: {', '.join(unknown)}"
        )
    missing = [name for name in schema["required"] if arguments.get(name) in (None, "")]
    if missing:
        raise ToolArgumentValidationError(
            f"{tool_name} missing required arguments: {', '.join(missing)}"
        )
    if tool_name == "extract_by_attribute" and not arguments.get("expression"):
        if not (arguments.get("field") and arguments.get("operator")):
            raise ToolArgumentValidationError(
                "extract_by_attribute requires expression or field and operator"
            )
    for name, value in arguments.items():
        _validate_value(tool_name, name, value, properties[name])
    return dict(arguments)


def _validate_value(tool_name: str, name: str, value: Any, schema: dict[str, Any]) -> None:
    expected = schema.get("type")
    valid = True
    if expected == "string":
        valid = isinstance(value, str)
    elif expected == "boolean":
        valid = isinstance(value, bool)
    elif expected == "integer":
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif expected == "number":
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif expected == "array":
        valid = isinstance(value, (list, tuple))
        if valid and schema.get("minItems") is not None:
            valid = len(value) >= int(schema["minItems"])
        if valid and schema.get("maxItems") is not None:
            valid = len(value) <= int(schema["maxItems"])
        item_type = (schema.get("items") or {}).get("type")
        if valid and item_type == "number":
            valid = all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value)
    if not valid:
        raise ToolArgumentValidationError(
            f"{tool_name}.{name} must match JSON Schema type {expected}"
        )
    if "enum" in schema and value not in schema["enum"]:
        raise ToolArgumentValidationError(
            f"{tool_name}.{name} must be one of {schema['enum']}"
        )
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolArgumentValidationError(f"{tool_name}.{name} is below minimum")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise ToolArgumentValidationError(f"{tool_name}.{name} must be greater than zero")


def native_reference_data(state: dict[str, Any]) -> dict[int, Any]:
    """Return the stable numeric reference table used by prompts and handlers."""
    session_vars = state.get("session_vars") or {}
    dependency_values: list[Any] = []
    for key, value in session_vars.items():
        if not str(key).startswith("dep_"):
            continue
        if isinstance(value, dict):
            primary = value.get("result")
            if primary is None:
                for alias in ("geojson", "pois", "layers", "locations"):
                    if value.get(alias) is not None:
                        primary = value[alias]
                        break
            dependency_values.append(primary if primary is not None else value)
        else:
            dependency_values.append(value)

    values: list[Any] = dependency_values
    if not values:
        # Backward compatibility for checkpoints that predate namespaced DAG
        # dependencies.  Upload metadata and internal controls are not runtime
        # geometry references and only make the model guess wrong indexes.
        for key, value in session_vars.items():
            key_text = str(key)
            if (
                key_text == "upload_file_ids"
                or key_text.startswith("upload_")
                or key_text.startswith("__")
            ):
                continue
            values.append(value)
    for result in state.get("tool_results") or []:
        data = result.data if hasattr(result, "data") else result.get("data") if isinstance(result, dict) else None
        if data is not None:
            values.append(data)
    return {index: value for index, value in enumerate(values)}


def native_reference_prompt(state: dict[str, Any]) -> str:
    """Render a compact catalog so the model can fill ``*_from`` indexes safely."""
    refs = native_reference_data(state)
    if not refs:
        return "No runtime references are available yet."
    lines = ["Runtime reference catalog (use these integer indexes in *_from/input_ref fields):"]
    for index, value in refs.items():
        if isinstance(value, dict):
            shape = f"dict keys={list(value)[:12]}"
        elif isinstance(value, list):
            shape = f"list length={len(value)}"
        else:
            shape = type(value).__name__
        lines.append(f"- {index}: {shape}")
    return "\n".join(lines)
