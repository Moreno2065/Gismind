"""API contract tests (route shapes / upload / CORS / sessions).

Chat SSE cases still stub ``run_react_loop`` to assert event framing only —
they are **not** production LangGraph wiring tests. Real chat/resume wiring is
covered by ``test_awaiting_input_e2e.py`` and ``test_resume_api.py``.
"""

import io
import json
from unittest.mock import patch, AsyncMock

import pytest
from fastapi.testclient import TestClient


# ============================================================
# fixtures
# ============================================================

@pytest.fixture
def client():
    """FastAPI TestClient。每次创建新 app 以避免路由污染。"""
    from app.main import create_app
    app = create_app()
    return TestClient(app)


@pytest.fixture
def geojson_bytes():
    """合法 GeoJSON Point FeatureCollection 字节。"""
    data = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [118.7845, 32.0429]},
                "properties": {"name": "蜜雪冰城(新街口店)"},
            },
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [118.7856, 32.0418]},
                "properties": {"name": "蜜雪冰城(珠江路店)"},
            },
        ],
    }
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


# ============================================================
# 1. health
# ============================================================

class TestHealth:
    """GET /api/health 返回 200 + status/checks/version。"""

    def test_health_returns_200(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200

    def test_health_body_structure(self, client):
        data = client.get("/api/health").json()
        assert "status" in data
        assert data["status"] in ("ok", "degraded")
        assert "checks" in data
        assert isinstance(data["checks"], dict)
        assert data["version"] == "1.6.0"

    def test_health_checks_keys(self, client):
        data = client.get("/api/health").json()
        for key in ("redis", "celery", "llm", "amap"):
            assert key in data["checks"]


# ============================================================
# 2. chat SSE 成功
# ============================================================

class TestChatSSE:
    """POST /api/chat SSE 流式返回。"""

    def test_chat_returns_event_stream(self, client):
        """响应 Content-Type 为 text/event-stream。"""
        with patch("app.api.chat.run_react_loop", new_callable=AsyncMock) as mock_loop:
            mock_loop.return_value = {
                "should_stop": True,
                "iteration": 1,
                "final_output": {
                    "summary": "找到 12 家蜜雪冰城",
                    "text": "共找到 12 家蜜雪冰城。",
                },
                "trace_id": "trace_test123",
            }
            resp = client.post(
                "/api/chat",
                json={"session_id": "sess_abc123", "message": "南京新街口蜜雪冰城"},
            )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

    def test_chat_emits_status_and_done_events(self, client):
        """body 含 event: status 和 event: done。"""
        with patch("app.api.chat.run_react_loop", new_callable=AsyncMock) as mock_loop:
            mock_loop.return_value = {
                "should_stop": True,
                "iteration": 1,
                "final_output": {
                    "summary": "找到 12 家蜜雪冰城",
                    "text": "共找到 12 家蜜雪冰城。",
                },
                "trace_id": "trace_test123",
            }
            resp = client.post(
                "/api/chat",
                json={"session_id": "sess_abc123", "message": "南京新街口蜜雪冰城"},
            )
        body = resp.text
        assert "event: status" in body
        assert "event: done" in body

    def test_chat_done_event_contains_trace_id(self, client):
        """done 事件的 data 含 trace_id，且以 trace_ 开头。"""
        with patch("app.api.chat.run_react_loop", new_callable=AsyncMock) as mock_loop:
            mock_loop.return_value = {
                "should_stop": True,
                "iteration": 1,
                "final_output": {"summary": "ok"},
                "trace_id": "trace_test123",
            }
            resp = client.post(
                "/api/chat",
                json={"session_id": "sess_abc123", "message": "test"},
            )
        body = resp.text
        # done 事件行
        assert "event: done" in body
        # 找到 done 事件的 data 行，解析 trace_id
        lines = body.split("\n")
        for i, line in enumerate(lines):
            if line.strip() == "event: done":
                data_line = lines[i + 1]
                assert data_line.startswith("data: ")
                payload = json.loads(data_line[len("data: "):])
                assert "trace_id" in payload
                assert payload["trace_id"].startswith("trace_")
                break
        else:
            pytest.fail("未找到 event: done")

    def test_chat_emits_token_event_when_text_present(self, client):
        """final_output.text 存在时发送 token 事件。"""
        with patch("app.api.chat.run_react_loop", new_callable=AsyncMock) as mock_loop:
            mock_loop.return_value = {
                "should_stop": True,
                "iteration": 1,
                "final_output": {
                    "summary": "找到 12 家",
                    "text": "共找到 12 家蜜雪冰城。",
                },
                "trace_id": "trace_test123",
            }
            resp = client.post(
                "/api/chat",
                json={"session_id": "sess_abc123", "message": "test"},
            )
        body = resp.text
        assert "event: token" in body
        assert "12 家蜜雪冰城" in body

    def test_chat_emits_map_event_when_map_present(self, client):
        """final_output.map 存在时发送 map 事件。"""
        map_payload = {
            "layers": [{"type": "point", "coordinates": [[118.78, 32.04]]}],
            "bbox": [118.77, 32.03, 118.79, 32.05],
        }
        with patch("app.api.chat.run_react_loop", new_callable=AsyncMock) as mock_loop:
            mock_loop.return_value = {
                "should_stop": True,
                "iteration": 1,
                "final_output": {
                    "summary": "ok",
                    "map": map_payload,
                },
                "trace_id": "trace_test123",
            }
            resp = client.post(
                "/api/chat",
                json={"session_id": "sess_abc123", "message": "test"},
            )
        body = resp.text
        assert "event: map" in body
        assert "bbox" in body

    @pytest.mark.skip(reason="chart SSE event not yet implemented in chat endpoint")
    def test_chat_emits_chart_event_when_chart_present(self, client):
        """final_output.chart 存在时应发送 chart 事件（待实现）。

        当前 chat endpoint (app/api/chat.py) 仅处理 status/token/map/done/error
        事件类型。chart 事件在 docs/01_api_spec.md 的 SSE 契约中定义但尚未实现。
        本测试作为 spec-conformance reminder，在 chart 事件实现后取消 skip。
        """
        chart_payload = {
            "type": "bar",
            "data": [{"label": "A", "value": 10}, {"label": "B", "value": 20}],
            "title": "POI 分布统计",
        }
        with patch("app.api.chat.run_react_loop", new_callable=AsyncMock) as mock_loop:
            mock_loop.return_value = {
                "should_stop": True,
                "iteration": 1,
                "final_output": {
                    "summary": "ok",
                    "chart": chart_payload,
                },
                "trace_id": "trace_test123",
            }
            resp = client.post(
                "/api/chat",
                json={"session_id": "sess_abc123", "message": "test"},
            )
        body = resp.text
        assert "event: chart" in body
        assert "POI 分布统计" in body

    def test_chat_calls_run_react_loop_with_args(self, client):
        """run_react_loop 被调用，参数含 message、session_id 和上传引用。"""
        with patch("app.api.chat.run_react_loop", new_callable=AsyncMock) as mock_loop:
            mock_loop.return_value = {
                "should_stop": True,
                "iteration": 1,
                "final_output": {"summary": "ok"},
                "trace_id": "trace_test123",
            }
            client.post(
                "/api/chat",
                json={
                    "session_id": "sess_abc123",
                    "message": "南京蜜雪冰城",
                    "upload_file_ids": ["file_xyz789"],
                },
            )
        mock_loop.assert_called_once()
        call_kwargs = mock_loop.call_args.kwargs
        assert call_kwargs["user_input"] == "南京蜜雪冰城"
        assert call_kwargs["session_id"] == "sess_abc123"
        assert call_kwargs["trace_id"].startswith("trace_")
        assert call_kwargs["upload_file_ids"] == ["file_xyz789"]

    def test_chat_trace_id_generated_per_request(self, client):
        """每次请求生成新 trace_id。

        注意：API 端点自身生成 trace_id（uuid）并传入 run_react_loop，
        SSE done 事件使用 API 生成的 trace_id，而非 run_react_loop 返回值中的。
        本测试验证两次请求的 trace_id 互不相同。
        """
        with patch("app.api.chat.run_react_loop", new_callable=AsyncMock) as mock_loop:
            mock_loop.return_value = {
                "should_stop": True,
                "iteration": 1,
                "final_output": {"summary": "ok"},
                "trace_id": "ignored_by_sse_done",
            }
            r1 = client.post(
                "/api/chat",
                json={"session_id": "sess_1", "message": "a"},
            )
            r2 = client.post(
                "/api/chat",
                json={"session_id": "sess_2", "message": "b"},
            )
        # 两次 trace_id 不同（done 事件中的）
        def extract_done_trace(body):
            lines = body.split("\n")
            for i, line in enumerate(lines):
                if line.strip() == "event: done":
                    payload = json.loads(lines[i + 1][len("data: "):])
                    return payload.get("trace_id")
            return None

        t1 = extract_done_trace(r1.text)
        t2 = extract_done_trace(r2.text)
        assert t1 is not None
        assert t2 is not None
        assert t1 != t2


# ============================================================
# 3. chat 错误
# ============================================================

class TestChatError:
    """run_react_loop 抛异常时的 SSE 契约。

    ``_run_loop_sync`` 可把异常归一化为结构化 ``final_output``，但 SSE
    终态必须是 ``error``，不能把失败伪装成 ``done``。
    """

    def test_chat_emits_failure_token_on_loop_exception(self, client):
        with patch("app.api.chat.run_react_loop", new_callable=AsyncMock) as mock_loop:
            mock_loop.side_effect = RuntimeError("LLM 服务不可用")
            resp = client.post(
                "/api/chat",
                json={"session_id": "sess_abc123", "message": "test"},
            )
        # SSE 响应仍是 200（流已开始）
        assert resp.status_code == 200
        body = resp.text
        assert "event: error" in body
        assert "LLM" in body or "不可用" in body
        assert "event: done" not in body

    def test_chat_failure_error_contains_trace_id(self, client):
        with patch("app.api.chat.run_react_loop", new_callable=AsyncMock) as mock_loop:
            mock_loop.side_effect = RuntimeError("boom")
            resp = client.post(
                "/api/chat",
                json={"session_id": "sess_abc123", "message": "test"},
            )
        body = resp.text
        assert "boom" in body
        assert "执行失败" in body
        lines = body.split("\n")
        for i, line in enumerate(lines):
            if line.strip() == "event: error":
                payload = json.loads(lines[i + 1][len("data: "):])
                assert "trace_id" in payload
                assert payload["trace_id"].startswith("trace_")
                break
        else:
            pytest.fail("未找到 event: error")

    def test_chat_failure_still_has_status_before(self, client):
        """异常发生前应已发送 status 事件，并以 error 收尾。"""
        with patch("app.api.chat.run_react_loop", new_callable=AsyncMock) as mock_loop:
            mock_loop.side_effect = RuntimeError("fail")
            resp = client.post(
                "/api/chat",
                json={"session_id": "sess_abc123", "message": "test"},
            )
        body = resp.text
        assert "event: status" in body
        assert "执行失败" in body
        assert "event: error" in body
        assert "event: done" not in body


# ============================================================
# 4. upload 成功
# ============================================================

class TestUploadSuccess:
    """POST /api/upload 合法文件成功。"""

    def test_upload_geojson_returns_200(self, client, geojson_bytes):
        resp = client.post(
            "/api/upload",
            files={"file": ("test.geojson", io.BytesIO(geojson_bytes), "application/json")},
        )
        assert resp.status_code == 200

    def test_upload_geojson_returns_file_id(self, client, geojson_bytes):
        resp = client.post(
            "/api/upload",
            files={"file": ("test.geojson", io.BytesIO(geojson_bytes), "application/json")},
        )
        data = resp.json()
        assert "file_id" in data
        assert data["file_id"].startswith("file_")

    def test_upload_geojson_returns_feature_count(self, client, geojson_bytes):
        resp = client.post(
            "/api/upload",
            files={"file": ("test.geojson", io.BytesIO(geojson_bytes), "application/json")},
        )
        data = resp.json()
        assert data["feature_count"] == 2
        assert data["filename"] == "test.geojson"
        assert data["crs"] == "GCJ02"
        assert "geometry_type" in data

    def test_upload_json_extension_accepted(self, client, geojson_bytes):
        """.json 扩展名也应被接受（白名单含 .json）。"""
        resp = client.post(
            "/api/upload",
            files={"file": ("data.json", io.BytesIO(geojson_bytes), "application/json")},
        )
        assert resp.status_code == 200
        assert resp.json()["feature_count"] == 2


# ============================================================
# 5. upload 拒绝类型
# ============================================================

class TestUploadRejectType:
    """不支持的文件类型返回 422。"""

    def test_upload_exe_rejected_422(self, client):
        resp = client.post(
            "/api/upload",
            files={"file": ("evil.exe", io.BytesIO(b"MZ\x90\x00"), "application/octet-stream")},
        )
        assert resp.status_code == 422

    def test_upload_exe_error_code_unsupported_file_type(self, client):
        resp = client.post(
            "/api/upload",
            files={"file": ("evil.exe", io.BytesIO(b"MZ"), "application/octet-stream")},
        )
        # FastAPI HTTPException detail 在 resp.json()["detail"]
        body = resp.json()
        detail = body.get("detail") if isinstance(body.get("detail"), dict) else body
        detail_str = json.dumps(detail, ensure_ascii=False) if isinstance(detail, dict) else str(detail)
        assert "UNSUPPORTED_FILE_TYPE" in detail_str

    def test_upload_no_extension_rejected(self, client):
        resp = client.post(
            "/api/upload",
            files={"file": ("noext", io.BytesIO(b"{}"), "application/octet-stream")},
        )
        assert resp.status_code == 422


# ============================================================
# 6. upload 超大
# ============================================================

class TestUploadTooLarge:
    """超过 UPLOAD_MAX_SIZE(50MB) 返回 413。"""

    def test_upload_oversize_returns_413(self, client):
        """构造超过 50MB 的内容。用 mock file.size 走预校验路径，
        同时也提供真实大内容以防预校验未触发。"""
        from app.config import settings
        max_bytes = settings.UPLOAD_MAX_SIZE * 1024 * 1024
        # 构造略超上限的内容（51MB）
        oversize = b"x" * (max_bytes + 1024)
        resp = client.post(
            "/api/upload",
            files={"file": ("big.geojson", io.BytesIO(oversize), "application/json")},
        )
        assert resp.status_code == 413

    def test_upload_oversize_error_code_file_too_large(self, client):
        from app.config import settings
        max_bytes = settings.UPLOAD_MAX_SIZE * 1024 * 1024
        oversize = b"x" * (max_bytes + 1024)
        resp = client.post(
            "/api/upload",
            files={"file": ("big.geojson", io.BytesIO(oversize), "application/json")},
        )
        body = resp.json()
        detail = body.get("detail") if isinstance(body.get("detail"), dict) else body
        detail_str = json.dumps(detail, ensure_ascii=False) if isinstance(detail, dict) else str(detail)
        assert "FILE_TOO_LARGE" in detail_str


# ============================================================
# 7. CORS 预检
# ============================================================

class TestCORS:
    """OPTIONS 预检请求返回正确 CORS 头。"""

    def test_options_preflight_returns_cors_headers(self, client):
        resp = client.options(
            "/api/chat",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        # 预检应返回 200
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"
        assert "POST" in resp.headers.get("access-control-allow-methods", "")

    def test_cors_allows_dev_origin(self, client):
        """开发环境 localhost:5173 在允许列表。"""
        resp = client.get(
            "/api/health",
            headers={"Origin": "http://localhost:5173"},
        )
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"

    def test_cors_rejects_unknown_origin(self, client):
        """未知 origin 不返回 allow-origin 头。"""
        resp = client.get(
            "/api/health",
            headers={"Origin": "http://evil.example.com"},
        )
        allow = resp.headers.get("access-control-allow-origin")
        assert allow is None or "evil.example.com" not in allow


# ============================================================
# 8. memory 空间记忆 API
# ============================================================

class TestMemoryAPI:
    """GET/DELETE /api/memory/{session_id} 端点契约。"""

    async def test_get_memory_empty_returns_empty_list(self, client):
        resp = client.get("/api/memory/sess_empty")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "sess_empty"
        assert data["memories"] == []

    async def test_get_memory_returns_stored_memories(self, client, fake_redis):
        from app.utils.memory import MemoryStore
        store = MemoryStore()
        await store.remember_origin("sess_api", "安师老校区", (118.78, 32.05))

        resp = client.get("/api/memory/sess_api")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "sess_api"
        assert len(data["memories"]) == 1
        assert data["memories"][0]["label"] == "安师老校区"
        assert data["memories"][0]["location"] == [118.78, 32.05]

    async def test_delete_memory_clears_memories(self, client, fake_redis):
        from app.utils.memory import MemoryStore
        store = MemoryStore()
        await store.remember_origin("sess_del", "原点", (1.0, 1.0))

        resp = client.delete("/api/memory/sess_del")
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] is True
        assert data["session_id"] == "sess_del"

        memories = await store.get_memories("sess_del")
        assert memories == []


# ============================================================
# 9. /api/sessions* 会话管理 API
# ============================================================

class TestSessionAPI:
    """POST/GET/PATCH/DELETE /api/sessions* 端点契约。"""

    async def test_create_session_returns_id_and_meta(self, client, fake_redis):
        """POST /api/sessions → 201，body 含 id 以 sess_ 开头、title、created_at、updated_at。"""
        from app.utils.redis import make_key

        resp = client.post("/api/sessions")
        assert resp.status_code in (200, 201)
        body = resp.json()
        assert body["id"].startswith("sess_")
        assert "title" in body
        assert "created_at" in body
        assert "updated_at" in body
        assert isinstance(body["created_at"], int)
        assert isinstance(body["updated_at"], int)

        # Redis 里 session:{id} 真存在
        session_id = body["id"]
        key = make_key("session", session_id)
        exists = await fake_redis.exists(key)
        assert exists == 1

    async def test_list_sessions_ordered_by_updated_at_desc(self, client, fake_redis):
        """GET /api/sessions → 200，items 按 updated_at 降序。"""
        import asyncio
        from app.utils.redis import set_redis_instance
        from app.utils.session import SessionStore
        import fakeredis

        # 生产环境 Redis 用 decode_responses=True，fakeredis 默认是 bytes。
        # list_all 内的 scan_iter 在 fakeredis 默认下返回 bytes，会触发现有
        # app 路径上的 TypeError。为贴近生产，这里显式替换为 decode_responses 实例。
        decoded = fakeredis.FakeAsyncRedis(decode_responses=True)
        async for k in fake_redis.scan_iter(match="session:*"):
            raw = await fake_redis.get(k)
            if raw is not None:
                if isinstance(k, bytes):
                    k = k.decode()
                await decoded.set(k, raw)
        set_redis_instance(decoded)
        try:
            store = SessionStore()
            ids = []
            for i in range(3):
                sid = await store.create()
                ids.append(sid)
                # 强制 millisecond 区分，避免 TestClient 内多次 await 落在同一 ms
                # 时排在 stability 之外
                if i < 2:
                    await asyncio.sleep(0.1)

            # 给第二个 session 追加一条消息，使其 updated_at 更新（更晚）
            await asyncio.sleep(0.1)
            await store.append_message(ids[1], "user", "trigger update")

            resp = client.get("/api/sessions")
            assert resp.status_code == 200
            data = resp.json()
            assert "items" in data
            assert len(data["items"]) >= 3

            returned_ids = [item["id"] for item in data["items"]]
            # ids[1] updated_at 最新，应排第一
            assert returned_ids[0] == ids[1]
            # updated_at 单调递减
            timestamps = [item["updated_at"] for item in data["items"]]
            assert timestamps == sorted(timestamps, reverse=True)
        finally:
            set_redis_instance(fake_redis)

    async def test_get_session_meta_404_on_unknown(self, client):
        """GET /api/sessions/{unknown} → 404，detail.code == SESSION_NOT_FOUND。"""
        resp = client.get("/api/sessions/sess_unknown")
        assert resp.status_code == 404
        body = resp.json()
        detail = body.get("detail") if isinstance(body.get("detail"), dict) else body
        if isinstance(detail, dict):
            assert detail.get("code") == "SESSION_NOT_FOUND"
        else:
            assert "SESSION_NOT_FOUND" in str(detail)

    async def test_rename_session_updates_title_only(self, client, fake_redis):
        """PATCH /api/sessions/{id} → 204；GET meta → 新 title；空 title → 400 INVALID_TITLE。"""
        from app.utils.session import SessionStore

        store = SessionStore()
        session_id = await store.create()

        # 正常重命名
        resp = client.patch(
            f"/api/sessions/{session_id}",
            json={"title": "新名字"},
        )
        assert resp.status_code == 204

        # GET meta 验证
        meta_resp = client.get(f"/api/sessions/{session_id}")
        assert meta_resp.status_code == 200
        meta = meta_resp.json()
        assert meta["title"] == "新名字"

        # 空 title：Pydantic min_length=1 会先拦截为 422；即便到路由层，INVALID_TITLE 也是 400
        resp_empty = client.patch(
            f"/api/sessions/{session_id}",
            json={"title": ""},
        )
        # 接受两种：路由层 400 INVALID_TITLE，或 Pydantic 422
        assert resp_empty.status_code in (400, 422)
        if resp_empty.status_code == 400:
            body = resp_empty.json()
            detail = body.get("detail") if isinstance(body.get("detail"), dict) else body
            if isinstance(detail, dict):
                assert detail.get("code") == "INVALID_TITLE"
            else:
                assert "INVALID_TITLE" in str(detail)

    async def test_delete_session_cascades_memory(self, client, fake_redis):
        """DELETE /api/sessions/{id} → 204，session 与 memory 都清除。"""
        from app.utils.memory import MemoryStore
        from app.utils.session import SessionStore

        store = SessionStore()
        mem_store = MemoryStore()
        session_id = await store.create()
        await mem_store.remember_origin(session_id, "原点", (118.78, 32.05))

        # 删除
        resp = client.delete(f"/api/sessions/{session_id}")
        assert resp.status_code == 204

        # 再次 GET → 404
        get_resp = client.get(f"/api/sessions/{session_id}")
        assert get_resp.status_code == 404

        # memory 也没了
        memories = await mem_store.get_memories(session_id)
        assert memories == []

    async def test_get_session_messages_returns_full_history(self, client, fake_redis):
        """GET /api/sessions/{id}/messages → 200，messages 包含全部记录。"""
        from app.utils.session import SessionStore

        store = SessionStore()
        session_id = await store.create()
        await store.append_message(session_id, "user", "你好")
        await store.append_message(session_id, "assistant", "你好，有什么可以帮你？")

        resp = client.get(f"/api/sessions/{session_id}/messages")
        assert resp.status_code == 200
        body = resp.json()
        assert "messages" in body
        assert len(body["messages"]) == 2

        # 第 0 条 user, 第 1 条 assistant
        assert body["messages"][0]["role"] == "user"
        assert body["messages"][0]["content"] == "你好"
        assert body["messages"][1]["role"] == "assistant"
        assert body["messages"][1]["content"] == "你好，有什么可以帮你？"
