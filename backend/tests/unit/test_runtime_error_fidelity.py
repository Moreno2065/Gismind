"""Business diagnostics must survive real spawn IPC and durable queue handling."""

import json
import os

import pytest


def _fault_worker(*args):
    """Inject only the handler result; execute the real worker/IPC/store path."""
    from app.runtime import queue, tool_handlers
    from app.runtime.errors import ErrorCode, ToolCallError

    scenario = args[6]["task_key"]

    def handler(**kwargs):
        if scenario == "semantic":
            return {"status": "success", "data": {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": None, "properties": {}}]}}
        if scenario == "empty_payload":
            return {"status": "success", "data": {"type": "FeatureCollection", "features": []}}
        if scenario == "explicit":
            return {"status": "error", "error_code": "DATA_PARSE_FAILED", "message": "bad input"}
        if scenario == "enum_code":
            raise ToolCallError(ErrorCode.INVALID_PARAMS, "bad input")
        if scenario == "large_error":
            error = ValueError("错误" * (256 * 1024))
            error.error_code = "DATA_PARSE_FAILED"
            raise error
        raise ValueError("bad input")

    tool_handlers.handle_vector_clip = handler
    queue._isolated_executor_worker(*args)


def _lost_worker(*args):
    os._exit(17)


def _malformed_worker(*args):
    args[-1].put(("unexpected",))


def _runtime(tmp_path, scenario, *, retry_lost=True):
    from app.runtime import ArtifactRef, ArtifactStore, Compiler, PlannerDraft, PlannerStepDraft, RuntimeDB, RuntimeStore, ToolContractRegistry
    from app.runtime.contracts import RetryPolicy

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    artifacts = ArtifactStore(tmp_path / "data", runtime)

    def publish(geometry):
        data = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": geometry, "properties": {}}]}
        artifact = artifacts.publish_staged(artifacts.stage_bytes(json.dumps(data).encode()), origin="upload", artifact_type="Vector", crs={"kind": "EPSG", "code": 4326}, schema={})
        return ArtifactRef(artifact_id=artifact["artifact_id"], type="Vector", crs={"kind": "EPSG", "code": 4326})

    source = publish({"type": "Point", "coordinates": [0, 0]})
    mask = publish({"type": "Polygon", "coordinates": [[[10, 10], [11, 10], [11, 11], [10, 11], [10, 10]]]})
    registry = ToolContractRegistry()
    for contract in ToolContractRegistry.with_builtins().values():
        if contract.name == "vector.clip" and retry_lost:
            contract = contract.model_copy(update={"retry_policy": RetryPolicy(max_attempts=2, retryable_error_codes=["WORKER_LOST"], backoff_seconds=[0])})
        registry.register(contract)
    draft = PlannerDraft(run_id="r1", tasks=[PlannerStepDraft(task_key=scenario, op="vector.clip", inputs={"input": source, "mask": mask}, params={})])
    plan = Compiler(registry, artifact_lookup=runtime.get_artifact).compile(draft)
    runtime.create_plan("p1", "r1", plan=plan)
    runtime.create_task(scenario, "p1", scenario, tool_name="vector.clip")
    return runtime, artifacts


def _assert_failed(runtime, task_id, code, event_type):
    attempts = runtime._read(lambda conn: [dict(row) for row in conn.execute("SELECT * FROM task_attempts WHERE task_id=?", (task_id,))])
    assert len(attempts) == 1
    assert attempts[0]["status"] == "failed"
    assert attempts[0]["error_code"] == code
    assert json.loads(attempts[0]["executor_identity"])["pid"] != os.getpid()
    assert runtime._read(lambda conn: conn.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0]) == "failed"
    assert runtime.get_run("r1")["status"] == "failed"
    assert not runtime._read(lambda conn: conn.execute("SELECT * FROM task_outputs WHERE task_id=?", (task_id,)).fetchall())
    events = runtime.list_events("r1")
    events = [{**event, "payload": json.loads(event["payload_json"])} for event in events]
    matching = [event for event in events if event["event_type"] == event_type and event["payload"].get("task_id") == task_id]
    assert matching, events
    assert matching[-1]["payload"]["error_code"] == code
    assert not any(event["event_type"] == "task.retry_wait" for event in events)


def test_real_disjoint_clip_is_task_empty_not_lost_worker(tmp_path):
    from app.runtime import RuntimeSupervisor

    runtime, artifacts = _runtime(tmp_path, "clip")
    with pytest.raises(RuntimeError) as caught:
        RuntimeSupervisor(runtime, artifacts, isolate=True).run_once("worker", run_id="r1")
    assert getattr(caught.value, "error_code", None) == "TASK_EMPTY"
    _assert_failed(runtime, "clip", "TASK_EMPTY", "task.empty")


@pytest.mark.parametrize("scenario,code,event", [("semantic", "VALIDATION_FAILED", "task.failed"), ("empty_payload", "TASK_EMPTY", "task.empty"), ("explicit", "DATA_PARSE_FAILED", "task.failed"), ("enum_code", "INVALID_PARAMS", "task.failed"), ("generic", "TOOL_EXECUTION_FAILED", "task.failed")])
def test_business_error_keeps_code_without_worker_lost_retry(tmp_path, monkeypatch, scenario, code, event):
    from app.runtime import RuntimeSupervisor, queue

    runtime, artifacts = _runtime(tmp_path, scenario)
    monkeypatch.setattr(queue, "_isolated_executor_worker", _fault_worker)
    with pytest.raises(RuntimeError) as caught:
        RuntimeSupervisor(runtime, artifacts, isolate=True).run_once("worker", run_id="r1")
    assert getattr(caught.value, "error_code", None) == code
    _assert_failed(runtime, scenario, code, event)


def test_actual_process_loss_is_explicit_worker_lost(tmp_path, monkeypatch):
    from app.runtime import RuntimeSupervisor, queue

    runtime, artifacts = _runtime(tmp_path, "lost", retry_lost=False)
    monkeypatch.setattr(queue, "_isolated_executor_worker", _lost_worker)
    with pytest.raises(RuntimeError, match="exited without a result") as caught:
        RuntimeSupervisor(runtime, artifacts, isolate=True).run_once("worker", run_id="r1")
    assert getattr(caught.value, "error_code", None) == "WORKER_LOST"
    _assert_failed(runtime, "lost", "WORKER_LOST", "task.failed")


def test_invalid_ipc_message_is_protocol_failure_not_process_loss(tmp_path, monkeypatch):
    from app.runtime import RuntimeSupervisor, queue

    runtime, artifacts = _runtime(tmp_path, "protocol")
    monkeypatch.setattr(queue, "_isolated_executor_worker", _malformed_worker)
    with pytest.raises(RuntimeError) as caught:
        RuntimeSupervisor(runtime, artifacts, isolate=True).run_once("worker", run_id="r1")
    assert getattr(caught.value, "error_code", None) == "WORKER_PROTOCOL_ERROR"
    _assert_failed(runtime, "protocol", "WORKER_PROTOCOL_ERROR", "task.failed")


def test_large_business_exception_is_bounded_without_ipc_deadlock(tmp_path, monkeypatch):
    from app.runtime import RuntimeSupervisor, queue

    runtime, artifacts = _runtime(tmp_path, "large_error")
    monkeypatch.setattr(queue, "_isolated_executor_worker", _fault_worker)
    with pytest.raises(Exception) as caught:
        RuntimeSupervisor(runtime, artifacts, isolate=True, timeout_seconds=5).run_once("worker", run_id="r1")
    assert getattr(caught.value, "error_code", None) == "DATA_PARSE_FAILED"
    assert len(str(caught.value).encode("utf-8")) <= 4096
    assert str(caught.value).endswith("... [truncated]")
    _assert_failed(runtime, "large_error", "DATA_PARSE_FAILED", "task.failed")
    attempt = runtime._read(lambda conn: dict(conn.execute("SELECT * FROM task_attempts WHERE task_id='large_error'").fetchone()))
    assert attempt["error"] == str(caught.value)


@pytest.mark.parametrize("behavior,code", [("raise ValueError('bad input')", "TOOL_EXECUTION_FAILED"), ("raise ToolCallError(ErrorCode.INVALID_PARAMS, 'bad input')", "INVALID_PARAMS"), ("raise TimeoutError('expired')", "TIMEOUT"), ("os._exit(17)", "WORKER_LOST")])
def test_external_handler_spawn_keeps_structured_error(tmp_path, monkeypatch, behavior, code):
    from app.runtime.executor import RuntimeExecutor

    (tmp_path / "error_fidelity_probe.py").write_text("import os\nfrom app.runtime.errors import ToolCallError, ErrorCode\ndef run():\n    " + behavior + "\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    class Artifacts:
        def create_staging_path(self, filename):
            return tmp_path / filename

    with pytest.raises((ValueError, RuntimeError)) as caught:
        RuntimeExecutor(None, Artifacts())._invoke_handler("error_fidelity_probe:run", None, {}, 20)
    assert getattr(caught.value, "error_code", None) == code
