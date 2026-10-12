"""Bounded physical upload inspection and format-aware reads from immutable CAS."""

from __future__ import annotations

import csv
import json
import math
import multiprocessing
import shutil
import stat
import tempfile
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

INSPECTOR_VERSION = "3"
MAX_METADATA_BYTES = 1024 * 1024


def _crs(value):
    if not value:
        return None
    from pyproj import CRS

    try:
        code = CRS.from_user_input(value).to_epsg()
    except Exception as exc:
        raise ValueError("CRS 声明不是有效的坐标系") from exc
    if code is None:
        raise ValueError("文件 CRS 无法表示为受支持的 EPSG 编码，请先转换文件坐标系")
    return {"kind": "EPSG", "code": int(code)}


def _crs_name(value):
    if not value:
        return None
    if value.get("kind") == "EPSG":
        return f"EPSG:{value['code']}"
    if value.get("kind") == "WGS84":
        return "EPSG:4326"
    if value.get("kind") == "GCJ02":
        raise ValueError("GCJ02 不是 EPSG CRS，不能按 WGS84 比较或赋值")
    raise ValueError("unsupported CRS declaration")


def validate_zip(path: Path, *, max_total: int, max_count: int) -> list[zipfile.ZipInfo]:
    """Validate all members before a GIS reader or extraction sees the ZIP."""
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > max_count:
                raise ValueError("ZIP contains too many files")
            seen = set()
            total = 0
            for member in members:
                name = member.filename.replace("\\", "/")
                parts = name.split("/")
                normalized = name.casefold().rstrip("/")
                if name.startswith("/") or any(part == ".." or ":" in part or part.endswith((".", " ")) for part in parts):
                    raise ValueError("ZIP contains an unsafe path")
                if normalized in seen or stat.S_ISLNK(member.external_attr >> 16) or member.flag_bits & 1:
                    raise ValueError("ZIP contains duplicate, symbolic-link or encrypted members")
                seen.add(normalized)
                total += member.file_size
                if total > max_total:
                    raise ValueError("ZIP uncompressed size exceeds configured limit")
                if member.file_size and (member.compress_size == 0 or member.file_size / member.compress_size > 1000):
                    raise ValueError("ZIP compression ratio exceeds configured limit")
            return members
    except zipfile.BadZipFile as exc:
        raise ValueError("invalid ZIP archive") from exc


def _format(path: Path, filename: str) -> str:
    with path.open("rb") as stream:
        head = stream.read(65536)
    suffix = Path(filename).suffix.lower()
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "shapefile_zip"
    if head.startswith((b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")):
        return "geotiff"
    if head.startswith(b"SQLite format 3\x00"):
        return "gpkg"
    stripped = head.lstrip(b"\xef\xbb\xbf \r\n\t")
    if stripped.startswith(b"{"):
        return "geojson"
    if stripped.startswith(b"<"):
        return "kml"
    # Strong content signatures take precedence over the untrusted filename.
    # Unknown .zip bytes still go through the bounded archive validator.
    if suffix == ".zip":
        return "shapefile_zip"
    if suffix in {".shp", ".shx", ".dbf", ".prj"}:
        raise ValueError("Shapefile 需要把 .shp、.shx、.dbf 和可选 .prj 一起打包为 ZIP")
    if suffix in {".geojson", ".json", ".gpkg", ".tif", ".tiff", ".kml"}:
        raise ValueError("文件内容与声明的 GIS 格式不符")
    try:
        text = head.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = head.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise ValueError("无法识别文件格式") from exc
    if "\x00" in text:
        raise ValueError("无法识别文件格式")
    if suffix in {".csv", ".tsv"}:
        return "tsv" if suffix == ".tsv" else "csv"
    try:
        delimiter = csv.Sniffer().sniff(text, delimiters=",\t;").delimiter
    except csv.Error as exc:
        raise ValueError("无法识别文件格式；支持 GeoJSON、CSV/TSV、GeoPackage、KML、GeoTIFF 和 Shapefile ZIP") from exc
    return "tsv" if delimiter == "\t" else "csv"


@contextmanager
def vector_source(path: Path, format_name: str, *, max_total=500 * 1024 * 1024, max_count=100):
    """Use fixed local names; uploaded member paths never become host paths."""
    with tempfile.TemporaryDirectory(prefix="gismind-upload-") as directory:
        root = Path(directory)
        if format_name == "shapefile_zip":
            members = validate_zip(path, max_total=max_total, max_count=max_count)
            files = {m.filename.replace("\\", "/").casefold(): m for m in members if not m.is_dir()}
            shapes = [name for name in files if name.endswith(".shp")]
            if len(shapes) != 1:
                raise ValueError("ZIP 必须包含且仅包含一个 Shapefile 图层")
            stem = shapes[0][:-4]
            for extension in (".shp", ".shx", ".dbf"):
                if stem + extension not in files:
                    raise ValueError(f"Shapefile ZIP 缺少配套文件 {extension}")
            with zipfile.ZipFile(path) as archive:
                for extension in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
                    member = files.get(stem + extension)
                    if member is not None:
                        with archive.open(member) as source, (root / ("layer" + extension)).open("wb") as destination:
                            shutil.copyfileobj(source, destination)
            yield root / "layer.shp"
        else:
            extension = {"geojson": ".geojson", "gpkg": ".gpkg", "kml": ".kml"}[format_name]
            target = root / ("input" + extension)
            shutil.copyfile(path, target)
            yield target


def read_table(path: Path, facts: dict) -> dict:
    import pandas as pd
    from app.tools.csv_reader import read_csv_frame

    frame = read_csv_frame(path, encoding=facts["encoding"], delimiter=facts["delimiter"])
    records = frame.astype(object).where(pd.notna(frame), None).to_dict(orient="records")
    return {"columns": list(frame.columns), "row_count": len(frame), "rows": records, "sample": records[:5]}


def _geographic_geojson_coordinates(payload: dict) -> bool:
    """Check geometry positions, never arbitrary property arrays or a bbox."""
    geographic = True

    def coordinates(value):
        nonlocal geographic
        if not isinstance(value, list):
            raise ValueError("GeoJSON coordinates 必须是数组")
        if not value:
            return
        if isinstance(value[0], list):
            for child in value:
                coordinates(child)
            return
        if len(value) < 2 or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in value):
            raise ValueError("GeoJSON 坐标必须包含至少两个有限数值")
        geographic = geographic and -180 <= value[0] <= 180 and -90 <= value[1] <= 90

    def geometry(value):
        if value is None:
            return
        if not isinstance(value, dict):
            raise ValueError("GeoJSON geometry 必须是对象")
        if value.get("type") == "GeometryCollection":
            for child in value.get("geometries", []):
                geometry(child)
        else:
            coordinates(value.get("coordinates"))

    features = payload.get("features") if payload.get("type") == "FeatureCollection" else [payload]
    if not isinstance(features, list):
        raise ValueError("GeoJSON features 必须是数组")
    for feature in features:
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise ValueError("GeoJSON features 必须包含 Feature 对象")
        geometry(feature.get("geometry"))
    return geographic


def inspect_file(path: str | Path, filename: str, *, max_total=500 * 1024 * 1024, max_count=100) -> dict[str, Any]:
    path = Path(path)
    format_name = _format(path, filename)
    facts: dict[str, Any] = {"version": INSPECTOR_VERSION, "source": "physical_file", "format": format_name, "crs": None, "crs_status": "not_applicable", "crs_source": "physical_file"}
    if format_name == "shapefile_zip":
        # Admission limits are part of the immutable inspection result. The
        # worker must not silently fall back to smaller module defaults.
        facts["zip_limits"] = {"max_total": max_total, "max_count": max_count}
    if format_name in {"csv", "tsv"}:
        from app.tools.csv_reader import read_csv_frame

        # UTF-8 first; decoding fallback is explicit and reused by the loader.
        encoding = "utf-8-sig"
        try:
            text = path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            encoding = "gb18030"
            text = path.read_text(encoding=encoding)
        delimiter = "\t" if format_name == "tsv" else ","
        try:
            delimiter = csv.Sniffer().sniff(text[:65536], delimiters=",\t;").delimiter
        except csv.Error:
            pass
        frame = read_csv_frame(path, encoding=encoding, delimiter=delimiter)
        facts.update(artifact_type="Table", encoding=encoding, delimiter=delimiter, row_count=len(frame), fields={str(key): str(value) for key, value in frame.dtypes.items()})
        return facts
    if format_name == "geotiff":
        import rasterio

        with rasterio.open(path) as dataset:
            physical = _crs(dataset.crs)
            facts.update(artifact_type="Raster", crs=physical, crs_status="present" if physical else "missing", width=dataset.width, height=dataset.height, bands=dataset.count, dtypes=list(dataset.dtypes), nodata=dataset.nodata if dataset.nodata is None or math.isfinite(dataset.nodata) else None, bbox=list(dataset.bounds), transform=list(dataset.transform), fields={})
        return facts
    explicit_geojson_crs = None
    if format_name == "geojson":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict) or payload.get("type") not in {"FeatureCollection", "Feature"}:
            raise ValueError("JSON 不是 GeoJSON FeatureCollection 或 Feature")
        geographic = _geographic_geojson_coordinates(payload)
        if payload.get("crs") is not None:
            crs_value = payload["crs"]
            if not isinstance(crs_value, dict) or crs_value.get("type") != "name":
                raise ValueError("GeoJSON CRS 声明格式不受支持")
            explicit_geojson_crs = _crs(crs_value.get("properties", {}).get("name"))
            if explicit_geojson_crs is None:
                raise ValueError("GeoJSON CRS 声明缺少名称")
            from pyproj import CRS

            if CRS.from_epsg(explicit_geojson_crs["code"]).is_geographic and not geographic:
                raise ValueError("GeoJSON CRS 为经纬度坐标系，但坐标超出经纬度范围")
        elif geographic:
            # RFC 7946 section 4 defines WGS84 longitude/latitude. This is
            # format evidence, distinct from an embedded legacy CRS member.
            explicit_geojson_crs = {"kind": "EPSG", "code": 4326}
            facts["crs_source"] = "RFC7946"
        else:
            facts["crs_source"] = "missing"
    if format_name == "kml":
        from xml.etree import ElementTree
        from xml.parsers import expat

        content = path.read_bytes()
        def reject_external(*_args):
            raise ValueError("KML 不允许 DTD 或外部实体")
        parser = expat.ParserCreate()
        parser.StartDoctypeDeclHandler = reject_external
        parser.EntityDeclHandler = reject_external
        parser.ExternalEntityRefHandler = reject_external
        parser.Parse(content, True)
        root = ElementTree.fromstring(content)
        if root.tag.rsplit("}", 1)[-1].lower() != "kml" or any(node.tag.rsplit("}", 1)[-1] == "NetworkLink" for node in root.iter()):
            raise ValueError("KML 必须为本地数据且不含 NetworkLink")
        for node in root.iter():
            tag = node.tag.rsplit("}", 1)[-1].lower()
            references = [node.text or ""] if tag in {"href", "styleurl", "schemaurl"} else []
            references += [value for key, value in node.attrib.items() if key.rsplit("}", 1)[-1].lower() in {"href", "styleurl", "schemaurl", "base"}]
            if any(value.strip() and not value.strip().startswith("#") for value in references):
                raise ValueError("KML 不允许引用外部或本地文件")
    import pyogrio

    with vector_source(path, format_name, max_total=max_total, max_count=max_count) as source:
        layers = pyogrio.list_layers(source)
        if len(layers) != 1:
            raise ValueError("文件含多个图层，请拆分为单图层后上传")
        metadata = pyogrio.read_info(source, force_feature_count=True, force_total_bounds=True)
    if format_name == "gpkg" and metadata["driver"] != "GPKG":
        raise ValueError("SQLite 文件不是 GeoPackage")
    physical = explicit_geojson_crs if format_name == "geojson" else _crs(metadata.get("crs"))
    bbox = metadata.get("total_bounds")
    bbox = list(bbox) if bbox is not None and all(math.isfinite(float(x)) for x in bbox) else None
    facts.update(artifact_type="Vector", crs=physical, crs_status="present" if physical else "missing", feature_count=int(metadata["features"]), geometry_types=[str(metadata["geometry_type"])] if metadata.get("geometry_type") else [], fields={str(key): str(value) for key, value in zip(metadata["fields"], metadata["dtypes"])}, bbox=bbox)
    return facts


def validate_declarations(facts: dict, declared_crs: dict | None, declared_bbox: list | None) -> None:
    actual = facts.get("crs")
    if declared_crs and declared_crs.get("kind") != "GCJ02":
        _crs(_crs_name(declared_crs))
    if declared_crs and actual:
        if declared_crs.get("kind") == "GCJ02" or _crs_name(declared_crs) != _crs_name(actual):
            raise ValueError("DECLARED_METADATA_MISMATCH: declared CRS does not match file CRS")
    bounds = facts.get("bbox")
    if declared_bbox and bounds and any(not math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9) for a, b in zip(declared_bbox, bounds)):
        raise ValueError("DECLARED_METADATA_MISMATCH: declared bbox does not match file bounds")


def read_vector(path: Path, facts: dict, declared_crs: dict | None = None):
    import geopandas as gpd

    physical = facts.get("crs")
    effective = physical or declared_crs
    if not effective:
        raise ValueError("CRS_REQUIRED: 文件缺少坐标系，请提供明确的 CRS 声明")
    validate_declarations(facts, declared_crs, None)
    zip_options: dict[str, int] = {}
    if facts["format"] == "shapefile_zip":
        from app.config import get_settings

        current = get_settings()
        recorded = facts.get("zip_limits") or {"max_total": 500 * 1024 * 1024, "max_count": 100}
        if not isinstance(recorded, dict):
            raise ValueError("ZIP inspection limits are invalid")
        for key, current_limit in (
            ("max_total", int(current.UPLOAD_ZIP_MAX_TOTAL_SIZE) * 1024 * 1024),
            ("max_count", int(current.UPLOAD_ZIP_MAX_FILE_COUNT)),
        ):
            value = recorded.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("ZIP inspection limits are invalid")
            zip_options[key] = min(value, current_limit)
    with vector_source(path, facts["format"], **zip_options) as source:
        frame = gpd.read_file(source)
    # Non-standard GeoJSON with projected coordinates can still receive a
    # GDAL WGS84 default. Only a persisted declaration may replace it.
    if effective.get("kind") == "GCJ02":
        # GeoDataFrame uses an EPSG geographic carrier internally; the datum
        # marker is mandatory and _jsonable preserves it in the payload. This
        # is never a claim that GCJ02 and EPSG:4326 are equivalent.
        frame = frame.set_crs("EPSG:4326", allow_override=True)
        frame.attrs["crs_label"] = "GCJ02"
    elif physical is None:
        frame = frame.set_crs(_crs_name(effective), allow_override=True)
    return frame


def read_raster(path: Path, facts: dict, declared_crs: dict | None = None) -> bytes:
    if facts.get("artifact_type") != "Raster":
        raise ValueError("上传文件不是栅格数据")
    physical = facts.get("crs")
    effective = physical or declared_crs
    if not effective:
        raise ValueError("CRS_REQUIRED: 栅格文件缺少坐标系，请提供明确的 CRS 声明")
    if effective.get("kind") == "GCJ02":
        raise ValueError("栅格不支持 GCJ02 CRS 声明")
    validate_declarations(facts, declared_crs, None)
    if physical:
        return path.read_bytes()
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.shutil import copy as copy_raster

    # Copy the complete dataset, including masks and band metadata, then set
    # the declaration on the derived bytes. The source CAS object stays intact.
    with MemoryFile() as memory:
        copy_raster(str(path), memory.name, driver="GTiff")
        with rasterio.open(memory.name, "r+") as dataset:
            dataset.crs = _crs_name(effective)
        return memory.read()


def _inspection_worker(path, filename, limits, connection):
    try:
        value = {"ok": True, "facts": inspect_file(path, filename, **limits)}
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > MAX_METADATA_BYTES:
            raise ValueError("上传文件元数据超过允许大小")
        connection.send_bytes(raw)
    except Exception as exc:
        connection.send_bytes(json.dumps({"ok": False, "error": str(exc)[:2000]}, ensure_ascii=False).encode("utf-8"))
    finally:
        connection.close()


def inspect_upload(path: Path, filename: str, *, max_total: int, max_count: int, timeout_seconds=30.0) -> dict:
    """Native parsers run in a killable process, never in the API process."""
    from .process import enforce_resource_limits, kill_process_tree, release_resource_limits, check_resource_budget

    ctx = multiprocessing.get_context("spawn")
    receiver, sender = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_inspection_worker, args=(str(path), filename, {"max_total": max_total, "max_count": max_count}, sender), daemon=True)
    process.start()
    sender.close()
    enforce_resource_limits(process, memory_mb=1024, cpu_weight=1)
    deadline = time.monotonic() + timeout_seconds
    try:
        while not receiver.poll(0.1):
            if not process.is_alive():
                raise ValueError("上传检查进程未返回有效元数据")
            if time.monotonic() >= deadline:
                raise ValueError("上传检查超时")
            if check_resource_budget(process, memory_mb=1024, cpu_weight=0, sample_interval=0.01).get("exceeded"):
                raise ValueError("上传检查超过内存限制")
        result = json.loads(receiver.recv_bytes(MAX_METADATA_BYTES))
        if not result.get("ok"):
            raise ValueError(result.get("error", "无法解析上传文件"))
        return result["facts"]
    except (EOFError, OSError) as exc:
        raise ValueError("上传检查进程未返回有效元数据") from exc
    finally:
        receiver.close()
        process.join(0.5)
        if process.is_alive():
            kill_process_tree(process)
            process.join(2)
        release_resource_limits(process)
