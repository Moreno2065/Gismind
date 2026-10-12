"""Filesystem CAS with transactional metadata publication."""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .db import utc_now
from .store import RuntimeStore


MIN_CONFIRM_GRACE_S = 86400
MAX_CONFIRM_GRACE_S = 365 * 86400
_CAS_LOCKS: dict[str, threading.Lock] = {}
_CAS_LOCKS_GUARD = threading.Lock()


def _cas_uri(content_hash: str) -> str:
    return f"artifacts/sha256/{content_hash[:2]}/{content_hash}"


def _cas_lock(content_hash: str) -> threading.Lock:
    with _CAS_LOCKS_GUARD:
        return _CAS_LOCKS.setdefault(content_hash, threading.Lock())


class ArtifactStore:
    def __init__(self, data_root: str | Path | RuntimeStore | None = None, runtime: RuntimeStore | None = None, *, artifact_dir: str | Path | None = None, staging_dir: str | Path | None = None, quarantine_dir: str | Path | None = None, upload_dir: str | Path | None = None):
        if isinstance(data_root, RuntimeStore) and runtime is None:
            runtime = data_root
            data_root = None
        if runtime is None:
            raise TypeError("runtime store is required")
        configured = None
        if data_root is None:
            try:
                from app.config import get_settings
                configured = get_settings().runtime_paths
            except (ImportError, KeyError, ValueError) as exc:
                raise ValueError("ArtifactStore requires resolved runtime configuration (RuntimePaths) or an explicit data_root") from exc
        data_root = data_root or configured["data_root"]
        self.data_root = Path(data_root).resolve()
        self.runtime = runtime
        self.artifacts_dir = Path(artifact_dir or (configured["artifact_dir"] if configured else self.data_root / "artifacts")).resolve()
        self.staging_dir = Path(staging_dir or (configured["staging_dir"] if configured else self.data_root / "staging")).resolve()
        self.quarantine_dir = Path(quarantine_dir or (configured["quarantine_dir"] if configured else self.data_root / "quarantine")).resolve()
        self.uploads_dir = Path(upload_dir or (configured["upload_dir"] if configured else self.data_root / "uploads")).resolve()
        # RuntimeStore receives the physical CAS root; database URIs stay
        # logical and are always rooted at artifacts/sha256/.
        self.runtime.set_artifact_root(self.artifacts_dir)
        for directory in (self.artifacts_dir, self.staging_dir, self.quarantine_dir, self.uploads_dir):
            self._under(directory, self.data_root)
        try:
            from app.config import same_volume
            if not same_volume(self.artifacts_dir, self.staging_dir):
                raise ValueError("artifact and staging directories must share a volume")
        except ImportError:
            raise ValueError("unable to confirm artifact and staging volume")
        for directory in (self.artifacts_dir, self.staging_dir, self.quarantine_dir, self.uploads_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def _under(self, path: Path, root: Path) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError("path escapes artifact store boundary") from exc
        return resolved

    def stage_bytes(self, data: bytes, *, filename: str | None = None) -> Path:
        path = self.create_staging_path(filename)
        path.write_bytes(data)
        return path

    def create_staging_path(self, filename: str | None = None) -> Path:
        """Allocate an empty staging path without reading the upload into memory."""
        token = uuid.uuid4().hex
        suffix = Path(filename or "").suffix
        return self._under(self.staging_dir / f"{token}{suffix}", self.staging_dir)

    def stage_file(self, source: str | Path, *, filename: str | None = None) -> Path:
        source_path = Path(source).resolve()
        if not source_path.is_file():
            raise ValueError("staging source is not a regular file")
        return self.stage_bytes(source_path.read_bytes(), filename=filename or source_path.name)

    def _validate(self, path: Path, expected_hash: str | None = None, *, artifact_type: str = "Artifact") -> tuple[str, int]:
        if not path.is_file():
            raise ValueError("staged artifact does not exist")
        digest_builder = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest_builder.update(chunk)
        digest = digest_builder.hexdigest()
        if expected_hash and digest != expected_hash:
            raise ValueError("artifact content hash mismatch")
        # File bytes are opaque to CAS: uploads have bounded physical inspection
        # and exports have format postconditions. A filename must not override
        # those facts (e.g. a valid Shapefile ZIP named points.geojson).
        # Typed Artifacts also undergo their type-specific validation below.
        if artifact_type != "File" and path.suffix.lower() in {".json", ".geojson"}:
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("invalid JSON artifact") from exc
        return digest, path.stat().st_size

    @staticmethod
    def _validate_artifact_content(path: Path, artifact_type: str, *, crs: dict[str, Any] | None = None,
                                   schema: dict[str, Any] | None = None) -> None:
        """Validate the physical representation required by an Artifact type.

        A syntactically valid JSON document is not a Raster, and a caller must
        never be able to assign a spatial type solely through metadata.
        """
        if artifact_type == "Vector":
            try:
                if path.suffix.lower() in {".parquet", ".geoparquet"}:
                    import geopandas as gpd
                    frame = gpd.read_parquet(path)
                    if frame.geometry.name not in frame.columns:
                        raise ValueError("Vector requires a geometry column")
                    if frame.crs is None:
                        raise ValueError("Vector GeoParquet is missing CRS metadata")
                else:
                    from shapely.geometry import shape
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
                        raise ValueError("Vector requires a GeoJSON FeatureCollection")
                    features = payload.get("features")
                    if not isinstance(features, list):
                        raise ValueError("Vector requires a features array")
                    for feature in features:
                        if not isinstance(feature, dict) or feature.get("type") != "Feature":
                            raise ValueError("Vector contains an invalid Feature")
                        geometry = feature.get("geometry")
                        if not isinstance(geometry, dict):
                            raise ValueError("Vector Feature requires geometry")
                        parsed = shape(geometry)
                        if parsed.is_empty:
                            raise ValueError("Vector Feature geometry is empty")
                if not isinstance(crs, dict) or not crs:
                    raise ValueError("Vector requires declared CRS metadata")
                if path.suffix.lower() in {".parquet", ".geoparquet"}:
                    from pyproj import CRS
                    kind = str(crs.get("kind", "")).upper()
                    if kind == "EPSG" and isinstance(crs.get("code"), int):
                        expected = CRS.from_epsg(crs["code"])
                    elif kind == "WGS84":
                        expected = CRS.from_epsg(4326)
                    elif kind == "GCJ02":
                        raise ValueError("Vector GeoParquet cannot declare GCJ02 as a physical CRS")
                    else:
                        raise ValueError("Vector declared CRS metadata is unsupported")
                    if frame.crs != expected:
                        raise ValueError("Vector CRS metadata does not match GeoParquet CRS")
            except Exception as exc:
                raise ValueError(f"Vector artifact is not readable spatial data: {exc}") from exc
        if artifact_type == "Raster":
            try:
                import rasterio
                from rasterio.crs import CRS

                if not isinstance(crs, dict) or not crs:
                    raise ValueError("Raster requires declared CRS metadata")
                crs_kind = str(crs.get("kind", "")).upper()
                if crs_kind == "EPSG" and isinstance(crs.get("code"), int):
                    declared_crs = CRS.from_epsg(crs["code"])
                elif crs_kind == "WGS84":
                    declared_crs = CRS.from_epsg(4326)
                elif crs_kind == "GCJ02":
                    raise ValueError("Raster cannot declare GCJ02 as a physical CRS")
                else:
                    raise ValueError("Raster declared CRS metadata is unsupported")
                with rasterio.open(path) as dataset:
                    if dataset.count < 1 or dataset.width < 1 or dataset.height < 1:
                        raise ValueError("Raster has no readable bands or cells")
                    if dataset.crs is None:
                        raise ValueError("Raster is missing CRS metadata")
                    if dataset.crs != declared_crs:
                        raise ValueError("Raster CRS metadata does not match GeoTIFF CRS")
                    if dataset.transform is None:
                        raise ValueError("Raster is missing an affine transform")
            except ValueError:
                raise
            except Exception as exc:
                raise ValueError("Raster artifact is not readable by Rasterio") from exc
        if artifact_type == "Table":
            try:
                if path.suffix.lower() == ".parquet":
                    import pandas as pd
                    pd.read_parquet(path)
                else:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(payload, list):
                        if not all(isinstance(row, dict) for row in payload):
                            raise ValueError("Table rows must be objects")
                    elif not isinstance(payload, dict):
                        raise ValueError("Table requires an object or an array of row objects")
            except Exception as exc:
                raise ValueError(f"Table artifact is not readable structured data: {exc}") from exc
        if artifact_type == "Coordinate":
            try:
                if not isinstance(crs, dict) or not crs:
                    raise ValueError("Coordinate requires declared CRS metadata")
                payload = json.loads(path.read_text(encoding="utf-8"))
                pair = payload.get("location") if isinstance(payload, dict) else payload
                if pair is None and isinstance(payload, dict) and "lng" in payload and "lat" in payload:
                    pair = [payload["lng"], payload["lat"]]
                if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                    raise ValueError("Coordinate requires a two-value location")
                x, y = pair[0], pair[1]
                if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, (int, float)) or not isinstance(y, (int, float)) or not math.isfinite(float(x)) or not math.isfinite(float(y)):
                    raise ValueError("Coordinate values must be finite numbers")
                kind = str((crs or {}).get("kind", "")).upper()
                geographic = kind in {"WGS84", "GCJ02"} or (kind == "EPSG" and (crs or {}).get("code") == 4326)
                if geographic and not (-180 <= float(x) <= 180 and -90 <= float(y) <= 90):
                    raise ValueError("Coordinate is outside geographic bounds")
            except Exception as exc:
                raise ValueError(f"Coordinate artifact is not valid: {exc}") from exc
        if artifact_type == "POISet":
            try:
                if not isinstance(crs, dict) or not crs:
                    raise ValueError("POISet requires declared CRS metadata")
                payload = json.loads(path.read_text(encoding="utf-8"))
                rows = payload
                if isinstance(payload, dict):
                    rows = payload.get("pois", payload.get("results", payload.get("data")))
                if not isinstance(rows, list):
                    raise ValueError("POISet requires an array of POI rows")
                for row in rows:
                    if not isinstance(row, dict):
                        raise ValueError("POISet rows must be objects")
                    pair = row.get("location")
                    if pair is None and "lng" in row and "lat" in row:
                        pair = [row["lng"], row["lat"]]
                    if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                        raise ValueError("POISet row requires location")
                    x, y = pair[0], pair[1]
                    if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, (int, float)) or not isinstance(y, (int, float)) or not math.isfinite(float(x)) or not math.isfinite(float(y)) or not (-180 <= float(x) <= 180 and -90 <= float(y) <= 90):
                        raise ValueError("POISet row has invalid geographic location")
            except Exception as exc:
                raise ValueError(f"POISet artifact is not valid: {exc}") from exc
        if artifact_type == "MapLayer":
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                layers = payload.get("layers") if isinstance(payload, dict) else None
                bbox = payload.get("bbox") if isinstance(payload, dict) else None
                if not isinstance(layers, list) or not layers or not all(isinstance(layer, dict) and isinstance(layer.get("type"), str) and layer["type"] for layer in layers):
                    raise ValueError("MapLayer requires one or more typed layers")
                if not isinstance(bbox, list) or len(bbox) != 4 or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) for value in bbox):
                    raise ValueError("MapLayer requires a finite four-value bbox")
                if float(bbox[0]) > float(bbox[2]) or float(bbox[1]) > float(bbox[3]):
                    raise ValueError("MapLayer bbox must be ordered")
            except ValueError:
                raise
            except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
                raise ValueError("MapLayer artifact is not valid JSON") from exc

    @staticmethod
    def _same_semantics(existing: dict[str, Any], *, artifact_type: str, crs: dict[str, Any] | None,
                        schema: dict[str, Any] | None, media_type: str | None) -> bool:
        """Check an idempotent re-publication of one logical Artifact."""
        canonical = lambda value: json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if existing.get("type", "Artifact") != artifact_type:
            return False
        if canonical(existing.get("crs")) != canonical(crs):
            return False
        if canonical(existing.get("schema")) != canonical(schema):
            return False
        saved_media_type = existing.get("media_type")
        return not (saved_media_type and media_type and saved_media_type != media_type)

    @staticmethod
    def _artifact_id(*, origin: str, producer_task_id: str | None, output_name: str | None,
                     output_ordinal: int, content_hash: str, artifact_type: str,
                     crs: dict[str, Any] | None, schema: dict[str, Any] | None,
                     media_type: str | None) -> str:
        """Allocate a semantic Artifact identity independently of its bytes.

        Uploads represent separate user facts and receive a fresh ID.  A task
        output is deterministic within its producer/port so a worker retry can
        safely observe the already committed immutable record.
        """
        if origin == "upload":
            return f"art_{uuid.uuid4().hex}"
        canonical = json.dumps(
            {
                "producer_task_id": producer_task_id,
                "output_name": output_name,
                "output_ordinal": output_ordinal,
                "content_hash": content_hash,
                "type": artifact_type,
                "crs": crs,
                "schema": schema or {},
                "media_type": media_type,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"art_{uuid.uuid5(uuid.NAMESPACE_URL, canonical).hex}"

    def cas_path(self, content_hash: str) -> Path:
        if len(content_hash) != 64 or any(char not in "0123456789abcdef" for char in content_hash):
            raise ValueError("content hash must be a SHA-256 hex digest")
        # The hash has already been strictly validated, so constructing this
        # path from the resolved physical root is sufficient.  Do not call
        # Path.resolve() on a not-yet-created prefix: on Windows that can race
        # another publisher's first mkdir and report a false boundary escape.
        return self.artifacts_dir / "sha256" / content_hash[:2] / content_hash

    def publish_staged(
        self,
        staged: str | Path,
        *,
        origin: str,
        producer_task_id: str | None = None,
        media_type: str | None = None,
        expected_hash: str | None = None,
        artifact_type: str = "Artifact",
        crs: dict[str, Any] | None = None,
        schema: dict[str, Any] | None = None,
        parents: list[str] | None = None,
        output_name: str | None = None,
        output_ordinal: int = 0,
        cache_key: str | None = None,
        event: tuple[str, str, Any] | None = None,
        fence=None,
    ) -> dict:
        if origin not in {"upload", "task"}:
            raise ValueError("artifact origin must be upload or task")
        if origin == "upload" and producer_task_id is not None:
            raise ValueError("upload artifacts cannot have a producer task")
        if origin == "task" and not producer_task_id:
            raise ValueError("task artifacts require a producer task")
        staged_path = self._under(Path(staged), self.staging_dir)
        digest, size = self._validate(staged_path, expected_hash, artifact_type=artifact_type)
        self._validate_artifact_content(staged_path, artifact_type, crs=crs, schema=schema)
        artifact_id = self._artifact_id(
            origin=origin,
            producer_task_id=producer_task_id,
            output_name=output_name,
            output_ordinal=output_ordinal,
            content_hash=digest,
            artifact_type=artifact_type,
            crs=crs,
            schema=schema,
            media_type=media_type,
        )
        # An idempotent task retry must not create a self-edge. Different
        # Artifacts may legitimately share a content hash, so compare IDs.
        parents = [parent for parent in (parents or []) if parent != artifact_id]
        # Artifact identity is Runtime-owned.  An event supplied by a handler
        # is a display summary only; it must never be allowed to name a
        # different artifact than the exact CAS object committed below.
        if event is not None:
            event_run_id, event_type, event_payload = event
            payload = dict(event_payload) if isinstance(event_payload, dict) else {"summary": event_payload}
            payload["artifact_id"] = artifact_id
            payload["content_hash"] = digest
            payload["byte_size"] = size
            event = (event_run_id, event_type, payload)
        with _cas_lock(digest):
            destination = self.cas_path(digest)
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Serialize the metadata lookup and CAS move with BEGIN IMMEDIATE.
            # A second publisher therefore sees the committed row and can only
            # revalidate or repair the canonical object; it cannot race the first
            # publisher into quarantining that destination.
            with self.runtime.transaction():
                # Look up metadata independently of the file. A crash can commit
                # the row after the CAS move (or lose the file later); in either
                # case the existing row must be repaired, never duplicated.
                existing = self.runtime.get_artifact(artifact_id)
                if existing:
                    if (
                        existing.get("validation_status") != "valid"
                        or existing.get("content_hash") != digest
                        or existing.get("storage_uri") != _cas_uri(digest)
                        or int(existing.get("size_bytes", -1)) != size
                    ):
                        raise ValueError("existing artifact metadata does not describe the canonical CAS object")
                    if not self._same_semantics(
                        existing,
                        artifact_type=artifact_type,
                        crs=crs,
                        schema=schema,
                        media_type=media_type,
                    ):
                        raise ValueError("artifact identity has incompatible semantics")
                    if not self._cas_integrity(existing):
                        # The staged bytes have already been validated against
                        # the same digest; replace it atomically.
                        os.replace(staged_path, destination)
                    else:
                        staged_path.unlink()
                    return self.runtime.attach_artifact_references(
                        artifact_id,
                        parents=parents,
                        output_name=output_name,
                        producer_task_id=producer_task_id,
                        ordinal=output_ordinal,
                        cache_key=cache_key,
                        event=event,
                        fence=fence,
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                # New semantic identities may share already-published bytes.
                # Replacing a healthy blob is unnecessary and fails on Windows
                # while a downloader holds an ordinary read handle. Verify the
                # physical digest and size before reusing it; missing/corrupt
                # canonical files still take the atomic repair path.
                if self._cas_integrity({
                    "content_hash": digest,
                    "storage_uri": _cas_uri(digest),
                    "size_bytes": size,
                }):
                    staged_path.unlink()
                else:
                    os.replace(staged_path, destination)
                artifact = {
                    "artifact_id": artifact_id,
                    "origin": origin,
                    "producer_task_id": producer_task_id,
                    "content_hash": digest,
                    "storage_uri": _cas_uri(digest),
                    "media_type": media_type,
                    "type": artifact_type,
                    "crs": crs,
                    "schema": schema or {},
                    "size_bytes": size,
                    "validation_status": "valid",
                    "created_at": utc_now(),
                }
                try:
                    return self.runtime.insert_artifact(
                        artifact,
                        parents=parents,
                        output_name=output_name,
                        output_ordinal=output_ordinal,
                        cache_key=cache_key,
                        event=event,
                        fence=fence,
                    )
                except Exception:
                    # CAS bytes can be shared by other semantic Artifacts.  Do
                    # not quarantine/delete the blob after a metadata failure;
                    # it is either still referenced or an immutable orphan for
                    # the normal verified GC path to reclaim.
                    if not self.runtime.has_artifact_with_content_hash(digest) and destination.exists():
                        quarantine = self._under(
                            self.quarantine_dir / f"{digest}-{uuid.uuid4().hex}", self.quarantine_dir
                        )
                        os.replace(destination, quarantine)
                    raise

    def path_for(self, artifact_id: str) -> Path:
        artifact = self.runtime.get_artifact(artifact_id)
        if not artifact or artifact["validation_status"] != "valid":
            raise ValueError("artifact is not published and valid")
        try:
            content_hash = artifact["content_hash"]
            expected = self.cas_path(content_hash)
            if artifact.get("storage_uri") != _cas_uri(content_hash):
                raise ValueError("artifact storage URI is not the canonical CAS path")
            path = self._under(self.artifacts_dir / "sha256" / content_hash[:2] / content_hash, self.artifacts_dir)
            if path != expected:
                raise ValueError("artifact storage URI is not the canonical CAS path")
            if not path.is_file() or path.stat().st_size != int(artifact["size_bytes"]):
                raise ValueError("artifact CAS file is missing or has an unexpected size")
            if hashlib.sha256(path.read_bytes()).hexdigest() != content_hash:
                raise ValueError("artifact CAS content hash mismatch")
            return path
        except (OSError, KeyError, TypeError) as exc:
            raise ValueError("artifact CAS payload cannot be verified") from exc

    def _effective_grace(self, grace_period_s: float | None) -> float:
        try:
            from app.config import get_settings
            configured_raw = get_settings().APP_ARTIFACT_GC_GRACE_S
        except (ImportError, KeyError, ValueError, AttributeError, TypeError) as exc:
            raise ValueError("GC grace configuration is unavailable") from exc
        if isinstance(configured_raw, bool) or not isinstance(configured_raw, (int, float)):
            raise ValueError("configured GC grace period must be numeric")
        configured = float(configured_raw)
        if not math.isfinite(configured):
            raise ValueError("configured GC grace period must be finite")
        if configured < MIN_CONFIRM_GRACE_S or configured > MAX_CONFIRM_GRACE_S:
            raise ValueError("configured GC grace period is outside policy")
        if grace_period_s is None:
            return configured
        if isinstance(grace_period_s, bool) or not isinstance(grace_period_s, (int, float)):
            raise ValueError("invalid GC grace period")
        requested = float(grace_period_s)
        if not math.isfinite(requested):
            raise ValueError("invalid GC grace period")
        if requested < configured:
            raise ValueError("GC grace period cannot be below configured minimum")
        if requested > MAX_CONFIRM_GRACE_S:
            raise ValueError("GC grace period exceeds policy maximum")
        return requested

    @staticmethod
    def _validate_manifest_grace(manifest: dict) -> None:
        if not isinstance(manifest, dict) or "grace_period_s" not in manifest:
            raise ValueError("candidate manifest grace period is required")
        value = manifest["grace_period_s"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("candidate manifest grace period must be a finite number")
        if not math.isfinite(float(value)):
            raise ValueError("candidate manifest grace period must be finite")
        if float(value) < MIN_CONFIRM_GRACE_S or float(value) > MAX_CONFIRM_GRACE_S:
            raise ValueError("candidate manifest grace period is outside policy")

    def _cas_integrity(self, artifact: dict) -> bool:
        if not artifact.get("storage_uri"):
            return True
        try:
            if artifact.get("storage_uri") != _cas_uri(artifact["content_hash"]):
                return False
            path = self._under(self.artifacts_dir / "sha256" / artifact["content_hash"][:2] / artifact["content_hash"], self.artifacts_dir)
            if not path.is_file() or path.stat().st_size != int(artifact["size_bytes"]):
                return False
            return hashlib.sha256(path.read_bytes()).hexdigest() == artifact["content_hash"]
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _candidate_rows(self, grace_period_s: float, *, verify_cas: bool = True, acquire_gc_lease: bool = False) -> list[dict]:
        """Snapshot roots under lock, then verify potentially large CAS files outside it."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=grace_period_s)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        with self.runtime.transaction() as conn:
            reachable = self.runtime.reachable_artifact_ids()
            reachable_hashes = self.runtime.reachable_content_hashes()
            rows = [dict(row) for row in conn.execute(
                "SELECT * FROM artifacts WHERE validation_status='valid' AND storage_uri <> '' AND created_at <= ? ORDER BY created_at,artifact_id",
                (cutoff,),
            )]
        candidates = []
        for row in rows:
            if row["artifact_id"] in reachable or row["content_hash"] in reachable_hashes or (verify_cas and not self._cas_integrity(row)):
                continue
            candidates.append(row)
        if not acquire_gc_lease:
            return candidates
        leased = []
        lease_until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        for row in candidates:
            try:
                self.runtime.lease_artifact(
                    None,
                    f"gc:{row['artifact_id']}",
                    expires_at=lease_until,
                    purpose="gc",
                    content_hash=row["content_hash"],
                    _validate_file=False,
                )
            except (RuntimeError, ValueError):
                continue
            leased.append(row)
        return leased

    def gc_candidates(self, *, grace_period_s: float | None = None) -> list[dict]:
        grace_period_s = self._effective_grace(grace_period_s)
        return self._candidate_rows(grace_period_s, verify_cas=True, acquire_gc_lease=False)

    def candidate_manifest(self, *, grace_period_s: float | None = None) -> dict:
        grace_period_s = self._effective_grace(grace_period_s)
        candidates = self._candidate_rows(grace_period_s, verify_cas=True, acquire_gc_lease=True)
        return {"version": 1, "generated_at": utc_now(), "grace_period_s": int(grace_period_s) if grace_period_s.is_integer() else grace_period_s, "candidates": candidates}

    def confirm_candidate_manifest(self, manifest: dict, *, grace_period_s: float | None = None) -> dict:
        """Revalidate candidates without holding a SQLite write lock over CAS IO."""
        confirmed = []
        try:
            self._validate_manifest_grace(manifest)
            grace_period_s = self._effective_grace(grace_period_s)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid candidate manifest grace period") from exc
        if not isinstance(manifest.get("candidates"), list):
            raise ValueError("candidate manifest candidates must be a list")
        now = datetime.now(timezone.utc)
        lease_until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")

        candidate_ids = [candidate.get("artifact_id") for candidate in manifest["candidates"] if isinstance(candidate, dict) and candidate.get("artifact_id")]
        # Remove leases emitted by this manifest before taking the read
        # snapshot. This is a short metadata-only transaction.
        with self.runtime.transaction() as conn:
            for artifact_id in candidate_ids:
                conn.execute("DELETE FROM artifact_leases WHERE lease_key=?", (f"gc:{artifact_id}",))

        # Stage one: read a fresh DB snapshot and verify potentially large CAS
        # payloads without any SQLite write transaction. The file lock keeps a
        # same-process publisher from replacing this hash between pre-read and
        # the short metadata commit below.
        reachable = self.runtime.reachable_artifact_ids()
        snapshots = []
        for candidate in manifest["candidates"]:
            if not isinstance(candidate, dict):
                raise ValueError("candidate manifest candidate must be a mapping")
            artifact_id = candidate.get("artifact_id")
            candidate_uri = candidate.get("storage_uri")
            if not artifact_id or not candidate_uri:
                continue
            current = self.runtime.get_artifact(artifact_id)
            if current is None or current["validation_status"] != "valid" or artifact_id in reachable or current["content_hash"] in self.runtime.reachable_content_hashes():
                continue
            if candidate_uri != current.get("storage_uri") or not current.get("storage_uri"):
                continue
            try:
                created_at = datetime.fromisoformat(str(current["created_at"]).replace("Z", "+00:00"))
                if created_at.tzinfo is None or (now - created_at).total_seconds() < grace_period_s:
                    continue
            except (TypeError, ValueError):
                continue
            with _cas_lock(current["content_hash"]):
                try:
                    path = self.cas_path(current["content_hash"])
                    pre_stat = path.stat()
                    if not self._cas_integrity(current):
                        continue
                    snapshots.append((candidate, current, pre_stat.st_size, pre_stat.st_mtime_ns))
                except (OSError, ValueError, KeyError, TypeError):
                    continue

        # Stage two: recheck references/metadata and acquire the GC lease in a
        # short write transaction. No CAS read or SHA-256 operation occurs in
        # this critical section; stat is only used as a cheap race check.
        for candidate, snapshot, pre_size, pre_mtime_ns in snapshots:
            artifact_id = snapshot["artifact_id"]
            with _cas_lock(snapshot["content_hash"]):
                try:
                    path = self.cas_path(snapshot["content_hash"])
                    current_stat = path.stat()
                except (OSError, ValueError):
                    continue
                if current_stat.st_size != pre_size or current_stat.st_mtime_ns != pre_mtime_ns:
                    continue
                with self.runtime.transaction() as conn:
                    current_row = conn.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
                    if current_row is None:
                        continue
                    current = dict(current_row)
                    if current["validation_status"] != "valid" or self.runtime.is_content_hash_reachable(current["content_hash"]):
                        continue
                    if candidate.get("storage_uri") != current.get("storage_uri") or current.get("content_hash") != snapshot.get("content_hash"):
                        continue
                    try:
                        created_at = datetime.fromisoformat(str(current["created_at"]).replace("Z", "+00:00"))
                        if created_at.tzinfo is None or (now - created_at).total_seconds() < grace_period_s:
                            continue
                    except (TypeError, ValueError):
                        continue
                    try:
                        self.runtime.lease_artifact(
                            None,
                            f"gc:{artifact_id}",
                            expires_at=lease_until,
                            purpose="gc",
                            content_hash=current["content_hash"],
                            _validate_file=False,
                        )
                    except (RuntimeError, ValueError):
                        continue
                    confirmed.append(current)
        return {"version": 1, "generated_at": utc_now(), "grace_period_s": int(grace_period_s) if grace_period_s.is_integer() else grace_period_s, "candidates": confirmed}

    gc_candidate_manifest = candidate_manifest
    get_candidate_manifest = candidate_manifest


ArtifactRepository = ArtifactStore
