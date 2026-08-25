"""POIQuery 单元测试。

覆盖维度（参考 docs/04_testing_strategy.md §2.4 / §3.2 + GIS_Agent_技术文档.md §4.3）：
1. 高德成功：mock requests.get 返回 amap_poi_sample，验证结果正确（GCJ02、source="Amap"）
2. 高德空触发 OSM 兜底（国内）：source="OSM_CN"，WGS84 坐标转 GCJ02
3. 双源都空：status="empty"
4. 高德超时触发 OSM：mock 高德抛 Timeout，OSM 正常
5. OSM 也超时：返回 empty
6. 国外区域：location 在旧金山，OSM 数据保持 WGS84，source="OSM_Global"
7. 去重：跨源同一 POI（距离 < threshold）去重，保留信息更全的（高德）
8. 去重不误合并：同源不同 POI 不合并
9. bbox 计算：_radius_to_bbox 给定中心和半径，返回合理 bbox

全部用 mock，不打真实 API。
"""

from unittest.mock import patch, MagicMock

import pytest
import requests

from app.tools.geo_transform import wgs84_to_gcj02, gcj02_to_wgs84, haversine_m
from app.tools.poi_query import POIQuery


# ============================================================
# 辅助：构造按 URL 分流的 mock requests.get
# ============================================================

def _make_get_dispatcher(amap_resp=None, osm_resp=None, amap_exc=None, osm_exc=None):
    """构造一个 mock get 函数：根据 URL 分流返回高德 / OSM 响应。

    amap_resp / osm_resp: dict，作为 response.json() 返回值。
    amap_exc / osm_exc: 异常实例或类，调用时抛出（优先于 resp）。
    """

    def _mock_get(url, params=None, timeout=None, **kwargs):
        if "restapi.amap.com" in url:
            if amap_exc is not None:
                raise amap_exc if isinstance(amap_exc, BaseException) else amap_exc()
            resp = MagicMock()
            resp.json.return_value = amap_resp or {}
            resp.status_code = 200
            resp.raise_for_status.return_value = None
            return resp
        if "overpass-api.de" in url or "overpass" in url or "/api/interpreter" in url:
            if osm_exc is not None:
                raise osm_exc if isinstance(osm_exc, BaseException) else osm_exc()
            resp = MagicMock()
            resp.json.return_value = osm_resp or {}
            resp.status_code = 200
            resp.raise_for_status.return_value = None
            return resp
        # 未知 URL：返回空
        resp = MagicMock()
        resp.json.return_value = {}
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        return resp

    return _mock_get


# ============================================================
# 1. 高德成功
# ============================================================

@patch("app.tools.poi_query.requests.get")
def test_amap_success(mock_get, amap_poi_sample):
    """高德返回非空 POI，直接返回，source=Amap，坐标保持 GCJ02。"""
    mock_get.side_effect = _make_get_dispatcher(amap_resp=amap_poi_sample)

    query = POIQuery(amap_key="test_key", amap_timeout=3, osm_timeout=3)
    result = query.search_poi_tool(
        query="蜜雪冰城",
        location=(118.7845, 32.0429),  # GCJ02 南京新街口
        radius=500,
    )

    assert result["status"] == "success"
    assert result["source"] == "Amap"
    data = result["data"]
    pois = data["pois"]
    assert len(pois) == 2
    # 第一个 POI 字段映射
    p0 = pois[0]
    assert p0["name"] == "蜜雪冰城(新街口店)"
    assert p0["address"] == "中山路1号"
    assert p0["tel"] == "025-12345678"
    assert p0["source"] == "Amap"
    assert p0["crs"] == "GCJ02"
    assert p0["poi_id"] == "B00170B01H"
    # location 是 (lng, lat) GCJ02，与原始样本一致
    assert p0["location"][0] == pytest.approx(118.7845, abs=1e-6)
    assert p0["location"][1] == pytest.approx(32.0429, abs=1e-6)


# ============================================================
# 2. 高德空触发 OSM 兜底（国内）
# ============================================================

@patch("app.tools.poi_query.requests.get")
def test_amap_empty_triggers_osm_cn(mock_get, osm_poi_sample):
    """高德返回空 pois -> 走 OSM -> 国内 -> WGS84 转 GCJ02，source=OSM_CN。"""
    mock_get.side_effect = _make_get_dispatcher(
        amap_resp={"status": "1", "count": "0", "pois": []},
        osm_resp=osm_poi_sample,
    )

    query = POIQuery(amap_key="test_key", amap_timeout=3, osm_timeout=3)
    result = query.search_poi_tool(
        query="蜜雪冰城",
        location=(118.7845, 32.0429),  # GCJ02 南京
        radius=500,
    )

    assert result["status"] == "success"
    assert result["source"] == "OSM_CN"
    pois = result["data"]["pois"]
    assert len(pois) == 1
    p = pois[0]
    # OSM 原始 WGS84 (118.7782, 32.0417) 应被转为 GCJ02
    expected_gcj = wgs84_to_gcj02(118.7782, 32.0417)
    assert p["location"][0] == pytest.approx(expected_gcj[0], abs=1e-6)
    assert p["location"][1] == pytest.approx(expected_gcj[1], abs=1e-6)
    assert p["crs"] == "GCJ02"
    assert p["source"] == "OSM_CN"
    assert p["name"] == "Mixue Ice Cream & Tea"
    assert p["poi_id"] == "123456789"


# ============================================================
# 3. 双源都空
# ============================================================

@patch("app.tools.poi_query.requests.get")
def test_both_empty_returns_empty(mock_get):
    """高德空 + OSM 空 -> status=empty，不抛异常。"""
    mock_get.side_effect = _make_get_dispatcher(
        amap_resp={"status": "1", "count": "0", "pois": []},
        osm_resp={"elements": []},
    )

    query = POIQuery(amap_key="test_key")
    result = query.search_poi_tool(
        query="不存在的店",
        location=(118.7845, 32.0429),
        radius=500,
    )

    assert result["status"] == "empty"
    assert result["data"] is None or result["data"] == {}


# ============================================================
# 4. 高德超时触发 OSM
# ============================================================

@patch("app.tools.poi_query.requests.get")
def test_amap_timeout_triggers_osm(mock_get, osm_poi_sample):
    """高德抛 Timeout -> 走 OSM 兜底。"""
    mock_get.side_effect = _make_get_dispatcher(
        amap_exc=requests.Timeout("amap timeout"),
        osm_resp=osm_poi_sample,
    )

    query = POIQuery(amap_key="test_key")
    result = query.search_poi_tool(
        query="蜜雪冰城",
        location=(118.7845, 32.0429),
        radius=500,
    )

    assert result["status"] == "success"
    assert result["source"] == "OSM_CN"


# ============================================================
# 5. OSM 也超时
# ============================================================

@patch("app.tools.poi_query.requests.get")
def test_osm_timeout_returns_empty(mock_get):
    """高德空 + OSM 超时 -> empty。"""
    mock_get.side_effect = _make_get_dispatcher(
        amap_resp={"status": "1", "count": "0", "pois": []},
        osm_exc=requests.Timeout("osm timeout"),
    )

    query = POIQuery(amap_key="test_key")
    result = query.search_poi_tool(
        query="蜜雪冰城",
        location=(118.7845, 32.0429),
        radius=500,
    )

    assert result["status"] == "empty"


@patch("app.tools.poi_query.requests.get")
def test_osm_backup_endpoint_is_used_after_primary_failure(mock_get, osm_poi_sample):
    """A rejected primary Overpass instance must fall through to its backup."""

    primary = "https://primary-overpass.example/api/interpreter"
    backup = "https://backup-overpass.example/api/interpreter"
    calls: list[str] = []

    def side_effect(url, params=None, timeout=None, **_kwargs):
        calls.append(url)
        if "restapi.amap.com" in url:
            response = MagicMock()
            response.json.return_value = {"status": "1", "count": "0", "pois": []}
            response.raise_for_status.return_value = None
            return response
        if url == primary:
            raise requests.Timeout("primary unavailable")
        assert url == backup
        response = MagicMock()
        response.json.return_value = osm_poi_sample
        response.raise_for_status.return_value = None
        return response

    mock_get.side_effect = side_effect
    query = POIQuery(
        amap_key="test_key",
        osm_endpoint=primary,
        osm_backup_endpoints=backup,
    )

    result = query.search_poi_tool("蜜雪冰城", (118.7845, 32.0429), 500)

    assert result["status"] == "success"
    assert result["source"] == "OSM_CN"
    assert calls == [
        "https://restapi.amap.com/v3/place/around",
        primary,
        backup,
    ]


# ============================================================
# 5b. 高德连接错误也降级为 empty（ConnectionError 同 Timeout 处理）
# ============================================================

@patch("app.tools.poi_query.requests.get")
def test_amap_connection_error_triggers_osm(mock_get, osm_poi_sample):
    """高德 ConnectionError -> 走 OSM 兜底。"""
    mock_get.side_effect = _make_get_dispatcher(
        amap_exc=requests.ConnectionError("amap down"),
        osm_resp=osm_poi_sample,
    )

    query = POIQuery(amap_key="test_key")
    result = query.search_poi_tool(
        query="蜜雪冰城",
        location=(118.7845, 32.0429),
        radius=500,
    )

    assert result["status"] == "success"
    assert result["source"] == "OSM_CN"


# ============================================================
# 6. 国外区域：OSM 数据保持 WGS84
# ============================================================

@patch("app.tools.poi_query.requests.get")
def test_overseas_keeps_wgs84(mock_get):
    """location 在旧金山 -> bbox 在国外 -> OSM 数据保持 WGS84，source=OSM_Global。"""
    osm_resp = {
        "elements": [
            {
                "type": "node",
                "id": 999,
                "lat": 37.7749,
                "lon": -122.4194,
                "tags": {"name": "Blue Bottle Coffee", "amenity": "cafe"},
            },
        ],
    }
    mock_get.side_effect = _make_get_dispatcher(
        amap_resp={"status": "1", "count": "0", "pois": []},
        osm_resp=osm_resp,
    )

    query = POIQuery(amap_key="test_key")
    result = query.search_poi_tool(
        query="coffee",
        location=(-122.4194, 37.7749),  # 旧金山 WGS84
        radius=1000,
    )

    assert result["status"] == "success"
    assert result["source"] == "OSM_Global"
    pois = result["data"]["pois"]
    assert len(pois) == 1
    p = pois[0]
    # 国外坐标不偏转，保持 WGS84
    assert p["location"][0] == pytest.approx(-122.4194, abs=1e-6)
    assert p["location"][1] == pytest.approx(37.7749, abs=1e-6)
    assert p["crs"] == "WGS84"
    assert p["source"] == "OSM_Global"


# ============================================================
# 7. 去重：跨源同一 POI，保留信息更全的（高德）
# ============================================================

def test_dedup_cross_source_keeps_richer():
    """高德和 OSM 同一 POI（距离 < 50m），去重后保留信息更全的高德记录。"""
    # 两者坐标已统一到同一坐标系（GCJ02），距离 < 50m
    amap_pois = [
        {
            "name": "蜜雪冰城",
            "location": (118.7845, 32.0429),
            "source": "Amap",
            "address": "中山路1号",
            "tel": "025-12345678",
            "crs": "GCJ02",
        },
    ]
    osm_pois = [
        {
            "name": "Mixue Ice Cream & Tea",
            "location": (118.7846, 32.0430),  # 偏 ~11m
            "source": "OSM_CN",
            "address": None,
            "tel": None,
            "crs": "GCJ02",
        },
    ]
    query = POIQuery(amap_key="test")
    merged = query._deduplicate(amap_pois, osm_pois, threshold=50)

    assert len(merged) == 1
    assert merged[0]["source"] == "Amap"
    assert merged[0]["address"] == "中山路1号"
    assert merged[0]["tel"] == "025-12345678"


# ============================================================
# 8. 去重不误合并：同源不同 POI
# ============================================================

def test_dedup_same_source_no_merge():
    """同源不同 POI（距离 > threshold）不合并。"""
    pois = [
        {"name": "A店", "location": (118.78, 32.04), "source": "Amap", "crs": "GCJ02"},
        {"name": "B店", "location": (118.79, 32.05), "source": "Amap", "crs": "GCJ02"},
    ]
    query = POIQuery(amap_key="test")
    merged = query._deduplicate(pois, [], threshold=50)
    assert len(merged) == 2


def test_dedup_far_apart_both_kept():
    """跨源但距离远（> threshold）的两条都保留。"""
    amap_pois = [
        {"name": "蜜雪冰城(新街口)", "location": (118.7845, 32.0429), "source": "Amap", "crs": "GCJ02"},
    ]
    osm_pois = [
        {"name": "Mixue(珠江路)", "location": (118.7950, 32.0480), "source": "OSM_CN", "crs": "GCJ02"},
    ]
    query = POIQuery(amap_key="test")
    merged = query._deduplicate(amap_pois, osm_pois, threshold=50)
    # 距离 > 1km，两条都保留
    assert len(merged) == 2


def test_dedup_empty_inputs():
    """空输入返回空列表，不报错。"""
    query = POIQuery(amap_key="test")
    assert query._deduplicate([], [], threshold=50) == []
    assert len(query._deduplicate([], [{"name": "x", "location": (118.78, 32.04)}], threshold=50)) == 1


# ============================================================
# 9. bbox 计算
# ============================================================

def test_radius_to_bbox_nanjing():
    """500m 半径的 bbox 大小应合理（约 0.009° lat x 0.0105° lng @南京纬度）。"""
    query = POIQuery(amap_key="test")
    bbox = query._radius_to_bbox((118.7845, 32.0429), 500)
    min_lng, min_lat, max_lng, max_lat = bbox
    # 中心点应在 bbox 内
    assert min_lng < 118.7845 < max_lng
    assert min_lat < 32.0429 < max_lat
    # 半径 ~500m -> 纬度跨度 ~0.009° (500/111000*2)
    lat_span = max_lat - min_lat
    lng_span = max_lng - min_lng
    assert 0.008 < lat_span < 0.011, f"纬度跨度 {lat_span} 不合理"
    # 经度跨度受纬度修正（cos(32°)≈0.848），应略大于纬度跨度
    assert 0.009 < lng_span < 0.013, f"经度跨度 {lng_span} 不合理"


def test_radius_to_bbox_overseas():
    """国外坐标 bbox 计算同样合理。"""
    query = POIQuery(amap_key="test")
    bbox = query._radius_to_bbox((-122.4194, 37.7749), 1000)
    min_lng, min_lat, max_lng, max_lat = bbox
    assert min_lng < -122.4194 < max_lng
    assert min_lat < 37.7749 < max_lat
    # 1000m -> 纬度跨度 ~0.018°
    assert 0.016 < (max_lat - min_lat) < 0.020


# ============================================================
# 10. _is_china_bbox 委托
# ============================================================

def test_is_china_bbox_delegates():
    """_is_china_bbox 应复用 geo_transform.is_china_bbox 的判断。

    本测试验证 POIQuery._is_china_bbox 正确委托到 geo_transform.is_china_bbox，
    而非重复实现判断逻辑。is_china_bbox 的详细边界测试在 test_geo_transform.py 中。
    """
    query = POIQuery(amap_key="test")
    assert query._is_china_bbox((118.77, 32.04, 118.79, 32.06)) is True   # 南京
    assert query._is_china_bbox((-122.42, 37.77, -122.41, 37.78)) is False  # 旧金山


# ============================================================
# 11. _format_results 结构
# ============================================================

def test_format_results_structure():
    """_format_results 返回 status=success + data.pois + source。"""
    query = POIQuery(amap_key="test")
    pois = [
        {"name": "A", "location": (118.78, 32.04), "source": "Amap", "crs": "GCJ02"},
    ]
    result = query._format_results(pois, source="Amap")
    assert result["status"] == "success"
    assert result["source"] == "Amap"
    assert result["data"]["pois"] == pois


# ============================================================
# 12. query_amap 直接调用（GCJ02 输入，GCJ02 输出）
# ============================================================

@patch("app.tools.poi_query.requests.get")
def test_query_amap_parses_response(mock_get, amap_poi_sample):
    """query_amap 直接调用，返回 POI 列表（GCJ02），字段映射正确。"""
    mock_get.side_effect = _make_get_dispatcher(amap_resp=amap_poi_sample)
    query = POIQuery(amap_key="test_key")
    pois = query.query_amap("蜜雪冰城", (118.7845, 32.0429), 500)
    assert len(pois) == 2
    assert pois[0]["poi_id"] == "B00170B01H"
    assert pois[0]["crs"] == "GCJ02"
    assert pois[0]["source"] == "Amap"


@patch("app.tools.poi_query.requests.get")
def test_query_amap_empty_returns_empty_list(mock_get):
    """高德返回空 pois 时 query_amap 返回空列表。"""
    mock_get.side_effect = _make_get_dispatcher(
        amap_resp={"status": "1", "count": "0", "pois": []},
    )
    query = POIQuery(amap_key="test_key")
    pois = query.query_amap("无结果", (118.7845, 32.0429), 500)
    assert pois == []


# ============================================================
# 13. query_osm 直接调用（WGS84 输入输出）
# ============================================================

@patch("app.tools.poi_query.requests.get")
def test_query_osm_parses_response(mock_get, osm_poi_sample):
    """query_osm 直接调用，返回 POI 列表（WGS84）。"""
    mock_get.side_effect = _make_get_dispatcher(osm_resp=osm_poi_sample)
    query = POIQuery(amap_key="test_key")
    bbox = (118.77, 32.04, 118.79, 32.06)
    pois = query.query_osm("蜜雪冰城", bbox)
    assert len(pois) == 1
    p = pois[0]
    assert p["name"] == "Mixue Ice Cream & Tea"
    assert p["location"][0] == pytest.approx(118.7782, abs=1e-6)
    assert p["location"][1] == pytest.approx(32.0417, abs=1e-6)
    assert p["crs"] == "WGS84"
    assert p["source"] == "OSM"
    assert p["poi_id"] == "123456789"
