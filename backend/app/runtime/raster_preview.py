"""Read-only, bounded WGS84 raster previews for the Artifact workbench."""

from __future__ import annotations

import base64
import io
import json
import math
import multiprocessing
import time

from .process import check_resource_budget, enforce_resource_limits, kill_process_tree, release_resource_limits

MAX_EDGE = 512
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
TIMEOUT_SECONDS = 20
MEMORY_MB = 1024


class RasterPreviewBusy(RuntimeError):
    """The Runtime's preview slot or the input Artifact is currently leased."""


def _shape(width: int, height: int) -> tuple[int, int]:
    scale = min(1, MAX_EDGE / max(width, height))
    return max(1, round(width * scale)), max(1, round(height * scale))


def _render(path: str, expected_crs: str | None) -> dict:
    import numpy as np
    import rasterio
    from PIL import Image
    from rasterio.crs import CRS
    from rasterio.transform import Affine, array_bounds, from_bounds
    from rasterio.warp import Resampling, reproject, transform_bounds

    geographic = CRS.from_epsg(4326)
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CHECK_WITH_INVERT_PROJ="YES", GDAL_CACHEMAX=64 * 1024 * 1024):
        with rasterio.open(path, driver="GTiff") as dataset:
            declared = CRS.from_string(expected_crs) if expected_crs else None
            source_crs = dataset.crs or declared
            if not source_crs or (declared and dataset.crs and dataset.crs != declared) or dataset.transform.is_identity:
                raise ValueError("栅格缺少可信的地理定位，无法生成地图预览")
            if np.issubdtype(np.dtype(dataset.dtypes[0]), np.complexfloating):
                raise ValueError("暂不支持复数栅格预览")

            width, height = _shape(dataset.width, dataset.height)
            native = dataset.read(1, out_shape=(height, width), masked=True)
            native_transform = dataset.transform * Affine.scale(dataset.width / width, dataset.height / height)
            samples = native.astype("float64").filled(np.nan)
            samples[~np.isfinite(samples)] = np.nan
            north_up = native_transform.b == native_transform.d == 0 and native_transform.a > 0 > native_transform.e
            needs_warp = source_crs != geographic or not north_up
            bounds = (transform_bounds(source_crs, geographic, *dataset.bounds) if needs_warp
                      else array_bounds(height, width, native_transform))
            west, south, east, north = bounds
            if not (all(map(math.isfinite, bounds)) and -180.000001 <= west < east <= 180.000001
                    and -90.000001 <= south < north <= 90.000001):
                raise ValueError("栅格范围超出可预览的 WGS84 经纬度")

            if needs_warp:
                # Preserve the complete extent and a positive grid even for one-cell crops.
                xspan, yspan = east - west, north - south
                step = math.hypot(xspan, yspan) / math.hypot(width, height)
                width, height = _shape(max(1, round(xspan / step)), max(1, round(yspan / step)))
                warped = np.full((height, width), np.nan)
                reproject(samples, warped, src_transform=native_transform, src_crs=source_crs, src_nodata=np.nan,
                          dst_transform=from_bounds(*bounds, width, height), dst_crs=geographic,
                          dst_nodata=np.nan, resampling=Resampling.nearest)
                samples = warped

            valid = np.isfinite(samples)
            values = samples[valid]
            value_range = None
            image = np.zeros((height, width, 2), dtype=np.uint8)
            if values.size:
                low, high = float(values.min()), float(values.max())
                value_range = [low, high]
                if low == high:
                    image[..., 0][valid] = 144
                else:
                    # Normalize before subtracting so large finite floats do not overflow.
                    scale = max(abs(low), abs(high))
                    normalized = (values / scale - low / scale) / (high / scale - low / scale)
                    image[..., 0][valid] = (48 + normalized.clip(0, 1) * 207).astype(np.uint8)
                image[..., 1][valid] = 255
            output = io.BytesIO()
            Image.fromarray(image).save(output, format="PNG")
            return {
                "crs": {"kind": "WGS84"}, "bbox": list(bounds),
                "width": width, "height": height, "band": 1, "band_count": dataset.count,
                "nodata": None if dataset.nodata is None else str(dataset.nodata),
                "value_range": value_range, "masked_pixels": int(valid.size - values.size),
                "png_b64": base64.b64encode(output.getvalue()).decode("ascii"),
            }


def _preview_worker(path, expected_crs, sender):
    try:
        raw = json.dumps({"preview": _render(path, expected_crs)}, allow_nan=False).encode("utf-8")
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("栅格预览超过允许大小")
        sender.send_bytes(raw)
    except ValueError as exc:
        sender.send_bytes(json.dumps({"error": str(exc)[:300]}, ensure_ascii=False).encode("utf-8"))
    except Exception:
        sender.send_bytes(json.dumps({"error": "无法读取栅格预览"}, ensure_ascii=False).encode("utf-8"))
    finally:
        sender.close()


def read_raster_preview(path: str, expected_crs: str | None) -> dict:
    """Keep native raster decoding and rendering in a killable child process."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_preview_worker, args=(path, expected_crs, sender), daemon=True)
    try:
        process.start()
        sender.close()
        enforce_resource_limits(process, memory_mb=MEMORY_MB, cpu_weight=1)
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while not receiver.poll(0.1):
            if not process.is_alive():
                raise ValueError("栅格预览进程未返回结果")
            if time.monotonic() >= deadline:
                raise ValueError("栅格预览超时")
            if check_resource_budget(process, memory_mb=MEMORY_MB, cpu_weight=0, sample_interval=0.01).get("exceeded"):
                raise ValueError("栅格预览超过内存限制")
        response = json.loads(receiver.recv_bytes(MAX_RESPONSE_BYTES))
        if "error" in response:
            raise ValueError(response["error"])
        return response["preview"]
    except (EOFError, OSError) as exc:
        raise ValueError("栅格预览进程未返回结果") from exc
    finally:
        sender.close()
        receiver.close()
        if process.pid is not None:
            process.join(0.5)
            if process.is_alive():
                kill_process_tree(process)
                process.join(2)
            release_resource_limits(process)
