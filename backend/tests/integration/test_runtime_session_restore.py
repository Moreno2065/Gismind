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
