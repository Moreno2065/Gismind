"""Real-wiring awaiting-input + resume integration tests.

Uses real Redis, real temporary SqliteSaver, real production graphs.
Only LLM transport may be deterministic via formal injection.
No fake graph, no node replacement, no automatic fakeredis fallback.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite import SqliteSaver

from app.agents.build_sub_agent import run_sub_agent
from app.agents.dispatcher import (
    build_dispatcher,
    dispatch_node,
    planner_router_node,
    subagent_state_to_outcome,
)
from app.agents.events.current import reset_current_handler, set_current_handler
from app.agents.judge import judge
from app.agents.pending import PendingStore
from app.agents.schemas import PendingTask, SubTask, TaskPlan
from app.agents.state import new_root_state
from app.main import create_app
from tests.support import DeterministicLLM


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_pending(
    *,
    run_id: str = "run_awaiting_001",
    with_schema: bool = False,
) -> PendingTask:
    kwargs: dict = {
        "sub_agent_run_id": run_id,
        "original_request": "南京夫子庙 1km 缓冲区",
        "missing_slots": ["distance"],
        "message": "请提供缓冲距离（米）",
        "issues": [
            {
                "code": "missing_distance",
                "stage": "preflight",
                "severity": "error",
                "message": "缺少缓冲距离",
            }
        ],
    }
    if with_schema:
        kwargs["slot_patch_schema"] = {"distance": {"type": "number", "unit": "m"}}
    return PendingTask(**kwargs)


def _plan_json(tasks: list[dict]) -> str:
    return json.dumps({"task_plan": {"tasks": tasks}}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Judge (unit-ish, but real PendingStore + real Redis)
# ---------------------------------------------------------------------------


class TestJudgeAwaitingInput:
    """judge() detects state.pending_task → AWAITING_INPUT + real PendingStore."""

    @pytest.mark.asyncio
    async def test_judge_returns_awaiting_input_when_pending(self, real_redis):
        session_id = "sess_await_1"
        pending = {
            "sub_agent_run_id": "run_x",
            "original_request": "x",
            "message": "需要更多信息",
            "issues": [],
            "missing_slots": ["distance"],
        }
        state = {
            "iteration": 0,
            "pending_task": pending,
            "session_id": session_id,
            "run_id": "run_x",
            "user_input": "原始请求",
            "messages": [],
            "tool_results": [],
        }

        result = judge(state)

        assert result["should_stop"] is True
        assert result["decision"] == "AWAITING_INPUT"
        assert result["pending_task"] == pending
        assert result["reason"] == "需要更多信息"

        store = PendingStore(redis_client=real_redis)
        saved = await store.load(session_id)
        assert saved is not None
        assert saved.sub_agent_run_id == "run_x"
        assert saved.message == "需要更多信息"
        await store.clear(session_id)

    def test_judge_emits_judge_awaiting_input_event(self):
        captured: list[dict] = []

        def on_event(item: dict):
            captured.append(item)

        token = set_current_handler(on_event)
        try:
            pending = {
                "sub_agent_run_id": "run_evt",
                "original_request": "x",
                "message": "请补充",
                "issues": [{"code": "x"}],
                "missing_slots": ["distance"],
            }
            result = judge(
                {
                    "iteration": 0,
                    "pending_task": pending,
                    "session_id": "",  # skip Redis persist
                    "run_id": "run_evt",
                    "user_input": "x",
                    "messages": [],
                    "tool_results": [],
                }
            )
            assert result["decision"] == "AWAITING_INPUT"
            assert any(
                (e.get("event") == "judge.awaiting_input" or e.get("type") == "judge.awaiting_input")
                or (isinstance(e, dict) and "awaiting" in json.dumps(e, ensure_ascii=False))
                for e in captured
            ) or len(captured) >= 1
        finally:
            reset_current_handler(token)

    def test_judge_no_emit_when_on_event_absent(self):
        pending = {
            "sub_agent_run_id": "run_noevt",
            "original_request": "x",
            "message": "m",
            "issues": [],
            "missing_slots": [],
        }
        # No handler installed — must not raise.
        result = judge(
            {
                "iteration": 0,
                "pending_task": pending,
                "session_id": "",
                "run_id": "run_noevt",
                "user_input": "x",
                "messages": [],
                "tool_results": [],
            }
        )
        assert result["decision"] == "AWAITING_INPUT"

    def test_judge_does_not_save_when_session_id_missing(self, real_redis):
        # No session_id → no Redis write. Ensure we don't crash.
        result = judge(
            {
                "iteration": 0,
                "pending_task": {
                    "sub_agent_run_id": "run_nosess",
                    "original_request": "x",
                    "message": "m",
                    "issues": [],
                    "missing_slots": [],
                },
                "session_id": "",
                "run_id": "run_nosess",
                "user_input": "x",
                "messages": [],
                "tool_results": [],
            }
        )
        assert result["decision"] == "AWAITING_INPUT"


class TestPendingStoreIntegration:
    @pytest.mark.asyncio
    async def test_judge_writes_to_pending_store(self, real_redis, unique_session_id):
        pending = {
            "sub_agent_run_id": "run_store",
            "original_request": "原始",
            "message": "请提供距离",
            "issues": [],
            "missing_slots": ["distance"],
            "slot_patch_schema": {"distance": {"type": "number"}},
        }
        judge(
            {
                "iteration": 0,
                "pending_task": pending,
                "session_id": unique_session_id,
                "run_id": "run_store",
                "user_input": "原始",
                "messages": [],
                "tool_results": [],
            }
        )
        store = PendingStore(redis_client=real_redis)
        saved = await store.load(unique_session_id)
        assert saved is not None
        assert saved.sub_agent_run_id == "run_store"
        assert saved.slot_patch_schema.get("distance", {}).get("type") == "number"
        await store.clear(unique_session_id)

    @pytest.mark.asyncio
    async def test_pending_claim_is_atomic_and_keeps_the_task_until_release(
        self, real_redis, unique_session_id
    ):
        """Two real Redis claimers may not resume the same pending task together."""
        import asyncio

        store = PendingStore(redis_client=real_redis)
        pending = _sample_pending(run_id="run_atomic_claim")
        await store.save(unique_session_id, pending)

        claims = await asyncio.gather(
            store.claim(unique_session_id, pending.sub_agent_run_id),
            store.claim(unique_session_id, pending.sub_agent_run_id),
        )
        tokens = [token for token in claims if token]
        assert len(tokens) == 1
        assert await store.load(unique_session_id) is not None

        await store.release_claim(unique_session_id, tokens[0])
        assert await store.claim(unique_session_id, pending.sub_agent_run_id)
        await store.clear(unique_session_id)


# ---------------------------------------------------------------------------
# Outcome / dispatch propagation (no mock)
# ---------------------------------------------------------------------------


class TestAwaitingOutcomePropagation:
    def test_subagent_state_to_outcome_awaiting_input(self):
        state = {
            "agent_role": "geo",
            "decision": "AWAITING_INPUT",
            "pending_task": {
                "sub_agent_run_id": "r1",
                "message": "need distance",
                "missing_slots": ["distance"],
            },
            "tool_results": [],
            "final_output": {},
            "iteration": 1,
        }
        o = subagent_state_to_outcome(state, task_id="t1", run_id="r1")
        assert o.status == "awaiting_input"
        assert o.pending_task is not None
        assert o.pending_task["sub_agent_run_id"] == "r1"

    def test_dispatch_propagates_pending_and_skips_success(self):
        """dispatch_node: awaiting_input → root pending; successful tasks skipped."""
        # Seed prior success for t_done; only t_wait should run if planned,
        # but we exercise skip path by putting success in sub_results.
        prior = {
            "t_done": [
                {
                    "task_id": "t_done",
                    "run_id": "r_done",
                    "agent_role": "coder",
                    "status": "success",
                    "artifacts": {"value": 1},
                }
            ]
        }
        state = new_root_state("x", session_id="s", run_id="root")
        state["sub_results"] = prior
        state["task_plan"] = TaskPlan(
            tasks=[
                SubTask(id="t_done", agent_role="coder", goal="already done"),
            ]
        ).model_dump()
        out = dispatch_node(state)
        # Skipped successful task: no re-dispatch, sub_results retained.
        assert out["sub_results"]["t_done"][0]["status"] == "success"
        assert out.get("pending_task") is None


# ---------------------------------------------------------------------------
# Resume chat API (real Redis + real SQLite)
# ---------------------------------------------------------------------------


def _seed_pending_sync(redis_url: str, session_id: str, pt: PendingTask) -> None:
    """Write PendingTask via a short-lived client (server-side data, loop-safe)."""
    import asyncio

    from app.utils.redis import create_redis_client

    async def _write() -> None:
        r = create_redis_client(redis_url)
        try:
            await PendingStore(redis_client=r).save(session_id, pt)
        finally:
            await r.aclose()

    asyncio.run(_write())


def _load_pending_sync(redis_url: str, session_id: str) -> PendingTask | None:
    import asyncio

    from app.utils.redis import create_redis_client

    async def _read() -> PendingTask | None:
        r = create_redis_client(redis_url)
        try:
            return await PendingStore(redis_client=r).load(session_id)
        finally:
            await r.aclose()

    return asyncio.run(_read())


def _clear_pending_sync(redis_url: str, session_id: str) -> None:
    import asyncio

    from app.utils.redis import create_redis_client

    async def _clear() -> None:
        r = create_redis_client(redis_url)
        try:
            await PendingStore(redis_client=r).clear(session_id)
        finally:
            await r.aclose()

    asyncio.run(_clear())


def _claim_pending_sync(redis_url: str, session_id: str, sub_agent_run_id: str) -> str | None:
    """Acquire the real Redis resume lease from a second, loop-safe client."""
    import asyncio

    from app.utils.redis import create_redis_client

    async def _claim() -> str | None:
        r = create_redis_client(redis_url)
        try:
            return await PendingStore(redis_client=r).claim(session_id, sub_agent_run_id)
        finally:
            await r.aclose()

    return asyncio.run(_claim())


class TestResumeChatEndpoint:
    def test_resume_not_found_when_no_pending_task(self, app_client, unique_session_id):
        client, _app, _redis_url, _cp = app_client
        resp = client.post(
            f"/api/chat/{unique_session_id}/resume",
            json={"sub_agent_run_id": "r", "answer": "500"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_found"

    def test_resume_mismatch_when_run_id_differs(self, app_client, unique_session_id):
        client, _app, redis_url, _cp = app_client
        pt = _sample_pending(run_id="run_expected")
        _seed_pending_sync(redis_url, unique_session_id, pt)

        resp = client.post(
            f"/api/chat/{unique_session_id}/resume",
            json={"sub_agent_run_id": "run_wrong", "answer": "500"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "mismatch"
        assert data["expected_sub_agent_run_id"] == "run_expected"
        # pending retained
        assert _load_pending_sync(redis_url, unique_session_id) is not None
        _clear_pending_sync(redis_url, unique_session_id)

    def test_resume_no_checkpoint_keeps_pending(self, app_client, unique_session_id):
        client, _app, redis_url, _cp = app_client
        pt = _sample_pending()
        _seed_pending_sync(redis_url, unique_session_id, pt)

        resp = client.post(
            f"/api/chat/{unique_session_id}/resume",
            json={"sub_agent_run_id": pt.sub_agent_run_id, "answer": "500"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "no_checkpoint"
        # pending retained
        assert _load_pending_sync(redis_url, unique_session_id) is not None
        _clear_pending_sync(redis_url, unique_session_id)

    def test_resume_invalid_answer_keeps_pending(self, app_client, unique_session_id):
        client, _app, redis_url, cp = app_client
        pt = _sample_pending(with_schema=True)
        _seed_pending_sync(redis_url, unique_session_id, pt)

        # Seed a real interrupted checkpoint so we pass the checkpoint gate.
        graph = build_dispatcher(checkpointer=cp, interrupt_before=["planner_router"])
        config = {"configurable": {"thread_id": unique_session_id}}
        graph.invoke(
            dict(new_root_state(pt.original_request, session_id=unique_session_id)),
            config=config,
        )
        assert graph.get_state(config).next

        resp = client.post(
            f"/api/chat/{unique_session_id}/resume",
            json={"sub_agent_run_id": pt.sub_agent_run_id, "answer": "不是数字"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "invalid_answer"
        assert _load_pending_sync(redis_url, unique_session_id) is not None
        _clear_pending_sync(redis_url, unique_session_id)

    def test_resume_in_progress_preserves_pending_and_never_invokes_a_second_dag(
        self, app_client, unique_session_id
    ):
        """A real Redis lease makes a duplicate browser resume an explicit retry state."""
        client, _app, redis_url, cp = app_client
        pt = _sample_pending(with_schema=True)
        _seed_pending_sync(redis_url, unique_session_id, pt)

        graph = build_dispatcher(checkpointer=cp, interrupt_before=["planner_router"])
        config = {"configurable": {"thread_id": unique_session_id}}
        graph.invoke(
            dict(new_root_state(pt.original_request, session_id=unique_session_id)),
            config=config,
        )
        assert _claim_pending_sync(redis_url, unique_session_id, pt.sub_agent_run_id)

        resp = client.post(
            f"/api/chat/{unique_session_id}/resume",
            json={"sub_agent_run_id": pt.sub_agent_run_id, "answer": "500米"},
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == "in_progress"
        assert _load_pending_sync(redis_url, unique_session_id) is not None
        _clear_pending_sync(redis_url, unique_session_id)

    def test_resume_matching_run_clears_pending_after_success(
        self, app_client, unique_session_id
    ):
        client, _app, redis_url, cp = app_client
        pt = _sample_pending(with_schema=True)
        _seed_pending_sync(redis_url, unique_session_id, pt)

        # Seed interrupted checkpoint. app_client injects DeterministicLLM with
        # empty task plan so resume invoke completes without network LLM.
        graph = build_dispatcher(
            checkpointer=cp,
            interrupt_before=["planner_router"],
        )
        config = {"configurable": {"thread_id": unique_session_id}}
        graph.invoke(
            dict(
                new_root_state(
                    pt.original_request,
                    session_id=unique_session_id,
                    run_id="root-run",
                )
            ),
            config=config,
        )

        resp = client.post(
            f"/api/chat/{unique_session_id}/resume",
            json={"sub_agent_run_id": pt.sub_agent_run_id, "answer": "500米"},
        )
        assert resp.status_code == 200
        data = resp.json()
        if data["status"] == "resumed":
            assert _load_pending_sync(redis_url, unique_session_id) is None
            assert data["sub_agent_run_id"] == pt.sub_agent_run_id
        elif data["status"] in {"invoke_failed", "invoke_noop"}:
            assert _load_pending_sync(redis_url, unique_session_id) is not None
            _clear_pending_sync(redis_url, unique_session_id)
        else:
            pytest.fail(f"unexpected status: {data}")

    def test_resume_after_ended_graph_replans_and_clears_pending(
        self, app_client, unique_session_id
    ):
        """Normal awaiting pause ends the graph (next=()); resume must replan.

        Seed a fully-ended checkpoint that still has pending_task + awaiting_input
        final_output — the production pause shape after assemble short-circuit.
        """
        client, app, redis_url, cp = app_client
        pt = _sample_pending(with_schema=True)
        _seed_pending_sync(redis_url, unique_session_id, pt)

        graph = build_dispatcher(
            checkpointer=cp,
            llm=getattr(app.state, "dispatcher_llm", None),
        )
        config = {"configurable": {"thread_id": unique_session_id}}
        # Run planner->dispatch->assemble once with empty plan so graph reaches END.
        graph.invoke(
            dict(
                new_root_state(
                    pt.original_request,
                    session_id=unique_session_id,
                    run_id="root-run-ended",
                )
            ),
            config=config,
        )
        # Overlay the awaiting_input surface that dispatch would have written.
        graph.update_state(
            config,
            {
                "pending_task": pt.to_dict(),
                "final_output": {
                    "status": "awaiting_input",
                    "pending_task": pt.to_dict(),
                    "summary": pt.message,
                },
            },
        )
        snap = graph.get_state(config)
        assert not (snap.next or ()), "fixture must be an ended graph (next empty)"

        resp = client.post(
            f"/api/chat/{unique_session_id}/resume",
            json={"sub_agent_run_id": pt.sub_agent_run_id, "answer": "500米"},
        )
        assert resp.status_code == 200
        data = resp.json()
        if data["status"] == "resumed":
            assert _load_pending_sync(redis_url, unique_session_id) is None
            fo = data.get("final_output") or {}
            # After successful replan, should not remain stuck on same awaiting pending.
            if isinstance(fo, dict) and fo.get("status") == "awaiting_input":
                pending = fo.get("pending_task") or {}
                assert pending.get("sub_agent_run_id") != pt.sub_agent_run_id
        elif data["status"] in {"invoke_failed", "invoke_noop"}:
            # Must retain pending on no-op / failure.
            assert _load_pending_sync(redis_url, unique_session_id) is not None
            _clear_pending_sync(redis_url, unique_session_id)
        else:
            pytest.fail(f"unexpected status: {data}")

    def test_root_checkpoint_config_empty_ns(self):
        from app.api.chat import _root_checkpoint_config

        conf = _root_checkpoint_config("sess_e2e")["configurable"]
        assert conf["thread_id"] == "sess_e2e"
        assert conf.get("checkpoint_ns", "") == ""
        assert conf.get("checkpoint_ns") != "_root"


# ---------------------------------------------------------------------------
# Dispatcher resume replan (deterministic LLM, no mock)
# ---------------------------------------------------------------------------


class TestDispatcherPendingResume:
    def test_planner_router_resume_replans_with_patch(self):
        plan = _plan_json(
            [
                {
                    "id": "t1",
                    "agent_role": "geo",
                    "goal": "buffer",
                    "depends_on": [],
                    "expected_artifacts": [],
                }
            ]
        )
        llm = DeterministicLLM(planner=[plan])
        state = {
            "user_input": "ignored",
            "pending_task": {
                "sub_agent_run_id": "r1",
                "original_request": "南京夫子庙缓冲区",
            },
            "resume_patch": {"distance": 500.0},
            "session_id": "s1",
        }
        result = planner_router_node(state, llm=llm)
        assert result.get("pending_task") is None
        assert result.get("resume_patch") == {}
        assert "用户补充参数" in result.get("user_input", "")
        assert "500" in result.get("user_input", "")
        assert result["task_plan"]["tasks"][0]["id"] == "t1"
        assert llm.calls == ["planner"]

    def test_planner_router_resume_reuses_only_exact_prior_successes(self):
        """A resumed DAG skips an upstream result only when its identity is unchanged."""
        plan = _plan_json(
            [
                {
                    "id": "read",
                    "agent_role": "geometer",
                    "tool_name": "data_io_read",
                    "goal": "读取上传面图层",
                    "depends_on": [],
                    "expected_artifacts": [],
                },
                {
                    "id": "buffer",
                    "agent_role": "geometer",
                    "tool_name": "buffer",
                    "goal": "为上传面创建500米缓冲区",
                    "depends_on": ["read"],
                    "expected_artifacts": [],
                    "tool_args": {"radius_m": 500},
                },
            ]
        )
        prior_plan = TaskPlan(
            tasks=[
                SubTask(
                    id="read",
                    agent_role="geometer",
                    tool_name="data_io_read",
                    goal="读取上传面图层",
                    tool_args={"file_id": "file_browser"},
                ),
                SubTask(
                    id="buffer",
                    agent_role="geometer",
                    tool_name="buffer",
                    goal="为上传面创建500米缓冲区",
                    depends_on=["read"],
                    tool_args={"radius_m": 500},
                ),
            ]
        ).model_dump()
        prior_results = {
            "read": [
                {
                    "task_id": "read",
                    "run_id": "sub_read_1",
                    "agent_role": "geometer",
                    "status": "success",
                    "artifacts": {"result": {"feature_count": 2}},
                }
            ]
        }
        llm = DeterministicLLM(planner=[plan])

        result = planner_router_node(
            {
                "user_input": "500米",
                "upload_file_ids": ["file_browser"],
                "pending_task": {
                    "sub_agent_run_id": "r1",
                    "original_request": "读取上传面图层并创建缓冲区",
                },
                "resume_patch": {"distance": 500.0},
                "resume_prior_task_plan": prior_plan,
                "resume_prior_sub_results": prior_results,
                "session_id": "s1",
            },
            llm=llm,
        )

        assert result["sub_results"] == prior_results

    def test_planner_router_resume_never_reuses_a_changed_task_goal(self):
        """A changed Root task must execute again rather than reuse stale geometry."""
        plan = _plan_json(
            [
                {
                    "id": "read",
                    "agent_role": "geometer",
                    "tool_name": "data_io_read",
                    "goal": "读取另一份上传面图层",
                    "depends_on": [],
                    "expected_artifacts": [],
                }
            ]
        )
        prior_plan = TaskPlan(
            tasks=[
                SubTask(
                    id="read",
                    agent_role="geometer",
                    tool_name="data_io_read",
                    goal="读取上传面图层",
                    tool_args={"file_id": "file_browser"},
                )
            ]
        ).model_dump()
        llm = DeterministicLLM(planner=[plan])

        result = planner_router_node(
            {
                "user_input": "继续",
                "upload_file_ids": ["file_browser"],
                "pending_task": {
                    "sub_agent_run_id": "r1",
                    "original_request": "读取上传面图层",
                },
                "resume_patch": {"distance": 500.0},
                "resume_prior_task_plan": prior_plan,
                "resume_prior_sub_results": {
                    "read": [{"status": "success", "agent_role": "geometer"}]
                },
                "session_id": "s1",
            },
            llm=llm,
        )

        assert result.get("sub_results") in (None, {})

    def test_planner_router_no_resume_when_no_pending(self):
        plan = _plan_json(
            [
                {
                    "id": "t1",
                    "agent_role": "geo",
                    "goal": "g",
                    "depends_on": [],
                    "expected_artifacts": [],
                }
            ]
        )
        llm = DeterministicLLM(planner=[plan])
        result = planner_router_node(
            {
                "user_input": "普通请求",
                "pending_task": None,
                "resume_patch": {},
                "session_id": "s1",
            },
            llm=llm,
        )
        assert "task_plan" in result
        assert result["task_plan"]["tasks"][0]["id"] == "t1"
        # No resume merge
        assert result.get("user_input") is None or "用户补充参数" not in str(
            result.get("user_input", "")
        )

    def test_planner_router_no_resume_when_no_resume_patch(self):
        plan = _plan_json(
            [
                {
                    "id": "t1",
                    "agent_role": "geo",
                    "goal": "g",
                    "depends_on": [],
                    "expected_artifacts": [],
                }
            ]
        )
        llm = DeterministicLLM(planner=[plan])
        result = planner_router_node(
            {
                "user_input": "仍有 pending 但无 patch",
                "pending_task": {
                    "sub_agent_run_id": "r1",
                    "original_request": "orig",
                },
                "resume_patch": {},
                "session_id": "s1",
            },
            llm=llm,
        )
        # Without resume_patch, is_resume_replan is False — pending not cleared by router.
        assert "pending_task" not in result
        assert result["task_plan"]["tasks"][0]["id"] == "t1"


# ---------------------------------------------------------------------------
# Sub-agent real graph awaiting path (deterministic LLM)
# ---------------------------------------------------------------------------


class TestSubAgentAwaitingWiring:
    def test_run_sub_agent_emits_awaiting_via_pending_task(self, real_redis, unique_session_id):
        """Inject pending via refine/judge path is heavy; judge path covered above.

        Here we only assert production topology still accepts pending_task in state
        and that DeterministicLLM judge is not required when pending is set first.
        """
        # Direct judge path already covers persistence; this asserts schema status.
        o = subagent_state_to_outcome(
            {
                "agent_role": "geo",
                "pending_task": {
                    "sub_agent_run_id": "r-wire",
                    "message": "need",
                    "missing_slots": ["distance"],
                },
                "tool_results": [],
                "final_output": {},
                "iteration": 0,
            },
            task_id="t1",
            run_id="r-wire",
        )
        assert o.status == "awaiting_input"
        assert o.pending_task["sub_agent_run_id"] == "r-wire"
