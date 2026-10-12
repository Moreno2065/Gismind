"""Gismind 测试公共 fixtures。"""

import pytest


class MockResponse:
    """模拟 LLM 响应，支持 content / tool_calls / json_data 三种返回"""

    def __init__(self, content=None, tool_calls=None, json_data=None):
        self.content = content or ""
        self.tool_calls = tool_calls or []
        self._json_data = json_data

    def json(self):
        return self._json_data or {}


@pytest.fixture
def amap_poi_sample():
    """高德 POI 响应样本"""
    return {
        "status": "1",
        "count": "2",
        "pois": [
            {
                "id": "B00170B01H",
                "name": "蜜雪冰城(新街口店)",
                "location": "118.7845,32.0429",
                "address": "中山路1号",
                "tel": "025-12345678",
                "typecode": "050500",
            },
            {
                "id": "B00170B01I",
                "name": "蜜雪冰城(珠江路店)",
                "location": "118.7856,32.0418",
                "address": "珠江路88号",
                "tel": "025-87654321",
                "typecode": "050500",
            },
        ],
    }


@pytest.fixture
def osm_poi_sample():
    """OSM Overpass 响应样本"""
    return {
        "version": 0.6,
        "generator": "Overpass API",
        "elements": [
            {
                "type": "node",
                "id": 123456789,
                "lat": 32.0417,
                "lon": 118.7782,
                "tags": {"name": "Mixue Ice Cream & Tea", "amenity": "cafe"},
            },
        ],
    }


@pytest.fixture
def transform_cases():
    """Runtime-owned coordinate cases for the public transform algorithm.

    These are generated in the test process rather than loaded from a legacy
    golden/evidence file, so the test exercises only the documented numerical
    contract and has no historical artifact dependency.
    """
    return [
        {"name": "北京天安门", "wgs84": [116.3912, 39.9075], "gcj02": [116.3975, 39.9087], "city": "北京"},
        {"name": "上海东方明珠", "wgs84": [121.4955, 31.2396], "gcj02": [121.5018, 31.2408], "city": "上海"},
        {"name": "南京新街口", "wgs84": [118.7782, 32.0417], "gcj02": [118.7845, 32.0429], "city": "南京"},
        {"name": "乌鲁木齐红山", "wgs84": [87.6168, 43.8256], "gcj02": [87.6232, 43.8268], "city": "乌鲁木齐"},
        {"name": "海口钟楼", "wgs84": [110.3310, 20.0310], "gcj02": [110.3374, 20.0322], "city": "海口"},
    ]
