"""Gismind 测试公共 fixtures。"""

import json
from pathlib import Path
import fakeredis
import pytest

from app.utils.redis import set_redis_instance


@pytest.fixture(autouse=True)
async def fake_redis():
    """为全部测试提供隔离的 fakeredis 实例。"""
    r = fakeredis.FakeAsyncRedis()
    set_redis_instance(r)
    yield r
    set_redis_instance(None)


class MockResponse:
    """模拟 LLM 响应，支持 content / tool_calls / json_data 三种返回"""

    def __init__(self, content=None, tool_calls=None, json_data=None):
        self.content = content or ""
        self.tool_calls = tool_calls or []
        self._json_data = json_data

    def json(self):
        return self._json_data or {}


@pytest.fixture
def golden_coords():
    """坐标转换黄金用例（WGS84 <-> GCJ02）"""
    path = Path(__file__).parent / "fixtures" / "golden_coords.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("cases", [])


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
