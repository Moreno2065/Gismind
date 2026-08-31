"""Unit tests for EventCollector and emit_event."""

import asyncio
import pytest

from app.agents.events import EVENT_CONTRACTS, EventCollector, emit_event


def _make_collector(**kwargs):
    """Helper: create EventCollector with a dedicated loop for sync tests."""
    loop = asyncio.new_event_loop()
    collector = EventCollector(loop=loop, **kwargs)
    return collector, loop


class TestEventCollector:
    """EventCollector unit tests."""

    @pytest.mark.asyncio
    async def test_emit_and_consume_single(self):
        """Basic emit + consume round-trip."""
        collector = EventCollector()
        collector.emit("run.session", "会话开始", session_id="test-session")

        events = []
        async for event in collector.consume():
            events.append(event)
            if len(events) >= 1:
                break

        assert len(events) == 1
        assert events[0]["event"] == "run.session"
        assert events[0]["event_type"] == "run.session"
        assert events[0]["display_kind"] == "progress"
        assert events[0]["session_id"] == "test-session"
        assert "timestamp" in events[0]

    @pytest.mark.asyncio
    async def test_emit_multiple_events_in_order(self):
        """Events are consumed in FIFO order."""
        collector = EventCollector()
        collector.emit("run.session", "start")
        collector.emit("code.generation", "generating")
        collector.emit("code.execution.start", "executing")

        consumed = []
        async for event in collector.consume():
            consumed.append(event["event"])
            if len(consumed) >= 3:
                break

        assert consumed == [
            "run.session",
            "code.generation",
            "code.execution.start",
        ]

    def test_event_contract_fields(self):
        """EVENT_CONTRACTS entries have the correct structure."""
        for name, (event_type, display_kind) in EVENT_CONTRACTS.items():
            assert name == event_type, f"{name}: event_type mismatch"
            assert display_kind in (
                "progress", "debug", "result", "workflow_step",
                "warning", "confirmation",
            ), f"{name}: unknown display_kind {display_kind}"

    def test_all_events_have_contract(self):
        """Collector only accepts events defined in EVENT_CONTRACTS."""
        collector, _loop = _make_collector()
        # Should not raise — unknown event gets (event, "debug") as fallback
        collector.emit("unknown.event", "test")
        item = collector._queue.get_nowait()
        assert item["event"] == "unknown.event"
        assert item["display_kind"] == "debug"

    @pytest.mark.asyncio
    async def test_emit_from_async_and_sync_threads(self):
        """Verify emit works from both async context and sync code path."""
        collector = EventCollector()

        # Direct async emit
        collector.emit("run.thought", "async emit")

        # Simulate sync-thread emit via a callback that calls emit from an
        # executor — the collector should detect it's not the same loop and
        # use call_soon_threadsafe.
        def _sync_emit():
            collector.emit("run.thought", "sync emit")

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _sync_emit)

        consumed = []
        async for event in collector.consume():
            consumed.append(event["message"])
            if len(consumed) >= 2:
                break

        assert "async emit" in consumed
        assert "sync emit" in consumed

    def test_queue_has_consumer(self):
        """queue_has_consumer() reflects the consume() lifecycle."""
        collector, loop = _make_collector()

        async def _scenario():
            it = collector.consume().__aiter__()
            # Start consuming
            task = loop.create_task(it.__anext__())
            await asyncio.sleep(0.01)
            assert collector.queue_has_consumer() is True

            collector.emit("run.session", "done")
            item = await task
            assert item["event"] == "run.session"
            assert collector.queue_has_consumer() is True

            loop.create_task(it.aclose())
            await asyncio.sleep(0.01)
            assert collector.queue_has_consumer() is False

        loop.run_until_complete(_scenario())

    def test_mark_no_consumer(self):
        collector, _loop = _make_collector()
        collector.mark_no_consumer()
        assert collector.queue_has_consumer() is False

    def test_emit_event_none_handler(self):
        """emit_event with None handler should not raise."""
        emit_event(None, "test", "message")

    def test_emit_event_with_handler(self):
        """emit_event calls the handler with the event dict."""
        received = []

        def handler(item: dict):
            received.append(item)

        emit_event(handler, "run.thought", "hello", extra="value")
        assert len(received) == 1
        assert received[0]["event"] == "run.thought"
        assert received[0]["message"] == "hello"
        assert received[0]["extra"] == "value"

    def test_emit_event_handler_raises(self):
        """emit_event should not raise when handler raises."""

        def _failing(_item):
            raise ValueError("boom")

        emit_event(_failing, "test", "x")  # Should not propagate

    def test_dedup_preflight_issues(self):
        """Dedup deduplicates preflight/postflight issues per (stage, code, tool_name)."""
        collector, _loop = _make_collector()

        # Emit the same preflight warning twice
        collector.emit("tool.preflight.warning", "CRS mismatch",
                       stage="preflight", code="buffer_crs_mismatch", tool_name="buffer")
        collector.emit("tool.preflight.warning", "CRS mismatch",
                       stage="preflight", code="buffer_crs_mismatch", tool_name="buffer")

        assert collector._queue.qsize() == 1, "second identical event should be deduped"

    def test_dedup_stdout_not_deduped(self):
        """stdout/stderr events are never deduped."""
        collector, _loop = _make_collector()

        collector.emit("code.execution.stdout", "line 1")
        collector.emit("code.execution.stdout", "line 2")

        assert collector._queue.qsize() == 2

    def test_clear_dedup(self):
        """clear_dedup resets the dedup set."""
        collector, _loop = _make_collector()
        collector.emit("tool.preflight.warning", "warn",
                       stage="preflight", code="test", tool_name="tool")
        assert collector._queue.qsize() == 1

        collector.clear_dedup()
        collector.emit("tool.preflight.warning", "warn again",
                       stage="preflight", code="test", tool_name="tool")
        assert collector._queue.qsize() == 2

    def test_queue_is_bounded_and_preserves_latest_events(self):
        """A stalled SSE client cannot grow the process queue without bound."""
        collector, _loop = _make_collector(max_queue_size=3)

        for index in range(5):
            collector.emit("code.execution.stdout", f"line {index}")

        assert collector._queue.qsize() == 3
        assert collector.dropped_count == 2
        assert [collector._queue.get_nowait()["message"] for _ in range(3)] == [
            "line 2", "line 3", "line 4",
        ]

    @pytest.mark.asyncio
    async def test_stop_unblocks_a_full_bounded_queue(self):
        """The stop sentinel must survive backpressure and terminate consume()."""
        collector = EventCollector(max_queue_size=2)
        collector.emit("code.execution.stdout", "line 1")
        collector.emit("code.execution.stdout", "line 2")
        collector.stop()

        received = []
        async for event in collector.consume():
            received.append(event["message"])

        assert received == ["line 2"]

    def test_event_has_required_fields(self):
        """All events from EVENT_CONTRACTS have event, event_type, display_kind, message, timestamp."""
        collector, _loop = _make_collector()
        collector.emit("tool.preflight.blocked", "CRS mismatch")
        item = collector._queue.get_nowait()
        assert item["event_type"] == "tool.preflight.blocked"
        assert item["display_kind"] == "warning"
        assert "timestamp" in item
        assert "event" in item
        assert "message" in item

    @pytest.mark.asyncio
    async def test_consume_stops_on_mark_no_consumer(self):
        """consume generator stops after mark_no_consumer."""
        collector = EventCollector()

        async def _consume():
            result = []
            async for event in collector.consume():
                result.append(event)
                if len(result) >= 2:
                    break
            return result

        task = asyncio.create_task(_consume())
        await asyncio.sleep(0.01)

        collector.emit("run.session", "e1")
        collector.emit("run.thought", "e2")
        collector.mark_no_consumer()

        # The consumer should still get its break at 2 items
        result = await asyncio.wait_for(task, timeout=2.0)
        assert len(result) == 2
