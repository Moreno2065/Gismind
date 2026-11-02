import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.runtime import ArtifactStore, RuntimeDB, RuntimeStore, RuntimeSupervisor


@pytest.fixture
def history_client(tmp_path):
    store = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    store.initialize()
    store.create_session("history")
    store.create_session("other")
    with store.transaction():
        for index in range(201):
            store.create_run(f"run_{index:04d}", "history")
        store.create_run("foreign", "other")
        store._write(lambda conn: conn.execute("UPDATE runs SET status='completed',created_at='2026-11-02T00:00:00Z'"))
    artifacts = ArtifactStore(tmp_path / "data", store)
    supervisor = RuntimeSupervisor(store, artifacts, autostart=False)
    with TestClient(create_app(runtime_store=store, artifact_store=artifacts, runtime_supervisor=supervisor)) as client:
        yield client, store


def test_run_history_pages_recover_the_oldest_run_even_when_timestamps_tie(history_client):
    client, _store = history_client
    first = client.get("/api/sessions/history/runs").json()
    assert len(first["items"]) == 200
    assert first["next_cursor"] == "run_0001"
    last = client.get("/api/sessions/history/runs", params={"before": first["next_cursor"]}).json()
    assert [row["run_id"] for row in last["items"]] == ["run_0000"]
    assert last.get("next_cursor") is None
    assert len({row["run_id"] for row in first["items"] + last["items"]}) == 201


def test_new_run_insertion_does_not_shift_history_page_boundaries(history_client):
    client, store = history_client
    first = client.get("/api/sessions/history/runs").json()
    store.create_run("newest", "history")
    last = client.get("/api/sessions/history/runs", params={"before": first["next_cursor"]}).json()
    assert [row["run_id"] for row in last["items"]] == ["run_0000"]


@pytest.mark.parametrize("cursor", ["foreign", "missing", ""])
def test_history_cursor_must_belong_to_the_requested_session(history_client, cursor):
    client, _store = history_client
    response = client.get("/api/sessions/history/runs", params={"before": cursor})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_RUN_CURSOR"
