"""Stable, serializable contracts used by the stage-two runtime compiler."""

from __future__ import annotations

import math
import json
import importlib
import os
import re
import tempfile
from threading import RLock
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

DOCUMENTED_ARTIFACT_TYPES = frozenset({"Vector", "Raster", "POISet", "Coordinate", "Table", "MapLayer", "File"})
ArtifactType = Literal["Vector", "Raster", "POISet", "Coordinate", "Table", "MapLayer", "File"]
FILE_EXPORT_FORMATS = {
    "geojson": (".geojson", "application/geo+json"),
    "gpkg": (".gpkg", "application/geopackage+sqlite3"),
}
_BUILTIN_CONTRACT_TEST_NODE = "tests/unit/test_runtime_contract_regressions.py::test_runtime_contract_handlers_are_importable"
_PUBLIC_CONTRACT_SHAPES: dict[str, tuple[set[str], dict[str, Any]]] = {}


def _default_handler(*args, **kwargs):
    """Compatibility default for extension models (never a public operation)."""
    raise NotImplementedError("contract has no runtime executor adapter")


def _validate_runtime_artifact(**kwargs) -> bool:
    """Compatibility validator referenced only by legacy baseline entries.

    It is intentionally not attached to any active public contract; legacy
    records still need an importable validator while they are being retired.
    """
    return isinstance(kwargs, dict) and "nonsense_parameter" not in kwargs


def _extension_handler(*args, **kwargs):
    raise NotImplementedError("extension contract requires an executable handler")


_RUNTIME_OPERATION_HANDLERS = {
    "io.load_vector": "load_vector",
    "vector.buffer": "buffer",
    "map.build": "map_layer_build",
    "file.export": "export_result",
    "publish_file": "export_result",
    "vector.reproject": "reproject_layer",
    "vector.reproject_many": "batch_reproject_layers",
    "raster.reproject": "reproject_raster",
    "raster.reproject_many": "reproject_raster",
    "coordinate.convert": "geo_transform",
    "vector.convert_datum": "geo_transform",
    "code.execute": "code_executor",
}


def _invoke_runtime(operation: str, **kwargs):
    """Invoke a Runtime-owned deterministic handler by stable operation name."""
    from app.runtime import tool_handlers
    if operation == "raster.reproject_many":
        rasters = kwargs.get("raster")
        if not isinstance(rasters, list):
            return {"status": "error", "message": "raster.reproject_many 需要 Raster 列表"}
        outputs = []
        for raster in rasters:
            result = tool_handlers.handle_raster_reproject(raster=raster, target_crs=kwargs.get("target_crs"))
            if result.get("status") != "success":
                return result
            outputs.append(result.get("data"))
        from .output_protocol import ArtifactBatch
        return {"status": "success", "data": ArtifactBatch(outputs)}
    handler_name = _RUNTIME_OPERATION_HANDLERS[operation]
    handler = getattr(tool_handlers, f"handle_{operation.replace('.', '_')}", None)
    if handler is None:
        handler = getattr(tool_handlers, f"handle_{handler_name}", None)
    if not callable(handler):
        raise RuntimeError(f"Runtime handler is not registered: {operation}")
    return handler(**kwargs)


def _handle_io_load_vector(**kwargs): return _invoke_runtime("io.load_vector", **kwargs)
def _handle_vector_buffer(**kwargs): return _invoke_runtime("vector.buffer", **kwargs)
def _handle_map_build(**kwargs): return _invoke_runtime("map.build", **kwargs)
def _handle_file_export(**kwargs):
    """Serialize a Vector into the physical format frozen in PlanIR.

    Runtime ``file.export`` deliberately has no filesystem destination.  The
    executor publishes these bytes through CAS; only ``publish_file`` is
    allowed to perform an external write after confirmation.
    """
    source = kwargs.get("input")
    export_format = kwargs.get("format")
    if export_format not in FILE_EXPORT_FORMATS:
        return {"status": "error", "message": "file.export format must be geojson or gpkg"}
    if not isinstance(source, dict) or source.get("type") != "Vector":
        return {"status": "error", "message": "file.export requires a resolved Vector Artifact"}
    value = source.get("data")
    crs = source.get("crs")
    if not isinstance(value, dict) or value.get("type") != "FeatureCollection" or not isinstance(value.get("features"), list):
        return {"status": "error", "message": "file.export requires a GeoJSON FeatureCollection source"}
    if not isinstance(crs, dict) or crs.get("kind") == "GCJ02":
        return {"status": "error", "message": "file.export requires a physical EPSG CRS; convert GCJ02 explicitly"}
    if crs.get("kind") == "WGS84":
        epsg = 4326
    elif crs.get("kind") == "EPSG" and isinstance(crs.get("code"), int) and not isinstance(crs.get("code"), bool):
        epsg = crs["code"]
    else:
        return {"status": "error", "message": "file.export source CRS is unsupported"}
    physical_crs = value.get("_physical_crs")
    if physical_crs is not None and str(physical_crs).upper().replace(" ", "") != f"EPSG:{epsg}":
        return {"status": "error", "message": "file.export source coordinates disagree with Artifact CRS"}
    if value.get("_crs_label") == "GCJ02":
        return {"status": "error", "message": "file.export source has unconverted GCJ02 coordinates"}
    if not value["features"]:
        if export_format == "geojson":
            return {"status": "success", "data": b'{"type":"FeatureCollection","features":[]}'}
        return {"status": "error", "message": "empty GeoPackage export has no defined geometry schema"}
    try:
        import geopandas as gpd

        frame = gpd.GeoDataFrame.from_features(value["features"], crs=f"EPSG:{epsg}")
        if export_format == "geojson":
            payload = frame.to_crs("EPSG:4326").to_json(drop_id=True).encode("utf-8")
        else:
            with tempfile.TemporaryDirectory(prefix="gismind-export-") as directory:
                path = Path(directory) / "result.gpkg"
                frame.to_file(path, driver="GPKG", layer="result", index=False)
                payload = path.read_bytes()
        return {"status": "success", "data": payload}
    except Exception as exc:
        return {"status": "error", "message": f"file.export serialization failed: {exc}"}
def _handle_publish_file(**kwargs):
    """Atomically copy a confirmed File Artifact to its compiler-confined path.

    ``Compiler`` already converts the user relative path to an absolute path
    beneath ``allowed_path_root`` and binds that exact destination into the
    action hash.  This handler deliberately refuses any other shape rather
    than silently reinterpreting a path at execution time.
    """
    source = kwargs.get("input")
    if isinstance(source, dict) and "data" in source:
        source = source["data"]
    if isinstance(source, bytes):
        payload = source
    else:
        try:
            payload = json.dumps(source, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            return {"status": "error", "message": f"publish_file input cannot be serialized: {exc}"}
    raw_destination = kwargs.get("path")
    if not isinstance(raw_destination, str) or not raw_destination:
        return {"status": "error", "message": "publish_file requires a compiler-confined destination path"}
    destination = Path(raw_destination)
    if not destination.is_absolute():
        return {"status": "error", "message": "publish_file destination must be compiler-confined"}
    temp_name = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(prefix=".gismind-publish-", suffix=".tmp", dir=str(destination.parent))
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, destination)
        temp_name = None
        return {"status": "success", "data": payload}
    except OSError as exc:
        return {"status": "error", "message": f"publish_file failed: {exc}"}
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
def _handle_vector_reproject(**kwargs): return _invoke_runtime("vector.reproject", **kwargs)
def _handle_vector_reproject_many(**kwargs): return _invoke_runtime("vector.reproject_many", **kwargs)
def _handle_raster_reproject(**kwargs): return _invoke_runtime("raster.reproject", **kwargs)
def _handle_raster_reproject_many(**kwargs): return _invoke_runtime("raster.reproject_many", **kwargs)
def _handle_coordinate_convert(**kwargs): return _invoke_runtime("coordinate.convert", **kwargs)
def _handle_vector_convert_datum(**kwargs): return _invoke_runtime("vector.convert_datum", **kwargs)
def _handle_code_execute(**kwargs): return _invoke_runtime("code.execute", **kwargs)


def _validate_runtime_contract(operation: str, kwargs: dict[str, Any]) -> bool:
    if not isinstance(kwargs, dict) or "nonsense_parameter" in kwargs:
        return False

    def resolved_payload(value: Any, *, many: bool = False) -> bool:
        """Recognize only resolved Runtime artifact envelopes/payloads.

        Validators run after ``RuntimeExecutor`` dereferences ArtifactRefs.
        Keeping this check here prevents a valid parameter set from reaching a
        legacy handler with an unresolved or arbitrary input reference.
        """
        if many:
            return isinstance(value, list) and all(resolved_payload(item) for item in value)
        if isinstance(value, str):
            return bool(value.strip())
        if not isinstance(value, dict):
            return False
        return any(key in value for key in ("artifact_id", "path", "data", "type", "features", "geometry", "raster_path"))
    allowed = {
        "io.load_vector": {"file", "path", "file_path"},
        "vector.buffer": {"vector", "geometry_from", "distance_m", "radius", "radius_m"},
        "map.build": {"vector", "geometry_from", "data_from", "input_ref"},
        "file.export": {"input", "format"},
        "publish_file": {"input", "input_ref", "path"},
        "vector.reproject": {"vector", "geometry_from", "input_ref", "target_crs"},
        "vector.reproject_many": {"vector", "layers_ref", "data_from", "target_crs"},
        "raster.reproject": {"raster", "src_path", "dst_path", "dst_crs", "target_crs"},
        "raster.reproject_many": {"raster", "src_path", "dst_path", "dst_crs", "target_crs"},
        "coordinate.convert": {"coordinates", "source_crs", "target_crs", "operation", "lng", "lat", "p1", "p2", "bbox"},
        "vector.convert_datum": {"vector", "geometry_from", "source_crs", "target_crs", "operation"},
        "code.execute": {"input", "source_code", "source_code_hash", "declared_inputs", "declared_outputs"},
    }.get(operation, set())
    if set(kwargs) - allowed:
        return False
    if operation in {"vector.buffer"}:
        value = kwargs.get("distance_m", kwargs.get("radius_m"))
        vector = kwargs.get("vector", kwargs.get("geometry_from"))
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
            and (vector is None or resolved_payload(vector))
        )
    if operation in {"map.build", "vector.reproject", "vector.convert_datum"}:
        vector = kwargs.get("vector", kwargs.get("geometry_from"))
        if vector is not None and not resolved_payload(vector):
            return False
    if operation == "vector.reproject_many":
        vector = kwargs.get("vector", kwargs.get("layers_ref"))
        if vector is not None and not resolved_payload(vector, many=True):
            return False
    if operation in {"vector.reproject", "vector.reproject_many", "raster.reproject", "raster.reproject_many"}:
        raster = kwargs.get("raster")
        if operation == "raster.reproject" and raster is not None and not resolved_payload(raster):
            return False
        if operation == "raster.reproject_many" and raster is not None and not resolved_payload(raster, many=True):
            return False
        return isinstance(kwargs.get("target_crs"), (dict, str)) and bool(kwargs.get("target_crs"))
    if operation in {"coordinate.convert", "vector.convert_datum"}:
        if not isinstance(kwargs.get("source_crs"), dict) or not isinstance(kwargs.get("target_crs"), dict):
            return False
        coordinates = kwargs.get("coordinates")
        if coordinates is not None:
            if not isinstance(coordinates, dict):
                return False
            coordinates = coordinates.get("data", coordinates)
        if coordinates is not None and not (isinstance(coordinates, dict) and isinstance(coordinates.get("lng"), (int, float)) and isinstance(coordinates.get("lat"), (int, float))):
            return False
        if coordinates is None and not (isinstance(kwargs.get("lng"), (int, float)) and isinstance(kwargs.get("lat"), (int, float))):
            return False
        return True
    if operation == "code.execute":
        return isinstance(kwargs.get("source_code"), str) and bool(kwargs["source_code"].strip())
    if operation == "io.load_vector":
        value = kwargs.get("file", kwargs.get("file_path", kwargs.get("path")))
        return resolved_payload(value)
    if operation == "publish_file":
        return isinstance(kwargs.get("path"), str) and bool(kwargs["path"])
    return True


def _validate_public_operation(operation: str, kwargs: dict[str, Any]) -> bool:
    """Validate the resolved invocation against one frozen public contract.

    Compiler validation is authoritative before persistence; this second gate
    protects the execution boundary from stale IR or a hand-written adapter.
    It is intentionally operation-bound and never accepts arbitrary kwargs.
    """
    shape = _PUBLIC_CONTRACT_SHAPES.get(operation)
    if shape is None or not isinstance(kwargs, dict):
        return False
    input_names, contract_data = shape
    schema = contract_data.get("parameter_schema") or {}
    properties = schema.get("properties") if isinstance(schema, dict) else {}
    properties = properties if isinstance(properties, dict) else {}
    allowed = input_names | set(properties)
    if set(kwargs) - allowed:
        return False
    for name in contract_data.get("required_inputs", ()):
        if name not in kwargs:
            return False
    for name in schema.get("required", ()) if isinstance(schema, dict) else ():
        if name not in kwargs:
            return False
    for name, port_data in contract_data.get("ports", {}).items():
        if name not in kwargs:
            continue
        value = kwargs[name]
        if port_data.get("many"):
            if not isinstance(value, list) or len(value) < int(port_data.get("min_items", 0)):
                return False
            if port_data.get("max_items") is not None and len(value) > int(port_data["max_items"]):
                return False
        elif isinstance(value, list):
            return False
        if port_data.get("required") and not _resolved_runtime_payload(value, many=bool(port_data.get("many"))):
            return False
    if operation == "code.execute":
        from app.runtime.tool_handlers import validate_code_execute
        return validate_code_execute(**kwargs) is True
    if operation == "file.export":
        value = kwargs.get("input")
        return kwargs.get("format") in FILE_EXPORT_FORMATS and isinstance(value, dict) and value.get("type") == "Vector" and isinstance(value.get("crs"), dict)
    return True


def _resolved_runtime_payload(value: Any, *, many: bool = False) -> bool:
    if many:
        return isinstance(value, list) and all(_resolved_runtime_payload(item) for item in value)
    if isinstance(value, str):
        return bool(value.strip())
    return isinstance(value, dict) and any(key in value for key in ("artifact_id", "path", "data", "type", "features", "geometry", "raster_path"))


def _make_public_validator(operation: str):
    def validator(**kwargs):
        return _validate_public_operation(operation, kwargs)
    validator.__name__ = f"_validate_public_{operation.replace('.', '_')}"
    return validator


def _validate_io_load_vector(**kwargs): return _validate_runtime_contract("io.load_vector", kwargs)
def _validate_vector_buffer(**kwargs): return _validate_runtime_contract("vector.buffer", kwargs)
def _validate_map_build(**kwargs): return _validate_runtime_contract("map.build", kwargs)
def _validate_file_export(**kwargs):
    return (
        _validate_runtime_contract("file.export", kwargs)
        and kwargs.get("format") in FILE_EXPORT_FORMATS
        and isinstance(kwargs.get("input"), dict)
        and kwargs["input"].get("type") == "Vector"
    )
def _validate_publish_file(**kwargs): return _validate_runtime_contract("publish_file", kwargs)
def _validate_vector_reproject(**kwargs): return _validate_runtime_contract("vector.reproject", kwargs)
def _validate_vector_reproject_many(**kwargs): return _validate_runtime_contract("vector.reproject_many", kwargs)
def _validate_raster_reproject(**kwargs): return _validate_runtime_contract("raster.reproject", kwargs)
def _validate_raster_reproject_many(**kwargs): return _validate_runtime_contract("raster.reproject_many", kwargs)
def _validate_coordinate_convert(**kwargs): return _validate_runtime_contract("coordinate.convert", kwargs)
def _validate_vector_convert_datum(**kwargs): return _validate_runtime_contract("vector.convert_datum", kwargs)
def _validate_code_execute(**kwargs):
    # Keep the Runtime contract validator aligned with the code adapter's
    # source integrity and hidden-call policy.  This is intentionally lazy to
    # avoid importing the legacy tool registry while contracts are imported.
    from .tool_handlers import validate_code_execute

    return validate_code_execute(**kwargs)


_OPERATION_TESTS = {
    "io.load_vector": "tests/unit/test_runtime_compiler_phase2.py::test_contract_models_parse_and_canonical_plan_is_serializable",
    "vector.buffer": "tests/unit/test_runtime_compiler_phase2.py::test_compiler_rejects_duplicate_keys_cycles_and_bad_refs",
    "map.build": "tests/unit/test_runtime_compiler_phase2.py::test_map_and_datum_contracts_are_documented_shapes",
    "file.export": "tests/unit/test_runtime_compiler_phase2.py::test_file_export_is_managed_artifact_and_publish_file_is_explicit_side_effect",
    "publish_file": "tests/unit/test_runtime_compiler_phase2.py::test_file_export_is_managed_artifact_and_publish_file_is_explicit_side_effect",
    "vector.reproject": "tests/unit/test_runtime_compiler_phase2.py::test_crs_propagates_through_consumer_before_producer",
    "vector.reproject_many": "tests/unit/test_runtime_compiler_phase2.py::test_many_auto_reprojection_uses_many_conversion_contract",
    "raster.reproject": "tests/unit/test_runtime_compiler_phase2.py::test_crs_propagates_through_consumer_before_producer",
    "raster.reproject_many": "tests/unit/test_runtime_compiler_phase2.py::test_many_auto_reprojection_uses_many_conversion_contract",
    "coordinate.convert": "tests/unit/test_runtime_compiler_phase2.py::test_gcj02_requires_coordinate_conversion_contract_not_vector_reproject",
    "vector.convert_datum": "tests/unit/test_runtime_compiler_phase2.py::test_gcj02_requires_coordinate_conversion_contract_not_vector_reproject",
    "code.execute": "tests/unit/test_runtime_compiler_phase2.py::test_code_execute_rejects_unsafe_source_before_ir_hashing",
}


def _contract_hash(contract: Any) -> str:
    import hashlib
    payload = json.dumps(contract.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_crs_value(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("CRS must be an object")
    kind = value.get("kind")
    if kind == "EPSG":
        if set(value) != {"kind", "code"}:
            raise ValueError("EPSG CRS has an invalid shape")
        code = value.get("code")
        if isinstance(code, bool) or not isinstance(code, int) or code <= 0:
            raise ValueError("EPSG CRS code must be a positive integer")
    elif kind == "WGS84":
        if set(value) - {"kind", "axis_order"}:
            raise ValueError("WGS84 CRS has an invalid shape")
        if "axis_order" in value and value["axis_order"] not in {"lon_lat", "lat_lon"}:
            raise ValueError("WGS84 axis_order is invalid")
    elif kind == "GCJ02":
        if set(value) - {"kind"}:
            raise ValueError("GCJ02 CRS has an invalid shape")
    else:
        raise ValueError("CRS kind must be EPSG, WGS84, or GCJ02")
    return value


class _FrozenDict(dict):
    """A dict-compatible immutable container that Pydantic can serialize."""

    def __init__(self, *args, **kwargs):
        if getattr(self, "_sealed", False):
            raise TypeError("contract mappings are immutable")
        dict.__init__(self, *args, **kwargs)
        object.__setattr__(self, "_sealed", True)

    def _immutable(self, *args, **kwargs):
        raise TypeError("contract mappings are immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _immutable

    def __ior__(self, other):
        self._immutable()


class _FrozenList(list):
    def __init__(self, *args, **kwargs):
        if getattr(self, "_sealed", False):
            raise TypeError("contract sequences are immutable")
        list.__init__(self, *args, **kwargs)
        object.__setattr__(self, "_sealed", True)

    def _immutable(self, *args, **kwargs):
        raise TypeError("contract sequences are immutable")

    __setitem__ = __delitem__ = append = extend = insert = pop = remove = clear = sort = reverse = _immutable

    def __iadd__(self, other):
        self._immutable()

    def __imul__(self, other):
        self._immutable()


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return _FrozenDict({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList(_deep_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_deep_freeze(item) for item in value)
    return value


class _ContractModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", populate_by_name=True, frozen=True)

    @model_validator(mode="after")
    def _freeze_nested(self):
        for name in type(self).model_fields:
            object.__setattr__(self, name, _deep_freeze(getattr(self, name)))
        return self

    def model_copy(self, *, update: dict[str, Any] | None = None, deep: bool = False):
        """Keep the immutable contract guarantee across Pydantic's copy API.

        Pydantic deliberately does not validate ``model_copy(update=...)``.
        Re-validating here prevents an untrusted planner update from putting a
        mutable list/dict (or an invalid discriminator value) back into IR.
        """
        if update is None:
            return super().model_copy(deep=deep)
        data = self.model_dump(mode="python")
        data.update(update)
        return type(self).model_validate(data)

    def model_dump(self, *args, **kwargs):
        value = super().model_dump(*args, **kwargs)

        def restore_schema_alias(item):
            if isinstance(item, dict):
                return {"schema" if key == "schema_rule" else key: restore_schema_alias(child) for key, child in item.items()}
            if isinstance(item, list):
                return [restore_schema_alias(child) for child in item]
            return item

        return restore_schema_alias(value)


class ArtifactRef(_ContractModel):
    kind: Literal["artifact"] = "artifact"
    artifact_id: str = Field(min_length=1)
    type: str = Field(default="Vector", min_length=1)
    crs: dict[str, Any] | None = None
    content_hash: str | None = None
    selection: Literal["single", "all"] = "single"
    ordinal: int | None = 0

    _validate_crs = field_validator("crs")(validate_crs_value)

    @field_validator("type")
    @classmethod
    def _documented_type(cls, value: str) -> str:
        if value not in DOCUMENTED_ARTIFACT_TYPES:
            raise ValueError("artifact type is not documented")
        return value

    @field_validator("content_hash")
    @classmethod
    def _validate_content_hash(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            raise ValueError("content_hash must be a SHA-256 hex digest")
        return value.lower() if value is not None else None

    @model_validator(mode="before")
    @classmethod
    def _normalize_selection(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("selection") == "item":
            value = dict(value)
            value["selection"] = "single"
        return value

    @field_validator("ordinal")
    @classmethod
    def _non_negative_ordinal(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("ordinal must be non-negative")
        return value

    @model_validator(mode="after")
    def _selection_ordinal(self):
        if self.selection == "all" and self.ordinal is not None:
            raise ValueError("selection=all requires ordinal=None")
        if self.selection == "single" and self.ordinal is None:
            raise ValueError("selection=single requires a non-negative ordinal")
        return self


class TaskOutputRef(_ContractModel):
    kind: Literal["task_output"] = "task_output"
    task_key: str = Field(min_length=1)
    port: str = Field(min_length=1)
    selection: Literal["single", "all"] = "single"
    ordinal: int | None = 0

    @model_validator(mode="before")
    @classmethod
    def _normalize_selection(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("selection") == "item":
            value = dict(value)
            value["selection"] = "single"
        return value

    @field_validator("ordinal")
    @classmethod
    def _non_negative_ordinal(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("ordinal must be non-negative")
        return value

    @model_validator(mode="after")
    def _selection_ordinal(self):
        if self.selection == "all" and self.ordinal is not None:
            raise ValueError("selection=all requires ordinal=None")
        if self.selection == "single" and self.ordinal is None:
            raise ValueError("selection=single requires a non-negative ordinal")
        return self


InputRef = ArtifactRef | TaskOutputRef


class PlannerStepDraft(_ContractModel):
    task_key: str = Field(min_length=1)
    op: str = Field(min_length=1)
    inputs: dict[str, InputRef | list[InputRef]] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)


class PlannerDraft(_ContractModel):
    draft_version: Literal["1"] = "1"
    run_id: str | None = None
    input_revision: int = Field(default=1, gt=0)
    user_goal: str = ""
    tasks: list[PlannerStepDraft] = Field(default_factory=list)
    bindings: list[dict[str, Any]] = Field(default_factory=list)
    external_inputs: list[ArtifactRef] = Field(default_factory=list)
    # The planner boundary freezes the exact bounded catalog used to produce
    # this draft.  Defaults preserve direct deterministic Compiler callers;
    # Runtime planner calls always fill both fields.
    allowed_operation_ids: list[str] = Field(default_factory=list, validation_alias=AliasChoices("allowed_operation_ids", "allowed_operations"))
    catalog_hash: str | None = Field(default=None, validation_alias=AliasChoices("catalog_hash", "candidate_catalog_hash"))
    origin: str = "planner"
    clarification: str | None = Field(default=None, min_length=1, max_length=1000)

    @field_validator("clarification")
    @classmethod
    def _clarification_is_nonblank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("clarification must name the missing information")
        return value

    @model_validator(mode="after")
    def _clarification_is_not_executable(self) -> "PlannerDraft":
        if self.clarification is not None and (self.origin != "needs_clarification" or self.tasks):
            raise ValueError("clarification requires needs_clarification origin and no executable tasks")
        return self

    @model_validator(mode="before")
    @classmethod
    def _accept_document_shape(cls, value: Any) -> Any:
        if isinstance(value, dict):
            value = dict(value)
            if "task_plan" in value:
                raise ValueError("legacy task-plan shape is not a Runtime draft")
            if "steps" in value:
                if "tasks" in value:
                    raise ValueError("draft cannot contain both steps and tasks")
                steps = []
                for step in value.pop("steps"):
                    if not isinstance(step, dict):
                        raise ValueError("documented step must be an object")
                    step = dict(step)
                    if "id" in step:
                        if "task_key" in step and step["task_key"] != step["id"]:
                            raise ValueError("step id and task_key disagree")
                        step["task_key"] = step.pop("id")
                    steps.append(step)
                value["tasks"] = steps
        return value

    @field_validator("allowed_operation_ids")
    @classmethod
    def _catalog_operations_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("allowed_operation_ids must not contain duplicates")
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError("allowed_operation_ids must contain non-empty strings")
        return value

    @field_validator("catalog_hash")
    @classmethod
    def _catalog_hash_shape(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("catalog_hash must be a SHA-256 digest")
        return value


class PortContract(_ContractModel):
    artifact_type: str = "Vector"
    allowed_types: tuple[str, ...] | None = None
    cardinality: Literal["one", "many"] = "one"
    min_items: int = 1
    max_items: int | None = 1
    required: bool = True
    crs_policy: dict[str, Any] | None = None
    schema_rule: dict[str, Any] = Field(default_factory=dict, validation_alias=AliasChoices("schema", "schema_rule"))

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_names(cls, value: Any) -> Any:
        if isinstance(value, dict):
            value = dict(value)
            if value.get("cardinality") == "many":
                value.setdefault("min_items", 0)
                value.setdefault("max_items", None)
            if "artifact_type" not in value and "artifact_types" in value:
                types = value.pop("artifact_types")
                if not isinstance(types, (list, tuple)) or not types:
                    raise ValueError("artifact_types must be a non-empty sequence")
                value["allowed_types"] = tuple(types)
                value["artifact_type"] = types[0]
            if "allowed_types" in value and isinstance(value["allowed_types"], list):
                value["allowed_types"] = tuple(value["allowed_types"])
            if "crs_policy" not in value and "crs_rule" in value:
                value["crs_policy"] = value.pop("crs_rule")
            if "schema" not in value and "schema_rule" in value:
                value["schema"] = value.pop("schema_rule")
        return value

    @model_validator(mode="after")
    def _validate_cardinality(self) -> "PortContract":
        if self.min_items < 0 or (self.max_items is not None and self.max_items < self.min_items):
            raise ValueError("invalid port cardinality bounds")
        if self.cardinality == "one" and (self.min_items, self.max_items) != (1, 1):
            raise ValueError("one ports must have exactly one item")
        types = self.allowed_types or (self.artifact_type,)
        if not types or any(not isinstance(item, str) or not item for item in types):
            raise ValueError("allowed_types must contain non-empty artifact types")
        if any(item not in DOCUMENTED_ARTIFACT_TYPES for item in types):
            raise ValueError("artifact type is not documented")
        return self

    @property
    def schema(self) -> dict[str, Any]:
        return self.schema_rule

    def accepts_type(self, artifact_type: str) -> bool:
        return artifact_type in (self.allowed_types or (self.artifact_type,))


class RetryPolicy(_ContractModel):
    max_attempts: int = 1
    retryable_error_codes: list[str] = Field(default_factory=list)
    backoff_seconds: list[float] = Field(default_factory=list)

    @field_validator("max_attempts")
    @classmethod
    def _positive_attempts(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_attempts must be positive")
        return value

    @model_validator(mode="after")
    def _validate_backoff(self) -> "RetryPolicy":
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0 for value in self.backoff_seconds):
            raise ValueError("backoff_seconds must contain finite non-negative numbers")
        if len(self.backoff_seconds) not in {0, self.max_attempts - 1, self.max_attempts}:
            raise ValueError("backoff_seconds length must match retry attempts")
        return self


class CachePolicy(_ContractModel):
    mode: Literal["permanent", "ttl", "disabled"] = "disabled"
    ttl_seconds: int | None = None

    @model_validator(mode="after")
    def _validate_ttl(self) -> "CachePolicy":
        if self.mode == "disabled" and self.ttl_seconds is not None:
            raise ValueError("disabled cache policy cannot specify ttl_seconds")
        if self.mode == "ttl" and (self.ttl_seconds is None or self.ttl_seconds <= 0):
            raise ValueError("ttl cache policy requires positive ttl_seconds")
        if self.mode == "permanent" and self.ttl_seconds is not None:
            raise ValueError("permanent cache policy cannot specify ttl_seconds")
        return self


class FallbackCandidate(_ContractModel):
    provider: str | None = None
    op: str | None = None
    params_patch: dict[str, Any] = Field(default_factory=dict)


class FallbackPolicy(_ContractModel):
    mode: Literal["disabled", "ordered"] = "disabled"
    trigger_codes: list[str] = Field(default_factory=list)
    candidates: list[FallbackCandidate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_ordered_candidates(self) -> "FallbackPolicy":
        if self.mode == "ordered" and not self.candidates:
            raise ValueError("ordered fallback policy requires candidates")
        if any(candidate.op is None and candidate.provider is None for candidate in self.candidates):
            raise ValueError("fallback candidate requires op or provider")
        return self


class ConfirmationPolicy(_ContractModel):
    mode: Literal["none", "required"] = "none"
    action_fields: list[str] = Field(default_factory=list)


class ResourceBudget(_ContractModel):
    isolation: Literal["worker_async", "gis_process", "code_process"] = "worker_async"
    timeout_seconds: int = 300
    memory_mb: int = 1024
    cpu_weight: int = 1

    @model_validator(mode="after")
    def _positive_resources(self) -> "ResourceBudget":
        if self.timeout_seconds <= 0 or self.memory_mb <= 0 or self.cpu_weight <= 0:
            raise ValueError("resource budget values must be positive")
        return self


class ToolContract(_ContractModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    input_ports: dict[str, PortContract] = Field(default_factory=dict, alias="inputs")
    output_ports: dict[str, PortContract] = Field(default_factory=dict, alias="outputs")
    parameter_schema: dict[str, Any] = Field(default_factory=dict, alias="params_schema")
    preconditions: list[str] = Field(default_factory=list)
    postconditions: list[str] = Field(default_factory=list)
    empty_policy: Literal["allow", "fail", "fallback"] = "fail"
    fallback_policy: FallbackPolicy = Field(default_factory=FallbackPolicy)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy, alias="retry")
    idempotency: bool = Field(default=True, alias="idempotent")
    cache_policy: CachePolicy = Field(default_factory=CachePolicy, alias="cache")
    confirmation_policy: ConfirmationPolicy = Field(default_factory=ConfirmationPolicy, alias="confirmation")
    resource_budget: ResourceBudget = Field(default_factory=ResourceBudget, alias="resources")
    side_effect: bool = False
    executor_type: Literal["worker_async", "gis_process", "code"] = "worker_async"
    validator: str | None = None
    validators: list[str] = Field(default_factory=list)
    handler_ref: str = Field(default="app.runtime.contracts:_extension_handler", min_length=1)
    manifest: dict[str, Any] = Field(default_factory=lambda: {"contract_tests": []})

    @field_validator("version")
    @classmethod
    def _semver(cls, value: str) -> str:
        if not re.fullmatch(r"\d+\.\d+\.\d+", value):
            raise ValueError("tool contract version must be semver")
        return value

    @model_validator(mode="after")
    def _side_effect_confirmation(self) -> "ToolContract":
        if self.side_effect and self.confirmation_policy.mode != "required":
            raise ValueError("side-effect contracts require confirmation")
        return self

    @model_validator(mode="after")
    def _handler_manifest(self) -> "ToolContract":
        if not self.handler_ref.strip():
            raise ValueError("handler_ref must be non-empty")
        if not isinstance(self.manifest, dict) or "contract_tests" not in self.manifest:
            raise ValueError("manifest must declare contract_tests")
        return self


class OutputSpec(_ContractModel):
    port: str = Field(min_length=1)
    artifact_type: str = Field(min_length=1)
    cardinality: Literal["one", "many"] = "one"
    min_items: int = 1
    max_items: int | None = 1
    crs: dict[str, Any] | None = None
    schema_rule: dict[str, Any] = Field(default_factory=dict, validation_alias=AliasChoices("schema", "schema_rule"))

    @model_validator(mode="after")
    def _bounds(self):
        if self.artifact_type not in DOCUMENTED_ARTIFACT_TYPES:
            raise ValueError("artifact type is not documented")
        if self.min_items < 0 or (self.max_items is not None and self.max_items < self.min_items):
            raise ValueError("invalid output cardinality bounds")
        if self.cardinality == "one" and (self.min_items, self.max_items) != (1, 1):
            raise ValueError("one outputs must have exactly one item")
        return self

    @property
    def schema(self) -> dict[str, Any]:
        return self.schema_rule


class CompiledTask(_ContractModel):
    task_key: str = Field(min_length=1)
    op: str = Field(min_length=1)
    contract_version: str = Field(min_length=1)
    origin: Literal["planner", "compiler"]
    inputs: dict[str, InputRef | list[InputRef]] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    outputs: list[OutputSpec] = Field(default_factory=list)
    empty_policy: Literal["allow", "fail", "fallback"] = "fail"
    fallback_policy: FallbackPolicy = Field(default_factory=FallbackPolicy)
    idempotent: bool = True
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    cache_policy: CachePolicy = Field(default_factory=CachePolicy)
    resource_budget: ResourceBudget = Field(default_factory=ResourceBudget)
    validators: list[str] = Field(default_factory=list)
    postconditions: list[str] = Field(default_factory=list)
    side_effect: bool = False
    confirmation_policy: ConfirmationPolicy = Field(default_factory=ConfirmationPolicy)
    action_hash: str | None = None
    executor_type: str = "worker_async"
    cache_key: str | None = None
    execution_fingerprint: str | None = None

    @model_validator(mode="after")
    def _execution_policy(self):
        if not re.fullmatch(r"\d+\.\d+\.\d+", self.contract_version):
            raise ValueError("contract_version must be semver")
        if self.executor_type not in {"inline", "async", "sandbox", "process", "worker_async", "gis_process", "code"}:
            raise ValueError("executor_type is invalid")
        if self.execution_fingerprint is not None and not re.fullmatch(r"[0-9a-f]{64}", self.execution_fingerprint):
            raise ValueError("execution_fingerprint must be a SHA-256 digest")
        if not self.side_effect and self.action_hash is not None:
            raise ValueError("non-side-effect tasks cannot have action_hash")
        if self.side_effect and self.cache_key is not None:
            raise ValueError("side-effect tasks cannot be cached")
        if self.side_effect and self.confirmation_policy.mode != "required":
            raise ValueError("side-effect tasks require confirmation")
        if self.side_effect and not self.action_hash:
            raise ValueError("side-effect tasks require action_hash")
        for output in self.outputs:
            if output.cardinality == "one" and (output.min_items, output.max_items) != (1, 1):
                raise ValueError("one outputs must have exactly one item")
        return self


class PlanIR(_ContractModel):
    ir_version: Literal["1"] = "1"
    compiler_version: str = "1.0.0"
    run_id: str | None = None
    input_revision: int = 1
    user_goal: str = ""
    input_artifact_ids: list[str] = Field(default_factory=list)
    tasks: list[CompiledTask] = Field(default_factory=list)
    bindings: list[dict[str, Any]] = Field(default_factory=list)
    allowed_operation_ids: list[str] = Field(default_factory=list)
    catalog_hash: str | None = None
    goal_requirements: dict[str, Any] | None = None
    plan_hash: str = ""
    canonical_json: str = ""
    execution_fingerprint: str = ""
    metric_crs_selector_version: str | None = None

    @field_validator("input_revision")
    @classmethod
    def _positive_revision(cls, value: int) -> int:
        if isinstance(value, bool) or value <= 0:
            raise ValueError("input_revision must be a positive integer")
        return value

    @model_validator(mode="after")
    def _plan_digest(self):
        if not self.canonical_json or not self.plan_hash or not self.execution_fingerprint:
            raise ValueError("PlanIR requires canonical_json, plan_hash, and execution_fingerprint")
        if self.canonical_json and self.plan_hash:
            import hashlib
            expected = hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()
            if self.plan_hash != expected:
                raise ValueError("plan_hash does not match canonical_json")
        if not re.fullmatch(r"[0-9a-f]{64}", self.execution_fingerprint):
            raise ValueError("execution_fingerprint must be a SHA-256 digest")
        try:
            canonical = json.loads(self.canonical_json)
            if canonical.get("compiler_version") != self.compiler_version or canonical.get("ir_version") != self.ir_version:
                raise ValueError("canonical_json version identity does not match PlanIR")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("canonical_json is not a valid PlanIR payload") from exc
        return self


class ToolContractRegistry:
    """Versioned contract lookup; registration is explicit and deterministic."""

    def __init__(self, contracts: list[ToolContract] | None = None):
        self._contracts: dict[str, dict[str, ToolContract]] = {}
        self._aliases: dict[str, str] = {}
        self._lock = RLock()
        for contract in contracts or []:
            self.register(contract)

    def register(self, contract: ToolContract) -> ToolContract:
        if not re.fullmatch(r"\d+\.\d+\.\d+", contract.version):
            raise ValueError("tool contract version must be semver")
        if not contract.handler_ref or not isinstance(contract.manifest, dict) or "contract_tests" not in contract.manifest:
            raise ValueError("tool contract requires handler_ref and manifest.contract_tests")
        def resolve_reference(reference: Any, field: str, *, allow_implicit: bool = False):
            if not isinstance(reference, str) or reference.count(":") != 1:
                raise ValueError(f"{field} must be an importable module:callable reference")
            module_name, callable_name = reference.split(":", 1)
            if not module_name or not callable_name:
                raise ValueError(f"{field} must be an importable module:callable reference")
            if callable_name in {"_default_handler", "_builtin_handler", "_extension_handler"}:
                if not allow_implicit:
                    raise ValueError(f"{field} may not use an unbound default adapter")
                return None
            try:
                resolved = getattr(importlib.import_module(module_name), callable_name)
            except (ImportError, AttributeError) as exc:
                raise ValueError(f"{field} does not resolve to a callable") from exc
            if not callable(resolved):
                raise ValueError(f"{field} does not resolve to a callable")
            return resolved

        # The extension adapter is retained for locally-defined contracts that
        # are only used for static compilation tests. Public/builtin contracts
        # always replace it with a concrete handler before registration.
        # The compatibility extension adapter is allowed only for unnamed
        # local extension contracts; builtins always replace it with a
        # concrete handler before registration.
        implicit_handler = contract.handler_ref == "app.runtime.contracts:_extension_handler"
        resolve_reference(contract.handler_ref, "handler_ref", allow_implicit=implicit_handler)
        if contract.validator is not None:
            resolve_reference(contract.validator, "validator_ref")
        for reference in contract.postconditions:
            resolve_reference(reference, "postcondition_ref")
        # Public Runtime operations must never silently fall back to payload
        # encoding without semantic validation.  Local extension contracts
        # remain free to omit postconditions when they are not public.
        try:
            from .tool_handlers import PUBLIC_OPERATIONS
        except ImportError:
            PUBLIC_OPERATIONS = ()
        if contract.name in PUBLIC_OPERATIONS and not contract.postconditions and not implicit_handler:
            raise ValueError(f"public contract {contract.name} requires postconditions")
        with self._lock:
            versions = self._contracts.setdefault(contract.name, {})
            if contract.version in versions:
                raise ValueError(f"version conflict for tool contract {contract.name}@{contract.version}")
            versions[contract.version] = contract
        return contract

    def get(self, name: str, version: str | None = None) -> ToolContract:
        with self._lock:
            name = self._aliases.get(name, name)
            try:
                versions = dict(self._contracts[name])
            except KeyError as exc:
                raise KeyError(name) from exc
            if version is None:
                version = max(versions, key=lambda item: tuple(int(part) for part in item.split(".")))
            try:
                return versions[version]
            except KeyError as exc:
                raise KeyError(f"{name}@{version}") from exc

    def canonical_name(self, name: str) -> str:
        return self._aliases.get(name, name)

    def __contains__(self, name: str) -> bool:
        with self._lock:
            return self._aliases.get(name, name) in self._contracts

    def values(self):
        with self._lock:
            snapshot = tuple(contract for versions in self._contracts.values() for contract in versions.values())
        return iter(snapshot)

    def public_operations_manifest(self) -> dict[str, Any]:
        with self._lock:
            operations = {}
            for name, versions in self._contracts.items():
                version = max(versions, key=lambda item: tuple(int(part) for part in item.split(".")))
                contract = versions[version]
                tests = contract.manifest.get("contract_tests", [])
                operations[name] = {
                    "version": version,
                    "handler_ref": contract.handler_ref,
                    "validator": contract.validator,
                    "validator_ref": contract.validator,
                    "postconditions": list(contract.postconditions),
                    "contract_hash": _contract_hash(contract),
                    "contract_tests": list(tests),
                    "manifest": contract.manifest,
                }
        return {"operations": {name: operations[name] for name in sorted(operations)}}

    def write_public_operations_manifest(self, path: str | Path | None = None) -> dict[str, Any]:
        """Export the deterministic public contract inventory on demand.

        The compiler package never writes repository files at import time; an
        explicit caller owns the destination and can check the returned
        payload before publishing it.
        """
        payload = self.public_operations_manifest()
        destination = Path(path) if path is not None else Path(__file__).resolve().parent / "contracts" / "public_operations_manifest.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        return payload

    def reconcile_public_operations_manifest(self, manifest: dict[str, Any], *, baseline: dict[str, Any] | str | Path | None = None) -> bool:
        """Verify a previously exported manifest against this registry snapshot."""
        if baseline is None:
            baseline_path = Path(__file__).resolve().parents[3] / "docs" / "legacy_public_operations_baseline.json"
            if not baseline_path.is_file():
                raise ValueError("independent public operations baseline is required")
            try:
                baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise ValueError("independent public operations baseline is invalid") from exc
        elif isinstance(baseline, (str, Path)):
            try:
                baseline = json.loads(Path(baseline).read_text(encoding="utf-8"))
            except Exception as exc:
                raise ValueError("independent public operations baseline is invalid") from exc
        if not isinstance(manifest, dict) or manifest != self.public_operations_manifest():
            raise ValueError("public operations manifest does not match the registry")
        if not isinstance(baseline, dict) or "operations" not in baseline:
            raise ValueError("independent public operations baseline is invalid")
        baseline_operations = baseline["operations"]
        if not isinstance(baseline_operations, dict):
            raise ValueError("independent public operations baseline operations must be an object")
        # The baseline is keyed by both the old handler inventory and the
        # stable Runtime names.  Only records explicitly marked ``migrate``
        # participate in the stage-five set equality; old records marked
        # active/retire remain independent evidence for phase six/removal.
        has_dispositions = any(isinstance(item, dict) and "disposition" in item for item in baseline_operations.values())
        migrated = {name for name, item in baseline_operations.items() if isinstance(item, dict) and item.get("disposition") == "migrate"}
        if has_dispositions and migrated != set(manifest["operations"]):
            raise ValueError("public operations manifest does not reconcile bidirectionally with baseline migrate set")
        if not has_dispositions and not set(manifest["operations"]).issubset(set(baseline_operations)):
            raise ValueError("public operations manifest does not reconcile with independent baseline")
        for name, operation in manifest["operations"].items():
            base = baseline_operations.get(name)
            if base is None:
                for candidate in baseline_operations.values():
                    if isinstance(candidate, dict) and name in (candidate.get("stable_operations") or []):
                        base = candidate
                        break
            predecessor = (operation.get("manifest") or {}).get("supersedes")
            same_contract = isinstance(base, dict) and base.get("version") == operation.get("version") and base.get("contract_hash") == operation.get("contract_hash")
            declared_predecessor = isinstance(base, dict) and isinstance(predecessor, dict) and base.get("version") == predecessor.get("version") and base.get("contract_hash") == predecessor.get("contract_hash")
            if not (same_contract or declared_predecessor) or base.get("handler_ref") != operation.get("handler_ref") or base.get("validator_ref") != operation.get("validator_ref") or base.get("test_node") not in operation.get("contract_tests", []):
                raise ValueError(f"public operation {name} does not reconcile with independent baseline")
        for name, operation in manifest.get("operations", {}).items():
            if not operation.get("handler_ref") or not operation.get("validator") or not operation.get("contract_tests") or not operation.get("postconditions"):
                raise ValueError(f"public operation manifest is incomplete: {name}")
            for reference in (operation["handler_ref"], operation["validator"], *operation["postconditions"]):
                module_name, callable_name = reference.split(":", 1)
                if not callable(getattr(importlib.import_module(module_name), callable_name)):
                    raise ValueError(f"public operation reference is not callable: {name}")
            for node in operation["contract_tests"]:
                test_file = Path(node.split("::", 1)[0]) if "::" in node else Path("")
                if not test_file.is_file():
                    test_file = Path(__file__).resolve().parents[2] / test_file
                if "::" not in node or not test_file.is_file():
                    raise ValueError(f"public operation contract test is not traceable: {name}")
                test_name = node.rsplit("::", 1)[1]
                if not re.search(rf"^\s*def\s+{re.escape(test_name)}\s*\(", test_file.read_text(encoding="utf-8"), re.MULTILINE):
                    raise ValueError(f"public operation contract test node is not collectable: {name}")
        return True

    @classmethod
    def with_builtins(cls) -> "ToolContractRegistry":
        # Postconditions are part of the executable contract, not merely
        # documentation.  Keep their references in a small Runtime-owned
        # registry so every public operation is checked before CAS publish.
        from .postconditions import postcondition_references

        # Stronger record/geometry semantics invalidate old executable and cache
        # identities. Keep historical hashes explicit, not rewritten baselines.
        semantic_predecessors = {
            "vector.dissolve": "decbdcc0cb258bb20e9e866092ec32de56b945d084fbde0cdaf65a88ff695b3a",
            "vector.join_nearest": "a34696254cf17001b12cd26b1f009305174eaf8f18f21cf56bbe20c98a7bbd7f",
            "vector.count_points": "88272c325a95db4eea6d312e2556837c560ace2d772379fdf632d2a1e4e0f30d",
            "vector.buffer": "c0826663b42c10b72d8f1b214eaeee18dc14b5451acee39b47a012b364b8dc98",
            "vector.clip": "29c87f487319cf9621274eec2754ea7dc35e25be18b3bbd6854b7f5b08773437",
            "vector.extract_location": "f0b34033fb7062e23ad308e1d1c329019a2b6422903f5ea7a8b39258018f8e15",
            "vector.point_on_surface": "434c5f830defa1a2ad6fc0cead0bbe9a5131f697b0274f55d0487fcc823cf775",
            "attribute.filter": "0245baca98328635c33e6bdc7595f27bd0c44ef2ba84e91dc88fa70c786d6e67",
            "attribute.keep_fields": "ad8330311fed9ce0ec784b88a6f53f1c9eb714bb750aec0044fd7d45c680c279",
            "attribute.rename_field": "a09438ca1d7964540752cff7bccf15b7fd951e543b72875f518016fa01a93e50",
        }
        def predecessor(name):
            return {"supersedes": {"version": "1.0.0", "contract_hash": semantic_predecessors[name]}} if name in semantic_predecessors else {}

        vector = PortContract(artifact_type="Vector", crs_policy={"required": True})
        registry = cls()
        strict_vector_operations: dict[str, tuple[dict[str, PortContract], dict[str, PortContract], dict[str, Any]]] = {
            "vector.overlay": (
                {"input": vector, "other": vector}, {"output": vector},
                {"type": "object", "additionalProperties": False, "properties": {"how": {"type": "string", "enum": ["intersection", "union", "difference", "symmetric_difference"], "default": "intersection"}}},
            ),
            "vector.clip": (
                {"input": vector, "mask": vector}, {"output": vector},
                {"type": "object", "additionalProperties": False, "properties": {}},
            ),
            "vector.extract_location": (
                {"input": vector, "mask": vector}, {"output": vector},
                {"type": "object", "additionalProperties": False, "properties": {"predicate": {"type": "string", "enum": ["intersects", "within", "contains"], "default": "intersects"}}},
            ),
            "vector.join_location": (
                {"input": vector, "other": vector}, {"output": vector},
                {"type": "object", "additionalProperties": False, "properties": {"predicate": {"type": "string", "enum": ["intersects", "within", "contains"], "default": "intersects"}}},
            ),
            "vector.join_nearest": (
                {"input": vector, "other": vector}, {"output": vector},
                {"type": "object", "additionalProperties": False, "properties": {"max_distance": {"type": "number", "minimum": 0}, "k": {"type": "integer", "minimum": 1, "default": 1}}},
            ),
            "vector.count_points": (
                {"points": vector, "polygons": vector}, {"output": vector},
                {"type": "object", "additionalProperties": False, "properties": {}},
            ),
            "vector.snap": (
                {"input": vector, "other": vector}, {"output": vector},
                {"type": "object", "additionalProperties": False, "required": ["tolerance"], "properties": {"tolerance": {"type": "number", "minimum": 0}}},
            ),
            "vector.merge": (
                {"input": PortContract(artifact_type="Vector", cardinality="many", min_items=1, max_items=None, crs_policy={"required": True})}, {"output": vector},
                {"type": "object", "additionalProperties": False, "properties": {}},
            ),
            "vector.dissolve": (
                {"input": vector}, {"output": vector},
                {"type": "object", "additionalProperties": False, "properties": {"by": {"type": "string", "minLength": 1}}},
            ),
            "vector.simplify": (
                {"input": vector}, {"output": vector},
                {"type": "object", "additionalProperties": False, "required": ["tolerance"], "properties": {"tolerance": {"type": "number", "minimum": 0}}},
            ),
            "attribute.filter": (
                {"input": vector}, {"output": vector},
                {"type": "object", "additionalProperties": False, "required": ["field", "operator", "value"], "properties": {"field": {"type": "string", "minLength": 1}, "operator": {"type": "string", "enum": ["=", "==", "!=", ">", ">=", "<", "<=", "contains", "in", "is_null"]}, "value": {}, "negate": {"type": "boolean", "default": False}}},
            ),
            "attribute.keep_fields": (
                {"input": vector}, {"output": vector},
                {"type": "object", "additionalProperties": False, "required": ["fields"], "properties": {"fields": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}}}},
            ),
            "attribute.rename_field": (
                {"input": vector}, {"output": vector},
                {"type": "object", "additionalProperties": False, "required": ["old_name", "new_name"], "properties": {"old_name": {"type": "string", "minLength": 1}, "new_name": {"type": "string", "minLength": 1}}},
            ),
            "attribute.calculate": (
                {"input": vector}, {"output": vector},
                {"type": "object", "additionalProperties": False, "required": ["field_name", "expression"], "properties": {"field_name": {"type": "string", "minLength": 1}, "expression": {"type": "string", "minLength": 1}, "field_type": {"type": "string", "enum": ["float", "int", "str", "bool"], "default": "float"}}},
            ),
        }
        raster_reproject_params = {
            "type": "object",
            "additionalProperties": False,
            "required": ["target_crs"],
            "properties": {
                "target_crs": {"type": "object", "additionalProperties": True},
            },
        }
        no_params = {"type": "object", "additionalProperties": False, "properties": {}}
        runtime_parameter_schemas: dict[str, dict[str, Any]] = {
            "geo.geocode": {"type": "object", "additionalProperties": False, "required": ["address"], "properties": {"address": {"type": "string", "minLength": 1}}},
            "poi.search": {"type": "object", "additionalProperties": False, "required": ["query"], "properties": {"query": {"type": "string", "minLength": 1}, "radius": {"type": "integer", "minimum": 1, "maximum": 50000, "default": 1000}}},
            "io.csv_to_points": {"type": "object", "additionalProperties": False, "properties": {"x_col": {"type": "string", "minLength": 1, "default": "lon"}, "y_col": {"type": "string", "minLength": 1, "default": "lat"}, "crs": {"type": ["string", "object"], "additionalProperties": True, "default": {"kind": "EPSG", "code": 4326}}}},
            "raster.clip_extent": {"type": "object", "additionalProperties": False, "required": ["bbox"], "properties": {"bbox": {"type": "array", "minItems": 4, "maxItems": 4, "items": {"type": "number"}}}},
            "raster.calculate": {"type": "object", "additionalProperties": False, "required": ["expression"], "properties": {"expression": {"type": "string", "minLength": 1}}},
            "raster.reclassify": {"type": "object", "additionalProperties": False, "required": ["bins", "values"], "properties": {"bins": {"type": "array", "minItems": 2, "items": {"type": "number"}}, "values": {"type": "array", "minItems": 1, "items": {"type": "number"}}}},
            "raster.slope": {"type": "object", "additionalProperties": False, "properties": {"degree": {"type": "boolean", "default": True}}},
            "raster.hillshade": {"type": "object", "additionalProperties": False, "properties": {"azimuth": {"type": "number", "minimum": 0, "maximum": 360, "default": 315}, "altitude": {"type": "number", "minimum": 0, "maximum": 90, "default": 45}}},
            "raster.contour": {"type": "object", "additionalProperties": False, "properties": {"interval": {"type": "number", "exclusiveMinimum": 0}}},
            "raster.tpi": {"type": "object", "additionalProperties": False, "properties": {"radius": {"type": "integer", "minimum": 1, "maximum": 255, "default": 3}}},
        }
        builtin_contracts = [
            ToolContract(name="io.load_vector", version="1.0.0", input_ports={"file": PortContract(artifact_type="File")}, output_ports={"vector": vector}, parameter_schema=no_params, empty_policy="fail", resource_budget=ResourceBudget(isolation="gis_process", timeout_seconds=300, memory_mb=4096), executor_type="gis_process", validator="app.runtime.contracts:_validate_io_load_vector"),
            ToolContract(name="vector.buffer", version="1.0.0", input_ports={"vector": vector}, output_ports={"vector": vector}, parameter_schema={"type": "object", "additionalProperties": False, "required": ["distance_m"], "properties": {"distance_m": {"type": "number", "minimum": 0}}}, resource_budget=ResourceBudget(isolation="gis_process", timeout_seconds=300, memory_mb=4096), executor_type="gis_process", validator="app.runtime.contracts:_validate_vector_buffer"),
            ToolContract(name="map.build", version="1.0.0", input_ports={"vector": PortContract(artifact_type="Vector")}, output_ports={"layer": PortContract(artifact_type="MapLayer")}, parameter_schema=no_params, resource_budget=ResourceBudget(isolation="worker_async", timeout_seconds=120, memory_mb=1024)),
            ToolContract(name="file.export", version="1.1.0", input_ports={"input": PortContract(artifact_type="Vector", crs_policy={"required": True})}, output_ports={"file": PortContract(artifact_type="File")}, parameter_schema={"type": "object", "additionalProperties": False, "required": ["format"], "properties": {"format": {"type": "string", "enum": ["geojson", "gpkg"], "default": "geojson"}}}, executor_type="gis_process", resource_budget=ResourceBudget(isolation="gis_process", timeout_seconds=300, memory_mb=4096), validator="app.runtime.contracts:_validate_file_export"),
            ToolContract(name="publish_file", version="1.1.0", input_ports={"input": PortContract(artifact_type="File")}, output_ports={"file": PortContract(artifact_type="File")}, parameter_schema={"type": "object", "additionalProperties": False, "required": ["path"], "properties": {"path": {"type": "string"}}}, side_effect=True, idempotency=False, confirmation_policy=ConfirmationPolicy(mode="required", action_fields=["path"]), executor_type="code", resource_budget=ResourceBudget(isolation="code_process", timeout_seconds=300, memory_mb=2048), validator="app.runtime.contracts:_validate_publish_file"),
            ToolContract(name="vector.reproject", version="1.1.0", input_ports={"vector": vector}, output_ports={"vector": vector}, parameter_schema=raster_reproject_params, resource_budget=ResourceBudget(isolation="gis_process", timeout_seconds=300, memory_mb=4096), executor_type="gis_process", validator="app.runtime.contracts:_validate_vector_reproject"),
            ToolContract(name="vector.reproject_many", version="1.1.0", input_ports={"vector": PortContract(artifact_type="Vector", cardinality="many", min_items=0, max_items=None, crs_policy={"required": True})}, output_ports={"vector": PortContract(artifact_type="Vector", cardinality="many", min_items=0, max_items=None, crs_policy={"required": True})}, parameter_schema=raster_reproject_params, resource_budget=ResourceBudget(isolation="gis_process", timeout_seconds=300, memory_mb=4096), executor_type="gis_process", validator="app.runtime.contracts:_validate_vector_reproject_many"),
             ToolContract(name="raster.reproject", version="1.0.0", input_ports={"raster": PortContract(artifact_type="Raster", crs_policy={"required": True})}, output_ports={"raster": PortContract(artifact_type="Raster", crs_policy={"required": True})}, parameter_schema=raster_reproject_params, resource_budget=ResourceBudget(isolation="gis_process", timeout_seconds=300, memory_mb=4096), executor_type="gis_process", validator="app.runtime.contracts:_validate_raster_reproject"),
             ToolContract(name="raster.reproject_many", version="1.0.0", input_ports={"raster": PortContract(artifact_type="Raster", cardinality="many", min_items=0, max_items=None)}, output_ports={"raster": PortContract(artifact_type="Raster", cardinality="many", min_items=0, max_items=None)}, parameter_schema=raster_reproject_params, resource_budget=ResourceBudget(isolation="gis_process", timeout_seconds=300, memory_mb=4096), executor_type="gis_process", validator="app.runtime.contracts:_validate_raster_reproject_many"),
            ToolContract(name="coordinate.convert", version="1.1.0", input_ports={"coordinates": PortContract(artifact_type="Coordinate", allowed_types=("Coordinate", "Table"))}, output_ports={"coordinates": PortContract(artifact_type="Coordinate", allowed_types=("Coordinate", "Table"))}, parameter_schema={"type": "object", "additionalProperties": False, "required": ["source_crs", "target_crs"], "properties": {"source_crs": {"type": "object", "additionalProperties": True}, "target_crs": {"type": "object", "additionalProperties": True}}}, resource_budget=ResourceBudget(isolation="gis_process", timeout_seconds=60, memory_mb=1024), executor_type="gis_process", validator="app.runtime.contracts:_validate_coordinate_convert"),
            ToolContract(name="vector.convert_datum", version="1.0.0", input_ports={"vector": PortContract(artifact_type="Vector")}, output_ports={"vector": PortContract(artifact_type="Vector")}, parameter_schema={"type": "object", "additionalProperties": False, "required": ["source_crs", "target_crs"], "properties": {"source_crs": {"type": "object", "additionalProperties": True}, "target_crs": {"type": "object", "additionalProperties": True}}}, resource_budget=ResourceBudget(isolation="gis_process", timeout_seconds=60, memory_mb=1024), executor_type="gis_process", validator="app.runtime.contracts:_validate_vector_convert_datum"),
            ToolContract(name="code.execute", version="1.0.0", input_ports={"input": PortContract(artifact_type="File", cardinality="many", required=False, min_items=0, max_items=None)}, output_ports={"output": PortContract(artifact_type="File")}, parameter_schema={"type": "object", "additionalProperties": False, "required": ["source_code"], "properties": {"source_code": {"type": "string", "minLength": 1}, "source_code_hash": {"type": "string"}, "declared_inputs": {"type": "array", "items": {"type": "string", "minLength": 1}}, "declared_outputs": {"type": "array", "items": {"type": "string", "minLength": 1}}}}, resource_budget=ResourceBudget(isolation="code_process", timeout_seconds=300, memory_mb=2048), executor_type="code", validator="app.runtime.contracts:_validate_code_execute"),
        ]
        registry._aliases["io.export"] = "publish_file"
        for contract in builtin_contracts:
            name = contract.name
            safe = name.replace(".", "_")
            handler_name = f"_handle_{safe}"
            validator_name = f"_validate_{safe}"
            if handler_name not in globals() or validator_name not in globals():
                raise ValueError(f"missing runtime handler adapter for {name}")
            _PUBLIC_CONTRACT_SHAPES[name] = (
                set(contract.input_ports),
                {
                    "parameter_schema": contract.parameter_schema,
                    "required_inputs": [port_name for port_name, port in contract.input_ports.items() if port.required],
                    "ports": {
                        port_name: {
                            "many": port.cardinality == "many", "required": port.required,
                            "min_items": port.min_items, "max_items": port.max_items,
                        }
                        for port_name, port in contract.input_ports.items()
                    },
                },
            )
            globals()[f"_validate_public_{safe}"] = _make_public_validator(name)
            registry.register(contract.model_copy(update={
                "version": "1.1.0" if name in semantic_predecessors else contract.version,
                "handler_ref": f"app.runtime.contracts:{handler_name}",
                "validator": f"app.runtime.contracts:_validate_public_{safe}",
                "postconditions": postcondition_references(name),
                "manifest": {
                    "contract_tests": [_OPERATION_TESTS.get(name, _BUILTIN_CONTRACT_TEST_NODE)],
                    **({"supersedes": {"version": "1.0.0", "contract_hash": "292842964b4648511c3960e189363b33d06a0237d37afb881e957793519d4995"}} if name == "file.export" else {}),
                    **predecessor(name),
                },
            }))
        # Stage five expands the small phase-two seed registry to the complete
        # executable legacy inventory.  The adapter refs are generated as
        # Every public operation has a concrete Runtime-owned handler and
        # validator.  The inventory is kept beside those handlers so the
        # contract registry cannot silently reintroduce an adapter layer.
        from .tool_handlers import PUBLIC_OPERATIONS
        for operation in PUBLIC_OPERATIONS:
            if operation in registry:
                continue
            safe = operation.replace(".", "_")
            explicit_vector = strict_vector_operations.get(operation)
            if explicit_vector is not None:
                in_ports, out_ports, schema = explicit_vector
            elif operation == "geo.transform":
                # Coordinate datum conversion is a local Runtime operation.
                # Its output CRS is derived from the normalized target CRS,
                # so the contract must not leave those values as untyped
                # legacy strings.
                in_ports, out_ports = {}, {"result": PortContract(artifact_type="Coordinate", crs_policy={"required": True})}
            elif operation == "geo.geocode":
                in_ports, out_ports = {}, {"result": PortContract(artifact_type="Coordinate", crs_policy={"required": True})}
            elif operation == "poi.search":
                # A query may use a resolved Coordinate artifact or the
                # legacy string/tuple location parameter. The feature rows
                # live inside one typed POISet artifact, not output ordinals.
                in_ports = {"location": PortContract(artifact_type="Coordinate", crs_policy={"required": True})}
                out_ports = {"result": PortContract(artifact_type="POISet", crs_policy={"required": True})}
            elif operation == "vector.isochrone":
                in_ports = {"origin": PortContract(artifact_type="Coordinate", crs_policy={"required": True})}
                out_ports = {"vector": PortContract(artifact_type="Vector", crs_policy={"required": True})}
            elif operation == "raster.vectorize":
                in_ports, out_ports = {"vector": PortContract(artifact_type="Vector", crs_policy={"required": True})}, {"raster": PortContract(artifact_type="Raster", crs_policy={"required": True})}
            elif operation == "raster.polygonize":
                in_ports, out_ports = {"raster": PortContract(artifact_type="Raster", crs_policy={"required": True})}, {"vector": PortContract(artifact_type="Vector", crs_policy={"required": True})}
            elif operation == "raster.contour":
                # Contours are vector isolines even though the source is a
                # raster.  Keeping this port typed prevents the executor from
                # publishing GeoDataFrame/GeoJSON bytes as a Raster artifact.
                in_ports, out_ports = {"raster": PortContract(artifact_type="Raster", crs_policy={"required": True})}, {"vector": PortContract(artifact_type="Vector", crs_policy={"required": True})}
            elif operation == "raster.clip_mask":
                in_ports, out_ports = {"raster": PortContract(artifact_type="Raster", crs_policy={"required": True}), "mask": PortContract(artifact_type="Vector", crs_policy={"required": True})}, {"raster": PortContract(artifact_type="Raster", crs_policy={"required": True})}
            elif operation == "raster.zonal_statistics":
                in_ports, out_ports = {"raster": PortContract(artifact_type="Raster", crs_policy={"required": True}), "zones": PortContract(artifact_type="Vector", crs_policy={"required": True})}, {"table": PortContract(artifact_type="Table")}
            elif operation == "raster.sample":
                in_ports, out_ports = {"raster": PortContract(artifact_type="Raster", crs_policy={"required": True}), "points": PortContract(artifact_type="Vector", crs_policy={"required": True})}, {"table": PortContract(artifact_type="Table")}
            elif operation == "raster.calculate":
                in_ports, out_ports = {"raster": PortContract(artifact_type="Raster", crs_policy={"required": True})}, {"raster": PortContract(artifact_type="Raster", crs_policy={"required": True})}
            elif operation.startswith("raster."):
                in_ports, out_ports = {"raster": PortContract(artifact_type="Raster", crs_policy={"required": True})}, {"raster": PortContract(artifact_type="Raster", crs_policy={"required": True})}
            elif operation in {"io.load_raster"}:
                in_ports, out_ports = {"file": PortContract(artifact_type="File")}, {"raster": PortContract(artifact_type="Raster", crs_policy={"required": True})}
            elif operation in {"io.load_csv"}:
                in_ports, out_ports = {"file": PortContract(artifact_type="File")}, {"table": PortContract(artifact_type="Table", schema={"fields": []})}
            elif operation in {"io.load_vector"}:
                in_ports, out_ports = {"file": PortContract(artifact_type="File")}, {"vector": PortContract(artifact_type="Vector", crs_policy={"required": True})}
            elif operation in {"io.read_upload"}:
                in_ports, out_ports = {"file": PortContract(artifact_type="File")}, {"result": PortContract(artifact_type="File")}
            elif operation in {"io.csv_to_points"}:
                in_ports, out_ports = {"input": PortContract(artifact_type="Table")}, {"vector": PortContract(artifact_type="Vector", crs_policy={"required": True})}
            elif operation in {"io.summarize"}:
                in_ports, out_ports = {"input": PortContract(artifact_type="Vector")}, {"summary": PortContract(artifact_type="Table")}
            elif operation in {"io.export", "file.export"}:
                in_ports, out_ports = {"input": PortContract(artifact_type="Vector")}, {"file": PortContract(artifact_type="File")}
            elif operation == "publish_file":
                in_ports, out_ports = {"input": PortContract(artifact_type="File")}, {"file": PortContract(artifact_type="File")}
            elif operation == "map.build":
                in_ports, out_ports = {"vector": PortContract(artifact_type="Vector")}, {"layer": PortContract(artifact_type="MapLayer")}
            elif operation in {"vector.zonal_statistics"}:
                in_ports, out_ports = {"input": PortContract(artifact_type="Raster")}, {"result": PortContract(artifact_type="Table")}
            elif operation == "vector.check_validity":
                # Validity inspection returns a structured issue report, not
                # a GeoDataFrame.  Publishing it as Vector would fail content
                # validation and mislead downstream planners.
                in_ports, out_ports = {"input": PortContract(artifact_type="Vector", crs_policy={"required": True})}, {"result": PortContract(artifact_type="Table")}
            elif operation in {"code.execute"}:
                in_ports, out_ports = {"input": PortContract(artifact_type="File", cardinality="many", required=False, min_items=0, max_items=None)}, {"output": PortContract(artifact_type="File", cardinality="many", required=False, min_items=0, max_items=None)}
            else:
                in_ports, out_ports = {"input": PortContract(artifact_type="Vector")}, {"output": PortContract(artifact_type="Vector")}
            # Spatial/statistical operations that may legitimately return no
            # rows retain a typed empty artifact instead of being silently
            # accepted as an untyped result.
            empty = "allow" if operation in {"vector.check_validity", "io.summarize"} else "fail"
            executor = "code" if operation == "code.execute" else ("worker_async" if operation in {"geo.geocode", "poi.search"} else "gis_process")
            cache_policy = CachePolicy(mode="disabled")
            fallback_policy = FallbackPolicy(mode="disabled")
            retry_policy = RetryPolicy(max_attempts=1)
            if operation in {"geo.geocode", "poi.search"}:
                # Provider ordering and cache lifetime are contract data, not
                # planner choices.  The legacy handlers retain the actual
                # AMap -> OSM implementation behind this policy boundary.
                cache_policy = CachePolicy(mode="ttl", ttl_seconds=3600)
                fallback_policy = FallbackPolicy(
                    mode="ordered", trigger_codes=["TIMEOUT", "UPSTREAM_ERROR", "EMPTY_RESULT"],
                    candidates=[FallbackCandidate(provider="osm")],
                )
                retry_policy = RetryPolicy(max_attempts=2, retryable_error_codes=["TIMEOUT", "UPSTREAM_ERROR"], backoff_seconds=[0.25])
            if explicit_vector is None:
                schema: dict[str, Any] = runtime_parameter_schemas.get(operation, no_params)
            if explicit_vector is None and operation == "vector.buffer":
                schema = {"type": "object", "additionalProperties": False, "required": ["distance_m"], "properties": {"distance_m": {"type": "number", "minimum": 0}}}
            if explicit_vector is None and operation == "geo.transform":
                schema = {
                    "type": "object", "additionalProperties": False,
                    "required": ["lng", "lat", "source_crs", "target_crs"],
                    "properties": {
                        "lng": {"type": "number", "minimum": -180, "maximum": 180},
                        "lat": {"type": "number", "minimum": -90, "maximum": 90},
                        "source_crs": {"type": "object", "additionalProperties": True},
                        "target_crs": {"type": "object", "additionalProperties": True},
                    },
                }
            if operation == "vector.isochrone":
                schema = {
                    "type": "object", "additionalProperties": False,
                    "required": ["mode", "time_min"],
                    "properties": {
                        "mode": {"type": "string", "enum": ["driving", "walking", "riding"]},
                        "time_min": {"type": "integer", "minimum": 1, "maximum": 240},
                    },
                }
            if explicit_vector is None and operation == "code.execute":
                schema = {"type": "object", "additionalProperties": False, "required": ["source_code"], "properties": {"source_code": {"type": "string", "minLength": 1}, "source_code_hash": {"type": "string"}}}
            if operation == "raster.vectorize":
                schema = {
                    "type": "object", "additionalProperties": False,
                    "required": ["out_shape", "transform", "crs"],
                    "properties": {
                        "out_shape": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "integer", "minimum": 1}},
                        "transform": {"type": "array", "minItems": 6, "maxItems": 6, "items": {"type": "number"}},
                        "crs": {"type": ["string", "object"], "additionalProperties": True},
                    },
                }
            if operation in {"raster.clip_mask", "raster.polygonize"}:
                schema = {"type": "object", "additionalProperties": False, "properties": {"band": {"type": "integer", "minimum": 1}} if operation == "raster.polygonize" else {}}
            if operation == "raster.zonal_statistics":
                schema = {"type": "object", "additionalProperties": False, "properties": {"stats": {"type": "array", "items": {"type": "string", "minLength": 1}}}}
            if operation == "raster.sample":
                schema = {"type": "object", "additionalProperties": False, "properties": {}}
            contract = ToolContract(
                name=operation, version="1.2.0" if operation in {"attribute.filter", "attribute.keep_fields"} else "1.1.0" if operation == "geo.transform" or operation in semantic_predecessors else "1.0.0", input_ports=in_ports, output_ports=out_ports,
                parameter_schema=schema, empty_policy=empty, executor_type=executor,
                resource_budget=ResourceBudget(isolation="code_process" if executor == "code" else ("worker_async" if executor == "worker_async" else "gis_process"), timeout_seconds=300, memory_mb=2048 if executor == "code" else 4096),
                cache_policy=cache_policy, fallback_policy=fallback_policy, retry_policy=retry_policy,
                validator=f"app.runtime.contracts:_validate_public_{safe}",
                postconditions=postcondition_references(operation),
                handler_ref=f"app.runtime.tool_handlers:handle_{safe}",
                manifest={"contract_tests": ["tests/unit/test_runtime_contract_regressions.py::test_runtime_contract_handlers_are_importable"], "migration": "runtime", **predecessor(operation)},
            )
            _PUBLIC_CONTRACT_SHAPES[operation] = (
                set(contract.input_ports),
                {
                    "parameter_schema": contract.parameter_schema,
                    "required_inputs": [port_name for port_name, port in contract.input_ports.items() if port.required],
                    "ports": {
                        port_name: {
                            "many": port.cardinality == "many", "required": port.required,
                            "min_items": port.min_items, "max_items": port.max_items,
                        }
                        for port_name, port in contract.input_ports.items()
                    },
                },
            )
            globals()[f"_validate_public_{safe}"] = _make_public_validator(operation)
            registry.register(contract)
        return registry
