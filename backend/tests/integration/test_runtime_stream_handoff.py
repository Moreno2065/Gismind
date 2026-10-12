"""Persisted-event/snapshot interleavings, without a planner or LLM substitute."""

import json

import pytest

from app.api import runtime as api
from app.runtime import RuntimeDB, RuntimeStore
from tests.integration.test_runtime_upload_formats import client


def _store(tmp_path):
    store = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    store.initialize()
    store.create_session("session")
    store.create_run("run", "session")
    return store


def _payloads(frames):
    return [json.loads(line[6:]) for frame in frames for line in frame.splitlines() if line.startswith("data: ")]


@pytest.mark.asyncio
async def test_terminal_commit_between_empty_poll_and_snapshot_is_not_lost(tmp_path, monkeypatch):
    store = _store(tmp_path)
    before = api._snapshot(store, "run")
    cursor = before["last_event_id"]
    original = api._events_after
    first = True

    def poll_then_commit(runtime, run_id, after):
        nonlocal first
        events = original(runtime, run_id, after)
        if first:
            first = False
            assert events == []
            store.update_run_status("run", "failed")
        return events

    monkeypatch.setattr(api, "_events_after", poll_then_commit)
    payloads = _payloads([frame async for frame in api._event_stream(store, "run", cursor)])
    assert [event["type"] for event in payloads] == ["run.status_changed", "run.failed"]
    assert payloads[0]["event_id"] > cursor
    assert payloads[-1]["event_id"] == api._snapshot(store, "run")["last_event_id"]


@pytest.mark.asyncio
async def test_snapshot_then_subscribe_replays_exactly_and_terminal_cursor_closes(tmp_path):
    store = _store(tmp_path)
    before = api._snapshot(store, "run")
    cursor = before["last_event_id"]
    store.update_run_status("run", "planning")
    store.update_run_status("run", "failed")
    payloads = _payloads([frame async for frame in api._event_stream(store, "run", cursor)])
    assert [event["type"] for event in payloads] == ["run.status_changed", "run.status_changed", "run.failed"]
    assert [event["seq"] for event in payloads] == [before["last_seq"] + offset for offset in (1, 2, 3)]
    assert [frame async for frame in api._event_stream(store, "run", payloads[-1]["event_id"])] == []


@pytest.mark.parametrize("via", ["header", "query"])
def test_event_cursor_outside_sqlite_integer_range_is_a_client_error(client, via):
    client.app.state.runtime_store.create_run("cursor-run", "uploads")
    value = str(2**63)
    kwargs = {"headers": {"Last-Event-ID": value}} if via == "header" else {"params": {"after_event_id": value}}
    response = client.get("/api/runs/cursor-run/events", **kwargs)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_EVENT_CURSOR"
