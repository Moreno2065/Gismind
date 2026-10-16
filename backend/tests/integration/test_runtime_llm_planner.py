"""Planner-worker integration tests.

These tests deliberately exercise the same durable Run -> planning lease ->
PlanIR path used by the public API.  The local planner is only a deterministic
test double; live model acceptance belongs in the separately marked suite.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.runtime import ArtifactRef, ArtifactStore, PlannerDraft, PlannerStepDraft, RuntimeDB, RuntimeStore, RuntimeSupervisor, TaskOutputRef
from app.runtime.llm_planner import PlannerError, PlannerRequest, candidate_catalog
from app.runtime.contracts import PortContract, ToolContract, ToolContractRegistry


class StaticPlanner:
    """A contract-shaped test double, never used by the production factory."""

    def create_draft(self, request):
        return PlannerDraft(
            run_id=request.run_id,
            input_revision=request.input_revision,
            user_goal=request.user_goal,
            origin="test",
            tasks=[PlannerStepDraft(task_key="geocode", op="geo.geocode", params={"address": "南京"})],
        )


def test_candidate_catalog_reserves_space_for_type_dependency_closure(monkeypatch):
    registry = ToolContractRegistry()
    registry.register(ToolContract(
        name="demo.target", version="1.0.0", input_ports={"vector": PortContract(artifact_type="Vector")},
        output_ports={"result": PortContract(artifact_type="Table")},
    ))
    registry.register(ToolContract(
        name="z.make_vector", version="1.0.0", input_ports={"file": PortContract(artifact_type="File")},
        output_ports={"vector": PortContract(artifact_type="Vector")},
    ))
    for index in range(18):
        registry.register(ToolContract(
            name=f"a{index:02d}.distractor", version="1.0.0",
            input_ports={"file": PortContract(artifact_type="File")},
            output_ports={"table": PortContract(artifact_type="Table")},
        ))
    monkeypatch.setitem(__import__("app.runtime.llm_planner", fromlist=["_ALIASES"])._ALIASES, "demo.target", ("目标分析",))
    request = PlannerRequest(
        run_id="run-catalog", input_revision=1, user_goal="执行目标分析",
        input_artifacts=({"artifact_id": "art-file", "type": "File"},), revision_patch={},
    )

    names = [item["op"] for item in candidate_catalog(registry, request, limit=18)]

    assert "demo.target" in names
    assert "z.make_vector" in names


def test_candidate_catalog_exposes_side_effect_for_explicit_chinese_intent():
    request = PlannerRequest(
        run_id="run-publish", input_revision=1,
        user_goal="请将结果发布到指定路径 exports/result.geojson",
        input_artifacts=({"artifact_id": "art-file", "type": "File"},), revision_patch={},
    )

    names = [item["op"] for item in candidate_catalog(ToolContractRegistry.with_builtins(), request, limit=18)]

    assert "publish_file" in names


def test_every_public_runtime_operation_has_live_natural_language_aliases():
    from app.runtime.llm_planner import _ALIASES

    operations = {contract.name for contract in ToolContractRegistry.with_builtins().values()}

    assert set(_ALIASES) == operations
    assert all(any(any("\u4e00" <= char <= "\u9fff" for char in token) for token in aliases) for aliases in _ALIASES.values())


def test_provider_request_does_not_mislabel_ordinary_schema_as_strict(monkeypatch):
    from app.runtime.llm_planner import OpenAICompatiblePlanner

    posted = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "{}"}}]}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, endpoint, *, json, headers):
            posted.append(json)
            return Response()

    monkeypatch.setattr("app.runtime.llm_planner.httpx.AsyncClient", Client)
    planner = OpenAICompatiblePlanner(
        api_key="key", base_url="https://provider.invalid/v1", model="model",
        timeout_seconds=1, max_tokens=512, max_retries=0,
    )
    planner._call([{"role": "user", "content": "plan"}])

    response_format = posted[0]["response_format"]
    assert response_format["type"] == "json_schema"
    # The execution contract permits optional/open parameter objects, which
    # are not the restricted provider strict-schema dialect.
    assert response_format["json_schema"]["strict"] is False
    assert response_format["json_schema"]["schema"]["additionalProperties"] is False
    assert posted[0]["thinking"] == {"type": "disabled"}


def test_provider_request_has_one_bounded_json_object_compatibility_fallback(monkeypatch):
    from app.runtime.llm_planner import OpenAICompatiblePlanner

    posted = []

    class Response:
        def __init__(self, status_code):
            self.status_code = status_code

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "{}"}}]}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, endpoint, *, json, headers):
            posted.append(json)
            return Response(400 if len(posted) == 1 else 200)

    monkeypatch.setattr("app.runtime.llm_planner.httpx.AsyncClient", Client)
    planner = OpenAICompatiblePlanner(
        api_key="key", base_url="https://provider.invalid/v1", model="model",
        timeout_seconds=1, max_tokens=512, max_retries=0,
    )
    planner._call([{"role": "user", "content": "plan"}])

    assert len(posted) == 2
    assert posted[0]["response_format"]["type"] == "json_schema"
    assert posted[1]["response_format"] == {"type": "json_object"}
    assert "enable_thinking" not in posted[1]
    assert "thinking" not in posted[1]


def test_provider_retries_reasoning_only_200_response_with_json_object(monkeypatch):
    from app.runtime.llm_planner import OpenAICompatiblePlanner

    posted = []

    class Response:
        def __init__(self, body):
            self.status_code = 200
            self._body = body

        def json(self):
            return self._body

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, endpoint, *, json, headers):
            posted.append(json)
            if len(posted) == 1:
                return Response({"choices": [{"message": {"content": "", "reasoning_content": "hidden"}}]})
            return Response({"choices": [{"message": {"content": "{}"}}]})

    monkeypatch.setattr("app.runtime.llm_planner.httpx.AsyncClient", Client)
    planner = OpenAICompatiblePlanner(
        api_key="key", base_url="https://provider.invalid/v1", model="model",
        timeout_seconds=1, max_tokens=512, max_retries=0,
    )
    raw, _latency = planner._call([{"role": "user", "content": "plan"}])

    assert raw["choices"][0]["message"]["content"] == "{}"
    assert len(posted) == 2
    assert posted[0]["response_format"]["type"] == "json_schema"
    assert posted[1]["response_format"] == {"type": "json_object"}
    assert posted[1]["thinking"] == {"type": "disabled"}


def test_planner_normalizes_provider_numeric_slope_flag_before_compiler(monkeypatch):
    from app.runtime.llm_planner import OpenAICompatiblePlanner

    planner = OpenAICompatiblePlanner(
        api_key="key", base_url="https://provider.invalid/v1", model="model",
        timeout_seconds=1, max_tokens=512, max_retries=0,
    )
    payload = {
        "draft_version": 1, "run_id": "wrong", "input_revision": 99,
        "user_goal": "坡度", "external_inputs": [],
        "tasks": [{"task_key": "slope", "op": "raster.slope", "inputs": {}, "params": {"degree": 1}}],
        "bindings": [], "origin": "planner",
    }
    monkeypatch.setattr(
        planner, "_call",
        lambda _messages: ({"choices": [{"message": {"content": json.dumps(payload)}}]}, 1),
    )
    request = PlannerRequest(run_id="run", input_revision=1, user_goal="坡度", input_artifacts=(), revision_patch={})

    call = planner.create_draft(request)

    assert call.draft.tasks[0].params["degree"] is True


def _runtime(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s-llm")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    supervisor = RuntimeSupervisor(runtime, artifacts, isolate=False, planner=StaticPlanner(), autostart=False)
    return runtime, artifacts, supervisor


def test_message_only_run_remains_queued_for_durable_planner(tmp_path):
    from app.main import create_app

    runtime, artifacts, supervisor = _runtime(tmp_path)
    app = create_app(runtime_store=runtime, artifact_store=artifacts, runtime_supervisor=supervisor)
    with TestClient(app) as client:
        response = client.post(
            "/api/runs",
            json={"session_id": "s-llm", "message": "查询南京", "client_request_id": "create-natural-language"},
        )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"


def test_planner_worker_claims_natural_language_run_and_persists_plan(tmp_path):
    runtime, _artifacts, supervisor = _runtime(tmp_path)
    runtime.create_run("run-llm", "s-llm")
    runtime.add_input_revision("run-llm", 1, {"prompt": "查询南京", "input_artifact_ids": []})

    result = supervisor.run_once("planner-test", run_id="run-llm")

    assert result == {"status": "planned", "run_id": "run-llm"}
    snapshot = runtime.get_run("run-llm")
    assert snapshot["status"] == "ready"
    plan = runtime._read(lambda c: c.execute("SELECT plan_hash FROM plans WHERE run_id=?", ("run-llm",)).fetchone())
    assert plan and plan[0]
    events = runtime.list_events("run-llm")
    assert [event["event_type"] for event in events][-2:] == ["run.status_changed", "run.ready"]


def test_planner_request_contains_bounded_session_history_and_explicit_prior_artifacts(tmp_path):
    captured = []

    class CapturingPlanner(StaticPlanner):
        def create_draft(self, request):
            captured.append(request)
            return super().create_draft(request)

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s-context")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    previous = artifacts.publish_staged(
        artifacts.stage_bytes(b'{"type":"FeatureCollection","features":[]}', filename="previous.geojson"),
        origin="upload", artifact_type="File", media_type="application/geo+json",
    )
    old_message = runtime.append_message("s-context", "assistant", "上一次已经生成道路图层")
    runtime.link_message_artifact(old_message["message_id"], previous["artifact_id"])
    for index in range(5):
        runtime.append_message("s-context", "user", f"旧消息{index}:" + "x" * 4000)
    runtime.append_message("s-context", "user", "请继续处理上一轮结果")
    runtime.create_run("run-context", "s-context")
    runtime.add_input_revision("run-context", 1, {"prompt": "把上一轮结果显示出来", "input_artifact_ids": []})
    supervisor = RuntimeSupervisor(
        runtime, artifacts, isolate=False, planner=CapturingPlanner(), autostart=False,
    )

    assert supervisor.run_once("planner-context", run_id="run-context")["status"] == "planned"
    request = captured[0]
    assert request.session_messages[-1]["content"] == "请继续处理上一轮结果"
    assert request.prior_artifacts[0]["artifact_id"] == previous["artifact_id"]
    assert request.prior_artifacts[0]["source"] == "message"


def test_planner_rejects_task_artifact_from_another_session_even_when_cas_object_is_valid(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s-visible")
    runtime.create_session("s-foreign")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    foreign = artifacts.publish_staged(
        artifacts.stage_bytes(b'{"type":"FeatureCollection","features":[]}', filename="foreign.geojson"),
        origin="upload", artifact_type="File", media_type="application/geo+json",
        crs={"kind": "EPSG", "code": 4326}, schema={},
    )
    foreign_message = runtime.append_message("s-foreign", "user", "另一会话的附件")
    runtime.link_message_artifact(foreign_message["message_id"], foreign["artifact_id"])

    class ForeignArtifactPlanner:
        def create_draft(self, request):
            return PlannerDraft(
                run_id=request.run_id, input_revision=request.input_revision, user_goal=request.user_goal,
                tasks=[PlannerStepDraft(
                    task_key="load", op="io.load_vector",
                    inputs={"file": ArtifactRef(artifact_id=foreign["artifact_id"], type="File")},
                )],
            )

    runtime.create_run("run-visible", "s-visible")
    runtime.add_input_revision("run-visible", 1, {
        "prompt": "读取上传的 GeoJSON 矢量文件", "input_artifact_ids": [],
    })
    supervisor = RuntimeSupervisor(runtime, artifacts, isolate=False, planner=ForeignArtifactPlanner(), autostart=False)

    assert supervisor.run_once("planner-visible", run_id="run-visible")["status"] == "planning_failed"
    assert runtime.get_run("run-visible")["status"] == "failed"
    assert runtime._read(lambda c: c.execute("SELECT COUNT(*) FROM plans WHERE run_id=?", ("run-visible",)).fetchone()[0]) == 0
    attempt = runtime._read(lambda c: c.execute(
        "SELECT error_code FROM planner_attempts WHERE run_id=?", ("run-visible",),
    ).fetchone())
    assert attempt[0] == "ARTIFACT_NOT_VISIBLE"


def test_planner_rejects_foreign_external_input_even_when_tasks_are_valid(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s-visible")
    runtime.create_session("s-foreign")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    foreign = artifacts.publish_staged(
        artifacts.stage_bytes(b"foreign", filename="foreign.bin"),
        origin="upload", artifact_type="File", schema={},
    )
    foreign_message = runtime.append_message("s-foreign", "user", "另一会话的附件")
    runtime.link_message_artifact(foreign_message["message_id"], foreign["artifact_id"])

    class ForeignExternalPlanner:
        def create_draft(self, request):
            return PlannerDraft(
                run_id=request.run_id, input_revision=request.input_revision, user_goal=request.user_goal,
                external_inputs=[ArtifactRef(artifact_id=foreign["artifact_id"], type="File")],
                tasks=[PlannerStepDraft(task_key="geocode", op="geo.geocode", params={"address": "南京"})],
            )

    runtime.create_run("run-visible", "s-visible")
    runtime.add_input_revision("run-visible", 1, {"prompt": "查询南京", "input_artifact_ids": []})
    supervisor = RuntimeSupervisor(runtime, artifacts, isolate=False, planner=ForeignExternalPlanner(), autostart=False)

    assert supervisor.run_once("planner-visible", run_id="run-visible")["status"] == "planning_failed"
    assert runtime.get_run("run-visible")["status"] == "failed"
    attempt = runtime._read(lambda c: c.execute(
        "SELECT error_code FROM planner_attempts WHERE run_id=?", ("run-visible",),
    ).fetchone())
    assert attempt[0] == "ARTIFACT_NOT_VISIBLE"


def test_planner_accepts_explicit_same_session_prior_artifact_and_task_output_ref(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s-prior")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    prior = artifacts.publish_staged(
        artifacts.stage_bytes(b'{"type":"FeatureCollection","features":[]}', filename="prior.geojson"),
        origin="upload", artifact_type="File", media_type="application/geo+json",
        crs={"kind": "EPSG", "code": 4326}, schema={},
    )
    message = runtime.append_message("s-prior", "user", "上一轮的矢量附件")
    runtime.link_message_artifact(message["message_id"], prior["artifact_id"])

    class PriorArtifactPlanner:
        def create_draft(self, request):
            return PlannerDraft(
                run_id=request.run_id, input_revision=request.input_revision, user_goal=request.user_goal,
                external_inputs=[ArtifactRef(artifact_id=prior["artifact_id"], type="File")],
                tasks=[
                    PlannerStepDraft(
                        task_key="load", op="io.load_vector",
                        inputs={"file": ArtifactRef(artifact_id=prior["artifact_id"], type="File")},
                    ),
                    PlannerStepDraft(
                        task_key="dedup", op="vector.delete_duplicates",
                        inputs={"input": TaskOutputRef(task_key="load", port="vector")},
                    ),
                ],
            )

    runtime.create_run("run-prior", "s-prior")
    runtime.add_input_revision("run-prior", 1, {
        "prompt": "读取上一轮的矢量附件并删除重复几何", "input_artifact_ids": [],
    })
    supervisor = RuntimeSupervisor(runtime, artifacts, isolate=False, planner=PriorArtifactPlanner(), autostart=False)

    result = supervisor.run_once("planner-prior", run_id="run-prior")
    attempts = runtime._read(lambda c: list(c.execute(
        "SELECT error_code,error FROM planner_attempts WHERE run_id=?", ("run-prior",),
    ).fetchall()))
    assert result["status"] == "planned", [(row[0], row[1]) for row in attempts]
    assert runtime.get_run("run-prior")["status"] == "ready"
    plan = runtime._read(lambda c: c.execute(
        "SELECT plan_json FROM plans WHERE run_id=?", ("run-prior",),
    ).fetchone())
    tasks = json.loads(plan[0])["tasks"]
    assert [task["op"] for task in tasks] == ["io.load_vector", "vector.delete_duplicates"]


def test_provider_parser_rejects_unlisted_task_artifact_before_compilation(monkeypatch):
    from app.runtime.llm_planner import OpenAICompatiblePlanner

    planner = OpenAICompatiblePlanner(
        api_key="key", base_url="https://provider.invalid/v1", model="model",
        timeout_seconds=1, max_tokens=512, max_retries=0,
    )
    payload = {
        "draft_version": "1", "origin": "llm", "external_inputs": [],
        "tasks": [{
            "task_key": "load", "op": "io.load_vector",
            "inputs": {"file": {"kind": "artifact", "artifact_id": "foreign-artifact", "type": "File"}},
            "params": {},
        }],
    }
    monkeypatch.setattr(
        planner, "_call",
        lambda _messages: ({"choices": [{"message": {"content": json.dumps(payload)}}]}, 1),
    )
    request = PlannerRequest(
        run_id="run-visible", input_revision=1, user_goal="读取矢量附件",
        input_artifacts=({"artifact_id": "visible-artifact", "type": "File"},), revision_patch={},
    )

    with pytest.raises(PlannerError) as exc:
        planner.create_draft(request)

    assert exc.value.code == "LLM_DRAFT_INVALID"
    assert "foreign-artifact" in exc.value.diagnostics["error"]


def _buffer_planning_case(tmp_path, *, prompt: str, planned_distance_m: float):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s-buffer")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    source = artifacts.publish_staged(
        artifacts.stage_bytes(b'{"type":"FeatureCollection","features":[]}', filename="source.geojson"),
        origin="upload", artifact_type="Vector", media_type="application/geo+json",
        crs={"kind": "EPSG", "code": 3857}, schema={},
    )

    class BufferPlanner:
        def create_draft(self, request):
            return PlannerDraft(
                run_id=request.run_id, input_revision=request.input_revision, user_goal=request.user_goal,
                tasks=[PlannerStepDraft(
                    task_key="buffer", op="vector.buffer",
                    inputs={"vector": ArtifactRef(
                        artifact_id=source["artifact_id"], type="Vector", crs=source["crs"],
                    )},
                    params={"distance_m": planned_distance_m},
                )],
            )

    runtime.create_run("run-buffer", "s-buffer")
    runtime.add_input_revision("run-buffer", 1, {
        "prompt": prompt, "input_artifact_ids": [source["artifact_id"]],
    })
    supervisor = RuntimeSupervisor(runtime, artifacts, isolate=False, planner=BufferPlanner(), autostart=False)
    return runtime, supervisor


def test_planner_rejects_buffer_distance_that_disagrees_with_explicit_user_distance(tmp_path):
    runtime, supervisor = _buffer_planning_case(
        tmp_path, prompt="把上传图层缓冲五百米", planned_distance_m=100,
    )

    assert supervisor.run_once("planner-buffer", run_id="run-buffer")["status"] == "planning_failed"
    assert runtime.get_run("run-buffer")["status"] == "failed"
    assert runtime._read(lambda c: c.execute(
        "SELECT COUNT(*) FROM plans WHERE run_id=?", ("run-buffer",),
    ).fetchone()[0]) == 0
    attempt = runtime._read(lambda c: c.execute(
        "SELECT error_code FROM planner_attempts WHERE run_id=?", ("run-buffer",),
    ).fetchone())
    assert attempt[0] == "BUFFER_DISTANCE_MISMATCH"


@pytest.mark.parametrize("prompt,planned_distance_m", [
    ("给图层缓冲500米", 500),
    ("给图层缓冲0.5公里", 500),
    ("给图层缓冲五百米", 500),
    ("给图层缓冲二点五公里", 2500),
])
def test_planner_accepts_buffer_distance_after_explicit_unit_normalization(tmp_path, prompt, planned_distance_m):
    runtime, supervisor = _buffer_planning_case(
        tmp_path, prompt=prompt, planned_distance_m=planned_distance_m,
    )

    assert supervisor.run_once("planner-buffer", run_id="run-buffer")["status"] == "planned"
    plan = runtime._read(lambda c: c.execute(
        "SELECT plan_json FROM plans WHERE run_id=?", ("run-buffer",),
    ).fetchone())
    assert json.loads(plan[0])["tasks"][0]["params"]["distance_m"] == planned_distance_m


@pytest.mark.parametrize("prompt", [
    "给图层缓冲几百米",
    "给图层缓冲500",
    "给图层缓冲500米或1公里",
])
def test_planner_asks_for_buffer_distance_when_user_distance_is_not_determinable(tmp_path, prompt):
    runtime, supervisor = _buffer_planning_case(tmp_path, prompt=prompt, planned_distance_m=500)

    assert supervisor.run_once("planner-buffer", run_id="run-buffer")["status"] == "awaiting_input"
    assert runtime.get_run("run-buffer")["status"] == "awaiting_input"
    assert runtime._read(lambda c: c.execute(
        "SELECT COUNT(*) FROM plans WHERE run_id=?", ("run-buffer",),
    ).fetchone()[0]) == 0


def test_buffer_distance_clarification_overrides_multiple_distances_in_original_goal(tmp_path):
    from app.main import create_app

    runtime, supervisor = _buffer_planning_case(
        tmp_path, prompt="给图层缓冲100米或200米", planned_distance_m=100,
    )
    assert supervisor.run_once("planner-buffer", run_id="run-buffer")["status"] == "awaiting_input"
    with TestClient(create_app(
        runtime_store=runtime, artifact_store=supervisor.artifacts,
        runtime_supervisor=supervisor,
    )) as client:
        response = client.post("/api/runs/run-buffer/resume", json={
            "command_id": "choose-buffer-100m",
            "patch": {"clarification": "缓冲距离为100米"},
        })
    assert response.status_code == 202, response.text

    assert supervisor.run_once("planner-buffer", run_id="run-buffer")["status"] == "planned"
    plan = runtime._read(lambda c: c.execute(
        "SELECT plan_json FROM plans WHERE run_id=?", ("run-buffer",),
    ).fetchone())
    assert json.loads(plan[0])["tasks"][0]["params"]["distance_m"] == 100


def test_buffer_distance_clarification_with_two_distances_remains_awaiting_input(tmp_path):
    from app.main import create_app

    runtime, supervisor = _buffer_planning_case(
        tmp_path, prompt="给图层缓冲100米或200米", planned_distance_m=100,
    )
    assert supervisor.run_once("planner-buffer", run_id="run-buffer")["status"] == "awaiting_input"
    with TestClient(create_app(
        runtime_store=runtime, artifact_store=supervisor.artifacts,
        runtime_supervisor=supervisor,
    )) as client:
        response = client.post("/api/runs/run-buffer/resume", json={
            "command_id": "still-ambiguous",
            "patch": {"clarification": "100米或200米都可以"},
        })
    assert response.status_code == 202, response.text

    assert supervisor.run_once("planner-buffer", run_id="run-buffer")["status"] == "awaiting_input"
    assert runtime._read(lambda c: c.execute(
        "SELECT COUNT(*) FROM plans WHERE run_id=?", ("run-buffer",),
    ).fetchone()[0]) == 0


def test_planning_failure_is_persisted_without_leaving_a_live_lease(tmp_path):
    class FailingPlanner:
        def create_draft(self, request):
            raise RuntimeError("provider unavailable")

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s-llm")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    supervisor = RuntimeSupervisor(runtime, artifacts, isolate=False, planner=FailingPlanner())
    runtime.create_run("run-fail", "s-llm")
    runtime.add_input_revision("run-fail", 1, {"prompt": "查询南京", "input_artifact_ids": []})

    result = supervisor.run_once("planner-test", run_id="run-fail")

    assert result == {"status": "planning_failed", "run_id": "run-fail"}
    run = runtime.get_run("run-fail")
    assert run["status"] == "failed"
    assert run["planner_lease_owner"] is None
    assert any(event["event_type"] == "plan.rejected" for event in runtime.list_events("run-fail"))


def test_invalid_provider_json_gets_one_durably_recorded_repair_attempt(tmp_path):
    class InvalidThenRepairPlanner:
        provider = "test-provider"
        model = "test-model"
        prompt_version = "test-prompt"

        def create_draft(self, request):
            raise PlannerError("LLM_DRAFT_INVALID", "malformed planner JSON", diagnostics={"error": "missing tasks"})

        def repair_draft(self, request, *, rejected_draft, diagnostic):
            assert rejected_draft is None
            assert diagnostic["code"] == "LLM_DRAFT_INVALID"
            return PlannerDraft(
                run_id=request.run_id, input_revision=request.input_revision,
                user_goal=request.user_goal, origin="test-repair",
                tasks=[PlannerStepDraft(task_key="geocode", op="geo.geocode", params={"address": "南京"})],
            )

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s-repair")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    supervisor = RuntimeSupervisor(runtime, artifacts, isolate=False, planner=InvalidThenRepairPlanner())
    runtime.create_run("run-repair", "s-repair")
    runtime.add_input_revision("run-repair", 1, {"prompt": "查询南京", "input_artifact_ids": []})

    assert supervisor.run_once("planner-test", run_id="run-repair") == {"status": "planned", "run_id": "run-repair"}
    attempts = runtime._read(lambda c: [tuple(row) for row in c.execute(
        "SELECT phase,status,error_code FROM planner_attempts WHERE run_id=? ORDER BY attempt_no", ("run-repair",)
    ).fetchall()])
    assert attempts == [("initial", "failed", "LLM_DRAFT_INVALID"), ("repair", "succeeded", None)]


def test_slow_planner_heartbeats_its_durable_lease_until_plan_commit(tmp_path, monkeypatch):
    renewed = threading.Condition()
    renewals = []
    provider_window = {}

    class SlowPlanner(StaticPlanner):
        def create_draft(self, request):
            # The provider cannot return until actual background renewals are
            # durable beyond the lease it saw on entry. This proves the slow
            # call needed renewal, without guessing a sleep/scheduler ratio.
            entry = runtime.get_run(request.run_id)
            provider_window["entry_expiry"] = entry["planner_lease_expires_at"]
            entry_expiry = datetime.fromisoformat(provider_window["entry_expiry"])

            def renewed_past_original_expiry():
                periodic = [row for row in renewals if row["background"]]
                return (
                    len(periodic) >= 2
                    and datetime.fromisoformat(periodic[-1]["persisted_at"]) > entry_expiry
                )

            with renewed:
                assert renewed.wait_for(renewed_past_original_expiry, timeout=10), (
                    "planner did not persist multiple background heartbeat renewals "
                    "beyond its initial lease"
                )
            return super().create_draft(request)

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s-slow")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    supervisor = RuntimeSupervisor(
        runtime, artifacts, planner=SlowPlanner(), isolate=False,
        # Preparation includes catalog construction and SQLite commits before
        # the heartbeat starts. A 50 ms total lease races that preparation
        # under coverage; the condition above still requires real expiry.
        lease_seconds=1.0, heartbeat_interval=0.05, autostart=False,
    )
    runtime.create_run("run-slow", "s-slow")
    runtime.add_input_revision("run-slow", 1, {"prompt": "查询南京", "input_artifact_ids": []})
    heartbeat = supervisor.queue.heartbeat_planning

    def observe_durable_heartbeat(claim):
        refreshed = heartbeat(claim)
        # Read through a fresh connection after the real heartbeat commits;
        # observing calls or an in-memory RunClaim alone is insufficient.
        row = runtime.get_run(claim.run_id)
        assert row["planner_lease_owner"] == refreshed.owner
        assert row["planner_lease_token"] == refreshed.token
        assert row["planner_generation"] == refreshed.generation
        assert row["planner_lease_expires_at"] >= refreshed.lease_expires_at
        assert refreshed.lease_expires_at > claim.lease_expires_at
        with renewed:
            renewals.append({
                "previous_expiry": claim.lease_expires_at,
                "renewed_expiry": refreshed.lease_expires_at,
                "persisted_expiry": row["planner_lease_expires_at"],
                "persisted_at": datetime.now(timezone.utc).isoformat(),
                "background": threading.current_thread().name.startswith("gismind-planner-lease-"),
            })
            renewed.notify_all()
        return refreshed

    monkeypatch.setattr(supervisor.queue, "heartbeat_planning", observe_durable_heartbeat)

    try:
        result = supervisor.run_once("planner-test", run_id="run-slow")
    finally:
        (tmp_path / "heartbeat-evidence.json").write_text(
            json.dumps({"lease_seconds": supervisor.queue.lease_seconds, "provider": provider_window, "renewals": renewals}, indent=2),
            encoding="utf-8",
        )

    assert result == {"status": "planned", "run_id": "run-slow"}
    periodic = [row for row in renewals if row["background"]]
    assert len(periodic) >= 2
    assert len({row["persisted_expiry"] for row in periodic}) >= 2
    assert datetime.fromisoformat(periodic[-1]["persisted_at"]) > datetime.fromisoformat(provider_window["entry_expiry"])
    assert datetime.fromisoformat(periodic[-1]["persisted_at"]) > datetime.fromisoformat(renewals[0]["previous_expiry"])
    assert runtime.get_run("run-slow")["status"] == "ready"
    assert runtime.get_run("run-slow")["planner_lease_owner"] is None
    with runtime.db.connection() as connection:
        assert [row[0] for row in connection.execute("SELECT status FROM planner_attempts WHERE run_id='run-slow'")] == ["succeeded"]
        assert connection.execute("SELECT COUNT(*) FROM plans WHERE run_id='run-slow'").fetchone()[0] == 1


def test_needs_clarification_draft_transitions_to_awaiting_input_not_an_empty_ready_plan(tmp_path):
    class ClarifyingPlanner:
        def create_draft(self, request):
            return PlannerDraft(
                run_id=request.run_id, input_revision=request.input_revision,
                user_goal=request.user_goal, origin="needs_clarification", tasks=[],
            )

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s-clarify")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    supervisor = RuntimeSupervisor(runtime, artifacts, planner=ClarifyingPlanner(), isolate=False, autostart=False)
    runtime.create_run("run-clarify", "s-clarify")
    runtime.add_input_revision("run-clarify", 1, {"prompt": "帮我分析这里", "input_artifact_ids": []})

    assert supervisor.run_once("planner-test", run_id="run-clarify") == {"status": "awaiting_input", "run_id": "run-clarify"}
    assert runtime.get_run("run-clarify")["status"] == "awaiting_input"
    events = runtime.list_events("run-clarify")
    awaiting = [event for event in events if event["event_type"] == "run.awaiting_input"]
    assert len(awaiting) == 1
    assert json.loads(awaiting[0]["payload_json"])["code"] == "CLARIFICATION_REQUIRED"


def test_repair_draft_cannot_turn_into_an_empty_ready_plan_when_it_requests_clarification(tmp_path):
    class InvalidThenClarifyPlanner:
        def create_draft(self, request):
            return PlannerDraft(
                run_id=request.run_id, input_revision=request.input_revision,
                user_goal=request.user_goal,
                tasks=[PlannerStepDraft(task_key="unknown", op="not.a.runtime.operation")],
            )

        def repair_draft(self, request, *, rejected_draft, diagnostic):
            assert rejected_draft is not None
            assert diagnostic["code"]
            return PlannerDraft(
                run_id=request.run_id, input_revision=request.input_revision,
                user_goal=request.user_goal, origin="needs_clarification", tasks=[],
            )

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s-repair-clarify")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    supervisor = RuntimeSupervisor(runtime, artifacts, planner=InvalidThenClarifyPlanner(), isolate=False, autostart=False)
    runtime.create_run("run-repair-clarify", "s-repair-clarify")
    runtime.add_input_revision("run-repair-clarify", 1, {"prompt": "分析这里", "input_artifact_ids": []})

    assert supervisor.run_once("planner-test", run_id="run-repair-clarify") == {"status": "awaiting_input", "run_id": "run-repair-clarify"}
    assert runtime.get_run("run-repair-clarify")["status"] == "awaiting_input"
