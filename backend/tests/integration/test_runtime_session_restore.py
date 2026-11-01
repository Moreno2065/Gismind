from fastapi.testclient import TestClient

from app.main import create_app
from app.runtime import ArtifactStore, RuntimeDB, RuntimeStore, RuntimeSupervisor


def test_session_run_discovery_is_scoped_and_includes_pending_runs(tmp_path):
    store = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    store.initialize()
    store.create_session("one")
    store.create_session("two")
    artifacts = ArtifactStore(tmp_path / "data", store)
    supervisor = RuntimeSupervisor(store, artifacts, autostart=False)
    app = create_app(runtime_store=store, artifact_store=artifacts, runtime_supervisor=supervisor)
    with TestClient(app) as client:
        run = client.post("/api/runs", json={"session_id": "one", "message": "处理上传文件", "client_request_id": "first"}).json()
        response = client.get("/api/sessions/one/runs")
        assert response.status_code == 200
        assert [item["run_id"] for item in response.json()["items"]] == [run["run_id"]]
        assert response.json()["items"][0]["status"] == "queued"
        assert response.json()["items"][0]["run_version"] == run["run_version"]
        assert client.get("/api/sessions/two/runs").json() == {"items": []}
        assert client.get("/api/sessions/missing/runs").status_code == 404
        client.delete("/api/sessions/one")
        assert client.get("/api/sessions/one/runs").status_code == 404


import pytest


@pytest.fixture
def session_client(tmp_path):
    store = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    store.initialize()
    store.create_session("kept")
    artifacts = ArtifactStore(tmp_path / "data", store)
    supervisor = RuntimeSupervisor(store, artifacts, isolate=False, autostart=False)
    with TestClient(create_app(runtime_store=store, artifact_store=artifacts, runtime_supervisor=supervisor)) as client:
        yield client, store


def test_session_rename_survives_metadata_listing_and_reopening_the_store(session_client):
    client, store = session_client
    original_version = store.get_session("kept")["session_version"]
    response = client.patch("/api/sessions/kept", json={"title": "  南京积水风险分析  "})
    assert response.status_code == 204
    assert client.get("/api/sessions/kept").json()["title"] == "南京积水风险分析"
    assert client.get("/api/sessions").json()["items"][0]["title"] == "南京积水风险分析"
    reopened = RuntimeStore(RuntimeDB(store.db.path))
    assert reopened.get_session("kept")["title"] == "南京积水风险分析"
    assert reopened.get_session("kept")["session_version"] == original_version + 1


@pytest.mark.parametrize("title", [None, "", " \t ", 12, True, [], {}, "x" * 121])
def test_invalid_session_title_never_changes_session_metadata(session_client, title):
    client, store = session_client
    before = store.get_session("kept")
    response = client.patch("/api/sessions/kept", json={"title": title})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_SESSION_TITLE"
    assert store.get_session("kept") == before


def test_deleted_session_does_not_expose_messages_or_allow_rename(session_client):
    client, store = session_client
    store.append_message("kept", "user", "历史消息")
    assert client.get("/api/sessions/kept/messages").json()["messages"][0]["content"] == "历史消息"
    assert client.delete("/api/sessions/kept").status_code == 204
    before = store.get_session("kept")
    assert client.get("/api/sessions/kept/messages").status_code == 404
    assert client.patch("/api/sessions/kept", json={"title": "not allowed"}).status_code == 404
    assert store.get_session("kept") == before


def test_session_title_upgrade_preserves_v11_data_and_is_repeatable(tmp_path):
    from app.runtime.migrations import MIGRATIONS
    path = tmp_path / "legacy.db"
    legacy = RuntimeStore(RuntimeDB(path, migrations={v: m for v, m in MIGRATIONS.items() if v <= 11}))
    legacy.initialize()
    legacy.create_session("old")
    legacy.append_message("old", "user", "保留消息")
    previous = legacy.get_session("old")
    upgraded = RuntimeStore(RuntimeDB(path))
    upgraded.initialize()
    assert upgraded.get_session("old")["title"] == "新会话"
    assert {key: upgraded.get_session("old")[key] for key in previous} == previous
    upgraded.rename_session("old", "迁移后标题")
    upgraded.initialize()
    assert upgraded.get_session("old")["title"] == "迁移后标题"
    assert upgraded._read(lambda c: c.execute("SELECT content FROM messages").fetchone()[0]) == "保留消息"
