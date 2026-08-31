"""POST /api/upload 端点：文件上传 + 安全校验。

实现参考 docs/01_api_spec.md §3 + docs/06_security.md §3：
- 大小校验：超过 UPLOAD_MAX_SIZE(50MB) → 413 FILE_TOO_LARGE
- 类型白名单：.zip/.geojson/.json/.kml/.tif/.tiff，其他 → 422 UNSUPPORTED_FILE_TYPE
- ZIP 安全：ZIP 炸弹（压缩比/解压大小/文件数）+ ZipSlip 路径穿越 → 422 FILE_PARSE_FAILED
- 解析：Sprint 1 简化，只解析 GeoJSON；shp zip 返回占位（data_io 未完整实现）

响应 UploadResponse：file_id / filename / crs / feature_count / geometry_type / preview / warnings
"""

import asyncio
import io
import json
import logging
import os
import shutil
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, UploadFile, HTTPException

from app.config import settings
from app.models.schemas import UploadResponse
from app.tools.data_io import DataIO
from app.utils.redis import get_redis, make_key

logger = logging.getLogger(__name__)

router = APIRouter()

ALLOWED_EXT = {".zip", ".geojson", ".json", ".kml", ".tif", ".tiff"}


def _remove_expired_upload(path: Path, *, attempts: int = 3) -> bool:
    """Best-effort removal that tolerates short-lived Windows file locks."""
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            if attempt + 1 < attempts:
                time.sleep(0.05 * (attempt + 1))
                continue
            logger.warning(
                "expired upload cleanup deferred because payload is locked: %s",
                path,
            )
            return False
        except OSError:
            logger.warning("expired upload cleanup failed: %s", path, exc_info=True)
            return False
    return False


def validate_file_type(filename: str) -> str:
    """校验扩展名白名单，返回小写扩展名。"""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXT:
        raise ValueError(
            f"不支持的文件类型 {ext or '(无扩展名)'}，"
            f"仅支持 {', '.join(sorted(ALLOWED_EXT))}"
        )
    return ext


def validate_zip_safety(zip_buffer: bytes) -> None:
    """校验 ZIP 包安全性，防止 ZIP 炸弹。

    参考 docs/06_security.md §3.1：
    1. 文件数量限制（UPLOAD_ZIP_MAX_FILE_COUNT=100）
    2. 解压后总大小限制（UPLOAD_ZIP_MAX_TOTAL_SIZE=500MB）
    3. 压缩比异常检测（>200 疑似炸弹）
    """
    max_total_size = settings.UPLOAD_ZIP_MAX_TOTAL_SIZE * 1024 * 1024
    max_file_count = settings.UPLOAD_ZIP_MAX_FILE_COUNT
    # 绝对解压大小上限：无论压缩比如何，解压后总大小不得超过此值
    ABSOLUTE_MAX_EXTRACTED = 100 * 1024 * 1024  # 100 MB

    with zipfile.ZipFile(io.BytesIO(zip_buffer), "r") as zip_ref:
        infos = zip_ref.infolist()

        if len(infos) > max_file_count:
            raise ValueError(
                f"ZIP 内文件数 {len(infos)} 超过上限 {max_file_count}"
            )

        total_size = sum(info.file_size for info in infos)
        # 绝对大小检查：无论压缩比，解压超 100MB 直接拒绝
        # 防止 compress_size=0 的 stored entries 绕过压缩比检查
        if total_size > ABSOLUTE_MAX_EXTRACTED:
            raise ValueError(
                f"ZIP 解压后总大小 {total_size / 1024 / 1024:.1f}MB "
                f"超过绝对上限 {ABSOLUTE_MAX_EXTRACTED / 1024 / 1024:.0f}MB"
            )
        if total_size > max_total_size:
            raise ValueError(
                f"ZIP 解压后总大小 {total_size / 1024 / 1024:.1f}MB "
                f"超过上限 {settings.UPLOAD_ZIP_MAX_TOTAL_SIZE}MB"
            )

        compressed_size = sum(info.compress_size for info in infos)
        if compressed_size > 0:
            ratio = total_size / compressed_size
            if ratio > 200:
                raise ValueError(
                    f"ZIP 压缩比 {ratio:.0f} 异常，疑似 ZIP 炸弹"
                )


def validate_zip_paths(zip_ref: zipfile.ZipFile) -> None:
    """防止 ZipSlip 攻击（路径穿越）。

    参考 docs/06_security.md §3.2。
    """
    for info in zip_ref.infolist():
        target_path = os.path.normpath(info.filename)
        if target_path.startswith("..") or os.path.isabs(target_path):
            raise ValueError(f"可疑路径：{info.filename}")


def parse_geojson(content: bytes, filename: str) -> dict:
    """解析 GeoJSON 字节，返回 UploadResponse 字段 dict。

    Sprint 1 简化：
    - 不做坐标系识别（默认 GCJ02，original_crs 空）
    - 不做拓扑修复
    - 返回 feature_count / geometry_type / preview.bbox / preview.sample_features

    Returns:
        dict 含 feature_count / geometry_type / preview / warnings
    """
    warnings = []

    # 编码探测：优先 utf-8，失败尝试 gbk（国内常见）
    text = None
    for encoding in ("utf-8", "utf-8-sig", "gbk"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("无法解码文件，编码不支持")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"GeoJSON 解析失败：{e}") from e

    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        raise ValueError("非合法 GeoJSON FeatureCollection")

    features = data.get("features") or []
    if not features:
        return {
            "feature_count": 0,
            "geometry_type": "",
            "preview": {},
            "warnings": warnings,
        }

    # 提取 geometry_type（取第一个 Feature 的几何类型）
    geom_types = set()
    coords_all = []
    sample_features = []
    for feat in features[:5]:
        sample_features.append(feat)
        geom = feat.get("geometry") or {}
        gt = geom.get("type")
        if gt:
            geom_types.add(gt)
        _collect_coords(geom, coords_all)

    geometry_type = "/".join(sorted(geom_types)) if geom_types else ""

    # 计算 bbox
    bbox = _compute_bbox(coords_all) if coords_all else None

    preview = {}
    if bbox:
        preview["bbox"] = bbox
    preview["sample_features"] = sample_features

    return {
        "feature_count": len(features),
        "geometry_type": geometry_type,
        "preview": preview,
        "warnings": warnings,
    }


def _collect_coords(geom: dict, out: list) -> None:
    """递归收集几何对象的所有坐标 [lng, lat] 点。"""
    if not isinstance(geom, dict):
        return
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if coords is None:
        return

    def _walk(c):
        if isinstance(c, (list, tuple)):
            if len(c) >= 2 and all(isinstance(x, (int, float)) for x in c[:2]):
                out.append([float(c[0]), float(c[1])])
            else:
                for item in c:
                    _walk(item)

    if gtype == "Point":
        _walk(coords)
    else:
        _walk(coords)


def _compute_bbox(points: list) -> list:
    """从 [lng, lat] 点列表计算 [minLng, minLat, maxLng, maxLat]。"""
    lngs = [p[0] for p in points]
    lats = [p[1] for p in points]
    return [min(lngs), min(lats), max(lngs), max(lats)]


def _build_preview_from_gdf(gdf) -> dict:
    """从 GeoDataFrame 构造 preview（bbox + sample_features）。"""
    preview: dict = {}
    if gdf is None or len(gdf) == 0:
        return preview
    try:
        bounds = gdf.total_bounds
        preview["bbox"] = [float(bounds[0]), float(bounds[1]), float(bounds[2]), float(bounds[3])]
    except Exception:
        pass
    try:
        import json
        sample = gdf.head(5).to_json()
        preview["sample_features"] = json.loads(sample).get("features", [])
    except Exception:
        preview["sample_features"] = []
    return preview


async def _persist_upload(file_id: str, content: bytes, filename: str) -> None:
    """Atomically persist payload on local disk and metadata in Redis.

    Single-machine development does not benefit from putting tens of megabytes
    of base64 in Redis.  The workspace is the payload store; Redis remains the
    expiring lookup/index used by the chat runtime.  A future object-store
    implementation can keep this metadata contract unchanged.
    """
    safe_id = "".join(ch for ch in file_id if ch.isalnum() or ch in ("_", "-"))
    if not safe_id or safe_id != file_id:
        raise ValueError("invalid upload file_id")
    suffix = Path(filename).suffix.lower()
    upload_root = (Path(settings.APP_WORKSPACE_DIR).resolve() / "uploads")
    file_dir = upload_root / safe_id
    final_path = file_dir / f"original{suffix}"
    temp_path = file_dir / f".{uuid.uuid4().hex}.tmp"

    def _write_atomically() -> None:
        upload_root.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - max(int(settings.UPLOAD_TTL_S), 1)
        for candidate in upload_root.iterdir():
            try:
                resolved = candidate.resolve()
                # Cleanup is intentionally limited to one direct child of the
                # configured uploads root; never follow a computed broad path.
                if (
                    candidate.is_dir()
                    and resolved.parent == upload_root.resolve()
                    and candidate.stat().st_mtime < cutoff
                ):
                    _remove_expired_upload(resolved)
            except FileNotFoundError:
                # Another concurrent upload cleanup already removed it.
                continue
            except OSError:
                logger.warning("expired upload cleanup failed: %s", candidate, exc_info=True)
        file_dir.mkdir(parents=True, exist_ok=True)
        temp_path.write_bytes(content)
        temp_path.replace(final_path)

    await asyncio.to_thread(_write_atomically)

    r = get_redis()
    payload = json.dumps({
        "filename": filename,
        "storage_path": str(final_path.resolve()),
        "size_bytes": len(content),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False)
    await r.set(make_key("upload", file_id), payload, ex=settings.UPLOAD_TTL_S)


@router.post("/api/upload", response_model=UploadResponse)
async def upload(file: UploadFile):
    """文件上传。校验大小、类型、ZIP 安全。返回 UploadResponse。

    Sprint 1 简化：只解析 GeoJSON，shp ZIP 返回占位（data_io 未实现完整）。
    但必须做安全校验（大小、类型白名单）。
    """
    filename = file.filename or ""

    # 1. 大小校验（先于内容读取，若 file.size 已知）
    max_bytes = settings.UPLOAD_MAX_SIZE * 1024 * 1024
    declared_size = getattr(file, "size", None)
    if declared_size and declared_size > max_bytes:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "FILE_TOO_LARGE",
                "message": f"上传文件超过 {settings.UPLOAD_MAX_SIZE}MB 限制",
                "detail": {
                    "max_size_mb": settings.UPLOAD_MAX_SIZE,
                    "actual_size_mb": round(declared_size / 1024 / 1024, 1),
                },
            },
        )

    # 2. 类型校验
    try:
        ext = validate_file_type(filename)
    except ValueError as e:
        raise HTTPException(
            status_code=422,
            detail={"code": "UNSUPPORTED_FILE_TYPE", "message": str(e)},
        )

    # 3. 读取内容（流式累加，中途超限即拒绝，防超大文件占内存）
    chunks = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)  # 1MB chunks
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail={
                    "code": "FILE_TOO_LARGE",
                    "message": f"上传文件超过 {settings.UPLOAD_MAX_SIZE}MB 限制",
                    "detail": {
                        "max_size_mb": settings.UPLOAD_MAX_SIZE,
                        "actual_size_mb": round(total / 1024 / 1024, 1),
                    },
                },
            )
        chunks.append(chunk)
    content = b"".join(chunks)

    # 4. ZIP 安全校验
    if ext == ".zip":
        try:
            validate_zip_safety(content)
            with zipfile.ZipFile(io.BytesIO(content), "r") as zf:
                validate_zip_paths(zf)
        except (ValueError, zipfile.BadZipFile) as e:
            raise HTTPException(
                status_code=422,
                detail={"code": "FILE_PARSE_FAILED", "message": str(e)},
            )

        # 真实 shp ZIP 解析
        result = DataIO().read_upload(content, filename)
        if result.get("status") != "ok":
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "FILE_PARSE_FAILED",
                    "message": result.get("message", "ZIP 解析失败"),
                },
            )

        file_id = f"file_{uuid.uuid4().hex[:12]}"
        await _persist_upload(file_id, content, filename)
        logger.info(
            "upload shp zip done file_id=%s filename=%s features=%d",
            file_id, filename, result.get("feature_count", 0),
        )
        return UploadResponse(
            file_id=file_id,
            filename=filename,
            crs=result.get("crs", "GCJ02"),
            original_crs=result.get("original_crs", ""),
            feature_count=result.get("feature_count", 0),
            geometry_type=result.get("geometry_type", ""),
            preview=_build_preview_from_gdf(result.get("data")),
            warnings=result.get("warnings", []),
        )

    if ext in {".tif", ".tiff"}:
        try:
            import rasterio
            from rasterio.io import MemoryFile

            with MemoryFile(content) as memory_file:
                with memory_file.open() as src:
                    crs = str(src.crs) if src.crs else ""
                    bounds = src.bounds
                    preview = {
                        "bbox": [
                            float(bounds.left),
                            float(bounds.bottom),
                            float(bounds.right),
                            float(bounds.top),
                        ],
                        "sample_features": [],
                    }
                    warnings = [] if src.crs else ["GeoTIFF 未声明 CRS"]
        except Exception as e:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "FILE_PARSE_FAILED",
                    "message": f"GeoTIFF 解析失败：{e}",
                },
            ) from e

        file_id = f"file_{uuid.uuid4().hex[:12]}"
        await _persist_upload(file_id, content, filename)
        return UploadResponse(
            file_id=file_id,
            filename=filename,
            crs=crs,
            original_crs=crs,
            feature_count=0,
            geometry_type="Raster",
            preview=preview,
            warnings=warnings,
        )

    # 5. 解析 GeoJSON / KML —— 走 DataIO 统一管线（坐标系识别 + 国内转 GCJ02）
    result = DataIO().read_upload(content, filename)
    if result.get("status") != "ok":
        raise HTTPException(
            status_code=422,
            detail={
                "code": "FILE_PARSE_FAILED",
                "message": result.get("message", "GeoJSON 解析失败"),
            },
        )

    file_id = f"file_{uuid.uuid4().hex[:12]}"
    await _persist_upload(file_id, content, filename)
    logger.info(
        "upload done file_id=%s filename=%s features=%d",
        file_id, filename, result.get("feature_count", 0),
    )

    return UploadResponse(
        file_id=file_id,
        filename=filename,
        crs=result.get("crs", "GCJ02"),
        original_crs=result.get("original_crs", ""),
        feature_count=result.get("feature_count", 0),
        geometry_type=result.get("geometry_type", ""),
        preview=_build_preview_from_gdf(result.get("data")),
        warnings=result.get("warnings", []),
    )
