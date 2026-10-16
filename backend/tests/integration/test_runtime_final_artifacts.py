"""Final output identity is derived from the same durable Run snapshot."""
from fastapi.testclient import TestClient

from app.runtime import ArtifactStore, RuntimeDB, RuntimeStore, RuntimeSupervisor


def fixture(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s")
    runtime.create_run("r", "s")
    runtime.create_plan("old", "r", plan_version=1)
    runtime.create_plan("active", "r", plan_version=2)
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    ids = {}
    operations = {
        "load": "io.load_vector", "buffer": "vector.buffer", "export": "file.export",
        "map": "map.build", "after_delivery": "demo.inspect", "validating": "demo.validating",
        "failed": "demo.failed", "historical": "demo.old",
    }
    for task_id, op in operations.items():
        runtime.create_task(task_id, "old" if task_id == "historical" else "active", task_id, tool_name=op)
        port = "file" if task_id == "export" else "layer" if task_id == "map" else "output"
        value = artifacts.publish_staged(artifacts.stage_bytes(task_id.encode()), origin="task",
                                         producer_task_id=task_id, output_name=port)
        ids[task_id] = value["artifact_id"]
    runtime.add_task_dependency("buffer", "load")
    runtime.add_task_dependency("export", "buffer")
    runtime.add_task_dependency("map", "buffer")
    runtime.add_task_dependency("after_delivery", "export")
    runtime.add_task_dependency("after_delivery", "map")
    runtime._write(lambda c: c.execute("UPDATE tasks SET status='succeeded'"))
    runtime._write(lambda c: c.execute("UPDATE tasks SET status='failed' WHERE task_id='failed'"))
    runtime._write(lambda c: c.execute("UPDATE tasks SET status='validating' WHERE task_id='validating'"))
    runtime._write(lambda c: c.execute("UPDATE runs SET active_plan_id='active' WHERE run_id='r'"))
    return runtime, artifacts, ids


def test_snapshot_final_ids_include_valid_leaves_and_named_delivery_outputs_only(tmp_path, monkeypatch):
    runtime, _artifacts, ids = fixture(tmp_path)

    def no_separate_read(*args):
        raise AssertionError("snapshot components must share the existing read transaction")

    monkeypatch.setattr(runtime, "_read", no_separate_read)
    snapshot = runtime.get_run_snapshot("r")
    assert set(snapshot["final_artifact_ids"]) == {ids["export"], ids["map"], ids["after_delivery"]}
    assert ids["load"] in {a["artifact_id"] for a in snapshot["output_artifacts"]}


def test_run_api_exposes_final_ids_from_snapshot(tmp_path):
    from app.main import create_app

    runtime, artifacts, ids = fixture(tmp_path)
    supervisor = RuntimeSupervisor(runtime, artifacts, planner=object(), autostart=False)
    with TestClient(create_app(runtime_store=runtime, artifact_store=artifacts, runtime_supervisor=supervisor)) as client:
        result = client.get("/api/runs/r")
    assert result.status_code == 200
    assert set(result.json()["final_artifact_ids"]) == {ids["export"], ids["map"], ids["after_delivery"]}


def test_snapshot_without_active_plan_has_no_final_artifacts(tmp_path):
    runtime, _artifacts, _ids = fixture(tmp_path)
    runtime._write(lambda c: c.execute("UPDATE runs SET active_plan_id=NULL WHERE run_id='r'"))
    assert runtime.get_run_snapshot("r")["final_artifact_ids"] == []
