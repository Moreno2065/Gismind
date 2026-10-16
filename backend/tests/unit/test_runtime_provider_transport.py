"""Offline transport fault injection, never real-model acceptance evidence."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import httpx
import pytest

from app.runtime import ArtifactStore, RuntimeDB, RuntimeStore, RuntimeSupervisor
from app.runtime.llm_planner import OpenAICompatiblePlanner, PlannerError
from app.runtime.planning_control import PlanningBudget, PlanningControlError


MESSAGES = [{"role": "user", "content": "offline transport fixture only"}]
BODY = {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}}


def planner(*, retries=0, base_url="https://offline.invalid/v1", **kwargs):
    return OpenAICompatiblePlanner(api_key="offline-only", base_url=base_url,
        model="offline-transport-fixture", timeout_seconds=2, max_tokens=128,
        max_retries=retries, **kwargs)


def mock_transport(monkeypatch, outcomes):
    """Keep the real HTTPX request/response boundary; replace only network I/O."""
    calls = []
    real_client = httpx.AsyncClient

    async def handle(request):
        calls.append(request)
        outcome = outcomes[len(calls) - 1]
        if callable(outcome):
            outcome = outcome()
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, httpx.Response):
            return outcome
        return httpx.Response(200, json=outcome)

    monkeypatch.setattr("app.runtime.llm_planner.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), trust_env=False, **kwargs))
    return calls


@contextmanager
def chunked_provider(outcomes):
    """A local HTTP server whose partial response lacks the terminating chunk."""
    calls = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            if outcomes[len(calls) - 1] == "cut":
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.write(b'10\r\n{"choices":')
            else:
                data = json.dumps(BODY).encode()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            self.wfile.flush()
            self.close_connection = True

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize("retries", [0, 1])
def test_real_incomplete_chunk_is_unavailable_after_exact_retry_allowance(retries):
    with chunked_provider(["cut"] * (retries + 1)) as (url, calls):
        with pytest.raises(PlannerError) as caught:
            planner(retries=retries, base_url=url)._call(MESSAGES)
        assert len(calls) == retries + 1
    assert caught.value.code == "LLM_UNAVAILABLE"
    assert str(caught.value) == "LLM provider could not be reached"
    assert caught.value.diagnostics["exception_type"] == "RemoteProtocolError"
    assert "RemoteProtocolError" in caught.value.diagnostics["cause_chain_types"]


def test_real_incomplete_chunk_retry_success_counts_both_http_calls():
    with chunked_provider(["cut", "complete"]) as (url, calls):
        body, _ = planner(retries=1, base_url=url, max_http_calls=2)._call(MESSAGES)
        assert len(calls) == 2
    assert body["usage"]["planner_http_calls"] == 2
    assert body["usage"]["planner_compatibility_retry"] is False
    assert body["choices"] == BODY["choices"]


def test_real_incomplete_chunk_has_durable_provider_failure_not_compile_failure(tmp_path):
    with chunked_provider(["cut"]) as (url, calls):
        runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
        runtime.initialize()
        runtime.create_session("offline")
        runtime.create_run("cut", "offline")
        runtime.add_input_revision("cut", 1, {"prompt": "查询南京", "input_artifact_ids": []})
        supervisor = RuntimeSupervisor(runtime, ArtifactStore(tmp_path / "cas", runtime),
            planner=planner(base_url=url), isolate=False, autostart=False)
        assert supervisor.run_once("offline", run_id="cut")["status"] == "planning_failed"
        assert len(calls) == 1
    attempts = runtime._read(lambda c: c.execute(
        "SELECT error_code,diagnostics_json,draft_json,response_hash FROM planner_attempts").fetchall())
    assert len(attempts) == 1
    assert attempts[0]["error_code"] == "LLM_UNAVAILABLE"
    diagnostic = json.loads(attempts[0]["diagnostics_json"])
    assert diagnostic["path"] == "planner_response"
    assert diagnostic["exception_type"] == "RemoteProtocolError"
    assert attempts[0]["draft_json"] is None and attempts[0]["response_hash"] is None
    assert runtime._read(lambda c: c.execute("SELECT COUNT(*) FROM plans").fetchone()[0]) == 0
    assert runtime._read(lambda c: c.execute("SELECT COUNT(*) FROM task_attempts").fetchone()[0]) == 0
    assert not any(event["event_type"] == "task.started" for event in runtime.list_events("cut"))


@pytest.mark.parametrize("error_class", [httpx.RemoteProtocolError, httpx.ReadTimeout,
    httpx.ConnectTimeout, httpx.ReadError, httpx.ConnectError])
@pytest.mark.parametrize("retries", [0, 1])
def test_retryable_errors_with_empty_messages_have_safe_typed_diagnostics(monkeypatch, error_class, retries):
    calls = mock_transport(monkeypatch, [error_class("") for _ in range(retries + 1)])
    with pytest.raises(PlannerError) as caught:
        planner(retries=retries)._call(MESSAGES)
    assert len(calls) == retries + 1
    assert caught.value.code == "LLM_UNAVAILABLE"
    assert str(caught.value) == "LLM provider could not be reached"
    assert caught.value.diagnostics["error"] == error_class.__name__
    assert caught.value.diagnostics["exception_type"] == error_class.__name__
    assert caught.value.diagnostics["cause_chain_types"] == []


def test_diagnostics_keep_only_types_not_request_or_cause_text(monkeypatch):
    secret = "https://user:credential@example.invalid/private?token=secret raw-response-body"
    error = httpx.RemoteProtocolError(secret, request=httpx.Request("POST", secret.split()[0]))
    error.__cause__ = OSError(secret)
    error.__cause__.__cause__ = ValueError(secret)
    mock_transport(monkeypatch, [error])
    with pytest.raises(PlannerError) as caught:
        planner()._call(MESSAGES)
    assert caught.value.diagnostics == {"error": "RemoteProtocolError",
        "exception_type": "RemoteProtocolError", "cause_chain_types": ["OSError", "ValueError"]}
    assert "credential" not in json.dumps(caught.value.diagnostics)
    assert "raw-response-body" not in json.dumps(caught.value.diagnostics)


@pytest.mark.parametrize("suppressed", [False, True])
def test_cause_chain_respects_explicit_suppression(monkeypatch, suppressed):
    error = httpx.ReadError("")
    error.__context__ = OSError("not retained")
    error.__suppress_context__ = suppressed
    mock_transport(monkeypatch, [error])
    with pytest.raises(PlannerError) as caught:
        planner()._call(MESSAGES)
    assert caught.value.diagnostics["cause_chain_types"] == ([] if suppressed else ["OSError"])


def test_cyclic_cause_chain_is_bounded(monkeypatch):
    error = httpx.ReadError("")
    error.__cause__ = OSError("not retained")
    error.__cause__.__cause__ = error
    mock_transport(monkeypatch, [error])
    with pytest.raises(PlannerError) as caught:
        planner()._call(MESSAGES)
    assert caught.value.diagnostics["cause_chain_types"] == ["OSError"]


def test_long_cause_chain_is_bounded(monkeypatch):
    error = httpx.ReadError("")
    tail = error
    for _ in range(20):
        tail.__cause__ = OSError("not retained")
        tail = tail.__cause__
    mock_transport(monkeypatch, [error])
    with pytest.raises(PlannerError) as caught:
        planner()._call(MESSAGES)
    assert caught.value.diagnostics["cause_chain_types"] == ["OSError"] * 8


@pytest.mark.parametrize("max_calls", [2, 3])
def test_compatibility_fallback_protocol_retry_keeps_payload_and_shared_budget(monkeypatch, max_calls):
    calls = mock_transport(monkeypatch, [httpx.Response(400, json={}),
        httpx.RemoteProtocolError("cut during compatibility response"), BODY])
    instance = planner(retries=1, max_http_calls=max_calls)
    if max_calls == 2:
        with pytest.raises(PlanningControlError) as caught:
            instance._call(MESSAGES)
        assert caught.value.code == "LLM_CALL_BUDGET_EXCEEDED"
    else:
        body, _ = instance._call(MESSAGES)
        assert body["usage"]["planner_http_calls"] == 3
        assert body["usage"]["planner_compatibility_retry"] is True
    assert len(calls) == max_calls
    payloads = [json.loads(request.content) for request in calls]
    assert "thinking" in payloads[0]
    for payload in payloads[1:]:
        assert payload["response_format"] == {"type": "json_object"}
        assert all(field not in payload for field in ("thinking", "enable_thinking", "reasoning_effort"))
    if max_calls == 3:
        assert payloads[1] == payloads[2]


def test_transport_retry_cannot_reset_shared_http_budget(monkeypatch):
    calls = mock_transport(monkeypatch, [httpx.RemoteProtocolError("cut"), BODY])
    budget = PlanningBudget(3, 1)
    with pytest.raises(PlanningControlError) as caught:
        asyncio.run(planner(retries=1)._call_async(MESSAGES, budget))
    assert caught.value.code == "LLM_CALL_BUDGET_EXCEEDED"
    assert len(calls) == budget.http_calls == 1


def test_transport_retry_cannot_extend_total_deadline(monkeypatch):
    calls = mock_transport(monkeypatch, [httpx.RemoteProtocolError("cut"), BODY])
    budget = PlanningBudget(0.1, 2)
    with pytest.raises(PlanningControlError) as caught:
        asyncio.run(planner(retries=1)._call_async(MESSAGES, budget))
    assert caught.value.code == "LLM_PLANNING_DEADLINE"
    assert len(calls) == budget.http_calls == 1


@pytest.mark.parametrize("code", ["RUN_CANCELLED", "STALE_PLANNER_LEASE", "WORKER_SHUTDOWN"])
def test_transport_retry_backoff_is_interruptible(monkeypatch, code):
    budget = PlanningBudget(3, 2)

    def interrupted():
        asyncio.get_running_loop().call_later(0.01, budget.cancel, code)
        return httpx.RemoteProtocolError("cut")

    calls = mock_transport(monkeypatch, [interrupted, BODY])
    with pytest.raises(PlanningControlError) as caught:
        asyncio.run(planner(retries=1)._call_async(MESSAGES, budget))
    assert caught.value.code == code
    assert len(calls) == budget.http_calls == 1


@pytest.mark.parametrize("error", [httpx.LocalProtocolError("bad request"),
    httpx.UnsupportedProtocol("bad scheme"), httpx.InvalidURL("bad URL"),
    httpx.DecodingError("bad encoding"), ValueError("bad configuration"), asyncio.CancelledError()])
def test_nonretryable_errors_and_cancellation_propagate_unchanged(monkeypatch, error):
    calls = mock_transport(monkeypatch, [error, BODY])
    with pytest.raises(type(error)) as caught:
        planner(retries=1)._call(MESSAGES)
    assert caught.value is error
    assert len(calls) == 1
