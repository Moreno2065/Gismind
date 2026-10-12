"""Phase four public Runtime API tests.

These tests use only temporary SQLite/CAS state and never exercise the legacy
blackbox/golden or Dispatcher API.
"""

from __future__ import annotations

import pytest
import io
import zipfile
from fastapi.testclient import TestClient

from app.runtime import ArtifactRef, ArtifactStore, Compiler, PlannerDraft, PlannerStepDraft, RuntimeDB, RuntimeStore, RuntimeSupervisor


class EmptyPlanner:
    """API contract tests do not evaluate LLM quality; they avoid network I/O."""

    def create_draft(self, request):
        return PlannerDraft(
            run_id=request.run_id, input_revision=request.input_revision,
            user_goal=request.user_goal, origin="api-contract-test", tasks=[],
        )


def _app(tmp_path):
    from app.main import create_app

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s-phase4")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    supervisor = RuntimeSupervisor(runtime, artifacts, isolate=False, planner=EmptyPlanner(), autostart=False)
    return create_app(runtime_store=runtime, artifact_store=artifacts, runtime_supervisor=supervisor)


@pytest.fixture
def client(tmp_path):
    with TestClient(_app(tmp_path)) as value:
        yield value


def test_create_run_is_idempotent_and_persists_created_event(client):
    request = {
        "session_id": "s-phase4",
        "message": "hello runtime",
        "client_request_id": "client-1",
    }
    first = client.post("/api/runs", json=request)
    assert first.status_code == 202
    payload = first.json()
    assert payload["status"] in {"queued", "ready"}
    assert payload["events_url"].endswith(f"/api/runs/{payload['run_id']}/events")

    second = client.post("/api/runs", json=request)
    assert second.status_code == 202
    assert second.json()["run_id"] == payload["run_id"]

    conflict = client.post(
        "/api/runs",
        json={**request, "message": "different"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "CLIENT_REQUEST_CONFLICT"

    client.post(
        f"/api/runs/{payload['run_id']}/cancel",
        json={"command_id": "cancel-before-replay", "expected_run_version": payload["run_version"]},
    )
    events = client.get(f"/api/runs/{payload['run_id']}/events")
    assert events.status_code == 200
    assert "id:" in events.text
    assert "event: run.created" in events.text


def test_health_reports_runtime_store_worker_and_queue_facts(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["checks"]["runtime_store"]["status"] == "ok"
    assert payload["checks"]["worker"]["running"] is False
    assert payload["checks"]["queue"] == {
        "queued_runs": 0,
        "planning_runs": 0,
        "ready_tasks": 0,
        "active_tasks": 0,
        "retry_wait_tasks": 0,
    }


def test_message_only_run_is_accepted_for_durable_planning(client):
    response = client.post("/api/runs", json={
        "session_id": "s-phase4", "message": "planner input missing", "client_request_id": "client-no-draft",
    })
    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "queued"
    events = client.app.state.runtime_store.list_events(payload["run_id"])
    assert [event["event_type"] for event in events] == ["run.created"]


def test_run_snapshot_has_tasks_outputs_and_cursor(client):
    created = client.post(
        "/api/runs",
        json={
            "session_id": "s-phase4",
            "message": "empty plan",
            "client_request_id": "client-snapshot",
        },
    ).json()
    response = client.get(f"/api/runs/{created['run_id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == created["run_id"]
    assert body["session_id"] == "s-phase4"
    assert isinstance(body["tasks"], list)
    assert isinstance(body["output_artifacts"], list)
    assert isinstance(body["last_event_id"], int)
    assert isinstance(body["last_seq"], int)


def test_events_replay_after_event_id_and_rejects_foreign_cursor(client, tmp_path):
    created = client.post(
        "/api/runs",
        json={
            "session_id": "s-phase4",
            "message": "event replay",
            "client_request_id": "client-events",
        },
    ).json()
    run_id = created["run_id"]
    runtime = client.app.state.runtime_store
    event = runtime.append_event(run_id, "run.thought", {"message": "one"})
    runtime.append_event(run_id, "run.cancel_requested", {"run_version": 1})
    client.post(
        f"/api/runs/{run_id}/cancel",
        json={"command_id": "event-cancel", "expected_run_version": client.get(f"/api/runs/{run_id}").json()["run_version"]},
    )

    replay = client.get(f"/api/runs/{run_id}/events?after_event_id={event['event_id']}")
    assert replay.status_code == 200
    assert "run.cancel_requested" in replay.text
    assert "run.cancelled" in replay.text
    assert "run.thought" not in replay.text

    mismatch = client.get(
        f"/api/runs/{run_id}/events",
        headers={"Last-Event-ID": str(client.get(f"/api/runs/{run_id}").json()["last_event_id"] + 1)},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "INVALID_EVENT_CURSOR"


def test_cancel_is_idempotent_and_emits_terminal_event(client):
    created = client.post(
        "/api/runs",
        json={
            "session_id": "s-phase4",
            "message": "cancel me",
            "client_request_id": "client-cancel",
        },
    ).json()
    run_id = created["run_id"]
    request = {"command_id": "cancel-command", "expected_run_version": created["run_version"]}
    cancelled = client.post(f"/api/runs/{run_id}/cancel", json=request)
    assert cancelled.status_code in {200, 202}
    assert cancelled.json()["status"] in {"cancelling", "cancelled"}
    repeated = client.post(f"/api/runs/{run_id}/cancel", json=request)
    assert repeated.status_code in {200, 202}
    assert repeated.json()["run_id"] == run_id
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "cancelled"


def test_artifact_metadata_and_content_do_not_expose_storage_uri(client, tmp_path):
    artifacts = client.app.state.artifact_store
    source = artifacts.publish_staged(
        artifacts.stage_bytes(b'{"hello":"world"}', filename="result.json"),
        origin="upload",
        artifact_type="File",
        media_type="application/json",
    )
    client.post(
        "/api/runs",
        json={
            "session_id": "s-phase4",
            "message": "consume file",
            "input_artifact_ids": [source["artifact_id"]],
            "client_request_id": "client-artifact",
        },
    ).json()
    metadata = client.get(f"/api/artifacts/{source['artifact_id']}")
    assert metadata.status_code == 200
    body = metadata.json()
    assert body["artifact_id"] == source["artifact_id"]
    assert "storage_uri" not in body
    assert body["content_url"].endswith(f"/api/artifacts/{source['artifact_id']}/content")
    content = client.get(f"/api/artifacts/{source['artifact_id']}/content")
    assert content.status_code == 200
    assert content.content == b'{"hello":"world"}'


def test_artifact_content_supports_suffix_range(client):
    artifacts = client.app.state.artifact_store
    source = artifacts.publish_staged(
        artifacts.stage_bytes(b"0123456789", filename="range.bin"),
        origin="upload", artifact_type="File", media_type="application/octet-stream",
    )
    created = client.post("/api/runs", json={
        "session_id": "s-phase4", "message": "range", "input_artifact_ids": [source["artifact_id"]],
        "client_request_id": "client-range",
    }).json()
    response = client.get(f"/api/artifacts/{source['artifact_id']}/content", headers={"Range": "bytes=-3"})
    assert response.status_code == 206
    assert response.content == b"789"
    assert response.headers["content-range"] == "bytes 7-9/10"


def test_command_id_cannot_be_reused_across_runs(client):
    request = {"command_id": "shared-command", "expected_run_version": 1}
    first = client.post("/api/runs", json={"session_id": "s-phase4", "message": "one", "client_request_id": "command-run-1"}).json()
    second = client.post("/api/runs", json={"session_id": "s-phase4", "message": "two", "client_request_id": "command-run-2"}).json()
    client.post(f"/api/runs/{first['run_id']}/cancel", json=request | {"expected_run_version": first["run_version"]})
    reused = client.post(f"/api/runs/{second['run_id']}/cancel", json=request | {"expected_run_version": second["run_version"]})
    assert reused.status_code == 409
    assert reused.json()["error"]["code"] == "COMMAND_ID_CONFLICT"


def test_upload_artifact_is_persisted_as_file_and_downloadable(client):
    response = client.post(
        "/api/artifacts/uploads",
        files={"file": ("sample.geojson", b'{"type":"FeatureCollection","features":[]}', "application/geo+json")},
        data={"declared_crs": '{"kind":"EPSG","code":4326}'},
    )
    assert response.status_code == 201
    artifact = response.json()
    assert artifact["type"] == "File"
    assert "storage_uri" not in artifact
    assert client.get(artifact["content_url"]).status_code in {200, 206}


def test_upload_artifact_persists_wrapped_file_metadata_and_declared_bbox(client):
    response = client.post(
        "/api/artifacts/uploads",
        files={"file": ("sample.geojson", b'{"type":"FeatureCollection","features":[]}', "application/geo+json")},
        data={
            "declared_crs": '{"kind":"EPSG","code":4326}',
            "declared_bbox": "[116.2,39.7,116.6,40.1]",
        },
    )
    assert response.status_code == 201, response.text
    artifact = response.json()
    assert artifact["crs"] == {"kind": "EPSG", "code": 4326}
    assert artifact["schema"]["file"] == {
        "filename": "sample.geojson",
        "media_type": "application/geo+json",
        "extension": ".geojson",
        "download_disposition": "attachment",
        "declared_crs": {"value": {"kind": "EPSG", "code": 4326}, "source": "user"},
        "declared_bbox": {"value": [116.2, 39.7, 116.6, 40.1], "source": "user"},
    }


def test_upload_artifact_rejects_invalid_declared_metadata(client):
    invalid_crs = client.post(
        "/api/artifacts/uploads",
        files={"file": ("sample.geojson", b"{}", "application/geo+json")},
        data={"declared_crs": '{"kind":"EPSG","code":0}'},
    )
    assert invalid_crs.status_code == 422
    assert invalid_crs.json()["error"]["code"] == "ARTIFACT_INVALID"

    invalid_bbox = client.post(
        "/api/artifacts/uploads",
        files={"file": ("sample.geojson", b"{}", "application/geo+json")},
        data={"declared_bbox": "[1,2,0,3]"},
    )
    assert invalid_bbox.status_code == 422
    assert invalid_bbox.json()["error"]["code"] == "ARTIFACT_INVALID"


def test_upload_artifact_enforces_streaming_size_limit(client, monkeypatch):
    from app.api import runtime as runtime_api

    monkeypatch.setattr(runtime_api.settings, "UPLOAD_MAX_SIZE", 1)
    response = client.post(
        "/api/artifacts/uploads",
        files={"file": ("oversized.bin", b"x" * (1024 * 1024 + 1), "application/octet-stream")},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "ARTIFACT_TOO_LARGE"


def test_upload_artifact_rejects_zip_path_traversal(client):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("../escape.geojson", "{}")
    response = client.post(
        "/api/artifacts/uploads",
        files={"file": ("unsafe.zip", payload.getvalue(), "application/zip")},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ARTIFACT_INVALID"


def test_awaiting_input_snapshot_and_resume_patch_validation(client):
    # Build this state through the Runtime store rather than retaining the
    # removed public draft-injection API.  The resume endpoint only owns
    # validation and durable revision recording.
    runtime = client.app.state.runtime_store
    runtime.create_run("run-awaiting", "s-phase4")
    runtime.add_input_revision("run-awaiting", 1, {"prompt": "load file", "input_artifact_ids": []})
    runtime.update_run_status("run-awaiting", "planning")
    runtime.append_event("run-awaiting", "run.awaiting_input", {
        "code": "CRS_REQUIRED", "diagnostic": {"code": "CRS_REQUIRED"},
        "patch_schema": {"type": "object", "required": ["crs"]},
    })
    runtime.update_run_status("run-awaiting", "awaiting_input")
    snapshot = client.get("/api/runs/run-awaiting").json()
    assert snapshot["status"] == "awaiting_input"
    assert snapshot["diagnostic"]["code"] == "CRS_REQUIRED"
    invalid = client.post("/api/runs/run-awaiting/resume", json={
        "command_id": "resume-invalid", "expected_run_version": snapshot["run_version"], "patch": {},
    })
    assert invalid.status_code == 422, invalid.text
    assert invalid.json()["error"]["code"] == "PATCH_SCHEMA_INVALID"


def test_confirm_releases_only_the_compiler_hashed_side_effect_task(client, tmp_path):
    """The public confirmation path cannot be replaced by a prompt or task name."""
    runtime = client.app.state.runtime_store
    artifacts = client.app.state.artifact_store
    supervisor = client.app.state.runtime_supervisor
    source = artifacts.publish_staged(
        artifacts.stage_bytes(b"report", filename="report.txt"), origin="upload",
        artifact_type="File", media_type="text/plain",
    )
    runtime.create_run("run-confirm", "s-phase4")
    claim = supervisor.queue.claim_planning_run("confirm-planner", run_id="run-confirm")
    assert claim is not None
    output_root = tmp_path / "exports"
    output_root.mkdir()
    draft = PlannerDraft(
        run_id="run-confirm", input_revision=1, user_goal="publish report", tasks=[
            PlannerStepDraft(
                task_key="publish", op="publish_file",
                inputs={"input": ArtifactRef(
                    artifact_id=source["artifact_id"], type="File", crs=None,
                    content_hash=source["content_hash"],
                )},
                params={"path": "report.txt"},
            )
        ],
    )
    plan = Compiler(
        supervisor.registry, artifact_lookup=runtime.get_artifact,
        policy={"allowed_path_root": str(output_root)},
    ).compile(draft)
    activated = runtime.activate_compiled_plan("run-confirm", plan, planner_claim=claim)
    task_id = activated["task_ids"]["publish"]
    task = runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone())
    assert task[0] == "blocked"
    confirmation = runtime._read(lambda c: c.execute("SELECT action_hash,status FROM task_confirmations WHERE task_id=?", (task_id,)).fetchone())
    assert confirmation[1] == "pending"
    snapshot = client.get("/api/runs/run-confirm").json()

    mismatch = client.post("/api/runs/run-confirm/confirm", json={
        "command_id": "confirm-mismatch", "expected_run_version": snapshot["run_version"],
        "task_id": task_id, "action_hash": "0" * 64, "confirmation": True,
    })
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "ACTION_HASH_MISMATCH"

    request = {
        "command_id": "confirm-good", "expected_run_version": snapshot["run_version"],
        "task_id": task_id, "action_hash": confirmation[0], "confirmation": True,
    }
    accepted = client.post("/api/runs/run-confirm/confirm", json=request)
    assert accepted.status_code == 202, accepted.text
    assert client.get("/api/runs/run-confirm").json()["tasks"][0]["status"] == "ready"
    repeated = client.post("/api/runs/run-confirm/confirm", json=request)
    assert repeated.status_code == 202
    # Confirmation is not merely a status transition: the exact compiler-
    # confined destination is atomically materialized by the isolated Task.
    assert supervisor.run_once("confirmation-worker", run_id="run-confirm") is not None
    assert (output_root / "report.txt").read_bytes() == b"report"
    assert client.get("/api/runs/run-confirm").json()["tasks"][0]["status"] == "succeeded"
