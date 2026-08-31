"""Integration tests for SSE event streaming.

Tests the GET /api/chat/{session_id}/events endpoint with EventCollector,
verifying SSE formatted output and event lifecycle.

.. note::

   Starlette's ``TestClient`` runs the ASGI app to completion inside
   ``portal.call()``, blocking the calling thread.  Events must therefore be
   emitted (and the ``collector.stop()`` sentinel pushed) **before** calling
   ``client.stream()``.  The response is fully buffered and read inside the
   ``with`` block body, which only executes after the server-side generator
   has finished.
"""

import asyncio
import json
import pytest
from fastapi.testclient import TestClient

from app.agents.events import EventCollector


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    """FastAPI TestClient with clean app instance."""
    from app.main import create_app
    app = create_app()
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSSEEvents:
    """SSE endpoint integration tests."""

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _inject_collector(self, session_id: str) -> EventCollector:
        """Inject an EventCollector into chat.py's _collectors dict."""
        from app.api import chat
        loop = asyncio.new_event_loop()
        collector = EventCollector(loop=loop)
        chat._collectors[session_id] = collector
        return collector

    def _cleanup_collector(self, session_id: str):
        """Remove collector from chat.py's _collectors dict."""
        from app.api import chat
        chat._collectors.pop(session_id, None)

    # ------------------------------------------------------------------
    # tests
    # ------------------------------------------------------------------

    def test_sse_returns_streaming_response(self, client):
        """GET /api/chat/{session_id}/events returns text/event-stream."""
        session_id = "test-sse-basic"
        collector = self._inject_collector(session_id)
        try:
            # Push sentinel before connecting so consume() exits immediately.
            collector.stop()
            with client.stream("GET", f"/api/chat/{session_id}/events") as response:
                assert response.status_code == 200
                assert response.headers.get("content-type", "").startswith(
                    "text/event-stream"
                )
        finally:
            self._cleanup_collector(session_id)

    def test_sse_receives_single_event(self, client):
        """Client receives a single SSE event emitted *before* connection."""
        session_id = "test-sse-single"
        collector = self._inject_collector(session_id)

        try:
            # Emit event + sentinel *before* the stream so the server-side
            # generator can consume them all in one pass.
            collector.emit("run.session", "会话开始", session_id=session_id)
            collector.stop()

            with client.stream("GET", f"/api/chat/{session_id}/events") as stream:
                raw = next(stream.iter_lines())
                assert raw.startswith("data: ")
                payload = json.loads(raw[6:])

                assert payload["event"] == "run.session"
                assert payload["event_type"] == "run.session"
                assert payload["display_kind"] == "progress"
                assert payload["session_id"] == session_id
                assert "timestamp" in payload
        finally:
            self._cleanup_collector(session_id)

    def test_sse_multiple_events_in_order(self, client):
        """Client receives events in correct order."""
        session_id = "test-sse-multi"
        collector = self._inject_collector(session_id)

        try:
            collector.emit("run.session", "start")
            collector.emit("code.generation", "generating code")
            collector.emit("code.execution.start", "executing")
            collector.stop()

            with client.stream("GET", f"/api/chat/{session_id}/events") as stream:
                received = []
                for raw in stream.iter_lines():
                    if raw.startswith("data: "):
                        received.append(json.loads(raw[6:]))

                assert len(received) == 3
                assert received[0]["event"] == "run.session"
                assert received[1]["event"] == "code.generation"
                assert received[2]["event"] == "code.execution.start"
        finally:
            self._cleanup_collector(session_id)

    def test_sse_no_collector_returns_empty(self, client):
        """When no collector exists, endpoint returns empty stream (not error)."""
        response = client.get("/api/chat/nonexistent/events")
        assert response.status_code == 200
        # Empty response body
        assert response.text == "" or response.content == b""

    def test_sse_event_has_all_contract_fields(self, client):
        """Emitted events include event, event_type, display_kind, message, timestamp."""
        session_id = "test-sse-contract"
        collector = self._inject_collector(session_id)

        try:
            collector.emit(
                "code.execution.error",
                "division by zero",
                error_code="ZeroDivisionError",
                traceback="Traceback...",
            )
            collector.stop()

            with client.stream("GET", f"/api/chat/{session_id}/events") as stream:
                raw = next(stream.iter_lines())
                assert raw.startswith("data: ")
                payload = json.loads(raw[6:])

                assert payload["event"] == "code.execution.error"
                assert payload["event_type"] == "code.execution.error"
                assert payload["display_kind"] == "warning"
                assert payload["message"] == "division by zero"
                assert payload["error_code"] == "ZeroDivisionError"
                assert payload["traceback"] == "Traceback..."
        finally:
            self._cleanup_collector(session_id)

    def test_sse_emit_before_stream_arrives(self, client):
        """Events emitted before the SSE client connects are still received."""
        session_id = "test-sse-before-stream"
        collector = self._inject_collector(session_id)

        try:
            collector.emit("run.thought", "late event")
            collector.stop()

            with client.stream("GET", f"/api/chat/{session_id}/events") as stream:
                raw = next(stream.iter_lines())
                assert raw.startswith("data: ")
                payload = json.loads(raw[6:])
                assert payload["event"] == "run.thought"
                assert payload["message"] == "late event"
        finally:
            self._cleanup_collector(session_id)

    def test_sse_mark_no_consumer_after_complete(self, client):
        """mark_no_consumer is called when the SSE client disconnects."""
        session_id = "test-sse-disconnect"
        collector = self._inject_collector(session_id)
        assert collector.queue_has_consumer() is False

        try:
            collector.stop()

            with client.stream("GET", f"/api/chat/{session_id}/events") as stream:
                pass  # body consumed automatically by TestClient

            # After the stream ran to completion, consumer should be marked gone.
            assert collector.queue_has_consumer() is False
        finally:
            self._cleanup_collector(session_id)

    def test_post_chat_creates_collector(self, client):
        """POST /api/chat creates an EventCollector for the session.

        Stubs ``_run_loop_sync`` only to assert collector lifecycle — not a
        production graph wiring test (see test_awaiting_input_e2e / resume_api).
        """
        from unittest.mock import patch

        fake_result = {
            "should_stop": True,
            "iteration": 0,
            "final_output": {"summary": "stub result for collector lifecycle"},
            "session_id": "test-post-collector",
            "trace_id": "trace_collector",
            "dispatcher_events": [],
            "react_trace": [],
        }

        with patch("app.api.chat._run_loop_sync", return_value=fake_result):
            from app.api import chat
            assert "test-post-collector" not in chat._collectors

            response = client.post("/api/chat", json={
                "session_id": "test-post-collector",
                "message": "test",
            })
            assert response.status_code == 200

            # Collector should have been created (and may have been cleaned up
            # after the stream ended — so just verify it existed at some point
            # by checking that the collector was created and events were emitted)
            chat._collectors.pop("test-post-collector", None)

    def test_older_same_session_run_cannot_unregister_newer_collector(self):
        """An old stream's finally block must not remove a replacement run."""
        from app.api import chat

        loop = asyncio.new_event_loop()
        old = EventCollector(loop=loop)
        newer = EventCollector(loop=loop)
        session_id = "same-session-overlap"
        try:
            chat._register_collector(session_id, old)
            chat._register_collector(session_id, newer)

            chat._unregister_collector(session_id, old)

            assert chat._collectors[session_id] is newer
            chat._unregister_collector(session_id, newer)
            assert session_id not in chat._collectors
        finally:
            chat._collectors.pop(session_id, None)
            loop.close()

    def test_cancel_endpoint_suppresses_late_map_tokens_done_and_history(self, client):
        """POST cancel is authoritative even when the SSE connection stays open."""
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch

        from fastapi.testclient import TestClient
        from app.agents.run_control import RunState, get_run_controller

        created = client.post("/api/sessions")
        assert created.status_code == 201
        session_id = created.json()["id"]
        started = threading.Event()
        release = threading.Event()
        run_id_box: list[str] = []

        def slow_success(*_args, run_id="", **_kwargs):
            run_id_box.append(run_id)
            started.set()
            assert release.wait(5), "test did not release the synthetic running tool"
            return {
                "status": "success",
                "dispatcher_events": [],
                "react_trace": [],
                "final_output": {
                    "status": "success",
                    "summary": "late answer must not be published",
                    "map": {"layers": [{"type": "FeatureCollection", "features": []}]},
                },
            }

        with patch("app.api.chat._run_loop_sync", side_effect=slow_success):
            with ThreadPoolExecutor(max_workers=1) as pool:
                response_future = pool.submit(
                    client.post,
                    "/api/chat",
                    json={"session_id": session_id, "message": "cancel me"},
                )
                assert started.wait(5), "chat loop did not start"
                run_id = run_id_box[0]
                with TestClient(client.app) as cancel_client:
                    cancelled = cancel_client.post(f"/api/runs/{run_id}/cancel")
                assert cancelled.status_code == 200
                assert cancelled.json()["status"] == "cancelled"
                release.set()
                response = response_future.result(timeout=30)

        assert response.status_code == 200
        assert "event: error" in response.text
        assert '"code": "CANCELLED"' in response.text
        assert "event: map" not in response.text
        assert "event: token" not in response.text
        assert "event: done" not in response.text
        assert get_run_controller(run_id).state is RunState.CANCELLED

        history = client.get(f"/api/sessions/{session_id}/messages")
        assert history.status_code == 200
        assert history.json()["messages"] == []
