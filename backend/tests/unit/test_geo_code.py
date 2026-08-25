"""geo_code 单元测试。全部 mock httpx，不消耗真实高德配额。"""

from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from app.tools.geo_code import GeoCoder


def _mock_response(json_data, status_code=200):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data
    r.raise_for_status.return_value = None
    return r


class TestGeocode:
    async def test_geocode_success(self, fake_redis):
        geo = GeoCoder(amap_key="test_key")
        mock_resp = _mock_response({
            "status": "1",
            "geocodes": [{"location": "118.7845,32.0429", "formatted_address": "江苏省南京市新街口"}],
        })
        with patch("app.tools.geo_code.httpx.AsyncClient") as mock_client_cls:
            client = AsyncMock()
            client.get.return_value = mock_resp
            client.__aenter__.return_value = client
            client.__aexit__.return_value = None
            mock_client_cls.return_value = client

            result = await geo.geocode("南京新街口")

        assert result["status"] == "success"
        assert result["location"] == [118.7845, 32.0429]
        assert "新街口" in result["formatted_address"]
        assert result["source"] == "Amap"

    async def test_geocode_cached_skips_api(self, fake_redis):
        geo = GeoCoder(amap_key="test_key")
        mock_resp = _mock_response({
            "status": "1",
            "geocodes": [{"location": "118.7845,32.0429", "formatted_address": "南京新街口"}],
        })
        with patch("app.tools.geo_code.httpx.AsyncClient") as mock_client_cls:
            client = AsyncMock()
            client.get.return_value = mock_resp
            client.__aenter__.return_value = client
            client.__aexit__.return_value = None
            mock_client_cls.return_value = client

            first = await geo.geocode("南京新街口")
            assert first["status"] == "success"
            assert client.get.call_count == 1

            second = await geo.geocode("南京新街口")
            assert client.get.call_count == 1
            assert second["cached"] is True
            assert second["location"] == [118.7845, 32.0429]

    async def test_geocode_empty_result(self, fake_redis):
        geo = GeoCoder(amap_key="test_key")
        mock_resp = _mock_response({"status": "1", "geocodes": []})
        with patch("app.tools.geo_code.httpx.AsyncClient") as mock_client_cls:
            client = AsyncMock()
            client.get.return_value = mock_resp
            client.__aenter__.return_value = client
            client.__aexit__.return_value = None
            mock_client_cls.return_value = client

            result = await geo.geocode("不存在的地名xyz")

        assert result["status"] == "empty"

    async def test_geocode_timeout_returns_empty(self, fake_redis):
        geo = GeoCoder(amap_key="test_key")
        import httpx
        with patch("app.tools.geo_code.httpx.AsyncClient") as mock_client_cls:
            client = AsyncMock()
            client.get.side_effect = httpx.TimeoutException("timeout")
            client.__aenter__.return_value = client
            client.__aexit__.return_value = None
            mock_client_cls.return_value = client

            result = await geo.geocode("南京新街口")

        assert result["status"] == "empty"
        assert "暂不可用" in result["message"]

    async def test_geocode_fallback_nominatim(self, fake_redis):
        """高德返回空時應 fallback 到 OSM Nominatim。"""
        geo = GeoCoder(amap_key="test_key")
        amap_empty = _mock_response({"status": "1", "geocodes": []})
        nominatim_resp = _mock_response([
            {"lat": "31.3412", "lon": "118.3712",
             "display_name": "安徽师范大学花津校区, 弋江区, 芜湖市, 安徽省, 中国",
             "type": "amenity"},
        ])
        with patch("app.tools.geo_code.httpx.AsyncClient") as mock_client_cls:
            client = AsyncMock()
            client.get.side_effect = [amap_empty, nominatim_resp]
            client.__aenter__.return_value = client
            client.__aexit__.return_value = None
            mock_client_cls.return_value = client

            result = await geo.geocode("安师大花津校区")

        assert result["status"] == "success"
        assert result["source"] == "OSM_Nominatim"
        assert result["location"][0] != 0.0  # 应有有效坐标
        assert result["location"][1] != 0.0
        assert "安徽师范大学" in result["formatted_address"]
        assert len(result["candidates"]) == 1

    async def test_geocode_both_sources_empty(self, fake_redis):
        """高德和 Nominatim 都返回空時應返回 empty。"""
        geo = GeoCoder(amap_key="test_key")
        amap_empty = _mock_response({"status": "1", "geocodes": []})
        nominatim_empty = _mock_response([])
        with patch("app.tools.geo_code.httpx.AsyncClient") as mock_client_cls:
            client = AsyncMock()
            client.get.side_effect = [amap_empty, nominatim_empty]
            client.__aenter__.return_value = client
            client.__aexit__.return_value = None
            mock_client_cls.return_value = client

            result = await geo.geocode("完全不存在的地名")

        assert result["status"] == "empty"

    async def test_geocode_empty_address(self, fake_redis):
        geo = GeoCoder(amap_key="test_key")
        result = await geo.geocode("")
        assert result["status"] == "empty"

    async def test_geocode_writes_to_cache(self, fake_redis):
        geo = GeoCoder(amap_key="test_key")
        mock_resp = _mock_response({
            "status": "1",
            "geocodes": [{"location": "118.78,32.04", "formatted_address": "南京"}],
        })
        with patch("app.tools.geo_code.httpx.AsyncClient") as mock_client_cls:
            client = AsyncMock()
            client.get.return_value = mock_resp
            client.__aenter__.return_value = client
            client.__aexit__.return_value = None
            mock_client_cls.return_value = client

            await geo.geocode("南京")

        keys = [k async for k in fake_redis.scan_iter("cache:geocode:*")]
        assert len(keys) == 1


class TestReverseGeocode:
    async def test_reverse_success(self, fake_redis):
        geo = GeoCoder(amap_key="test_key")
        mock_resp = _mock_response({
            "status": "1",
            "regeocode": {"formatted_address": "江苏省南京市玄武区"},
        })
        with patch("app.tools.geo_code.httpx.AsyncClient") as mock_client_cls:
            client = AsyncMock()
            client.get.return_value = mock_resp
            client.__aenter__.return_value = client
            client.__aexit__.return_value = None
            mock_client_cls.return_value = client

            result = await geo.reverse_geocode((118.78, 32.04))

        assert result["status"] == "success"
        assert "南京" in result["formatted_address"]

    async def test_reverse_timeout(self, fake_redis):
        geo = GeoCoder(amap_key="test_key")
        import httpx
        with patch("app.tools.geo_code.httpx.AsyncClient") as mock_client_cls:
            client = AsyncMock()
            client.get.side_effect = httpx.ConnectError("conn")
            client.__aenter__.return_value = client
            client.__aexit__.return_value = None
            mock_client_cls.return_value = client

            result = await geo.reverse_geocode((118.78, 32.04))

        assert result["status"] == "empty"

    # --------------------------------------------------------
    # Sprint 1 增量：top-N candidates / disambiguated / principal_rank
    # --------------------------------------------------------

    async def test_top_n_candidates(self, fake_redis):
        """高德返回 3 个 geocodes → candidates 长度 == 3，rank 严格 0/1/2，主点距离为 0。"""
        geo = GeoCoder(amap_key="test_key")
        mock_resp = _mock_response({
            "status": "1",
            "geocodes": [
                {"location": "118.7845,32.0429", "formatted_address": "江苏省南京市新街口",
                 "location_type": "POI"},
                {"location": "118.7910,32.0450", "formatted_address": "江苏省南京市新街口地铁站",
                 "location_type": "地铁站"},
                {"location": "118.7950,32.0800", "formatted_address": "江苏省南京市玄武湖",
                 "location_type": "地名"},
            ],
        })
        with patch("app.tools.geo_code.httpx.AsyncClient") as mock_client_cls:
            client = AsyncMock()
            client.get.return_value = mock_resp
            client.__aenter__.return_value = client
            client.__aexit__.return_value = None
            mock_client_cls.return_value = client

            result = await geo.geocode("新街口", top_n=3)

        assert result["status"] == "success"
        cands = result["candidates"]
        assert len(cands) == 3
        # rank 严格递增 0,1,2
        assert [c["rank"] for c in cands] == [0, 1, 2]
        # 主点（rank=0）的 distance_to_principal 必为 0
        assert cands[0]["distance_to_principal"] == 0
        # 其他 candidate 距离 > 0
        assert cands[1]["distance_to_principal"] > 0
        assert cands[2]["distance_to_principal"] > 0
        # 主点 location 与 candidates[0].location 一致
        assert result["location"] == cands[0]["location"]

    async def test_disambiguated_flag(self, fake_redis):
        """两条 location_type 都是 '其他'（基础置信度 0.6） → disambiguated=True。"""
        geo = GeoCoder(amap_key="test_key")
        mock_resp = _mock_response({
            "status": "1",
            "geocodes": [
                {"location": "118.7845,32.0429", "formatted_address": "新街口 A",
                 "location_type": "其他"},
                {"location": "118.7950,32.0600", "formatted_address": "新街口 B",
                 "location_type": "其他"},
            ],
        })
        with patch("app.tools.geo_code.httpx.AsyncClient") as mock_client_cls:
            client = AsyncMock()
            client.get.return_value = mock_resp
            client.__aenter__.return_value = client
            client.__aexit__.return_value = None
            mock_client_cls.return_value = client

            result = await geo.geocode("新街口", top_n=3)

        assert result["status"] == "success"
        # 两个 candidate 的 location_type 基础置信度都是 0.6，差 < 0.15 → 应歧义化
        assert result["disambiguated"] is True
        assert len(result["candidates"]) >= 2

    async def test_principal_rank_persists(self, fake_redis):
        """principal_rank 参数让用户切换主点：第二次以 rank=1 调 → 主点变为 candidates[1]。"""
        geo = GeoCoder(amap_key="test_key")
        mock_resp = _mock_response({
            "status": "1",
            "geocodes": [
                {"location": "118.7845,32.0429", "formatted_address": "新街口 A",
                 "location_type": "POI"},
                {"location": "118.7950,32.0600", "formatted_address": "新街口 B",
                 "location_type": "POI"},
            ],
        })
        with patch("app.tools.geo_code.httpx.AsyncClient") as mock_client_cls:
            client = AsyncMock()
            client.get.return_value = mock_resp
            client.__aenter__.return_value = client
            client.__aexit__.return_value = None
            mock_client_cls.return_value = client

            # 第一次：默认 rank=0
            first = await geo.geocode("新街口")
            assert first["status"] == "success"
            cands = first["candidates"]
            assert len(cands) == 2
            assert first["principal_rank"] == 0
            assert first["location"] == cands[0]["location"]

            # 第二次：以 principal_rank=1 重新调用 → 命中缓存，切换主点
            second = await geo.geocode("新街口", principal_rank=1)
            assert second["status"] == "success"
            assert second["principal_rank"] == 1
            assert second["location"] == cands[1]["location"]
            assert second["cached"] is True
