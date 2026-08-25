"""POI 多源融合查询工具。

实现参考 GIS_Agent_技术文档.md §4.3 + docs/02_data_models.md §3.1。

核心设计：
1. **双源策略**：高德为主（全量、结构化、GCJ02），OSM 补漏（开源、无配额、WGS84）
2. **Fallback 封装在 Tool 内部**：不让 LLM 决定"高德查不到再查 OSM"，
   避免额外 React Loop 轮次
3. **TimeoutError / ConnectionError 降级为 Empty Result**：对 LLM 来说，
   "查不到"和"查超时"都是没数据，Tool 内部 catch 后统一返回 status="empty"
4. **3 秒硬超时**：OSM Overpass 响应慢，超时直接返回
5. **国内数据统一 GCJ02**：OSM 的 WGS84 用 geo_transform.wgs84_to_gcj02 转换
6. **国外数据保持 WGS84**：高德海外底图自动切换为无偏移 WGS84
7. **R-Tree 去重**：用 rtree 库建空间索引，非暴力两两比较；
   前提是 results_a / results_b 已统一坐标系
"""

import logging
import math
from typing import Any

import requests

from app.tools.geo_transform import (
    is_china_bbox,
    wgs84_to_gcj02,
    haversine_m,
)

logger = logging.getLogger(__name__)

# 高德周边搜索 endpoint
_AMAP_PLACE_AROUND_URL = "https://restapi.amap.com/v3/place/around"
# OSM Overpass endpoint（默认主，可在 config 中配置备份）
_OSM_ENDPOINT = "https://overpass-api.de/api/interpreter"

# 去重默认阈值（米）：小于此距离视为同一 POI
_DEDUP_DEFAULT_THRESHOLD_M = 50


class POIQuery:
    """POI 多源融合查询。

    对 Agent 层屏蔽数据源差异：调用方只管传 query/location/radius，
    Tool 内部决定走高德还是 OSM、是否兜底、坐标系如何统一。
    """

    def __init__(
        self,
        amap_key: str,
        amap_timeout: int = 3,
        osm_timeout: int = 3,
        osm_endpoint: str = _OSM_ENDPOINT,
        osm_backup_endpoints: str | list[str] | tuple[str, ...] = (),
        dedup_threshold_m: float = _DEDUP_DEFAULT_THRESHOLD_M,
        within_source_dedup: bool = True,
    ):
        self.amap_key = amap_key
        self.amap_timeout = amap_timeout
        self.osm_timeout = osm_timeout
        self.osm_endpoint = osm_endpoint
        if isinstance(osm_backup_endpoints, str):
            backup_candidates = osm_backup_endpoints.split(",")
        else:
            backup_candidates = list(osm_backup_endpoints)
        self.osm_backup_endpoints = tuple(
            endpoint.strip()
            for endpoint in backup_candidates
            if endpoint and endpoint.strip() and endpoint.strip() != osm_endpoint
        )
        # 跨/同源去重距离阈值（米），小于该距离视为同一 POI。
        # 同商场内多家连锁店可能在高德返回里出现多条，用此阈值合并。
        self.dedup_threshold_m = dedup_threshold_m
        # 是否对单数据源内部也去重（按名称 + 距离）。关闭则只做跨源去重。
        self.within_source_dedup = within_source_dedup

    # ------------------------------------------------------------------
    # 数据源查询
    # ------------------------------------------------------------------

    def query_amap(self, keyword: str, location: tuple, radius: int) -> list[dict]:
        """高德周边搜索。

        Args:
            keyword: 搜索关键词，如 "蜜雪冰城"
            location: GCJ02 (lng, lat)
            radius: 搜索半径（米）

        Returns:
            POI 列表（GCJ02 坐标系），字段对齐 POI 模型：
            name / address / tel / location / crs / source / category / poi_id / distance
            空结果返回 []。

        Raises:
            requests.Timeout / requests.ConnectionError: 由上层 search_poi_tool 捕获
        """
        lng, lat = location
        params = {
            "key": self.amap_key,
            "keywords": keyword,
            "location": f"{lng},{lat}",
            "radius": radius,
            "offset": 20,  # 每页 20 条
            "page": 1,
            "extensions": "all",
            "output": "json",
        }
        resp = requests.get(
            _AMAP_PLACE_AROUND_URL,
            params=params,
            timeout=self.amap_timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        # 高德 status="1" 表示成功
        if data.get("status") != "1":
            logger.warning("amap query failed: %s", data.get("info", "unknown"))
            return []

        pois_raw = data.get("pois") or []
        results: list[dict] = []
        for p in pois_raw:
            location_str = p.get("location", "")
            try:
                p_lng, p_lat = [float(x) for x in location_str.split(",")]
            except (ValueError, AttributeError):
                # location 字段异常，跳过该 POI
                logger.debug("amap poi invalid location: %r", location_str)
                continue

            poi = {
                "name": p.get("name", ""),
                "address": p.get("address") or None,
                "tel": p.get("tel") or None,
                "location": (p_lng, p_lat),  # GCJ02
                "crs": "GCJ02",
                "source": "Amap",
                "category": p.get("typecode") or None,
                "poi_id": p.get("id") or None,
                "distance": self._safe_float(p.get("distance")),
            }
            results.append(poi)
        return results

    @staticmethod
    def _sanitize_overpass_tag(tag: str) -> str:
        """转义 Overpass QL 中的特殊字符，防止注入。

        Overpass QL 字符串用双引号包裹，需要转义 " 和 \\ 字符。
        """
        return tag.replace("\\", "\\\\").replace('"', '\\"')

    def query_osm(self, tag: str, bbox: tuple) -> list[dict]:
        """OSM Overpass QL 查询。

        Args:
            tag: 搜索关键词（映射到 name 正则）
            bbox: (minLng, minLat, maxLng, maxLat) WGS84

        Returns:
            POI 列表（WGS84 坐标系），字段对齐 POI 模型。
            source 标记为 "OSM"（search_poi_tool 会根据国内/国外改写为 OSM_CN / OSM_Global）。

        Raises:
            requests.Timeout / requests.ConnectionError: 由上层捕获
        """
        min_lng, min_lat, max_lng, max_lat = bbox
        # 转义用户输入，防止 Overpass QL 注入
        safe_tag = self._sanitize_overpass_tag(tag)
        # Overpass QL：bbox 顺序为 (south, west, north, east) = (minLat, minLng, maxLat, maxLng)
        query = (
            f'[out:json][timeout:{self.osm_timeout}];\n'
            f'(\n'
            f'  node["name"~"{safe_tag}",i]({min_lat},{min_lng},{max_lat},{max_lng});\n'
            f'  way["name"~"{safe_tag}",i]({min_lat},{min_lng},{max_lat},{max_lng});\n'
            f');\n'
            f'out center 20;\n'
        )
        # Public Overpass instances regularly rate-limit or temporarily reject
        # requests.  Try the configured backup endpoints before declaring the
        # provider unavailable; an empty *valid* response is still authoritative
        # and therefore does not fan out to another endpoint.
        data: dict | None = None
        last_error: requests.RequestException | ValueError | None = None
        for endpoint in (self.osm_endpoint, *self.osm_backup_endpoints):
            try:
                resp = requests.get(
                    endpoint,
                    params={"data": query},
                    timeout=self.osm_timeout,
                    headers={"User-Agent": "Gismind/1.6 (+local GIS agent)", "Accept": "application/json"},
                )
                resp.raise_for_status()
                candidate = resp.json()
                if not isinstance(candidate, dict):
                    raise ValueError("Overpass response is not a JSON object")
                data = candidate
                break
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                logger.info("overpass endpoint failed (%s): %s", endpoint, exc)

        if data is None:
            if last_error is not None:
                raise last_error
            raise requests.ConnectionError("no Overpass endpoint configured")

        elements = data.get("elements") or []
        results: list[dict] = []
        for el in elements:
            # node 有 lat/lon；way 用 center.lat/center.lon
            if el.get("type") == "node":
                lat = el.get("lat")
                lng = el.get("lon")
            elif el.get("type") == "way":
                center = el.get("center") or {}
                lat = center.get("lat")
                lng = center.get("lon")
            else:
                continue

            if lat is None or lng is None:
                continue

            tags = el.get("tags") or {}
            name = tags.get("name") or tags.get("name:en") or ""
            if not name:
                continue

            poi = {
                "name": name,
                "address": self._osm_tags_to_address(tags),
                "tel": tags.get("phone") or tags.get("contact:phone") or None,
                "location": (float(lng), float(lat)),  # WGS84
                "crs": "WGS84",
                "source": "OSM",
                "category": self._osm_category(tags),
                "poi_id": str(el.get("id")) if el.get("id") is not None else None,
                "distance": None,
            }
            results.append(poi)
        return results

    # ------------------------------------------------------------------
    # 统一入口（对 Agent 暴露）
    # ------------------------------------------------------------------

    def search_poi_tool(
        self,
        query: str,
        location: tuple,
        radius: int,
        dedup_threshold_m: float | None = None,
        within_source_dedup: bool | None = None,
    ) -> dict:
        """统一 POI 搜索，对 Agent 屏蔽数据源差异。

        - 优先高德（GCJ02）
        - 高德空/超时/断连 -> OSM 兜底（3 秒硬超时）
        - 国内 OSM 数据转 GCJ02
        - 国外数据保持 WGS84
        - 返回 {"status": "success"|"empty", "data": {...}, "source": ...}

        Args:
            query: 搜索关键词
            location: (lng, lat)，国内为 GCJ02，国外为 WGS84
            radius: 搜索半径（米）
            dedup_threshold_m: 同/跨源去重距离阈值（米），None 时沿用构造时的值。
                蜜雪冰城这种连锁可调高（如 100/150），避免同栋楼多家被合并成 1。
            within_source_dedup: 是否对单数据源内部按"同名 + 距离 < 阈值"做去重。
                关闭后可保留所有原始记录，但跨源仍去重。

        Returns:
            dict: status=success 时含 data.pois 和 source；
                  status=empty 时 data 为空，对 LLM 来说"查不到"和"查超时"无差别
        """
        threshold = self.dedup_threshold_m if dedup_threshold_m is None else dedup_threshold_m
        within = self.within_source_dedup if within_source_dedup is None else within_source_dedup

        # 1. 优先高德
        try:
            amap_results = self.query_amap(query, location, radius)
            if within and amap_results:
                amap_results = self._dedup_within(amap_results, threshold)
            if amap_results:
                return self._format_results(amap_results, source="Amap")
        except (requests.Timeout, requests.ConnectionError) as e:
            # 高德超时/断连 -> 对 LLM 来说就是"没数据"，继续走 OSM 兜底
            logger.info("amap fallback to osm: %s", e)
        except Exception as e:  # noqa: BLE001
            # 其他意外异常也降级，避免 Tool 抛错打断 React Loop
            logger.warning("amap unexpected error, fallback to osm: %s", e)

        # 2. OSM 兜底
        bbox = self._radius_to_bbox(location, radius)
        try:
            osm_results = self.query_osm(query, bbox)
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info("osm also failed: %s", e)
            return {"status": "empty", "data": None, "source": None,
                    "message": "POI 数据源暂时不可用"}
        except Exception as e:  # noqa: BLE001
            logger.warning("osm unexpected error: %s", e)
            return {"status": "empty", "data": None, "source": None,
                    "message": "POI 查询失败"}

        if not osm_results:
            return {"status": "empty", "data": None, "source": None,
                    "message": "未找到相关 POI"}

        # 3. 根据国内/国外决定坐标系
        if self._is_china_bbox(bbox):
            # 国内：WGS84 -> GCJ02（适配高德底图）
            for p in osm_results:
                lng, lat = p["location"]
                gcj_lng, gcj_lat = wgs84_to_gcj02(lng, lat)
                p["location"] = (gcj_lng, gcj_lat)
                p["crs"] = "GCJ02"
                p["source"] = "OSM_CN"
            if within:
                osm_results = self._dedup_within(osm_results, threshold)
            return self._format_results(osm_results, source="OSM_CN")
        else:
            # 国外：保持 WGS84
            for p in osm_results:
                p["source"] = "OSM_Global"
                p["crs"] = "WGS84"
            if within:
                osm_results = self._dedup_within(osm_results, threshold)
            return self._format_results(osm_results, source="OSM_Global")

    # ------------------------------------------------------------------
    # 去重
    # ------------------------------------------------------------------

    def _dedup_within(self, results: list[dict], threshold: float) -> list[dict]:
        """同源内部去重 — 按名称归一化 + 距离阈值。

        高德常对同一连锁店多次返回（不同 poi_id 但坐标极近），
        加上"蜜雪冰城（新街口地铁站店）""蜜雪冰城（新街口店）"等 name 微差，
        这里用"名称归一化 + Haversine < threshold"判同一实体。
        """
        if len(results) <= 1:
            return results

        def norm(n: str | None) -> str:
            if not n:
                return ""
            s = n.strip().lower()
            # 去括号备注、空格差异
            s = s.replace(" ", "").replace("（", "(").replace("）", ")")
            # 去掉常见商业关键词便于跨店匹配。
            # "店" / "分店" / "加盟店" 触发切分（去掉后缀变体）；
            # "no." / "号" 仅做字符串检测但极少出现在中文 POI 名称中，暂不切分。
            for kw in ("店", "分店", "加盟店", "no.", "号"):
                s = s.split(kw)[0] if kw in s and kw in ("店", "分店", "加盟店") else s
            return s

        kept: list[dict] = []
        for p in results:
            pname = norm(p.get("name"))
            dup_idx = None
            for i, k in enumerate(kept):
                kname = norm(k.get("name"))
                if not kname or not pname:
                    continue
                # 名称相同或高度相关 + 距离 < threshold 视为同一 POI
                if kname == pname or (pname in kname) or (kname in pname):
                    d = haversine_m(k["location"], p["location"])
                    if d < threshold:
                        dup_idx = i
                        break
            if dup_idx is not None:
                # 保留字段更全的（同名）
                if self._info_richness(p) > self._info_richness(kept[dup_idx]):
                    kept[dup_idx] = p
            else:
                kept.append(p)
        return kept

    def _deduplicate(
        self,
        results_a: list[dict],
        results_b: list[dict],
        threshold: float = _DEDUP_DEFAULT_THRESHOLD_M,
    ) -> list[dict]:
        """R-Tree 空间索引去重。

        前提：results_a 和 results_b 必须已统一坐标系（同为 GCJ02 或同为 WGS84）。
        距离小于 threshold 米的视为同一 POI，保留信息更全的（非空字段更多）。

        Args:
            results_a: 已统一坐标系的 POI 列表
            results_b: 已统一坐标系的 POI 列表
            threshold: 去重距离阈值（米），默认 50m

        Returns:
            去重后的合并列表
        """
        if not results_a and not results_b:
            return []
        if not results_a:
            return list(results_b)
        if not results_b:
            return list(results_a)

        # 尝试使用 rtree 建空间索引；不可用则降级为暴力比较
        try:
            from rtree import index as rtree_index
            use_rtree = True
        except ImportError:  # pragma: no cover - rtree 已确认可用
            use_rtree = False

        # 合并：a 全部保留，b 中与 a 任一点距离 < threshold 的丢弃
        merged = list(results_a)

        if use_rtree:
            idx = rtree_index.Index()
            for i, p in enumerate(merged):
                lng, lat = p["location"]
                # 给每个点一个极小的 bbox（点查询），插入索引
                idx.insert(i, (lng, lat, lng, lat))

            for b_poi in results_b:
                lng, lat = b_poi["location"]
                # 查询 threshold 米范围内的候选点。
                # threshold 米近似经纬度容差（粗略，再用 haversine 精确判断）
                lat_tol, lon_tol = self._meters_to_deg_tol(lat, threshold)
                candidates = list(idx.intersection((lng - lon_tol, lat - lat_tol,
                                                     lng + lon_tol, lat + lat_tol)))
                is_dup = False
                for ci in candidates:
                    a_poi = merged[ci]
                    d = haversine_m(a_poi["location"], b_poi["location"])
                    if d < threshold:
                        # 重复：比较信息完整度，保留更全的
                        if self._info_richness(b_poi) > self._info_richness(a_poi):
                            merged[ci] = b_poi
                        is_dup = True
                        break
                if not is_dup:
                    merged.append(b_poi)
                    idx.insert(len(merged) - 1,
                               (lng, lat, lng, lat))
        else:
            # 暴力 O(n*m) 降级方案（rtree 不可用时）
            # 保留接口一致，仅性能差异
            for b_poi in results_b:
                is_dup = False
                for i, a_poi in enumerate(merged):
                    d = haversine_m(a_poi["location"], b_poi["location"])
                    if d < threshold:
                        if self._info_richness(b_poi) > self._info_richness(a_poi):
                            merged[i] = b_poi
                        is_dup = True
                        break
                if not is_dup:
                    merged.append(b_poi)

        return merged

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _radius_to_bbox(self, location: tuple, radius: int) -> tuple:
        """将中心点 + 半径转为 bbox。

        Args:
            location: (lng, lat)
            radius: 半径（米）

        Returns:
            (minLng, minLat, maxLng, maxLat)
        """
        lng, lat = location
        # 1 米对应的纬度度数（近似，地球周长约 40075km）
        meter_to_deg_lat = 1.0 / 111_320.0
        # 经度度数随纬度变化（cos 修正）
        import math
        meter_to_deg_lng = meter_to_deg_lat / max(math.cos(math.radians(lat)), 1e-6)

        dlat = radius * meter_to_deg_lat
        dlng = radius * meter_to_deg_lng
        return (lng - dlng, lat - dlat, lng + dlng, lat + dlat)

    def _format_results(self, results: list, source: str) -> dict:
        """格式化返回结果。"""
        return {
            "status": "success",
            "source": source,
            "data": {"pois": results},
        }

    def _is_china_bbox(self, bbox: tuple) -> bool:
        """复用 geo_transform.is_china_bbox 判断 bbox 是否在国内。"""
        return is_china_bbox(bbox)

    # --- 小工具 ---

    @staticmethod
    def _safe_float(v: Any) -> float | None:
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _osm_tags_to_address(tags: dict) -> str | None:
        """从 OSM tags 拼装标准中文地址（省/市/区/街道/门牌号）。"""
        parts = []
        for key in ("addr:province", "addr:city", "addr:district",
                    "addr:street", "addr:housenumber"):
            v = tags.get(key)
            if v:
                parts.append(v)
        if not parts:
            return None
        return ", ".join(parts)

    @staticmethod
    def _osm_category(tags: dict) -> str | None:
        """从 OSM tags 提取类别（取第一个有意义的分类 tag）。"""
        for key in ("amenity", "shop", "tourism", "office", "leisure", "cuisine"):
            v = tags.get(key)
            if v:
                return f"{key}={v}"
        return None

    @staticmethod
    def _info_richness(poi: dict) -> int:
        """POI 信息完整度评分：非空字段越多分越高。用于去重时保留更全的。"""
        score = 0
        for key in ("name", "address", "tel", "category", "poi_id", "distance"):
            v = poi.get(key)
            if v is not None and v != "":
                score += 1
        # 高德数据天然更结构化，同等字段数时优先高德
        if poi.get("source") == "Amap":
            score += 1
        return score

    @staticmethod
    def _meters_to_deg_tol(lat: float, meters: float) -> tuple[float, float]:
        """将米转为经纬度容差（用于 R-Tree 查询窗口）。

        纬度方向：1 度 ≈ 111320 米。
        经度方向：用 cos(lat) 修正，高纬度地区经线收窄。
        返回 (lat_tol, lon_tol)。
        """
        lat_radians = math.radians(lat)
        lat_tol = meters / 111_320.0
        lon_tol = meters / (111_320.0 * max(math.cos(lat_radians), 1e-6))
        return lat_tol, lon_tol
