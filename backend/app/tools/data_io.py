"""数据 I/O 工具：矢量数据的读取与导出。

实现参考 GIS_Agent_技术文档.md §4.5 + §8.4 + docs/06_security.md §3：
- shp 必须以 ZIP 上传（含 .shp .shx .dbf .prj，可选 .cpg）
- 编码三重降级：.cpg 声明 -> chardet 探测 .dbf -> 轮询常见中文编码
- 所有编码失败返回友好错误（status=error），不抛 UnicodeDecodeError 给 LLM
- 国内数据统一转 GCJ02（适配高德底图）
- 内存解压不落盘
- 上传安全校验（ZIP 炸弹 / ZipSlip / 文件类型白名单）已在 app/api/upload.py 完成，
  本模块专注解析

公共返回结构：
    {
        "status": "ok" | "error",
        "data": GeoDataFrame,            # status=ok 时
        "crs": str,                      # "GCJ02" / "EPSG:4326" / ...
        "feature_count": int,
        "geometry_type": str,            # "Point" / "LineString" / ...
        "warnings": list[str],
        "message": str,                  # status=error 时
        "encodings_tried": list[str],    # status=error 且编码相关时
    }
"""

from __future__ import annotations

import io
import json
import logging
import os
import zipfile
from pathlib import Path
from typing import Any, Optional, Union

import chardet
import geopandas as gpd
import pandas as pd
from shapely.geometry import Point, mapping

from app.tools.geo_transform import out_of_china, wgs84_to_gcj02

try:
    import rasterio
    _RASTERIO_AVAILABLE = True
except ImportError:  # pragma: no cover
    rasterio = None  # type: ignore[assignment]
    _RASTERIO_AVAILABLE = False

logger = logging.getLogger(__name__)


class DataIO:
    """矢量数据 I/O（shp ZIP / geojson / kml）。

    所有方法均以内存对象（bytes / BytesIO）操作，不落盘。
    """

    # 常见 GIS 中文编码候选列表
    # Linux 服务器默认 UTF-8，但用户从 ArcGIS 导出的 shp 常为 GBK
    COMMON_CN_ENCODINGS = ["utf-8", "gbk", "gb2312", "gb18030", "big5"]

    # chardet 探测置信度阈值
    _CHARDET_CONFIDENCE_THRESHOLD = 0.7

    # 中国大陆经纬度范围（与 geo_transform 一致，用于 _is_china_data 启发式判断）
    _CHINA_LNG_MIN = 73.66
    _CHINA_LNG_MAX = 135.05
    _CHINA_LAT_MIN = 3.86
    _CHINA_LAT_MAX = 53.55

    # ------------------------------------------------------------------
    # 编码探测
    # ------------------------------------------------------------------

    def _detect_dbf_encoding(
        self, zip_ref: zipfile.ZipFile, dbf_path: str
    ) -> Optional[str]:
        """用 chardet 探测 .dbf 文件编码。

        读取 .dbf 前 4KB 二进制流交给 chardet，置信度 > 0.7 时采纳。
        gb2312 / gb18030 统一归一化为 gbk（Python codecs 兼容性更好）。

        Args:
            zip_ref: 已打开的 ZipFile 对象
            dbf_path: ZIP 内 .dbf 文件路径

        Returns:
            探测到的编码名（小写），或 None
        """
        try:
            with zip_ref.open(dbf_path) as dbf_file:
                raw_data = dbf_file.read(4096)
            result = chardet.detect(raw_data)
            confidence = result.get("confidence", 0) or 0
            encoding = result.get("encoding")
            if confidence > self._CHARDET_CONFIDENCE_THRESHOLD and encoding:
                enc_lower = encoding.lower()
                # gb2312 / gb18030 归一化为 gbk（gbk 是超集，解码兼容性更好）
                if enc_lower in ("gb2312", "gb18030"):
                    return "gbk"
                return enc_lower
        except Exception as e:
            logger.debug("chardet 探测 .dbf 失败 path=%s err=%s", dbf_path, e)
        return None

    # ------------------------------------------------------------------
    # 主读取入口
    # ------------------------------------------------------------------

    def read_upload(self, file_bytes: bytes, filename: str) -> dict:
        """读取上传的矢量数据。

        支持：
        - .zip：shp ZIP 包（含 .shp .shx .dbf .prj，可选 .cpg）
        - .geojson / .json：GeoJSON
        - .kml：KML

        处理流程：
        1. 文件类型分发
        2. shp ZIP：.cpg 声明 -> chardet 探测 .dbf -> 轮询常见中文编码
        3. geojson：UTF-8 优先，失败降级 GBK
        4. 坐标系识别 + 国内数据转 GCJ02
        5. 返回统一结构

        Args:
            file_bytes: 文件二进制内容
            filename: 文件名（用于类型分发）

        Returns:
            dict，结构见模块 docstring
        """
        name_lower = (filename or "").lower()

        try:
            if name_lower.endswith(".zip"):
                gdf, warnings, encodings_tried = self._read_shp_zip(file_bytes)
                if gdf is None:
                    return {
                        "status": "error",
                        "message": (
                            "Shapefile 编码解析失败，已尝试编码: "
                            f"{encodings_tried}。建议另存为 UTF-8 后重新上传。"
                        ),
                        "encodings_tried": encodings_tried,
                    }
            elif name_lower.endswith((".geojson", ".json")):
                gdf, warnings = self._read_geojson(file_bytes)
                if gdf is None:
                    return {
                        "status": "error",
                        "message": "GeoJSON 解析失败：编码不支持或格式非法。",
                        "encodings_tried": list(self.COMMON_CN_ENCODINGS),
                    }
            elif name_lower.endswith(".kml"):
                gdf, warnings = self._read_kml(file_bytes)
                if gdf is None:
                    return {
                        "status": "error",
                        "message": "KML 解析失败：格式非法或编码不支持。",
                    }
            else:
                return {
                    "status": "error",
                    "message": (
                        f"不支持的文件类型：{filename or '(空)'}，"
                        "仅支持 .zip / .geojson / .json / .kml"
                    ),
                }
        except Exception as e:
            logger.exception("read_upload 解析异常 filename=%s", filename)
            return {
                "status": "error",
                "message": f"文件解析异常：{e}",
            }

        # 统一返回结构
        return self._build_ok_result(gdf, warnings)

    # ------------------------------------------------------------------
    # shp ZIP 读取
    # ------------------------------------------------------------------

    def _read_shp_zip(
        self, file_bytes: bytes
    ) -> tuple[Optional[gpd.GeoDataFrame], list[str], list[str]]:
        """读取 shp ZIP 包，返回 (gdf, warnings, encodings_tried)。

        编码三重降级：
        1. 预扫描 .cpg 编码声明文件
        2. chardet 探测 .dbf 文件头
        3. 轮询 COMMON_CN_ENCODINGS

        所有编码失败时 gdf 返回 None。
        """
        warnings: list[str] = []
        zip_buffer = io.BytesIO(file_bytes)

        detected_encoding: Optional[str] = None
        has_prj = False

        # 预扫描：.cpg / .dbf / .prj
        try:
            with zipfile.ZipFile(zip_buffer, "r") as zip_ref:
                names = [n.lower() for n in zip_ref.namelist()]
                cpg_files = [n for n in names if n.endswith(".cpg")]
                dbf_files = [n for n in names if n.endswith(".dbf")]
                prj_files = [n for n in names if n.endswith(".prj")]

                has_prj = bool(prj_files)

                # 1. .cpg 声明优先
                if cpg_files:
                    try:
                        with zip_ref.open(cpg_files[0]) as cpg_file:
                            cpg_raw = cpg_file.read().decode("ascii").strip()
                        if cpg_raw:
                            detected_encoding = self._normalize_encoding(cpg_raw)
                            logger.info(
                                "shp ZIP 使用 .cpg 声明编码: %s", detected_encoding
                            )
                    except Exception as e:
                        logger.debug("读取 .cpg 失败: %s", e)

                # 2. 无 .cpg 时 chardet 探测 .dbf
                if not detected_encoding and dbf_files:
                    detected_encoding = self._detect_dbf_encoding(
                        zip_ref, dbf_files[0]
                    )
                    if detected_encoding:
                        logger.info(
                            "shp ZIP chardet 探测编码: %s", detected_encoding
                        )
        except zipfile.BadZipFile as e:
            logger.warning("非法 ZIP: %s", e)
            return None, warnings, []

        # 3. 构建编码尝试列表
        encodings_to_try: list[str] = []
        if detected_encoding:
            encodings_to_try.append(detected_encoding)
        for enc in self.COMMON_CN_ENCODINGS:
            if enc not in encodings_to_try:
                encodings_to_try.append(enc)

        # 4. 多重降级尝试读取
        last_error: Optional[Exception] = None
        for enc in encodings_to_try:
            try:
                zip_buffer.seek(0)
                gdf = gpd.read_file(zip_buffer, encoding=enc)
                if gdf is None:
                    last_error = ValueError("fiona 读取返回 None")
                    continue
                if len(gdf) == 0:
                    # 有效但空的 shapefile — 返回空 GDF，不继续尝试其他编码
                    logger.info("shp ZIP 读取成功但为空 encoding=%s", enc)
                    return gdf, warnings, encodings_to_try
                logger.info("shp ZIP 读取成功 encoding=%s rows=%d", enc, len(gdf))
                if not has_prj:
                    warnings.append(
                        "Shapefile 缺少 .prj 投影文件，已按经纬度范围启发式"
                        "识别为 EPSG:4326（WGS84）。"
                    )
                    # 若无 crs，按启发式赋予 WGS84
                    if gdf.crs is None:
                        gdf.set_crs("EPSG:4326", inplace=True)
                return gdf, warnings, encodings_to_try
            except UnicodeDecodeError as e:
                last_error = e
                continue
            except Exception as e:
                # 部分编码下 fiona 会抛 FionaError / DriverError
                last_error = e
                continue

        logger.warning(
            "shp ZIP 所有编码均失败 tried=%s last_error=%s",
            encodings_to_try, last_error,
        )
        return None, warnings, encodings_to_try

    # ------------------------------------------------------------------
    # GeoJSON 读取
    # ------------------------------------------------------------------

    def _read_geojson(
        self, file_bytes: bytes
    ) -> tuple[Optional[gpd.GeoDataFrame], list[str]]:
        """读取 GeoJSON，UTF-8 优先，GBK 降级。"""
        warnings: list[str] = []

        # 先尝试 UTF-8 / UTF-8-SIG
        text = None
        for enc in ("utf-8-sig", "utf-8"):
            try:
                text = file_bytes.decode(enc)
                break
            except UnicodeDecodeError:
                continue

        # 降级 GBK
        if text is None:
            try:
                text = file_bytes.decode("gbk")
                warnings.append("GeoJSON 非 UTF-8 编码，已按 GBK 降级解析。")
            except UnicodeDecodeError:
                return None, warnings

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning("GeoJSON 解析失败: %s", e)
            return None, warnings

        if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
            warnings.append("GeoJSON 缺少 type=FeatureCollection 声明。")

        try:
            gdf = gpd.read_file(io.StringIO(text))
        except Exception as e:
            logger.warning("GeoDataFrame 读取失败: %s", e)
            return None, warnings

        # 若无 crs，默认 WGS84
        if gdf.crs is None:
            gdf.set_crs("EPSG:4326", inplace=True)

        return gdf, warnings

    # ------------------------------------------------------------------
    # KML 读取
    # ------------------------------------------------------------------

    def _read_kml(
        self, file_bytes: bytes
    ) -> tuple[Optional[gpd.GeoDataFrame], list[str]]:
        """读取 KML 文件。"""
        warnings: list[str] = []
        try:
            gdf = gpd.read_file(io.BytesIO(file_bytes))
        except Exception as e:
            logger.warning("KML 解析失败: %s", e)
            return None, warnings

        if gdf.crs is None:
            gdf.set_crs("EPSG:4326", inplace=True)
        return gdf, warnings

    # ------------------------------------------------------------------
    # 结果构建 + 坐标转换
    # ------------------------------------------------------------------

    def _build_ok_result(
        self, gdf: gpd.GeoDataFrame, warnings: list[str]
    ) -> dict:
        """构建成功返回结构，含坐标系识别 + 国内数据转 GCJ02。"""
        original_crs = gdf.crs.to_string() if gdf.crs else "Unknown"

        # 国内数据转 GCJ02
        is_china = self._is_china_data(gdf)
        if is_china and original_crs != "Unknown":
            gdf = self._to_gcj02(gdf)
            final_crs = "GCJ02"
        else:
            final_crs = original_crs if original_crs != "Unknown" else "EPSG:4326"

        # 几何类型
        geom_types = set()
        for geom in gdf.geometry:
            if geom is None or geom.is_empty:
                continue
            geom_types.add(geom.geom_type)
        geometry_type = "/".join(sorted(geom_types)) if geom_types else ""

        return {
            "status": "ok",
            "data": gdf,
            "crs": final_crs,
            "original_crs": original_crs,
            "feature_count": len(gdf),
            "geometry_type": geometry_type,
            "warnings": warnings,
        }

    # ------------------------------------------------------------------
    # 国内数据判断
    # ------------------------------------------------------------------

    def _is_china_data(self, gdf: gpd.GeoDataFrame) -> bool:
        """判断数据是否在中国大陆范围内。

        启发式：取所有几何质心的经纬度，若大部分落在国内范围则判定为国内数据。
        避免单点误差，用多数投票。
        """
        if gdf is None or len(gdf) == 0:
            return False

        total = 0
        in_china = 0
        # Pre-convert to WGS84 once if needed (projected CRS case).
        # Avoids repeated to_crs() calls and the fragile list().index() lookup.
        wgs84_gdf = None
        if gdf.crs is not None:
            try:
                wgs84_gdf = gdf.to_crs("EPSG:4326")
            except Exception:
                wgs84_gdf = None

        for idx, geom in enumerate(gdf.geometry):
            if geom is None or geom.is_empty:
                continue
            try:
                centroid = geom.centroid
                lng, lat = centroid.x, centroid.y
                # 仅对地理坐标范围做判断（投影坐标数值巨大，需先转 WGS84）
                if abs(lng) > 180 or abs(lat) > 90:
                    # 投影坐标，查预先转换的 WGS84 GDF 中对应索引的几何
                    if wgs84_gdf is not None:
                        c2 = wgs84_gdf.geometry.iloc[idx].centroid
                        lng, lat = c2.x, c2.y
                    else:
                        continue
                total += 1
                if (
                    self._CHINA_LNG_MIN <= lng <= self._CHINA_LNG_MAX
                    and self._CHINA_LAT_MIN <= lat <= self._CHINA_LAT_MAX
                ):
                    in_china += 1
            except Exception:
                continue

        if total == 0:
            return False
        # 多数投票：>50% 点在国内即判定国内数据
        return in_china / total > 0.5

    # ------------------------------------------------------------------
    # WGS84 -> GCJ02
    # ------------------------------------------------------------------

    def _to_gcj02(self, gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        """将 WGS84 GeoDataFrame 转为 GCJ02。

        国外坐标不偏转（out_of_china）。GCJ02 无标准 EPSG，crs 仍使用
        EPSG:4326 作为兼容容器，但 ``attrs["crs_label"]`` 显式标记为
        ``GCJ02``。下游空间计算据此先做数学反偏转，导出也会恢复 WGS84，
        因此不会把偏转后的数值直接交给 pyproj 或写成错误的 KML 坐标。
        """
        if gdf is None or len(gdf) == 0:
            return gdf

        # 确保 WGS84 下做偏转
        original_crs = gdf.crs
        if original_crs is None:
            gdf = gdf.set_crs("EPSG:4326")
        elif original_crs.to_string() != "EPSG:4326":
            gdf = gdf.to_crs("EPSG:4326")

        def _transform_geom(geom):
            if geom is None or geom.is_empty:
                return geom
            # shapely 2.x：transform 支持
            from shapely.ops import transform

            def _fn(x, y, z=None):
                new_coords = []
                for xi, yi in zip(x, y):
                    if out_of_china(xi, yi):
                        new_coords.append((xi, yi))
                    else:
                        gx, gy = wgs84_to_gcj02(xi, yi)
                        new_coords.append((gx, gy))
                return tuple(zip(*new_coords)) if new_coords else ((), ())

            return transform(_fn, geom)

        new_geoms = gdf.geometry.apply(_transform_geom)
        result = gdf.copy()
        result.geometry = new_geoms
        # 保留原 crs 标签（GCJ02 无标准 EPSG，下游按数值渲染）
        if original_crs is not None:
            result.set_crs(original_crs, inplace=True, allow_override=True)
        else:
            result.set_crs("EPSG:4326", inplace=True)
        # EPSG:4326 is only a compatibility container here: the coordinates
        # above are GCJ02 numbers. Persist the semantic label so the next
        # spatial step converts them back to WGS84 before using pyproj.
        result.attrs = {**dict(gdf.attrs or {}), "crs_label": "GCJ02"}
        return result

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------

    def export(self, gdf: gpd.GeoDataFrame, fmt: str = "geojson") -> bytes:
        """导出 GeoDataFrame 为指定格式。

        KML 导出时自动检测 GCJ02 坐标并转回 WGS84，避免导出文件声称 WGS84
        但实际含偏移坐标的问题。

        Args:
            gdf: 地理数据
            fmt: "geojson" / "shp" / "kml"

        Returns:
            bytes（geojson 为 UTF-8 文本字节，shp 为 ZIP 字节，kml 为 XML 字节）
        """
        if gdf is None or len(gdf) == 0:
            if fmt == "geojson":
                return json.dumps(
                    {"type": "FeatureCollection", "features": []}
                ).encode("utf-8")
            raise ValueError("空数据无法导出为 " + fmt)

        fmt_lower = (fmt or "").lower()

        if fmt_lower == "geojson":
            text = gdf.to_json()
            return text.encode("utf-8") if isinstance(text, str) else text

        if fmt_lower == "shp":
            return self._export_shp_zip(gdf)

        if fmt_lower == "kml":
            # KML 规范要求 WGS84 坐标；若数据含 GCJ02 偏移则先转回
            export_gdf = self._gcj02_to_wgs84_for_export(gdf)
            buf = io.BytesIO()
            export_gdf.to_file(buf, driver="KML", encoding="utf-8")
            return buf.getvalue()

        raise ValueError(f"不支持的导出格式：{fmt}，仅支持 geojson/shp/kml")

    def _gcj02_to_wgs84_for_export(self, gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        """KML 导出前将 GCJ02 坐标转回 WGS84。

        检测策略（任一为真即视为 GCJ02）：
        1. attrs["crs_label"] == "GCJ02"（spatial_analysis 管线标记）
        2. 数据在中国大陆范围内（启发式：质心多数落在国内 bbox）

        转换使用 gcj02_to_wgs84 数学偏转（与 _to_gcj02 互逆）。
        非 GCJ02 数据原样返回。
        """
        from app.tools.geo_transform import gcj02_to_wgs84

        # 检测 1：显式 crs_label
        crs_label = (gdf.attrs or {}).get("crs_label", "").upper()
        is_gcj02 = crs_label == "GCJ02"

        # 检测 2：启发式坐标范围
        if not is_gcj02:
            is_gcj02 = self._is_china_data(gdf)

        if not is_gcj02:
            return gdf

        logger.info("KML 导出检测到 GCJ02 数据，自动转回 WGS84")

        from shapely.ops import transform

        def _transform_geom(geom):
            if geom is None or geom.is_empty:
                return geom

            def _fn(x, y, z=None):
                new_coords = []
                for xi, yi in zip(x, y):
                    wx, wy = gcj02_to_wgs84(float(xi), float(yi))
                    new_coords.append((wx, wy))
                return tuple(zip(*new_coords)) if new_coords else ((), ())

            return transform(_fn, geom)

        new_geoms = gdf.geometry.apply(_transform_geom)
        result = gdf.copy()
        result.geometry = new_geoms
        result.set_crs("EPSG:4326", inplace=True, allow_override=True)
        return result

    def _export_shp_zip(self, gdf: gpd.GeoDataFrame) -> bytes:
        """导出为 shp ZIP 包（含 .shp .shx .dbf .prj .cpg）。"""
        import os
        import tempfile

        tmp = tempfile.NamedTemporaryFile(suffix=".shp", delete=False)
        tmp_path = tmp.name
        tmp.close()

        try:
            gdf.to_file(tmp_path, driver="ESRI Shapefile", encoding="utf-8")
            base = os.path.splitext(tmp_path)[0]
            exts = [".shp", ".shx", ".dbf", ".prj", ".cpg"]

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for ext in exts:
                    p = base + ext
                    if os.path.exists(p):
                        zf.write(p, arcname="data" + ext)
            return buf.getvalue()
        finally:
            base = os.path.splitext(tmp_path)[0]
            for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
                p = base + ext
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass

    # ------------------------------------------------------------------
    # 通用矢量/栅格/CSV 加载（任务 1 扩展）
    # ------------------------------------------------------------------

    def load_vector(self, path: str) -> dict:
        """读取矢量文件，返回 GeoJSON dict + metadata。

        Args:
            path: 文件路径（支持 shp / geojson / gpkg / kml 等 fiona 支持的格式）。

        Returns:
            {"status": "success", "data": geojson_dict, "metadata": {...}}
            或 {"status": "error", "message": "..."}
        """
        try:
            gdf = gpd.read_file(path)
            if gdf is None or len(gdf) == 0:
                return {
                    "status": "success",
                    "data": {"type": "FeatureCollection", "features": []},
                    "metadata": {
                        "crs": None,
                        "geometry_type": "",
                        "feature_count": 0,
                        "fields": [],
                        "bbox": None,
                    },
                }
            geojson_dict = json.loads(gdf.to_json())
            meta = self._extract_metadata(gdf)
            return {"status": "success", "data": geojson_dict, "metadata": meta}
        except Exception as e:
            logger.exception("load_vector 失败 path=%s", path)
            return {"status": "error", "message": f"矢量文件读取失败：{e}"}

    def load_raster(self, path: str, include_data: bool = False) -> dict:
        """读取栅格文件，返回元数据，可选返回像素数据。

        Args:
            path: 文件路径。
            include_data: 是否返回像素数据（仅限小栅格，>10MB 跳过）。

        Returns:
            {"status": "success", "data": {"metadata": {...}, "pixels": [...]}}
            或 {"status": "error", "message": "..."}
        """
        if not _RASTERIO_AVAILABLE:
            return {"status": "error", "message": "rasterio 未安装，无法读取栅格文件。"}
        try:
            with rasterio.open(path) as src:
                meta = {
                    "crs": str(src.crs) if src.crs else None,
                    "transform": list(src.transform) if src.transform else None,
                    "shape": (src.height, src.width),
                    "bands": src.count,
                    "dtype": str(src.dtypes[0]) if src.dtypes else None,
                    "nodata": src.nodata,
                    "bounds": {
                        "left": src.bounds.left,
                        "bottom": src.bounds.bottom,
                        "right": src.bounds.right,
                        "top": src.bounds.top,
                    },
                }
                result: dict[str, Any] = {"metadata": meta}
                if include_data:
                    file_size = os.path.getsize(path)
                    if file_size > 10 * 1024 * 1024:
                        result["pixels"] = None
                        result["pixel_note"] = (
                            f"文件过大（{file_size / 1024 / 1024:.1f} MB），"
                            f"跳过像素数据读取。"
                        )
                    else:
                        bands_data = []
                        for b in range(1, src.count + 1):
                            band = src.read(b)
                            bands_data.append(band.tolist())
                        result["pixels"] = bands_data
                return {"status": "success", "data": result}
        except Exception as e:
            logger.exception("load_raster 失败 path=%s", path)
            return {"status": "error", "message": f"栅格文件读取失败：{e}"}

    def load_csv(
        self, path: str, encoding: str = "utf-8"
    ) -> dict:
        """读取 CSV 文件，返回列名、行数、前 5 行样本。

        Args:
            path: CSV 文件路径。
            encoding: 文件编码，默认 utf-8。

        Returns:
            {"status": "success", "data": {"columns": [...],
             "row_count": int, "sample": [...]}}
            或 {"status": "error", "message": "..."}
        """
        try:
            df = pd.read_csv(path, encoding=encoding)
            sample = df.head(5).to_dict(orient="records")
            return {
                "status": "success",
                "data": {
                    "columns": list(df.columns),
                    "row_count": len(df),
                    "sample": sample,
                },
            }
        except UnicodeDecodeError as e:
            return {
                "status": "error",
                "message": (
                    f"CSV 编码 {encoding} 解析失败，请尝试其他编码（如 gbk）：{e}"
                ),
            }
        except Exception as e:
            logger.exception("load_csv 失败 path=%s", path)
            return {"status": "error", "message": f"CSV 读取失败：{e}"}

    def csv_to_points(
        self,
        csv_df_or_path: Union[str, pd.DataFrame, dict],
        x_field: str,
        y_field: str,
        crs: str = "EPSG:4326",
    ) -> dict:
        """将 CSV 转为点矢量 GeoDataFrame，输出 GeoJSON dict。

        内部校验：
        - x_field / y_field 不存在 → error 返回
        - lon 超出 [-180, 180] 或 lat 超出 [-90, 90] → warning + data 中标注

        Args:
            csv_df_or_path: CSV 文件路径 或 pandas DataFrame 或 dict（含 data.sample）。
            x_field: 经度/横坐标字段名。
            y_field: 纬度/纵坐标字段名。
            crs: 坐标系，默认 EPSG:4326。

        Returns:
            {"status": "success", "data": geojson_dict, "warnings": [...]}
            或 {"status": "error", "message": "..."}
        """
        warnings: list[str] = []

        # 解析输入
        try:
            if isinstance(csv_df_or_path, str):
                df = pd.read_csv(csv_df_or_path)
            elif isinstance(csv_df_or_path, pd.DataFrame):
                df = csv_df_or_path
            elif isinstance(csv_df_or_path, dict):
                # 从 load_csv 结果中提取
                inner = csv_df_or_path.get("data", csv_df_or_path)
                sample = inner.get("sample", [])
                if sample:
                    df = pd.DataFrame(sample)
                else:
                    return {
                        "status": "error",
                        "message": "无法从输入 dict 中提取表格数据。",
                    }
            else:
                return {
                    "status": "error",
                    "message": (
                        f"不支持的输入类型：{type(csv_df_or_path).__name__}，"
                        f"仅支持 str / DataFrame / dict。"
                    ),
                }
        except Exception as e:
            return {"status": "error", "message": f"CSV 解析失败：{e}"}

        if df is None or len(df) == 0:
            return {"status": "error", "message": "输入数据为空。"}

        # 校验字段存在
        if x_field not in df.columns:
            return {
                "status": "error",
                "message": (
                    f"X 坐标字段 '{x_field}' 不存在。"
                    f"可用字段：{list(df.columns)}"
                ),
            }
        if y_field not in df.columns:
            return {
                "status": "error",
                "message": (
                    f"Y 坐标字段 '{y_field}' 不存在。"
                    f"可用字段：{list(df.columns)}"
                ),
            }

        # 坐标值范围检查
        df_clean = df.dropna(subset=[x_field, y_field]).copy()
        if len(df_clean) < len(df):
            dropped = len(df) - len(df_clean)
            warnings.append(f"移除了 {dropped} 行坐标缺失的记录。")

        x_series = pd.to_numeric(df_clean[x_field], errors="coerce")
        y_series = pd.to_numeric(df_clean[y_field], errors="coerce")
        valid_mask = x_series.notna() & y_series.notna()

        lon_oor = (x_series[valid_mask] < -180) | (x_series[valid_mask] > 180)
        lat_oor = (y_series[valid_mask] < -90) | (y_series[valid_mask] > 90)
        oo_range = lon_oor | lat_oor

        if oo_range.any():
            oo_count = int(oo_range.sum())
            warnings.append(
                f"坐标值范围异常：{oo_count} 个点超出预期范围 "
                f"(lon ∈ [-180, 180], lat ∈ [-90, 90])。"
                f"请检查 x_field / y_field 是否选反。"
            )

        # 构造 Point geometry（保留超出范围的点，仅移除无效坐标）
        geometries = [
            (
                Point(float(x_series.iloc[i]), float(y_series.iloc[i]))
                if valid_mask.iloc[i]
                else None
            )
            for i in range(len(df_clean))
        ]

        gdf = gpd.GeoDataFrame(
            df_clean.drop(columns=[x_field, y_field]),
            geometry=geometries,
            crs=crs,
        )
        # 移除无效几何（坐标无法转为数值的行）
        gdf = gdf[gdf.geometry.notna()].copy()

        geojson_dict = json.loads(gdf.to_json())
        return {
            "status": "success",
            "data": geojson_dict,
            "warnings": warnings,
        }

    def summarize_layer(
        self,
        gdf_or_dict: Union[gpd.GeoDataFrame, dict],
    ) -> dict:
        """提取图层的 crs、geometry_type、feature_count、fields、bbox。

        Args:
            gdf_or_dict: GeoDataFrame 或 GeoJSON dict。

        Returns:
            {"status": "success", "data": {"crs": ..., "geometry_type": ...,
             "feature_count": ..., "fields": ..., "bbox": ...}}
        """
        try:
            if isinstance(gdf_or_dict, gpd.GeoDataFrame):
                gdf = gdf_or_dict
            elif isinstance(gdf_or_dict, dict):
                gdf = gpd.GeoDataFrame.from_features(
                    gdf_or_dict.get("features", [])
                )
            else:
                return {
                    "status": "error",
                    "message": (
                        f"不支持的输入类型：{type(gdf_or_dict).__name__}"
                    ),
                }

            if gdf is None or len(gdf) == 0:
                return {
                    "status": "success",
                    "data": {
                        "crs": None,
                        "geometry_type": "",
                        "feature_count": 0,
                        "fields": [],
                        "bbox": None,
                    },
                }

            meta = self._extract_metadata(gdf)
            return {"status": "success", "data": meta}
        except Exception as e:
            logger.exception("summarize_layer 失败")
            return {"status": "error", "message": f"图层摘要提取失败：{e}"}

    def export_result(
        self,
        gdf_or_dict: Union[gpd.GeoDataFrame, dict],
        path: str,
        driver: str = "GPKG",
    ) -> dict:
        """将 GeoDataFrame 或 GeoJSON dict 导出到文件。

        Args:
            gdf_or_dict: GeoDataFrame 或 GeoJSON dict。
            path: 输出文件路径。
            driver: OGR 驱动名，默认 "GPKG"。

        Returns:
            {"status": "success", "data": {"path": ..., "feature_count": ...}}
            或 {"status": "error", "message": "..."}
        """
        try:
            if isinstance(gdf_or_dict, gpd.GeoDataFrame):
                gdf = gdf_or_dict
            elif isinstance(gdf_or_dict, dict):
                gdf = gpd.GeoDataFrame.from_features(
                    gdf_or_dict.get("features", [])
                )
                if gdf_or_dict.get("crs"):
                    try:
                        gdf.set_crs(gdf_or_dict["crs"], inplace=True)
                    except Exception:
                        pass
            else:
                return {
                    "status": "error",
                    "message": (
                        f"不支持的输入类型：{type(gdf_or_dict).__name__}"
                    ),
                }

            if gdf is None or len(gdf) == 0:
                return {
                    "status": "error",
                    "message": "数据为空，无法导出。",
                }

            # 确保输出目录存在
            parent = Path(path).parent
            parent.mkdir(parents=True, exist_ok=True)

            gdf.to_file(path, driver=driver)
            return {
                "status": "success",
                "data": {
                    "path": str(Path(path).resolve()),
                    "feature_count": len(gdf),
                },
            }
        except Exception as e:
            logger.exception("export_result 失败 path=%s", path)
            return {"status": "error", "message": f"数据导出失败：{e}"}

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_metadata(gdf: gpd.GeoDataFrame) -> dict[str, Any]:
        """从 GeoDataFrame 提取元数据（crs, geometry_type, feature_count, fields, bbox）。"""
        crs_str = str(gdf.crs) if gdf.crs else None

        geom_types: set[str] = set()
        for geom in gdf.geometry:
            if geom is not None and not geom.is_empty:
                geom_types.add(geom.geom_type)
        geometry_type = "/".join(sorted(geom_types)) if geom_types else ""

        fields = list(gdf.columns)

        try:
            bounds = gdf.total_bounds
            bbox = [float(bounds[0]), float(bounds[1]), float(bounds[2]), float(bounds[3])]
        except Exception:
            bbox = None

        return {
            "crs": crs_str,
            "geometry_type": geometry_type,
            "feature_count": len(gdf),
            "fields": fields,
            "bbox": bbox,
        }

    @staticmethod
    def _normalize_encoding(name: str) -> str:
        """归一化编码名（小写，gb2312/gb18030 -> gbk）。"""
        n = name.strip().lower()
        if n in ("gb2312", "gb18030"):
            return "gbk"
        return n
