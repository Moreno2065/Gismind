"""Injected provider transcripts, followed by the real upload/Worker/CAS path."""
import copy
import json

import pytest

from app.runtime import ArtifactRef, PlannerDraft, PlannerStepDraft, RuntimeSupervisor
from app.runtime.llm_planner import OpenAICompatiblePlanner
from tests.integration.test_runtime_upload_formats import client, _geojson, _upload


def compatibility_worker(client, monkeypatch, *, fallback_status, max_calls=3, retries=1):
    uploaded = _upload(client, "points.geojson", _geojson()).json()
    draft = PlannerDraft(tasks=[PlannerStepDraft(
        task_key="load", op="io.load_vector",
        inputs={"file": ArtifactRef(artifact_id=uploaded["artifact_id"], type="File")},
    )])
    posted = []

    class Response:
        def __init__(self, status, body):
            self.status_code, self.body = status, body

        def json(self):
            return self.body

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, endpoint, *, json, headers):
            posted.append(copy.deepcopy(json))
            if len(posted) == 1:
                return Response(200, {"choices": [{"message": {"content": "", "reasoning_content": "not a plan"}}]})
            if len(posted) == 2:
                return Response(fallback_status, {})
            assert len(posted) == 3, "the bounded transcript must not perform extra requests"
            return Response(200, {"choices": [{"message": {"content": draft.model_dump_json()}, "finish_reason": "stop"}], "usage": {"total_tokens": 9}})

    monkeypatch.setattr("app.runtime.llm_planner.httpx.AsyncClient", Client)
    planner = OpenAICompatiblePlanner(
        api_key="test-only", base_url="https://provider.invalid/v1", model="test-model",
        timeout_seconds=2, max_tokens=2048, max_retries=retries, max_http_calls=max_calls,
    )
    store, artifacts = client.app.state.runtime_store, client.app.state.artifact_store
    worker = RuntimeSupervisor(store, artifacts, planner=planner, isolate=True, autostart=False)
    response = client.post("/api/runs", json={
        "session_id": "uploads", "message": "读取上传的矢量文件", "input_artifact_ids": [uploaded["artifact_id"]],
        "client_request_id": "reasoning-compatibility",
    })
    assert response.status_code == 202, response.text
    return worker, response.json()["run_id"], posted


@pytest.mark.parametrize("status", [429, 503])
def test_reasoning_compatibility_transient_error_recovers_to_real_spatial_output(client, monkeypatch, status):
    worker, run_id, posted = compatibility_worker(client, monkeypatch, fallback_status=status)
    result = worker.run_once("compatibility-plan", run_id=run_id)
    assert result["status"] == "planned", result
    assert len(posted) == 3
    assert posted[1]["response_format"] == posted[2]["response_format"] == {"type": "json_object"}
    store = client.app.state.runtime_store
    attempts = store._read(lambda conn: [dict(row) for row in conn.execute(
        "SELECT status,usage_json FROM planner_attempts WHERE run_id=?", (run_id,),
    )])
    assert len(attempts) == 1 and attempts[0]["status"] == "succeeded"
    assert json.loads(attempts[0]["usage_json"])["planner_http_calls"] == 3
    assert worker.run_once("compatibility-worker", run_id=run_id) is not None
    snapshot = client.get("/api/runs/" + run_id).json()
    assert snapshot["status"] == "completed", snapshot
    assert len(snapshot["final_artifact_ids"]) == 1
    artifact = store.get_artifact(snapshot["final_artifact_ids"][0])
    assert artifact["crs"] == {"kind": "EPSG", "code": 4326}
    path = client.app.state.artifact_store.path_for(artifact["artifact_id"])
    assert json.loads(path.read_bytes())["features"][0]["geometry"]["coordinates"] == [116.4, 39.9]


@pytest.mark.parametrize("status,max_calls,retries,code", [
    (429, 2, 1, "LLM_CALL_BUDGET_EXCEEDED"),
    (503, 3, 0, "LLM_HTTP_ERROR"),
    (403, 5, 2, "LLM_HTTP_ERROR"),
])
def test_reasoning_compatibility_preserves_call_budget_and_permanent_failures(client, monkeypatch, status, max_calls, retries, code):
    worker, run_id, posted = compatibility_worker(client, monkeypatch, fallback_status=status, max_calls=max_calls, retries=retries)
    worker.run_once("compatibility-plan", run_id=run_id)
    snapshot = client.get("/api/runs/" + run_id).json()
    assert snapshot["status"] == "failed", snapshot
    assert snapshot["diagnostic"]["code"] == code
    assert len(posted) == 2
