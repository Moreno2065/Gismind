"""Offline transport/lease regressions; these are not live LLM acceptance."""
from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.runtime import ArtifactStore, PlannerDraft, PlannerStepDraft, RuntimeDB, RuntimeStore, RuntimeSupervisor
from app.runtime.llm_planner import OpenAICompatiblePlanner, PlannerError


@contextmanager
def provider(responder):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            status, body = responder(payload)
            data = json.dumps(body).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def draft(request, *, valid=True):
    return PlannerDraft(run_id=request.run_id, input_revision=request.input_revision,
                        user_goal=request.user_goal, origin="test",
                        tasks=[PlannerStepDraft(task_key="one", op="geo.geocode" if valid else "missing.operation",
                                                params={"address": "南京"})])


def setup_runtime(tmp_path, planner):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s")
    supervisor = RuntimeSupervisor(runtime, ArtifactStore(tmp_path / "data", runtime), planner=planner,
                                   isolate=False, autostart=False, heartbeat_interval=0.02)
    return runtime, supervisor


def add_run(runtime, run_id):
    runtime.create_run(run_id, "s")
    runtime.add_input_revision(run_id, 1, {"prompt": "查询南京", "input_artifact_ids": []})


def response(content="{}"):
    return {"choices": [{"message": {"content": content}}]}


def test_schema_repair_then_compile_failure_has_only_one_repair(tmp_path):
    class Planner:
        calls = 0

        def create_draft(self, request):
            self.calls += 1
            raise PlannerError("LLM_DRAFT_INVALID", "bad JSON")

        def repair_draft(self, request, **kwargs):
            self.calls += 1
            return draft(request, valid=False)

    planner = Planner()
    runtime, supervisor = setup_runtime(tmp_path, planner)
    add_run(runtime, "bad")
    assert supervisor.run_once("worker")["status"] == "planning_failed"
    attempts = runtime._read(lambda c: c.execute(
        "SELECT attempt_no,status,error_code FROM planner_attempts ORDER BY attempt_no").fetchall())
    assert planner.calls == 2
    assert [tuple(row[:2]) for row in attempts] == [(1, "failed"), (2, "failed")]
    rejected = [e for e in runtime.list_events("bad") if e["event_type"] == "plan.rejected"][-1]
    assert "UNIQUE constraint" not in json.dumps(rejected)
    assert "missing.operation" in json.dumps(rejected)


def test_cancel_interrupts_provider_io_and_worker_can_plan_next_run(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def respond(payload):
        calls.append(payload)
        if len(calls) == 1:
            entered.set()
            release.wait(4)
        return 200, response('{"tasks":[],"origin":"needs_clarification","clarification":"请提供地址。"}')

    with provider(respond) as url:
        planner = OpenAICompatiblePlanner(api_key="offline", base_url=url, model="offline",
                                          timeout_seconds=4, max_tokens=128, max_retries=0)
        runtime, supervisor = setup_runtime(tmp_path, planner)
        add_run(runtime, "cancel")
        add_run(runtime, "next")
        errors = []

        def work():
            try:
                supervisor.worker_loop("worker", max_tasks=2)
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=work)
        worker.start()
        try:
            assert entered.wait(3)
            supervisor.queue.cancel_run("cancel")
            worker.join(1.5)
            assert not worker.is_alive(), "cancelled provider I/O still holds the only worker"
            assert errors == []
            assert runtime.get_run("cancel")["status"] == "cancelled"
            assert runtime.get_run("next")["status"] == "awaiting_input"
            assert runtime._read(lambda c: c.execute("SELECT COUNT(*) FROM plans WHERE run_id='cancel'").fetchone()[0]) == 0
            attempt = runtime._read(lambda c: c.execute("SELECT status,error_code FROM planner_attempts WHERE run_id='cancel'").fetchone())
            assert tuple(attempt) == ("failed", "RUN_CANCELLED")
        finally:
            release.set()
            worker.join(5)


def test_total_deadline_aborts_slow_transport_with_durable_failure(tmp_path):
    release = threading.Event()

    def respond(payload):
        release.wait(3)
        return 200, response()

    with provider(respond) as url:
        planner = OpenAICompatiblePlanner(api_key="offline", base_url=url, model="offline",
                                          timeout_seconds=3, max_tokens=128, max_retries=0)
        planner.total_timeout_seconds = 0.25
        runtime, supervisor = setup_runtime(tmp_path, planner)
        add_run(runtime, "deadline")
        started = time.monotonic()
        try:
            result = supervisor.run_once("worker")
            assert time.monotonic() - started < 1.5
            assert result["status"] == "planning_failed"
            row = runtime._read(lambda c: c.execute("SELECT status,error_code FROM planner_attempts").fetchone())
            assert tuple(row) == ("failed", "LLM_PLANNING_DEADLINE")
        finally:
            release.set()


def test_http_call_budget_is_shared_by_initial_schema_failure_and_repair(tmp_path):
    calls = []

    def respond(payload):
        calls.append(payload)
        return (400, {}) if len(calls) == 1 else (200, response("not JSON"))

    with provider(respond) as url:
        planner = OpenAICompatiblePlanner(api_key="offline", base_url=url, model="offline",
                                          timeout_seconds=2, max_tokens=128, max_retries=3)
        planner.max_http_calls = 2
        runtime, supervisor = setup_runtime(tmp_path, planner)
        add_run(runtime, "budget")
        assert supervisor.run_once("worker")["status"] == "planning_failed"
    assert len(calls) == 2, "repair reset the shared HTTP-call budget"
    row = runtime._read(lambda c: c.execute("SELECT error_code FROM planner_attempts ORDER BY attempt_no DESC").fetchone())
    assert row[0] == "LLM_CALL_BUDGET_EXCEEDED"


def test_ready_task_gets_a_turn_before_another_planning_run(tmp_path, monkeypatch):
    class Planner:
        def create_draft(self, request):
            return draft(request)

    runtime, supervisor = setup_runtime(tmp_path, Planner())
    add_run(runtime, "ready")
    assert supervisor.run_once("worker", run_id="ready")["status"] == "planned"
    add_run(runtime, "new")
    claim_next = supervisor.queue.claim_next

    def observe_claim(*args, **kwargs):
        claim = claim_next(*args, **kwargs)
        assert claim is not None and claim.run_id == "ready"
        raise RuntimeError("READY_TASK_CLAIMED")

    monkeypatch.setattr(supervisor.queue, "claim_next", observe_claim)
    with pytest.raises(RuntimeError, match="READY_TASK_CLAIMED"):
        supervisor.run_once("worker")
    assert runtime.get_run("new")["status"] == "queued"


def test_reclaim_finalizes_only_old_generation_planner_attempt(tmp_path):
    runtime, supervisor = setup_runtime(tmp_path, object())
    add_run(runtime, "lost")
    claim = supervisor.queue.claim_planning_run("old", run_id="lost")
    attempt = runtime.create_planner_attempt("lost", planner_claim=claim, input_revision=1, attempt_no=1,
        phase="initial", provider="test", model="test", prompt_version="test", catalog_hash="test", request_hash="test")
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    runtime._write(lambda c: c.execute("UPDATE runs SET planner_lease_expires_at=? WHERE run_id='lost'", (expired,)))
    new_claim = supervisor.queue.claim_planning_run("new", run_id="lost")
    assert new_claim.generation == claim.generation + 1
    row = runtime._read(lambda c: c.execute("SELECT status,error_code FROM planner_attempts WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone())
    assert tuple(row) == ("failed", "STALE_PLANNER_LEASE")


def test_supplied_worker_stop_event_interrupts_io_and_requeues_owned_run(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    stopping = threading.Event()

    def respond(payload):
        entered.set()
        release.wait(4)
        return 200, response()

    with provider(respond) as url:
        planner = OpenAICompatiblePlanner(api_key="offline", base_url=url, model="offline",
                                          timeout_seconds=4, max_tokens=128, max_retries=0)
        runtime, supervisor = setup_runtime(tmp_path, planner)
        add_run(runtime, "stop")
        worker = threading.Thread(target=supervisor.worker_loop, args=("worker",), kwargs={"stop_event": stopping})
        worker.start()
        try:
            assert entered.wait(3)
            stopping.set()
            worker.join(1.5)
            assert not worker.is_alive(), "external worker stop signal did not reach provider I/O"
            assert runtime.get_run("stop")["status"] == "queued"
            row = runtime._read(lambda c: c.execute("SELECT status,error_code FROM planner_attempts").fetchone())
            assert tuple(row) == ("failed", "WORKER_SHUTDOWN")
        finally:
            release.set()
            worker.join(5)


def test_cancellation_polling_preserves_configured_heartbeat_write_interval(tmp_path, monkeypatch):
    class Planner:
        def create_draft(self, request):
            time.sleep(0.35)
            return draft(request)

    runtime, supervisor = setup_runtime(tmp_path, Planner())
    supervisor.heartbeat_interval = 1.0
    add_run(runtime, "heartbeat")
    heartbeat = supervisor.queue.heartbeat_planning
    writes = []

    def record(claim):
        writes.append(time.monotonic())
        return heartbeat(claim)

    monkeypatch.setattr(supervisor.queue, "heartbeat_planning", record)
    assert supervisor.run_once("worker")["status"] == "planned"
    assert len(writes) == 2, "cancellation polling multiplied durable lease writes"
