"""基于 rasterio + numpy 的栅格分析引擎。

不依赖 QGIS processing。所有方法接收文件路径(str)作为输入参数。

返回值约定：
- 成功：{"status": "success", "data": ...}
- 错误：{"status": "error", "message": "..."}
"""

from __future__ import annotations

import base64
import io
import logging
import tempfile
import os
from typing import Any, Optional

import numpy as np
import rasterio
from rasterio.transform import Affine
from rasterio.warp import reproject as rio_reproject, calculate_default_transform
from rasterio.mask import mask
from rasterio.features import rasterize as rio_rasterize, shapes as rio_shapes
from rasterio.windows import from_bounds
from rasterio.transform import from_origin

logger = logging.getLogger(__name__)


class RasterAnalyzer:
    """基于 rasterio + numpy 的栅格分析引擎，不依赖 QGIS processing。"""

    # ------------------------------------------------------------------
    # 内部 helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _read_single_band(src_path: str, band: int = 1) -> tuple[np.ndarray, dict]:
        """读取栅格单波段数据。

        Args:
            src_path: 栅格文件路径。
            band: 波段号（1-based）。

        Returns:
            (array, profile) 元组。profile 是 rasterio 的 meta dict。
        """
        with rasterio.open(src_path) as src:
            arr = src.read(band)
            profile = src.profile.copy()
        return arr, profile

    @staticmethod
    def _output_profile(array: np.ndarray, profile: dict) -> dict:
        """Return a source profile normalized for a derived output array."""
        out_profile = profile.copy()
        out_profile.update({
            "height": array.shape[0],
            "width": array.shape[1],
            "dtype": array.dtype,
            "count": 1,
        })
        # Source profiles may carry creation options and nodata values that are
        # invalid for a derived raster with a different dtype (for example a
        # float DEM nodata=-9999 written as uint8 hillshade).
        if not out_profile.get("tiled"):
            out_profile.pop("blockxsize", None)
            out_profile.pop("blockysize", None)
        nodata = out_profile.get("nodata")
        if nodata is not None:
            dtype = np.dtype(array.dtype)
            limits = np.iinfo(dtype) if np.issubdtype(dtype, np.integer) else np.finfo(dtype)
            if not limits.min <= nodata <= limits.max:
                out_profile["nodata"] = None
        return out_profile

    @staticmethod
    def _to_tempfile(
        array: np.ndarray, profile: dict, suffix: str = ".tif"
    ) -> str:
        """将 array + profile 写入临时 .tif 文件。

        Args:
            array: 2D numpy 数组。
            profile: rasterio profile dict。
            suffix: 文件后缀。

        Returns:
            临时文件路径。
        """
        out_profile = RasterAnalyzer._output_profile(array, profile)
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        tmp_path = tmp.name
        tmp.close()
        with rasterio.open(tmp_path, "w", **out_profile) as dst:
            dst.write(array, 1)
        return tmp_path

    @staticmethod
    def _focal_window(
        array: np.ndarray, radius: int, fn: Any
    ) -> np.ndarray:
        """滑动窗口通用函数。

        对 array 的每个像元，取以它为中心的 (2*radius+1)×(2*radius+1) 窗口，
        传给 fn 函数，返回值填入输出数组对应位置。
        边界像元以边缘值填充（pad mode='edge'）。

        Args:
            array: 2D numpy 数组。
            radius: 窗口半径（窗口边长 = 2*radius + 1）。
            fn: 函数，接收 2D 窗口数组，返回标量。

        Returns:
            与输入同形状的输出数组。
        """
        from numpy.lib.stride_tricks import sliding_window_view

        padded = np.pad(array, radius, mode="edge")
        windows = sliding_window_view(padded, (2 * radius + 1, 2 * radius + 1))
        h, w = array.shape
        result = np.zeros((h, w), dtype=np.float32)
        for i in range(h):
            for j in range(w):
                result[i, j] = fn(windows[i, j])
        return result

    # ------------------------------------------------------------------
    # RasterLayer 渲染 helpers（PNG base64 + bbox）
    # ------------------------------------------------------------------

    SUPPORTED_COLORMAPS = {"terrain", "viridis", "grayscale", "aspect"}

    @staticmethod
    def _apply_colormap(arr: np.ndarray, cmap_name: str = "terrain") -> np.ndarray:
        """把单波段 arr 映射为 RGB uint8 (H, W, 3)。

        Args:
            arr: 2D numpy 数组。
            cmap_name: 'terrain' | 'viridis' | 'grayscale' | 'aspect' (循环色相 0-360)。
                      未知名称降级到 grayscale。

        Returns:
            RGB uint8 ndarray，shape (H, W, 3)。
        """
        mask = np.isnan(arr)
        if np.all(mask) or np.nanmin(arr) == np.nanmax(arr):
            return np.zeros((*arr.shape, 3), dtype=np.uint8)

        if cmap_name == "grayscale":
            norm = (arr - np.nanmin(arr)) / (np.nanmax(arr) - np.nanmin(arr) + 1e-10)
            norm = np.clip(norm, 0, 1)
            rgb = np.stack([norm * 255] * 3, axis=-1).astype(np.uint8)
            rgb[mask] = 0
            return rgb
        elif cmap_name == "aspect":
            # 坡向：0° 北用蓝色，90° 东用红，180° 南用绿，270° 西用黄
            # 使用 HSV 循环色相
            h = ((arr % 360) / 360.0) * 1.0
            s = np.full_like(arr, 0.9)
            v = np.full_like(arr, 0.9)
            h[mask] = 0; s[mask] = 0; v[mask] = 0
            hsv = np.stack([h, s, v], axis=-1)
            import matplotlib.colors as mcolors
            rgb = (mcolors.hsv_to_rgb(hsv) * 255).astype(np.uint8)
            return rgb
        else:
            try:
                import matplotlib.cm as cm
                # matplotlib >=3.9 移除 cm.get_cmap，改用 colormaps 或 plt.get_cmap
                try:
                    cmap = cm.get_cmap(cmap_name)
                except AttributeError:
                    import matplotlib.pyplot as plt
                    cmap = plt.get_cmap(cmap_name)
                norm = (arr - np.nanmin(arr)) / (np.nanmax(arr) - np.nanmin(arr) + 1e-10)
                norm = np.clip(norm, 0, 1)
                rgba = cmap(norm)  # (H, W, 4)
                rgb = (rgba[:, :, :3] * 255).astype(np.uint8)
                rgb[mask] = 0
                return rgb
            except ValueError:
                # cmap 名无效，降级 grayscale
                logger.warning("Invalid colormap %r, falling back to grayscale", cmap_name)
                norm = (arr - np.nanmin(arr)) / (np.nanmax(arr) - np.nanmin(arr) + 1e-10)
                norm = np.clip(norm, 0, 1)
                rgb = np.stack([norm * 255] * 3, axis=-1).astype(np.uint8)
                rgb[mask] = 0
                return rgb

    @staticmethod
    def _downsample_array(
        arr: np.ndarray, transform: Any, max_dim: int = 1024
    ) -> tuple[np.ndarray, Any]:
        """若 arr 任意维度 > max_dim，降采样到 max_dim（短边 ≤ max_dim）。

        Args:
            arr: 2D numpy 数组。
            transform: rasterio Affine。
            max_dim: 输出最大边长。

        Returns:
            (新数组, 新 transform)。
        """
        h, w = arr.shape
        if max(h, w) <= max_dim:
            return arr, transform

        scale = max_dim / float(max(h, w))
        new_h = max(1, int(round(h * scale)))
        new_w = max(1, int(round(w * scale)))

        from skimage.transform import resize

        resized = resize(arr, (new_h, new_w), preserve_range=True, anti_aliasing=True)
        # preserve_range 保持 dtype 量级，但 resize 总返回 float
        resized = resized.astype(arr.dtype)

        # Affine 调整：a/e 按 scale 缩放，b/c/d/f 保持
        try:
            new_transform = Affine(
                transform.a / scale,
                transform.b,
                transform.c,
                transform.d,
                transform.e / scale,
                transform.f,
            )
        except Exception:
            # fallback：用 list 重建
            vals = list(transform)[:6]
            vals[0] = vals[0] / scale
            vals[4] = vals[4] / scale
            new_transform = Affine(*vals)
        return resized, new_transform

    def _array_to_png_b64(
        self,
        arr_2d: np.ndarray,
        transform: Any,
        cmap_name: str = "terrain",
        max_dim: int = 1024,
        value_kind: str = "general",
    ) -> dict:
        """把 2D numpy 数组转 PNG base64 + bbox + 元数据（RasterLayer 格式）。

        若 arr 任意维度 > max_dim，降采样到 max_dim（保持比例）。

        Args:
            arr_2d: 2D numpy 数组。
            transform: rasterio Affine。
            cmap_name: colormap 名称。
            max_dim: 输出最大边长。
            value_kind: 语义标签（'slope_degrees' | 'hillshade' | 'aspect' | 'general'）。

        Returns:
            RasterLayer dict：含 type/png_b64/bbox/width/height/value_range/colormap/value_kind。
        """
        # 降采样（如需）
        arr_ds, transform_ds = self._downsample_array(arr_2d, transform, max_dim=max_dim)

        # colormap
        rgb = self._apply_colormap(arr_ds, cmap_name)

        # 编码 PNG
        from PIL import Image

        img = Image.fromarray(rgb)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        # bbox = (minx, miny, maxx, maxy)
        try:
            bounds = rasterio.transform.array_bounds(arr_ds.shape[0], arr_ds.shape[1], transform_ds)
        except Exception:
            # fallback：直接用 4 个角推算
            h, w = arr_ds.shape
            c = transform_ds.c
            f = transform_ds.f
            minx = c
            maxx = c + transform_ds.a * w + transform_ds.b * h
            miny = f + transform_ds.d * w + transform_ds.e * h
            maxy = f
            # 规范 order
            bounds = (min(minx, maxx), min(miny, maxy), max(minx, maxx), max(miny, maxy))

        # value range（用降采样后的真实范围）
        valid = arr_ds[~np.isnan(arr_ds)]
        if valid.size > 0:
            vmin = float(valid.min())
            vmax = float(valid.max())
        else:
            vmin, vmax = 0.0, 0.0

        return {
            "type": "raster",
            "png_b64": png_b64,
            "bbox": [float(b) for b in bounds],
            "width": int(rgb.shape[1]),
            "height": int(rgb.shape[0]),
            "value_range": [vmin, vmax],
            "colormap": cmap_name,
            "value_kind": value_kind,
            "opacity": 0.7,
        }

    def _to_raster_layer(
        self,
        arr: np.ndarray,
        transform,
        cmap_name: str = "terrain",
        value_kind: str = "general",
        max_dim: int = 1024,
    ) -> dict:
        """把 2D numpy 数组转 RasterLayer dict（PNG base64 + bbox + 元数据）。

        若 arr 任意维度 > max_dim，降采样到 max_dim（保持比例）。

        Args:
            arr: 2D numpy 数组。
            transform: rasterio Affine。
            cmap_name: colormap 名称。
            value_kind: 语义标签（'slope_degrees' | 'hillshade' | 'aspect' | 'general'）。
            max_dim: 输出最大边长。

        Returns:
            RasterLayer dict：含 type/png_b64/bbox/width/height/value_range/colormap/value_kind。
        """
        import io
        import base64
        from PIL import Image

        if arr.ndim != 2:
            return {"type": "error", "message": f"Expected 2D array, got {arr.ndim}D"}

        h, w = arr.shape
        # 降采样
        if max(h, w) > max_dim:
            from skimage.transform import resize as sk_resize

            scale = max_dim / max(h, w)
            new_h, new_w = int(h * scale), int(w * scale)
            arr = sk_resize(arr, (new_h, new_w), preserve_range=True, order=1, anti_aliasing=True)
            import rasterio

            transform = rasterio.Affine(
                transform.a / (w / new_w),
                transform.b,
                transform.c,
                transform.d,
                transform.e / (h / new_h),
                transform.f,
            )

        rgb = self._apply_colormap(arr, cmap_name)
        img = Image.fromarray(rgb)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        png_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        import rasterio

        bounds = rasterio.transform.array_bounds(rgb.shape[0], rgb.shape[1], transform)

        value_range = [float(np.nanmin(arr)), float(np.nanmax(arr))] if not np.all(np.isnan(arr)) else [0, 0]

        return {
            "type": "raster",
            "png_b64": png_b64,
            "bbox": [float(b) for b in bounds],
            "width": rgb.shape[1],
            "height": rgb.shape[0],
            "value_range": value_range,
            "colormap": cmap_name,
            "value_kind": value_kind,
            "opacity": 0.7,
        }

    @staticmethod
    def _resolve_colormap(cmap_name: Optional[str]) -> str:
        """校验 colormap 名：未知降级到 grayscale。"""
        if cmap_name and cmap_name in RasterAnalyzer.SUPPORTED_COLORMAPS:
            return cmap_name
        return "grayscale"

    # ------------------------------------------------------------------
    # 重投影
    # ------------------------------------------------------------------

    def reproject_raster(
        self, src_path: str, dst_crs: str, dst_path: Optional[str] = None
    ) -> dict:
        """重投影栅格到目标 CRS。

        Args:
            src_path: 源栅格文件路径。
            dst_crs: 目标 CRS（如 "EPSG:4326"）。
            dst_path: 输出文件路径。None 时不写盘，仅返回 RasterLayer。

        Returns:
            {"status": "success", "data": <RasterLayer dict>}，附加 dst_path（若提供）/ transform / crs / shape。
        """
        try:
            with rasterio.open(src_path) as src:
                transform, width, height = calculate_default_transform(
                    src.crs, dst_crs, src.width, src.height, *src.bounds
                )
                out_meta = src.meta.copy()
                out_meta.update({
                    "crs": dst_crs,
                    "transform": transform,
                    "width": width,
                    "height": height,
                })

                if dst_path is None:
                    tmp = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
                    dst_path = tmp.name
                    tmp.close()

                with rasterio.open(dst_path, "w", **out_meta) as dst:
                    for i in range(1, src.count + 1):
                        rio_reproject(
                            source=rasterio.band(src, i),
                            destination=rasterio.band(dst, i),
                            src_transform=src.transform,
                            src_crs=src.crs,
                            dst_transform=transform,
                            dst_crs=dst_crs,
                            resampling=rasterio.enums.Resampling.bilinear,
                        )
                # 读取波段 1 转 PNG
                with rasterio.open(dst_path) as src2:
                    arr = src2.read(1)
                    t = src2.transform

            raster_layer = self._to_raster_layer(
                arr,
                t,
                cmap_name="grayscale",
                value_kind="reproject",
            )
            # 附加原 .tif 路径/transform/crs/shape 给需要 .tif 的下游
            raster_layer["dst_path"] = dst_path
            raster_layer["transform"] = list(transform)
            raster_layer["crs"] = str(dst_crs)
            raster_layer["shape"] = [height, width]
            return {"status": "success", "data": raster_layer}
        except Exception as e:
            logger.exception("reproject_raster 失败: %s", e)
            return {"status": "error", "message": f"重投影失败: {str(e)}"}

    # ------------------------------------------------------------------
    # 裁剪
    # ------------------------------------------------------------------

    def clip_raster_by_mask(
        self,
        src_path: str,
        mask_gdf_or_path: Any,
        dst_path: Optional[str] = None,
    ) -> dict:
        """按矢量掩膜裁剪栅格。

        Args:
            src_path: 源栅格文件路径。
            mask_gdf_or_path: GeoDataFrame 或 shapefile 路径。
            dst_path: 输出文件路径，None 则写入临时文件。

        Returns:
            {"status": "success", "data": {"dst_path": ..., "transform": ..., "shape": ...}}
        """
        try:
            import geopandas as gpd

            if isinstance(mask_gdf_or_path, str):
                mask_gdf = gpd.read_file(mask_gdf_or_path)
            else:
                mask_gdf = mask_gdf_or_path

            with rasterio.open(src_path) as src:
                # 确保 CRS 一致
                mask_crs = str(mask_gdf.crs).upper() if mask_gdf.crs else ""
                src_crs = str(src.crs).upper() if src.crs else ""
                if mask_crs and src_crs and mask_crs != src_crs:
                    mask_gdf = mask_gdf.to_crs(src.crs)

                out_image, out_transform = mask(
                    src, mask_gdf.geometry, crop=True
                )
                out_meta = src.meta.copy()
                out_meta.update({
                    "driver": "GTiff",
                    "height": out_image.shape[1],
                    "width": out_image.shape[2],
                    "transform": out_transform,
                })

            if dst_path is None:
                tmp = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
                dst_path = tmp.name
                tmp.close()

            with rasterio.open(dst_path, "w", **out_meta) as dst:
                dst.write(out_image)
                out_image_arr = out_image[0] if out_image.ndim == 3 else out_image

            with rasterio.open(dst_path) as src2:
                arr = src2.read(1)
                t = src2.transform

            raster_layer = self._to_raster_layer(
                arr,
                t,
                cmap_name="grayscale",
                value_kind="clip_mask",
            )
            data = dict(raster_layer)
            data["dst_path"] = dst_path
            data["transform"] = list(out_transform)
            data["shape"] = [out_meta["height"], out_meta["width"]]
            return {"status": "success", "data": data}
        except Exception as e:
            logger.exception("clip_raster_by_mask 失败: %s", e)
            return {"status": "error", "message": f"掩膜裁剪失败: {str(e)}"}

    def clip_raster_by_extent(
        self,
        src_path: str,
        bbox: tuple,
        dst_path: Optional[str] = None,
    ) -> dict:
        """按 bbox (minx, miny, maxx, maxy) 裁剪栅格。

        Args:
            src_path: 源栅格文件路径。
            bbox: (minx, miny, maxx, maxy)。
            dst_path: 输出文件路径，None 则写入临时文件。

        Returns:
            {"status": "success", "data": {"dst_path": ..., "transform": ..., "shape": ...}}
        """
        try:
            minx, miny, maxx, maxy = bbox
            with rasterio.open(src_path) as src:
                # from_bounds requires (left, bottom, right, top).
                # For south-up transforms (e>0), bottom > top; for north-up, bottom < top.
                if src.transform.e > 0:
                    window = from_bounds(minx, maxy, maxx, miny, src.transform)
                else:
                    window = from_bounds(minx, miny, maxx, maxy, src.transform)
                window = window.round_lengths().round_offsets()
                out_image = src.read(window=window)
                out_transform = src.window_transform(window)
                out_meta = src.meta.copy()
                out_meta.update({
                    "height": out_image.shape[1],
                    "width": out_image.shape[2],
                    "transform": out_transform,
                })

            if dst_path is None:
                tmp = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
                dst_path = tmp.name
                tmp.close()

            with rasterio.open(dst_path, "w", **out_meta) as dst:
                dst.write(out_image)

            with rasterio.open(dst_path) as src2:
                arr = src2.read(1)
                t = src2.transform

            raster_layer = self._to_raster_layer(
                arr,
                t,
                cmap_name="grayscale",
                value_kind="clip_extent",
            )
            data = dict(raster_layer)
            data["dst_path"] = dst_path
            data["transform"] = list(out_transform)
            data["shape"] = [out_meta["height"], out_meta["width"]]
            return {"status": "success", "data": data}
        except Exception as e:
            logger.exception("clip_raster_by_extent 失败: %s", e)
            return {"status": "error", "message": f"范围裁剪失败: {str(e)}"}

    # ------------------------------------------------------------------
    # 栅格计算器
    # ------------------------------------------------------------------

    def raster_calculator(
        self,
        rasters: dict[str, str],
        expression: str,
        dst_path: Optional[str] = None,
    ) -> dict:
        """栅格计算器。

        Args:
            rasters: {"a": path, "b": path} 名称→路径映射。
            expression: numpy 表达式，如 "a + b * 2"。
            dst_path: 输出文件路径，None 则写入临时文件。

        Returns:
            {"status": "success", "data": {"dst_path": ..., "shape": ...}}
        """
        try:
            # 读取所有栅格
            arrays: dict[str, np.ndarray] = {}
            profile = None
            for name, path in rasters.items():
                with rasterio.open(path) as src:
                    arr = src.read(1)
                    if profile is None:
                        profile = src.profile.copy()
                arrays[name] = arr

            if profile is None:
                return {"status": "error", "message": "未提供有效栅格"}

            # 安全 eval：只允许 numpy 函数 + 基本运算
            result = self._safe_eval(expression, arrays)

            if dst_path is None:
                tmp = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
                dst_path = tmp.name
                tmp.close()

            out_profile = profile.copy()
            out_profile.update({
                "height": result.shape[0],
                "width": result.shape[1],
                "dtype": result.dtype,
                "count": 1,
            })
            out_t = profile["transform"]
            with rasterio.open(dst_path, "w", **out_profile) as dst:
                dst.write(result, 1)

            raster_layer = self._to_raster_layer(
                result,
                out_t,
                cmap_name="viridis",
                value_kind="raster_calculator",
            )
            data = dict(raster_layer)
            data["dst_path"] = dst_path
            data["shape"] = [result.shape[0], result.shape[1]]
            return {"status": "success", "data": data}
        except Exception as e:
            logger.exception("raster_calculator 失败: %s", e)
            return {"status": "error", "message": f"栅格计算失败: {str(e)}"}

    @staticmethod
    def _safe_eval(expression: str, arrays: dict[str, np.ndarray]) -> np.ndarray:
        """安全 eval numpy 表达式。

        仅允许 numpy 模块函数 + 基本四则运算 + 幂运算 + 比较运算。
        """
        safe_builtins = {
            "abs": abs,
            "min": min,
            "max": max,
            "True": True,
            "False": False,
        }
        safe_globals = {"__builtins__": safe_builtins, "np": np}
        safe_locals = dict(arrays)
        try:
            result = eval(expression, safe_globals, safe_locals)
            return np.asarray(result, dtype=np.float32)
        except Exception as e:
            raise ValueError(f"表达式求值失败: {expression} — {e}") from e

    # ------------------------------------------------------------------
    # 分区统计
    # ------------------------------------------------------------------

    def zonal_statistics(
        self,
        raster_path: str,
        vector_gdf_or_path: Any,
        stats: Optional[list[str]] = None,
    ) -> dict:
        """分区统计。

        优先使用 rasterstats.zonal_stats，fallback 到手动实现。

        Args:
            raster_path: 栅格文件路径。
            vector_gdf_or_path: GeoDataFrame 或矢量文件路径。
            stats: 统计指标列表，默认 ["mean", "min", "max", "sum", "std", "count"]。

        Returns:
            {"status": "success", "data": [...]} 每个 feature 一条记录。
        """
        if stats is None:
            stats = ["mean", "min", "max", "sum", "std", "count"]

        try:
            import geopandas as gpd

            if isinstance(vector_gdf_or_path, str):
                vector = gpd.read_file(vector_gdf_or_path)
            else:
                vector = vector_gdf_or_path

            try:
                import rasterstats as rs

                result = rs.zonal_stats(
                    vector, raster_path, stats=stats, geojson_out=True
                )
                return {"status": "success", "data": result}
            except ImportError:
                # Fallback 手动实现
                return self._zonal_stats_manual(raster_path, vector, stats)

        except Exception as e:
            logger.exception("zonal_statistics 失败: %s", e)
            return {"status": "error", "message": f"分区统计失败: {str(e)}"}

    def _zonal_stats_manual(
        self,
        raster_path: str,
        vector: Any,
        stats: list[str],
    ) -> dict:
        """手动分区统计 fallback（不使用 rasterstats）。

        对每个 geometry 执行 mask 裁剪并计算统计量。
        """
        try:
            import geopandas as gpd
        except ImportError:
            return {"status": "error", "message": "geopandas 不可用"}

        with rasterio.open(raster_path) as src:
            results = []
            for _, row in vector.iterrows():
                geom = row.geometry
                if geom is None or geom.is_empty:
                    results.append({s: None for s in stats})
                    continue
                try:
                    out_image, _ = mask(src, [geom], crop=True, nodata=src.nodata)
                    data = out_image[out_image != src.nodata] if src.nodata is not None else out_image.flatten()
                    data = data.astype(np.float64)
                    if data.size == 0:
                        results.append({s: None for s in stats})
                        continue
                    record = {}
                    for s in stats:
                        if s == "mean":
                            record[s] = float(np.mean(data))
                        elif s == "min":
                            record[s] = float(np.min(data))
                        elif s == "max":
                            record[s] = float(np.max(data))
                        elif s == "sum":
                            record[s] = float(np.sum(data))
                        elif s == "std":
                            record[s] = float(np.std(data))
                        elif s == "count":
                            record[s] = int(data.size)
                        else:
                            record[s] = None
                    results.append(record)
                except Exception:
                    results.append({s: None for s in stats})

        return {"status": "success", "data": results}

    # ------------------------------------------------------------------
    # 采样
    # ------------------------------------------------------------------

    def raster_sampling(
        self, raster_path: str, points_gdf: Any
    ) -> dict:
        """从栅格采样点值。

        Args:
            raster_path: 栅格文件路径。
            points_gdf: 点 GeoDataFrame。

        Returns:
            {"status": "success", "data": [{"x": ..., "y": ..., "value": ...}, ...]}
        """
        try:
            with rasterio.open(raster_path) as src:
                results = []
                for _, row in points_gdf.iterrows():
                    geom = row.geometry
                    if geom is None or geom.is_empty:
                        results.append({"x": None, "y": None, "value": None})
                        continue
                    try:
                        # 确保 CRS 一致
                        x, y = geom.x, geom.y
                        row_idx, col_idx = src.index(x, y)
                        value = src.read(1, window=((row_idx, row_idx + 1), (col_idx, col_idx + 1)))
                        results.append({
                            "x": x,
                            "y": y,
                            "value": float(value[0, 0]) if value.size > 0 else None,
                        })
                    except Exception:
                        results.append({"x": geom.x, "y": geom.y, "value": None})
            return {"status": "success", "data": results}
        except Exception as e:
            logger.exception("raster_sampling 失败: %s", e)
            return {"status": "error", "message": f"采样失败: {str(e)}"}

    # ------------------------------------------------------------------
    # 矢栅互转
    # ------------------------------------------------------------------

    def rasterize_vector(
        self,
        vector_gdf_or_path: Any,
        out_shape: tuple,
        transform: Any,
        crs: str,
        dst_path: Optional[str] = None,
    ) -> dict:
        """矢量转栅格。

        Args:
            vector_gdf_or_path: GeoDataFrame 或矢量文件路径。
            out_shape: (height, width)。
            transform: affine transform。
            crs: 目标 CRS。
            dst_path: 输出文件路径。

        Returns:
            {"status": "success", "data": {"dst_path": ..., "shape": ...}}
        """
        try:
            import geopandas as gpd

            if isinstance(vector_gdf_or_path, str):
                vector = gpd.read_file(vector_gdf_or_path)
            else:
                vector = vector_gdf_or_path

            shapes_iter = ((g, 1) for g in vector.geometry if g is not None and not g.is_empty)

            out_arr = rio_rasterize(
                shapes=shapes_iter,
                out_shape=out_shape,
                transform=transform,
                fill=0,
                dtype=np.uint8,
            )

            if dst_path is None:
                tmp = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
                dst_path = tmp.name
                tmp.close()

            profile = {
                "driver": "GTiff",
                "height": out_shape[0],
                "width": out_shape[1],
                "count": 1,
                "dtype": np.uint8,
                "crs": crs,
                "transform": transform,
            }
            with rasterio.open(dst_path, "w", **profile) as dst:
                dst.write(out_arr, 1)

            raster_layer = self._to_raster_layer(
                out_arr.astype(np.float32),
                transform,
                cmap_name="viridis",
                value_kind="rasterize",
            )
            data = dict(raster_layer)
            data["dst_path"] = dst_path
            data["shape"] = list(out_shape)
            return {"status": "success", "data": data}
        except Exception as e:
            logger.exception("rasterize_vector 失败: %s", e)
            return {"status": "error", "message": f"矢量转栅格失败: {str(e)}"}

    def polygonize_raster(self, raster_path: str, band: int = 1) -> dict:
        """栅格转矢量多边形。

        Args:
            raster_path: 栅格文件路径。
            band: 波段号（1-based）。

        Returns:
            {"status": "success", "data": GeoDataFrame}
        """
        try:
            import geopandas as gpd
            from shapely.geometry import shape

            with rasterio.open(raster_path) as src:
                arr = src.read(band)
                mask_arr = arr != src.nodata if src.nodata is not None else None
                results = rio_shapes(arr, mask=mask_arr, transform=src.transform)

                geoms = []
                values = []
                for geom_dict, val in results:
                    geoms.append(shape(geom_dict))
                    values.append(val)

            gdf = gpd.GeoDataFrame(
                {"value": values, "geometry": geoms},
                crs=str(src.crs),
            )
            return {"status": "success", "data": gdf}
        except Exception as e:
            logger.exception("polygonize_raster 失败: %s", e)
            return {"status": "error", "message": f"栅格转矢量失败: {str(e)}"}

    # ------------------------------------------------------------------
    # 地形分析
    # ------------------------------------------------------------------

    def slope(
        self, dem_path: str, dst_path: Optional[str] = None, degree: bool = True
    ) -> dict:
        """计算坡度。

        用 numpy gradient + arctan，先计算 x/y 方向梯度，再按像元分辨率换算。
        返回 RasterLayer 格式的 dict（含 PNG base64 + bbox），
        同时保留 `dst_path` 字段以便继续读 .tif。

        Args:
            dem_path: DEM 文件路径。
            dst_path: 输出文件路径。None 时写入临时文件。
            degree: True 返回角度，False 返回弧度。

        Returns:
            {"status": "success", "data": <RasterLayer dict>}，
            含 png_b64 / bbox / dst_path / unit(value_kind 仅前端用) 等。
        """
        try:
            arr, profile = self._read_single_band(dem_path)
            transform = profile["transform"]
            dy = abs(transform[4])
            dx = abs(transform[0])
            dzdy, dzdx = np.gradient(arr, dy, dx)

            # slope = arctan(sqrt(dzdx^2 + dzdy^2))
            slope_rad = np.arctan(np.sqrt(dzdx**2 + dzdy**2))

            if degree:
                slope_out = np.degrees(slope_rad)
                unit = "degree"
                value_kind = "slope_degrees"
            else:
                slope_out = slope_rad
                unit = "radian"
                value_kind = "slope_radians"

            # 兼容旧测试：始终产出 .tif
            if dst_path is None:
                dst_path = self._to_tempfile(slope_out.astype(np.float32), profile)
            else:
                out_profile = profile.copy()
                out_profile.update({"dtype": np.float32, "count": 1})
                with rasterio.open(dst_path, "w", **out_profile) as dst:
                    dst.write(slope_out.astype(np.float32), 1)

            raster_layer = self._to_raster_layer(
                slope_out.astype(np.float32),
                transform,
                cmap_name="terrain",
                value_kind=value_kind,
            )
            # 合并 legacy 字段，保持向后兼容
            data = dict(raster_layer)
            data["dst_path"] = dst_path
            data["unit"] = unit
            return {"status": "success", "data": data}
        except Exception as e:
            logger.exception("slope 失败: %s", e)
            return {"status": "error", "message": f"坡度计算失败: {str(e)}"}

    def aspect(self, dem_path: str, dst_path: Optional[str] = None) -> dict:
        """计算坡向。

        用 numpy gradient + arctan2，0=北(N)，顺时针增加。
        返回 RasterLayer 格式 dict（循环色相 colormap）。

        Args:
            dem_path: DEM 文件路径。
            dst_path: 输出文件路径。None 时写入临时文件。

        Returns:
            {"status": "success", "data": <RasterLayer dict>}
        """
        try:
            arr, profile = self._read_single_band(dem_path)
            transform = profile["transform"]
            dy = abs(transform[4])
            dx = abs(transform[0])
            dzdy, dzdx = np.gradient(arr, dy, dx)

            # aspect = arctan2(dzdy, -dzdx) → 地理方位：0=东，逆时针 → 转换为 0=北，顺时针
            aspect_rad = np.arctan2(dzdy, -dzdx)
            aspect_deg = np.degrees(aspect_rad)
            # 从数学角度（0=东，逆时针）转为地理方位（0=北，顺时针）
            # 方位(clockwise from N) = 90 - 数学角度
            aspect_geo = 90.0 - aspect_deg
            # 规范化到 [0, 360)
            aspect_geo = np.where(aspect_geo < 0, aspect_geo + 360.0, aspect_geo)
            # 平坦区域 aspect = -1（dzdx≈0 且 dzdy≈0）
            flat = (np.abs(dzdx) < 1e-10) & (np.abs(dzdy) < 1e-10)
            aspect_geo[flat] = -1.0

            if dst_path is None:
                dst_path = self._to_tempfile(aspect_geo.astype(np.float32), profile)
            else:
                out_profile = profile.copy()
                out_profile.update({"dtype": np.float32, "count": 1})
                with rasterio.open(dst_path, "w", **out_profile) as dst:
                    dst.write(aspect_geo.astype(np.float32), 1)

            raster_layer = self._to_raster_layer(
                aspect_geo.astype(np.float32),
                transform,
                cmap_name="aspect",
                value_kind="aspect_degrees",
            )
            data = dict(raster_layer)
            data["dst_path"] = dst_path
            return {"status": "success", "data": data}
        except Exception as e:
            logger.exception("aspect 失败: %s", e)
            return {"status": "error", "message": f"坡向计算失败: {str(e)}"}

    def hillshade(
        self,
        dem_path: str,
        azimuth: float = 315,
        altitude: float = 45,
        dst_path: Optional[str] = None,
    ) -> dict:
        """计算山体阴影。

        返回 RasterLayer 格式 dict（灰度）。

        Args:
            dem_path: DEM 文件路径。
            azimuth: 光源方位角（度，0=北，顺时针）。
            altitude: 光源高度角（度，0=地平线，90=天顶）。
            dst_path: 输出文件路径。None 时写入临时文件。

        Returns:
            {"status": "success", "data": <RasterLayer dict>}
        """
        try:
            arr, profile = self._read_single_band(dem_path)
            transform = profile["transform"]
            dy = abs(transform[4])
            dx = abs(transform[0])
            dzdy, dzdx = np.gradient(arr, dy, dx)

            az_rad = np.radians(360.0 - azimuth + 90.0)  # 转为数学角
            alt_rad = np.radians(altitude)

            slope_rad = np.arctan(np.sqrt(dzdx**2 + dzdy**2))
            aspect_rad = np.arctan2(dzdy, -dzdx)
            # 从数学角度（0=东，逆时针）转为地理方位弧度（0=北，顺时针）
            aspect_geo_rad = np.pi / 2 - aspect_rad
            aspect_geo_rad = np.where(aspect_geo_rad < 0, aspect_geo_rad + 2 * np.pi, aspect_geo_rad)

            hs = (
                np.cos(alt_rad) * np.cos(slope_rad)
                + np.sin(alt_rad) * np.sin(slope_rad) * np.cos(az_rad - aspect_geo_rad)
            )
            # 限制到 [0, 255]
            hs = np.clip(hs * 255, 0, 255).astype(np.uint8)

            if dst_path is None:
                dst_path = self._to_tempfile(hs, profile)
            else:
                out_profile = self._output_profile(hs, profile)
                with rasterio.open(dst_path, "w", **out_profile) as dst:
                    dst.write(hs, 1)

            raster_layer = self._to_raster_layer(
                hs.astype(np.float32),
                transform,
                cmap_name="grayscale",
                value_kind="hillshade",
            )
            data = dict(raster_layer)
            data["dst_path"] = dst_path
            return {"status": "success", "data": data}
        except Exception as e:
            logger.exception("hillshade 失败: %s", e)
            return {"status": "error", "message": f"山体阴影计算失败: {str(e)}"}

    # ------------------------------------------------------------------
    # 等高线
    # ------------------------------------------------------------------

    def contour(
        self,
        dem_path: str,
        interval: Optional[float] = None,
        dst_path: Optional[str] = None,
    ) -> dict:
        """生成等高线。

        用 matplotlib contour → shapely LineString。

        Args:
            dem_path: DEM 文件路径。
            interval: 等高距，None 时自动估算。
            dst_path: 输出 shapefile 路径。

        Returns:
            {"status": "success", "data": GeoDataFrame}
        """
        try:
            # 服务端和 CI 没有桌面事件循环；在导入 pyplot 前固定无头后端，
            # 避免 Windows 默认 TkAgg 从工作线程创建 GUI 而崩溃。
            import matplotlib
            matplotlib.use("Agg", force=True)
            import matplotlib.pyplot as plt
            from shapely.geometry import LineString, MultiLineString
            import geopandas as gpd

            arr, profile = self._read_single_band(dem_path)

            if interval is None:
                # 自动估算等高距
                data_range = float(np.nanmax(arr) - np.nanmin(arr))
                interval = max(data_range / 20, 1.0)

            levels = np.arange(
                np.floor(np.nanmin(arr) / interval) * interval,
                np.ceil(np.nanmax(arr) / interval) * interval + interval,
                interval,
            )

            fig, ax = plt.subplots()
            contour_set = ax.contour(arr, levels=levels)
            plt.close(fig)

            transform = profile["transform"]
            lines = []
            elevations = []
            for level_idx, seg_set in enumerate(contour_set.allsegs):
                elev = levels[level_idx]
                for seg in seg_set:
                    if len(seg) < 2:
                        continue
                    # 从像素坐标转 CRS 坐标
                    coords = [transform * (x, y) for x, y in seg]
                    line = LineString(coords)
                    if not line.is_empty:
                        lines.append(line)
                        elevations.append(float(elev))

            gdf = gpd.GeoDataFrame(
                {"elevation": elevations, "geometry": lines},
                crs=str(profile.get("crs", "")),
            )

            if dst_path is not None:
                gdf.to_file(dst_path)

            return {"status": "success", "data": gdf}
        except Exception as e:
            logger.exception("contour 失败: %s", e)
            return {"status": "error", "message": f"等高线生成失败: {str(e)}"}

    # ------------------------------------------------------------------
    # 重分类
    # ------------------------------------------------------------------

    def reclassify_raster(
        self,
        src_path: str,
        bins: list,
        values: list,
        dst_path: Optional[str] = None,
    ) -> dict:
        """重分类栅格。

        bins 是分界点列表，values 是新值列表。
        若 len(values) == len(bins) + 1：区间 [bins[i], bins[i+1]) → values[i]，
          两侧外延到 values[0] 和 values[-1]。
        若 len(values) == len(bins)：逐值替换，bins[i] → values[i]，其余保持原值。

        Args:
            src_path: 源栅格文件路径。
            bins: 分界点列表。
            values: 新值列表。
            dst_path: 输出文件路径。

        Returns:
            {"status": "success", "data": {"dst_path": ...}}
        """
        try:
            arr, profile = self._read_single_band(src_path)
            result = np.full_like(arr, np.nan, dtype=np.float32)

            if len(values) == len(bins) + 1:
                # 区间重分类
                result[arr < bins[0]] = values[0]
                for i in range(len(bins) - 1):
                    mask = (arr >= bins[i]) & (arr < bins[i + 1])
                    result[mask] = values[i + 1]
                result[arr >= bins[-1]] = values[-1]
            elif len(values) == len(bins):
                # 逐值替换
                result = arr.astype(np.float32).copy()
                for src_val, dst_val in zip(bins, values):
                    result[arr == src_val] = dst_val
            else:
                return {
                    "status": "error",
                    "message": (
                        f"bins/values 长度不匹配: "
                        f"bins={len(bins)}, values={len(values)}，"
                        f"期望 values 长度 = bins 长度 或 bins 长度 + 1"
                    ),
                }

            if dst_path is None:
                dst_path = self._to_tempfile(result, profile)
            else:
                out_profile = profile.copy()
                out_profile.update({"dtype": np.float32, "count": 1})
                with rasterio.open(dst_path, "w", **out_profile) as dst:
                    dst.write(result, 1)

            raster_layer = self._to_raster_layer(
                result,
                profile["transform"],
                cmap_name="viridis",
                value_kind="reclassified",
            )
            data = dict(raster_layer)
            data["dst_path"] = dst_path
            return {"status": "success", "data": data}
        except Exception as e:
            logger.exception("reclassify_raster 失败: %s", e)
            return {"status": "error", "message": f"重分类失败: {str(e)}"}

    # ------------------------------------------------------------------
    # 地形指标
    # ------------------------------------------------------------------

    def terrain_ruggedness_index(self, dem_path: str) -> dict:
        """地形崎岖指数 TRI。

        3x3 窗口内中心像元与邻域差值的均方根。
        TRI = sqrt( sum_j (z_center - z_j)^2 )

        返回 RasterLayer dict（含 PNG base64 + bbox）。

        Args:
            dem_path: DEM 文件路径。

        Returns:
            {"status": "success", "data": <RasterLayer dict>}
        """
        try:
            arr, profile = self._read_single_band(dem_path)

            def _tri_fn(window: np.ndarray) -> float:
                center = window[1, 1]
                diffs = np.array([
                    window[0, 0], window[0, 1], window[0, 2],
                    window[1, 0], window[1, 2],
                    window[2, 0], window[2, 1], window[2, 2],
                ])
                return float(np.sqrt(np.mean((center - diffs) ** 2)))

            result = self._focal_window(arr.astype(np.float32), 1, _tri_fn)
            raster_layer = self._to_raster_layer(
                result,
                profile["transform"],
                cmap_name="terrain",
                value_kind="tri",
            )
            return {"status": "success", "data": raster_layer}
        except Exception as e:
            logger.exception("terrain_ruggedness_index 失败: %s", e)
            return {"status": "error", "message": f"TRI 计算失败: {str(e)}"}

    def topographic_position_index(
        self, dem_path: str, radius: int = 3
    ) -> dict:
        """地形位置指数 TPI。

        TPI = 中心像元值 - 邻域均值。

        返回 RasterLayer dict（含 PNG base64 + bbox）。

        Args:
            dem_path: DEM 文件路径。
            radius: 邻域半径（像元数）。

        Returns:
            {"status": "success", "data": <RasterLayer dict>}
        """
        try:
            arr, profile = self._read_single_band(dem_path)

            def _tpi_fn(window: np.ndarray) -> float:
                center = window[window.shape[0] // 2, window.shape[1] // 2]
                neigh_mean = np.mean(window)
                return float(center - neigh_mean)

            result = self._focal_window(arr.astype(np.float32), radius, _tpi_fn)
            raster_layer = self._to_raster_layer(
                result,
                profile["transform"],
                cmap_name="terrain",
                value_kind="tpi",
            )
            return {"status": "success", "data": raster_layer}
        except Exception as e:
            logger.exception("topographic_position_index 失败: %s", e)
            return {"status": "error", "message": f"TPI 计算失败: {str(e)}"}

    def roughness(self, dem_path: str) -> dict:
        """地表粗糙度。

        3x3 窗口内最大值减最小值。

        返回 RasterLayer dict（含 PNG base64 + bbox）。

        Args:
            dem_path: DEM 文件路径。

        Returns:
            {"status": "success", "data": <RasterLayer dict>}
        """
        try:
            arr, profile = self._read_single_band(dem_path)

            def _roughness_fn(window: np.ndarray) -> float:
                return float(np.max(window) - np.min(window))

            result = self._focal_window(arr.astype(np.float32), 1, _roughness_fn)
            raster_layer = self._to_raster_layer(
                result,
                profile["transform"],
                cmap_name="terrain",
                value_kind="roughness",
            )
            return {"status": "success", "data": raster_layer}
        except Exception as e:
            logger.exception("roughness 失败: %s", e)
            return {"status": "error", "message": f"粗糙度计算失败: {str(e)}"}
