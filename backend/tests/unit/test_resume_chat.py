"""Unit tests for resume_chat control-path correctness (P0-2).

Mocks only the dispatcher graph surface so we can assert:
- invoke is called with a resume payload (not None-only after ended graph)
- PendingStore is cleared only when replan success is observed
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request

from app.agents.pending import PendingTask


def _pending(**overrides) -> PendingTask:
    data = {
        "sub_agent_run_id": "run-await-1",
        "original_request": "南京夫子庙缓冲区",
        "missing_slots": ["distance"],
        "message": "请提供缓冲距离（米）",
        "issues": [],
        "slot_patch_schema": {"distance": {"type": "number", "unit": "m"}},
    }
    data.update(overrides)
    return PendingTask(**data)


def _make_request(body: dict, redis=None) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/chat/sess-1/resume",
        "headers": [],
        "app": SimpleNamespace(state=SimpleNamespace(redis=redis, checkpointer=object(), dispatcher_llm=None)),
    }
    request = Request(scope)

    async def _json():
        return body

    request.json = _json  # type: ignore[method-assign]
    return request


def _enable_resume_claim(store: MagicMock) -> None:
    store.claim = AsyncMock(return_value="claim-token")
    store.release_claim = AsyncMock(return_value=True)


@pytest.mark.asyncio
async def test_resume_chat_invokes_with_payload_not_none_only():
    """After ended graph, resume must re-enter planner with resume payload."""
    from app.api.chat import resume_chat

    pt = _pending()
    store = MagicMock()
    store.load = AsyncMock(return_value=pt)
    store.clear = AsyncMock()
    _enable_resume_claim(store)

    captured: dict = {}

    class FakeApp:
        def update_state(self, config, values, **kwargs):
            captured["update_state"] = (config, values, kwargs)

        def invoke(self, input_values, config=None):
            captured["invoke_input"] = input_values
            captured["invoke_config"] = config
            # Successful replan: planner cleared pending and rewrote user_input.
            return {
                "pending_task": None,
                "user_input": "南京夫子庙缓冲区\n\n用户补充参数：{\"distance\": 500.0}",
                "task_plan": {"tasks": [{"id": "t1", "agent_role": "geometer", "goal": "buffer"}]},
                "final_output": {"status": "ok", "summary": "done"},
            }

        def get_state(self, config):
            return SimpleNamespace(values={"user_input": "x"}, created_at="t")

    with (
        patch("app.agents.pending.PendingStore", return_value=store),
        patch("app.agents.dispatcher.build_dispatcher", return_value=FakeApp()),
        patch("app.agents.checkpointer.get_sqlite_checkpointer", return_value=object()),
    ):
        request = _make_request(
            {"sub_agent_run_id": pt.sub_agent_run_id, "answer": "500米"},
        )
        result = await resume_chat("sess-1", request)

    assert result["status"] == "resumed"
    # Must not be a pure None continue-from-checkpoint no-op after ended graph.
    invoke_input = captured.get("invoke_input")
    assert invoke_input is not None, "resume must re-enter with resume_values, not invoke(None)"
    assert invoke_input.get("pending_task") is not None
    assert invoke_input.get("resume_patch", {}).get("distance") == 500.0
    store.clear.assert_awaited_once_with("sess-1")


@pytest.mark.asyncio
async def test_resume_payload_records_checkpoint_and_prior_execution_state():
    """A resume carries its exact source checkpoint and only prior DAG state."""
    from app.api.chat import resume_chat

    pt = _pending()
    store = MagicMock()
    store.load = AsyncMock(return_value=pt)
    store.clear = AsyncMock()
    _enable_resume_claim(store)
    captured: dict = {}

    class FakeApp:
        def invoke(self, input_values, config=None):
            captured["invoke_input"] = input_values
            return {
                "pending_task": None,
                "user_input": "南京夫子庙缓冲区\n\n用户补充参数：{\"distance\": 500.0}",
                "task_plan": {"tasks": []},
                "final_output": {"status": "ok"},
            }

        def get_state(self, config):
            return SimpleNamespace(
                values={
                    "task_plan": {"tasks": [{"id": "read"}]},
                    "sub_results": {"read": [{"status": "success"}]},
                },
                created_at="t",
                config={"configurable": {"checkpoint_id": "checkpoint-before-resume"}},
            )

    with (
        patch("app.agents.pending.PendingStore", return_value=store),
        patch("app.agents.dispatcher.build_dispatcher", return_value=FakeApp()),
        patch("app.agents.checkpointer.get_sqlite_checkpointer", return_value=object()),
    ):
        result = await resume_chat(
            "sess-1",
            _make_request({"sub_agent_run_id": pt.sub_agent_run_id, "answer": "500米"}),
        )

    assert result["status"] == "resumed"
    resume_input = captured["invoke_input"]
    assert resume_input["resume_provenance"]["source_sub_agent_run_id"] == pt.sub_agent_run_id
    assert resume_input["resume_provenance"]["source_checkpoint_id"] == "checkpoint-before-resume"
    assert resume_input["resume_provenance"]["source_checkpoint_version"] == "checkpoint-before-resume"
    assert resume_input["resume_provenance"]["resume_run_id"] == resume_input["run_id"]
    assert resume_input["resume_prior_task_plan"] == {"tasks": [{"id": "read"}]}
    assert resume_input["resume_prior_sub_results"] == {
        "read": [{"status": "success"}]
    }


@pytest.mark.asyncio
async def test_resume_chat_retains_pending_on_invoke_noop():
    """If invoke succeeds but still looks like awaiting same pending, do not clear."""
    from app.api.chat import resume_chat

    pt = _pending()
    store = MagicMock()
    store.load = AsyncMock(return_value=pt)
    store.clear = AsyncMock()
    _enable_resume_claim(store)

    class FakeApp:
        def update_state(self, config, values, **kwargs):
            return None

        def invoke(self, input_values, config=None):
            # No-op: still awaiting same pending, no replan markers.
            return {
                "pending_task": pt.to_dict(),
                "user_input": "500米",
                "task_plan": {},
                "final_output": {
                    "status": "awaiting_input",
                    "pending_task": pt.to_dict(),
                },
            }

        def get_state(self, config):
            return SimpleNamespace(values={"user_input": "x"}, created_at="t")

    with (
        patch("app.agents.pending.PendingStore", return_value=store),
        patch("app.agents.dispatcher.build_dispatcher", return_value=FakeApp()),
        patch("app.agents.checkpointer.get_sqlite_checkpointer", return_value=object()),
    ):
        request = _make_request(
            {"sub_agent_run_id": pt.sub_agent_run_id, "answer": "500米"},
        )
        result = await resume_chat("sess-1", request)

    assert result["status"] in {"invoke_noop", "invoke_failed"}
    store.clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_chat_retains_pending_when_planner_failed_without_marker():
    """Dispatch must not null pending without replan evidence; API must retain."""
    from app.api.chat import resume_chat

    pt = _pending()
    store = MagicMock()
    store.load = AsyncMock(return_value=pt)
    store.clear = AsyncMock()
    store.save = AsyncMock()
    _enable_resume_claim(store)

    class FakeApp:
        def update_state(self, config, values, **kwargs):
            return None

        def invoke(self, input_values, config=None):
            # Planner failed: no merge marker, pending still present as awaiting.
            return {
                "pending_task": pt.to_dict(),
                "user_input": "500米",
                "task_plan": {"tasks": []},
                "final_output": {
                    "status": "awaiting_input",
                    "pending_task": pt.to_dict(),
                },
            }

        def get_state(self, config):
            return SimpleNamespace(values={"user_input": "x"}, created_at="t")

    with (
        patch("app.agents.pending.PendingStore", return_value=store),
        patch("app.agents.dispatcher.build_dispatcher", return_value=FakeApp()),
        patch("app.agents.checkpointer.get_sqlite_checkpointer", return_value=object()),
    ):
        request = _make_request(
            {"sub_agent_run_id": pt.sub_agent_run_id, "answer": "500米"},
        )
        result = await resume_chat("sess-1", request)

    assert result["status"] in {"invoke_noop", "invoke_failed"}
    store.clear.assert_not_awaited()
    store.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_chat_resaves_new_pending_after_replan_re_pause():
    """If replan immediately re-pauses, replace store entry instead of bare clear."""
    from app.api.chat import resume_chat

    pt = _pending()
    new_pending = {
        "sub_agent_run_id": "run-await-2",
        "original_request": "南京夫子庙缓冲区",
        "missing_slots": ["output_format"],
        "message": "请选择输出格式",
        "issues": [],
        "slot_patch_schema": {"output_format": {"type": "string"}},
    }
    store = MagicMock()
    store.load = AsyncMock(return_value=pt)
    store.clear = AsyncMock()
    store.save = AsyncMock()
    _enable_resume_claim(store)

    class FakeApp:
        def update_state(self, config, values, **kwargs):
            return None

        def invoke(self, input_values, config=None):
            return {
                "pending_task": new_pending,
                "user_input": "南京夫子庙缓冲区\n\n用户补充参数：{\"distance\": 500.0}",
                "task_plan": {"tasks": [{"id": "t1", "agent_role": "geometer", "goal": "buffer"}]},
                "final_output": {
                    "status": "awaiting_input",
                    "pending_task": new_pending,
                    "summary": "请选择输出格式",
                },
            }

        def get_state(self, config):
            return SimpleNamespace(values={"user_input": "x"}, created_at="t")

    with (
        patch("app.agents.pending.PendingStore", return_value=store),
        patch("app.agents.dispatcher.build_dispatcher", return_value=FakeApp()),
        patch("app.agents.checkpointer.get_sqlite_checkpointer", return_value=object()),
    ):
        request = _make_request(
            {"sub_agent_run_id": pt.sub_agent_run_id, "answer": "500米"},
        )
        result = await resume_chat("sess-1", request)

    assert result["status"] == "resumed"
    store.clear.assert_not_awaited()
    store.save.assert_awaited_once()
    saved = store.save.await_args.args[1]
    assert saved.sub_agent_run_id == "run-await-2"


@pytest.mark.asyncio
async def test_resume_chat_retains_pending_when_stale_task_plan_present():
    """Ended checkpoint often still has the first task_plan; that alone must not clear."""
    from app.api.chat import resume_chat

    pt = _pending()
    store = MagicMock()
    store.load = AsyncMock(return_value=pt)
    store.clear = AsyncMock()
    _enable_resume_claim(store)

    class FakeApp:
        def update_state(self, config, values, **kwargs):
            return None

        def invoke(self, input_values, config=None):
            return {
                "pending_task": pt.to_dict(),
                "user_input": "500米",
                # Stale plan from the pre-pause run — must NOT count as replan.
                "task_plan": {
                    "tasks": [{"id": "t1", "agent_role": "geometer", "goal": "buffer"}]
                },
                "final_output": {
                    "status": "awaiting_input",
                    "pending_task": pt.to_dict(),
                },
            }

        def get_state(self, config):
            return SimpleNamespace(values={"user_input": "x"}, created_at="t")

    with (
        patch("app.agents.pending.PendingStore", return_value=store),
        patch("app.agents.dispatcher.build_dispatcher", return_value=FakeApp()),
        patch("app.agents.checkpointer.get_sqlite_checkpointer", return_value=object()),
    ):
        request = _make_request(
            {"sub_agent_run_id": pt.sub_agent_run_id, "answer": "500米"},
        )
        result = await resume_chat("sess-1", request)

    assert result["status"] in {"invoke_noop", "invoke_failed"}
    store.clear.assert_not_awaited()
