"""地理编码工具：地名↔坐标，带 Redis 缓存。

参考 docs/02_data_models.md §5（cache:geocode:{hash}）+ 原文档 §3.1 功能 4。
高德地理编码 API 返回 GCJ02 坐标，无需转换。

Sprint 1 增量（坐标系原点漂移修复）：
- 返回值增加 candidates / confidence / disambiguated 三个字段，让上层 Agent
  可基于候选列表反问或切换主点，而不是静默硬取第一条。
- confidence 启发式 0.0–1.0：基于 location_type 优先级 + 多结果折扣。
- disambiguated=True 当 top-2 candidate 的 confidence 差 < 0.15。
- 缓存 schema：原 key 不变，写入完整 candidates 列表 + principal_rank。
  旧 v0 schema 缓存值（无 candidates 字段）反序列化时降级 — 仅保留主点。
"""

import hashlib
import json
from typing import Optional

import httpx

from app.config import settings
from app.utils.redis import get_redis, make_key
from app.tools.geo_transform import haversine_m, wgs84_to_gcj02

GEOCODE_ENDPOINT = "https://restapi.amap.com/v3/geocode/geo"
REVERSE_ENDPOINT = "https://restapi.amap.com/v3/geocode/regeo"

# OSM Nominatim 兜底（與 poi_query.py 的高德→OSM 雙層策略一致）
_NOMINATIM_ENDPOINT = "https://nominatim.openstreetmap.org/search"
_NOMINATIM_HEADERS = {
    "User-Agent": "GismindGIS/1.0 (GIS Agent; geocoding fallback; contact@example.com)",
}

# 候选数量上限；高德通常 1-3 条，少数情况 5+
DEFAULT_TOP_N = 3

# 各 location_type 的基础置信度。
# 优先级：POI/门牌 > 地铁站/公交 > 道路 > 行政区 > 全国地名（同名风险最高）
_LOCATION_TYPE_BASE_CONF = {
    "POI": 1.0,            # 兴趣点（最具体）
    "门牌号": 0.95,
    "地铁站": 0.9,
    "公交站": 0.85,
    "道路": 0.7,
    "商圈": 0.65,
    "行政区": 0.5,
    "地名": 0.45,          # 同名多发，置信度低
    "其他": 0.6,
}


def _location_type_conf(t: str) -> float:
    return _LOCATION_TYPE_BASE_CONF.get(t, 0.6)


def _cache_key(text: str) -> str:
    return make_key("cache:geocode", hashlib.md5(text.encode("utf-8")).hexdigest())


def _normalize_geocode_entry(g: dict, rank: int, principal_loc: tuple) -> dict:
    """把高德原始 geocode 条目归一化为 candidate 结构。"""
    loc_str = g.get("location", "")
    if "," in loc_str:
        try:
            lng, lat = (float(x) for x in loc_str.split(",", 1))
        except (TypeError, ValueError):
            lng, lat = 0.0, 0.0
    else:
        lng, lat = 0.0, 0.0
    loc_type = g.get("location_type") or g.get("type") or "其他"
    return {
        "rank": rank,
        "location": [lng, lat],
        "formatted_address": g.get("formatted_address", ""),
        "location_type": loc_type,
        "distance_to_principal": haversine_m(principal_loc, (lng, lat)),
    }


def _score_confidence(candidates: list[dict], top_loc_type: str) -> float:
    """主点置信度启发式：location_type 优先级 × 单结果折扣。"""
    base = _location_type_conf(top_loc_type)
    if len(candidates) >= 2:
        # 多结果本身意味着搜索不唯一；按候选数衰减，但不低于 0.3
        base = max(0.3, base - 0.15 * (len(candidates) - 1))
    return round(min(1.0, base), 3)


def _should_disambiguate(candidates: list[dict]) -> bool:
    """top-2 candidate 的 confidence 差 < 0.15 → 需要 LLM 反问确认。"""
    if len(candidates) < 2:
        return False
    c0 = _location_type_conf(candidates[0]["location_type"])
    c1 = _location_type_conf(candidates[1]["location_type"])
    return abs(c0 - c1) < 0.15


class GeoCoder:
    """地理编码，高德 API + Redis 缓存。"""

    def __init__(self, amap_key: Optional[str] = None, timeout: int = 3):
        self.amap_key = amap_key or settings.AMAP_KEY
        self.timeout = timeout

    async def geocode(self, address: str, principal_rank: int = 0, top_n: int = DEFAULT_TOP_N) -> dict:
        """地名 → GCJ02 坐标。先查 Redis，再打高德。

        Args:
            address: 用户输入的地址 / 地名。
            principal_rank: 在已缓存的 candidates 里指定哪个作为主点。
                历史上下文已选过的情况下传入选中的 rank，未知传 0（第一条）。
            top_n: 候选数量上限（默认 3）。

        Returns:
            {
              "status": "success" | "empty",
              "location": [lng, lat],        # 主坐标，向后兼容
              "formatted_address": str,
              "source": "Amap" | "Redis",
              "candidates": [                 # 新增：top-N 候选
                {"rank": 0, "location": [...], "formatted_address": "...",
                 "location_type": "...", "distance_to_principal": 0},
                ...
              ],
              "confidence": 0.0–1.0,           # 新增：主点置信度
              "disambiguated": bool,           # 新增：是否需要 LLM 主动反问
              "cached": bool,
            }
        """
        if not address or not address.strip():
            return {"status": "empty", "message": "地址为空"}

        r = get_redis()
        key = _cache_key(address)
        cached = await r.get(key)
        if cached:
            try:
                data = json.loads(cached)
            except json.JSONDecodeError:
                data = None
            if data and isinstance(data.get("candidates"), list) and data["candidates"]:
                # 旧 schema 兼容：若 principal_rank 越界，落回 0
                if principal_rank < 0 or principal_rank >= len(data["candidates"]):
                    principal_rank = 0
                cand = data["candidates"][principal_rank]
                data["location"] = cand["location"]
                data["formatted_address"] = cand["formatted_address"]
                data["source"] = "Redis"
                data["cached"] = True
                data["principal_rank"] = principal_rank
                # 重新算 confidence / disambiguated
                data["confidence"] = _score_confidence(data["candidates"], cand["location_type"])
                data["disambiguated"] = _should_disambiguate(data["candidates"])
                return data

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(GEOCODE_ENDPOINT, params={
                    "key": self.amap_key,
                    "address": address,
                })
                resp.raise_for_status()
                body = resp.json()
        except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPStatusError):
            return {"status": "empty", "message": "地理编码服务暂不可用"}

        if body.get("status") != "1":
            # 区分服务端错误（key 无效/限频） vs 其他异常
            infocode = body.get("infocode", "")
            if infocode in ("10001", "10002", "10003", "10004", "10005"):
                # key 无效/过期、无权限、日配额超限、访问频率过高、IP 白名单错误
                return {
                    "status": "error",
                    "message": f"高德地理编码服务异常: {body.get('info', 'unknown error')}",
                }
            # 其他服务端返回非成功状态 → OSM Nominatim 兜底
            fallback = await self._fallback_nominatim(address)
            if fallback:
                await r.set(key, json.dumps(fallback, ensure_ascii=False),
                            ex=settings.CACHE_TTL_GEOCODE)
                return fallback
            return {"status": "empty", "message": f"未找到 {address} 的坐标"}

        if not body.get("geocodes"):
            # 空结果（高德无匹配）→ OSM Nominatim 兜底
            fallback = await self._fallback_nominatim(address)
            if fallback:
                await r.set(key, json.dumps(fallback, ensure_ascii=False),
                            ex=settings.CACHE_TTL_GEOCODE)
                return fallback
            return {"status": "empty", "message": f"未找到 {address} 的坐标"}

        raw = body["geocodes"][:top_n]
        # 用第一条作为 principal 锚点，其它 candidate 算与它的距离
        principal_loc_raw = raw[0].get("location", "")
        if "," not in principal_loc_raw:
            return {"status": "empty", "message": "高德返回坐标格式异常"}
        principal_lng, principal_lat = (float(x) for x in principal_loc_raw.split(",", 1))
        principal_loc = (principal_lng, principal_lat)

        candidates = [_normalize_geocode_entry(g, i, principal_loc) for i, g in enumerate(raw)]

        cand0 = candidates[0]
        result = {
            "status": "success",
            "location": cand0["location"],
            "formatted_address": cand0["formatted_address"],
            "source": "Amap",
            "candidates": candidates,
            "confidence": _score_confidence(candidates, cand0["location_type"]),
            "disambiguated": _should_disambiguate(candidates),
            "principal_rank": 0,
            "cached": False,
        }

        # 缓存用 schema v1（含 candidates），并行加 principal_rank 字段
        await r.set(key, json.dumps(result, ensure_ascii=False),
                    ex=settings.CACHE_TTL_GEOCODE)
        return result

    async def _fallback_nominatim(self, address: str) -> dict | None:
        """OSM Nominatim 兜底地理编码。

        當高德返回空時調用。返回與 geocode() 同 schema 的 dict，或 None（也失敗時）。
        Nominatim 返回 WGS84，國內結果轉 GCJ02（與高德底圖一致）。
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    _NOMINATIM_ENDPOINT,
                    params={"q": address, "format": "json", "limit": DEFAULT_TOP_N},
                    headers=_NOMINATIM_HEADERS,
                )
                resp.raise_for_status()
                results = resp.json()
        except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPStatusError):
            return None

        if not results or not isinstance(results, list):
            return None

        # 解析原始結果，WGS84 → GCJ02
        parsed = []
        for r in results[:DEFAULT_TOP_N]:
            try:
                lat = float(r.get("lat", 0))
                lon = float(r.get("lon", 0))
            except (TypeError, ValueError):
                continue
            if lat == 0 and lon == 0:
                continue
            gcj_lng, gcj_lat = wgs84_to_gcj02(lon, lat)
            parsed.append({
                "gcj02": (gcj_lng, gcj_lat),
                "display_name": r.get("display_name", ""),
                "type": r.get("type", "其他"),
            })

        if not parsed:
            return None

        principal = parsed[0]["gcj02"]
        candidates = [
            {
                "rank": i,
                "location": [p["gcj02"][0], p["gcj02"][1]],
                "formatted_address": p["display_name"],
                "location_type": p["type"],
                "distance_to_principal": haversine_m(principal, p["gcj02"]) if i > 0 else 0.0,
            }
            for i, p in enumerate(parsed)
        ]

        cand0 = candidates[0]
        return {
            "status": "success",
            "location": cand0["location"],
            "formatted_address": cand0["formatted_address"],
            "source": "OSM_Nominatim",
            "candidates": candidates,
            "confidence": _score_confidence(candidates, cand0["location_type"]),
            "disambiguated": _should_disambiguate(candidates),
            "principal_rank": 0,
            "cached": False,
        }

    async def reverse_geocode(self, location: tuple) -> dict:
        """坐标 → 地址。location 是 GCJ02 (lng, lat)。"""
        lng, lat = location
        key_str = f"rev:{lng:.6f},{lat:.6f}"
        r = get_redis()
        key = _cache_key(key_str)
        cached = await r.get(key)
        if cached:
            try:
                return json.loads(cached)
            except json.JSONDecodeError:
                pass

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(REVERSE_ENDPOINT, params={
                    "key": self.amap_key,
                    "location": f"{lng},{lat}",
                })
                resp.raise_for_status()
                body = resp.json()
        except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPStatusError):
            return {"status": "empty", "message": "逆地理编码服务暂不可用"}

        if body.get("status") != "1" or not body.get("regeocode"):
            return {"status": "empty", "message": "逆地理编码失败"}

        addr = body["regeocode"].get("formatted_address", "")
        result = {
            "status": "success",
            "formatted_address": addr,
            "source": "Amap",
            "cached": False,
        }
        await r.set(key, json.dumps(result, ensure_ascii=False),
                    ex=settings.CACHE_TTL_GEOCODE)
        return result
