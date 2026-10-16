"""Real temporary Runtime fixtures; these checks are not live LLM evidence."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(next(parent / 'backend' for parent in ROOT.parents
    if (parent / 'backend/app').is_dir())))


def test_bootstrap_exists():
    assert (ROOT / "history_readonly_server.py").is_file(), "isolated observer bootstrap is not implemented"


@pytest.fixture
def observer():
    path = ROOT / "history_readonly_server.py"
    assert path.is_file(), "isolated observer bootstrap is not implemented"
    spec = importlib.util.spec_from_file_location("history_readonly_server", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def history(tmp_path):
    from app.runtime import ArtifactStore, RuntimeDB, RuntimeStore
    original = tmp_path / "original_shard"
    original.mkdir()
    runtime = RuntimeStore(RuntimeDB(original / "runtime.db"))
    runtime.initialize()
    runtime.create_session("session_history")
    runtime.append_message("session_history", "user", "Historical fixture")
    runtime.create_run("run_history", "session_history")
    runtime.create_plan("plan_history", "run_history")
    runtime.create_task("task_history", "plan_history", "map", tool_name="map.build")
    # Match the actual cohort server: shard/runtime.db and shard/cas/artifacts.
    artifacts = ArtifactStore(original / "cas", runtime)
    payload = json.dumps({"layers": [{"id": "history", "type": "point", "data": [{"lng": 118.78, "lat": 32.04}]}], "bbox": [118.78, 32.04, 118.78, 32.04]}).encode()
    artifact = artifacts.publish_staged(artifacts.stage_bytes(payload), origin="task", producer_task_id="task_history", output_name="layer", artifact_type="MapLayer", media_type="application/json", crs={"kind": "WGS84"})
    runtime._write(lambda c: c.execute("UPDATE tasks SET status='succeeded'"))
    runtime._write(lambda c: c.execute("UPDATE runs SET active_plan_id='plan_history',status='completed'"))
    runtime.append_event("run_history", "run.completed", {"final_artifact_ids": [artifact["artifact_id"]]})
    return original, artifact, payload


def files_hash(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in root.rglob("*") if path.is_file()}


def test_clone_preserves_bytes_and_original_state(observer, history, tmp_path):
    original, artifact, _payload = history
    before = files_hash(original)
    clone = tmp_path / "clone"
    receipt = observer.prepare_clone(original, clone)
    assert receipt["copied_bytes_identical"] and receipt["original_bytes_unchanged"]
    assert receipt["source_files"] == receipt["copied_files"]
    assert receipt["logical_identity"] == observer.logical_identity(clone / "runtime.db")
    assert files_hash(original) == before
    assert (clone / "cas" / "artifacts" / "sha256" / artifact["content_hash"][:2] / artifact["content_hash"]).is_file()
    assert not (clone / "artifacts").exists()
    assert artifact["storage_uri"] == f"artifacts/sha256/{artifact['content_hash'][:2]}/{artifact['content_hash']}"


def test_clone_rejects_incorrect_root_artifacts_layout(observer, history, tmp_path):
    import shutil
    wrong = tmp_path / "wrong_layout"
    wrong.mkdir()
    shutil.copyfile(history[0] / "runtime.db", wrong / "runtime.db")
    shutil.copytree(history[0] / "cas" / "artifacts", wrong / "artifacts")
    with pytest.raises(ValueError, match="cas/artifacts"):
        observer.prepare_clone(wrong, tmp_path / "rejected_clone")


def test_clone_rejects_unsafe_targets(observer, history, tmp_path):
    for target in (ROOT.parent / "outside_clone", history[0] / "nested"):
        with pytest.raises(ValueError):
            observer.prepare_clone(history[0], target)
    target = tmp_path / "existing"
    target.mkdir()
    with pytest.raises(ValueError, match="exist"):
        observer.prepare_clone(history[0], target)


def test_lifespan_gets_restore_history_without_worker_or_mutations(observer, history, tmp_path):
    original, artifact, payload = history
    before = files_hash(original)
    clone = tmp_path / "clone"
    receipt = observer.prepare_clone(original, clone)
    app = observer.create_history_app(clone)
    with TestClient(app) as client:
        supervisor = app.state.runtime_supervisor
        assert not supervisor.autostart and not supervisor.health_snapshot()["running"]
        assert supervisor._worker_thread is None
        assert isinstance(supervisor.planner, observer.ForbiddenPlanner)
        assert client.get("/api/sessions").json()["items"][0]["id"] == "session_history"
        assert client.get("/api/sessions/session_history").status_code == 200
        assert client.get("/api/sessions/session_history/messages").json()["messages"][0]["content"] == "Historical fixture"
        assert client.get("/api/sessions/session_history/runs").json()["items"][0]["run_id"] == "run_history"
        assert artifact["artifact_id"] in client.get("/api/runs/run_history").json()["final_artifact_ids"]
        assert "event: run.completed" in client.get("/api/runs/run_history/events").text
        assert client.get(f"/api/artifacts/{artifact['artifact_id']}").json()["content_hash"] == artifact["content_hash"]
        assert client.get(f"/api/artifacts/{artifact['artifact_id']}/content").content == payload
        assert client.get("/api/health").status_code == 200
        assert supervisor.planner.calls == 0
    assert observer.logical_identity(clone / "runtime.db") == receipt["logical_identity"]
    assert files_hash(original) == before


@pytest.mark.parametrize("method,path", [("POST", "/api/sessions"), ("PATCH", "/api/sessions/session_history"), ("DELETE", "/api/sessions/session_history"), ("POST", "/api/runs"), ("POST", "/api/runs/run_history/resume"), ("POST", "/api/runs/run_history/cancel"), ("POST", "/api/runs/run_history/recover"), ("POST", "/api/artifacts/uploads"), ("GET", "/docs"), ("GET", "/api/unknown"), ("OPTIONS", "/api/runs/run_history/recover")])
def test_writes_and_unlisted_routes_are_denied(observer, history, tmp_path, method, path):
    clone = tmp_path / "clone"
    receipt = observer.prepare_clone(history[0], clone)
    app = observer.create_history_app(clone)
    with TestClient(app) as client:
        response = client.request(method, path, json={"message": "never plan", "api_key": "secret_not_in_error"})
        assert response.status_code in (403, 405)
        assert response.json()["error"]["code"] == "INDEPENDENT_HISTORY_READONLY"
        assert "secret_not_in_error" not in response.text
        assert app.state.runtime_supervisor.planner.calls == 0
    assert observer.logical_identity(clone / "runtime.db") == receipt["logical_identity"]


def test_forbidden_planner_is_fail_closed(observer):
    planner = observer.ForbiddenPlanner()
    with pytest.raises(observer.ObserverPolicyError, match="forbidden"):
        planner(object())
    assert planner.calls == 1


def test_outbound_http_is_blocked_before_io(observer, history, tmp_path):
    clone = tmp_path / "clone"
    observer.prepare_clone(history[0], clone)
    with TestClient(observer.create_history_app(clone)):
        with pytest.raises(observer.ObserverPolicyError, match="outbound"):
            httpx.get("https://provider.invalid/chat/completions", headers={"Authorization": "secret_never_logged"})


def test_factory_uses_explicit_clone_root(observer, history, tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    observer.prepare_clone(history[0], clone)
    monkeypatch.setenv("GISMIND_HISTORY_CLONE_ROOT", str(clone))
    assert observer.factory().state.history_clone_root == clone.resolve()


def test_worker_entrypoints_refuse_execution(observer, history, tmp_path):
    clone = tmp_path / "clone"
    observer.prepare_clone(history[0], clone)
    app = observer.create_history_app(clone)
    supervisor = app.state.runtime_supervisor
    with TestClient(app):
        for name, args in (("run_once", ("observer",)), ("start", ("observer",)), ("worker_loop", ("observer",)), ("enqueue_run", ("run_history",)), ("recover_expired_leases", ()), ("recover_cancelling_runs", ())):
            with pytest.raises(observer.ObserverPolicyError, match="worker"):
                getattr(supervisor, name)(*args)
        assert supervisor._worker_thread is None
        assert supervisor.planner.calls == 0


def test_head_and_options_do_not_mutate_facts(observer, history, tmp_path):
    clone = tmp_path / "clone"
    receipt = observer.prepare_clone(history[0], clone)
    with TestClient(observer.create_history_app(clone)) as client:
        assert client.head("/api/runs/run_history").status_code == 200
        assert client.head("/api/runs/run_history").content == b""
        assert client.options("/api/runs/run_history").status_code == 204
    assert observer.logical_identity(clone / "runtime.db") == receipt["logical_identity"]


def test_clone_startup_rejects_changed_database(observer, history, tmp_path):
    import sqlite3
    clone = tmp_path / "clone"
    observer.prepare_clone(history[0], clone)
    with sqlite3.connect(clone / "runtime.db") as conn:
        conn.execute("UPDATE messages SET content='tampered'")
    with pytest.raises(observer.ObserverPolicyError, match="logical identity"):
        observer.create_history_app(clone)


def test_clone_startup_rejects_changed_cas(observer, history, tmp_path):
    clone = tmp_path / "clone"
    observer.prepare_clone(history[0], clone)
    artifact = history[1]
    cas = clone / "cas" / "artifacts" / "sha256" / artifact["content_hash"][:2] / artifact["content_hash"]
    cas.write_bytes(b"tampered fixture")
    with pytest.raises(observer.ObserverPolicyError, match="CAS identity"):
        observer.create_history_app(clone)


def test_retained_wal_is_copied_and_used_for_logical_identity(observer, history, tmp_path):
    import sqlite3
    original = history[0]
    with sqlite3.connect(original / "runtime.db") as conn:
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("INSERT INTO messages(message_id,session_id,role,content,message_index,metadata_json,created_at) VALUES ('retained_wal','session_history','user','WAL-only fixture',2,'{}','2026-10-14T00:00:00Z')")
        conn.commit()
        before = files_hash(original)
        assert (original / "runtime.db-wal").stat().st_size > 0
        receipt = observer.prepare_clone(original, tmp_path / "clone")
        assert "runtime.db-wal" in receipt["source_files"]
        assert "runtime.db-shm" in receipt["source_files"]
        assert receipt["logical_identity"]["tables"]["messages"]["rows"] == 2
        assert files_hash(original) == before


@pytest.fixture
def history_501(history):
    import sqlite3
    original = history[0]
    with sqlite3.connect(original / "runtime.db") as conn:
        conn.execute("UPDATE sessions SET created_at='2026-01-01T00:00:00Z',updated_at='2026-01-01T00:00:00Z' WHERE session_id='session_history'")
        conn.executemany("INSERT INTO sessions(session_id,created_at,updated_at) VALUES (?,?,?)", [(f"session_new_{index:03d}", f"2026-10-14T00:00:00.{index:06d}Z", f"2026-10-14T00:00:00.{index:06d}Z") for index in range(500)])
    conn.close()  # Context-manager exit commits; explicitly close before sealing.
    return history


def test_observer_header_makes_old_501st_session_reachable(observer, history_501, tmp_path):
    original = history_501[0]
    before = files_hash(original)
    clone = tmp_path / "clone"
    receipt = observer.prepare_clone(original, clone)
    app = observer.create_history_app(clone)
    with TestClient(app) as client:
        ordinary = client.get("/api/sessions").json()["items"]
        assert len(ordinary) == 200
        assert all(item["id"] != "session_history" for item in ordinary)
        response = client.get("/api/sessions", headers={"X-History-Observer-Session": "session_history"})
        items = response.json()["items"]
        assert items[0] == {"id": "session_history", "title": "新会话", "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}
        assert items[1:] == ordinary
        assert len({item["id"] for item in items}) == len(items) == 201
        assert response.headers["X-History-Observer-Policy"] == "history-readonly-observer-v2"
        assert response.headers["X-History-Observer-Session"] == "session_history"
        assert app.state.history_observer_policy["title_basis"] == "stock_constant_no_persisted_title"
        assert app.state.runtime_supervisor.planner.calls == 0
    assert observer.logical_identity(clone / "runtime.db") == receipt["logical_identity"]
    assert files_hash(original) == before


def test_no_observer_header_keeps_stock_recent200_response(observer, history_501, tmp_path):
    from app.main import create_app
    clone = tmp_path / "clone"
    observer.prepare_clone(history_501[0], clone)
    app = observer.create_history_app(clone)
    stock = create_app(runtime_store=app.state.runtime_store, artifact_store=app.state.artifact_store, runtime_supervisor=app.state.runtime_supervisor)
    with TestClient(stock) as client:
        expected = client.get("/api/sessions").json()
    with TestClient(app) as client:
        result = client.get("/api/sessions")
        assert result.json() == expected
        assert len(result.json()["items"]) == 200
        assert "X-History-Observer-Session" not in result.headers


def test_observer_header_deduplicates_recent_session(observer, history_501, tmp_path):
    clone = tmp_path / "clone"
    observer.prepare_clone(history_501[0], clone)
    with TestClient(observer.create_history_app(clone)) as client:
        result = client.get("/api/sessions", headers={"X-History-Observer-Session": "session_new_499"})
        items = result.json()["items"]
        assert len(items) == 200
        assert sum(item["id"] == "session_new_499" for item in items) == 1


@pytest.mark.parametrize("target", ["session_missing", "session_new_001"])
def test_observer_header_unknown_or_deleted_is_explicit_404(observer, history_501, tmp_path, target):
    import sqlite3
    with sqlite3.connect(history_501[0] / "runtime.db") as conn:
        conn.execute("UPDATE sessions SET deleted_at='2026-10-14T00:00:00Z' WHERE session_id='session_new_001'")
    conn.close()
    clone = tmp_path / "clone"
    receipt = observer.prepare_clone(history_501[0], clone)
    with TestClient(observer.create_history_app(clone)) as client:
        result = client.get("/api/sessions", headers={"X-History-Observer-Session": target})
        assert result.status_code == 404
        assert result.json()["error"]["code"] == "HISTORY_OBSERVER_SESSION_NOT_FOUND"
    assert observer.logical_identity(clone / "runtime.db") == receipt["logical_identity"]


def test_observer_header_only_affects_exact_session_list_get(observer, history, tmp_path):
    clone = tmp_path / "clone"
    observer.prepare_clone(history[0], clone)
    with TestClient(observer.create_history_app(clone)) as client:
        for path in ("/api/sessions/session_history", "/api/sessions/session_history/messages", "/api/sessions/session_history/runs", "/api/runs/run_history"):
            expected = client.get(path)
            result = client.get(path, headers={"X-History-Observer-Session": "session_missing"})
            assert result.status_code == expected.status_code == 200
            assert result.json() == expected.json()
            assert "X-History-Observer-Session" not in result.headers
        result = client.post("/api/sessions", headers={"X-History-Observer-Session": "session_history"})
        assert result.status_code == 405
        assert result.json()["error"]["code"] == "INDEPENDENT_HISTORY_READONLY"


@pytest.mark.parametrize("target", ["", "bad/session", "has space", "s" * 257])
def test_observer_selector_rejects_malformed_header(observer, history, tmp_path, target):
    clone = tmp_path / "clone"
    observer.prepare_clone(history[0], clone)
    with TestClient(observer.create_history_app(clone)) as client:
        result = client.get("/api/sessions", headers={"X-History-Observer-Session": target})
        assert result.status_code == 400
        assert result.json()["error"]["code"] == "HISTORY_OBSERVER_INVALID_SESSION"
