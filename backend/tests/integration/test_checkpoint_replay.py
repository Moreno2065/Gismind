"""Real-wiring SqliteSaver tests against production graphs.

Uses temporary SQLite + production ``build_dispatcher`` / ``build_sub_agent``.
No test-built StateGraph, no mock/patch of nodes or checkpointer.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from app.agents.build_sub_agent import build_sub_agent
from app.agents.checkpointer import (
    _resolve_path,
    checkpoint_path,
    get_sqlite_checkpointer,
    reset_sqlite_checkpointer,
)
from app.agents.dispatcher import build_dispatcher
from app.agents.state import new_root_state
from app.agents.registry import get_spec


def _new_sqlite_saver(db_path: Path) -> tuple[SqliteSaver, sqlite3.Connection]:
    """Create a non-singleton SqliteSaver on an explicit path."""
    _resolve_path(db_path)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    return SqliteSaver(conn), conn


def _sub_agent_initial_state(
    *,
    agent_role: str,
    run_id: str,
    user_input: str = "checkpoint probe",
    parent_task_id: str | None = None,
    session_id: str = "sess-checkpoint",
) -> dict:
    spec = get_spec(agent_role)
    return {
        "messages": [],
        "iteration": 0,
        "tool_results": [],
        "should_stop": False,
        "user_input": user_input,
        "agent_role": agent_role,
        "parent_task_id": parent_task_id,
        "run_id": run_id,
        "refine_history": [],
        "max_iterations": spec.max_iterations,
        "verifier_required": spec.verifier_required,
        "duplicate_actions": [],
        "session_id": session_id,
        "pending_task": None,
    }


# ============================================================
# Factory
# ============================================================


class TestCheckpointerFactory:
    """get_sqlite_checkpointer / checkpoint_path factory tests."""

    def test_factory_returns_sqlite_saver(self):
        reset_sqlite_checkpointer()
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test.db"
            cp = get_sqlite_checkpointer(db_path)
            assert isinstance(cp, SqliteSaver)
            assert db_path.exists()
            reset_sqlite_checkpointer()

    def test_factory_singleton(self):
        """Same path returns the same instance (process-local singleton)."""
        reset_sqlite_checkpointer()
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "singleton.db"
            c1 = get_sqlite_checkpointer(db)
            c2 = get_sqlite_checkpointer(db)
            assert c1 is c2
            reset_sqlite_checkpointer()

    def test_factory_rejects_different_path_while_bound(self):
        """Bound singleton must not silently reuse against a different path."""
        reset_sqlite_checkpointer()
        with tempfile.TemporaryDirectory() as td:
            db1 = Path(td) / "first.db"
            db2 = Path(td) / "second.db"
            c1 = get_sqlite_checkpointer(db1)
            with pytest.raises(ValueError, match="already bound"):
                get_sqlite_checkpointer(db2)
            assert get_sqlite_checkpointer(db1) is c1
            assert db1.exists()
            assert not db2.exists()
            reset_sqlite_checkpointer()

    def test_checkpoint_path_default(self):
        """checkpoint_path returns absolute path from settings."""
        p = checkpoint_path()
        assert isinstance(p, Path)
        assert p.name.endswith(".db")
        assert p.parent.exists()

    def test_resolve_path_creates_parent_dir(self):
        """_resolve_path creates parent directories."""
        with tempfile.TemporaryDirectory() as td:
            nested = Path(td) / "a" / "b" / "c" / "nested.db"
            assert not nested.parent.exists()
            result = _resolve_path(nested)
            assert nested.parent.exists()
            assert result.is_absolute()

    def test_reset_clears_path_binding(self):
        """After reset, a different path may be bound."""
        reset_sqlite_checkpointer()
        with tempfile.TemporaryDirectory() as td:
            db1 = Path(td) / "first.db"
            db2 = Path(td) / "second.db"
            get_sqlite_checkpointer(db1)
            reset_sqlite_checkpointer()
            c2 = get_sqlite_checkpointer(db2)
            assert isinstance(c2, SqliteSaver)
            assert db2.exists()
            reset_sqlite_checkpointer()


# ============================================================
# Production graph persistence
# ============================================================


class TestProductionDispatcherCheckpoint:
    """Persist/restore via production build_dispatcher + temporary SQLite."""

    def test_dispatcher_interrupt_before_planner_persists_root_state(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "dispatcher.db"
            cp, conn = _new_sqlite_saver(db)
            app = build_dispatcher(
                checkpointer=cp,
                interrupt_before=["planner_router"],
            )

            thread_id = "root-thread-1"
            config = {"configurable": {"thread_id": thread_id}}
            initial = new_root_state(
                user_input="persist root state",
                session_id=thread_id,
                run_id="run-root-1",
                trace_id="trace-root-1",
            )

            # Interrupt before first node: values should match initial input.
            app.invoke(dict(initial), config=config)

            snapshot = app.get_state(config)
            assert snapshot is not None
            assert snapshot.values.get("user_input") == "persist root state"
            assert snapshot.values.get("session_id") == thread_id
            assert snapshot.values.get("run_id") == "run-root-1"
            assert "planner_router" in (snapshot.next or ())
            assert snapshot.config["configurable"]["thread_id"] == thread_id
            # Root production config uses empty checkpoint_ns.
            assert snapshot.config["configurable"].get("checkpoint_ns", "") in ("", None)

            conn.close()

    def test_dispatcher_rebuild_from_same_db_restores_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "dispatcher-rebuild.db"
            cp1, conn1 = _new_sqlite_saver(db_path)
            app1 = build_dispatcher(
                checkpointer=cp1,
                interrupt_before=["planner_router"],
            )
            thread_id = "root-rebuild"
            config = {"configurable": {"thread_id": thread_id}}
            initial = new_root_state(
                user_input="rebuild me",
                session_id=thread_id,
                run_id="run-rebuild",
            )
            app1.invoke(dict(initial), config=config)
            snap1 = app1.get_state(config)
            assert "planner_router" in (snap1.next or ())
            conn1.close()

            # Rebuild production graph on a fresh saver against the same file.
            cp2, conn2 = _new_sqlite_saver(db_path)
            app2 = build_dispatcher(
                checkpointer=cp2,
                interrupt_before=["planner_router"],
            )
            snap2 = app2.get_state(config)
            assert snap2.values.get("user_input") == "rebuild me"
            assert snap2.values.get("session_id") == thread_id
            assert "planner_router" in (snap2.next or ())
            conn2.close()

    def test_dispatcher_threads_are_isolated(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "dispatcher-threads.db"
            cp, conn = _new_sqlite_saver(db)
            app = build_dispatcher(
                checkpointer=cp,
                interrupt_before=["planner_router"],
            )
            config_a = {"configurable": {"thread_id": "thread-A"}}
            config_b = {"configurable": {"thread_id": "thread-B"}}
            app.invoke(
                dict(new_root_state("input-A", session_id="thread-A", run_id="ra")),
                config=config_a,
            )
            app.invoke(
                dict(new_root_state("input-B", session_id="thread-B", run_id="rb")),
                config=config_b,
            )
            assert app.get_state(config_a).values.get("user_input") == "input-A"
            assert app.get_state(config_b).values.get("user_input") == "input-B"
            conn.close()


class TestProductionSubAgentCheckpoint:
    """Persist/restore via production build_sub_agent + temporary SQLite.

    Standalone production sub-agent graphs (not nested as LangGraph subgraphs)
    persist under empty ``checkpoint_ns``; isolation is by ``thread_id``
    (``{role}-{run_id}``), matching how SqliteSaver records top-level runs.
    ``run_sub_agent`` still *passes* a namespaced config for nested-subgraph
    compatibility, but top-level get_state must use empty ns.
    """

    def test_sub_agent_interrupt_before_planner_persists_state(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "subagent.db"
            cp, conn = _new_sqlite_saver(db)
            try:
                role = "geo"
                run_id = "sub-run-1"
                parent_task_id = "task-1"
                app = build_sub_agent(
                    role,
                    run_id=run_id,
                    parent_task_id=parent_task_id,
                    checkpointer=cp,
                    interrupt_before=["planner"],
                )

                # Production run_sub_agent thread_id shape.
                thread_id = f"{role}-{run_id}"
                # Invoke with production-like namespaced config; storage is empty ns.
                invoke_config = {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": f"{role}:{parent_task_id}:{run_id}",
                    }
                }
                read_config = {"configurable": {"thread_id": thread_id}}
                initial = _sub_agent_initial_state(
                    agent_role=role,
                    run_id=run_id,
                    parent_task_id=parent_task_id,
                    user_input="sub agent checkpoint probe",
                )
                app.invoke(dict(initial), config=invoke_config)

                snapshot = app.get_state(read_config)
                assert snapshot.values.get("user_input") == "sub agent checkpoint probe"
                assert snapshot.values.get("agent_role") == role
                assert snapshot.values.get("run_id") == run_id
                assert snapshot.values.get("parent_task_id") == parent_task_id
                assert "planner" in (snapshot.next or ())
                conf = snapshot.config["configurable"]
                assert conf["thread_id"] == thread_id
                assert conf.get("checkpoint_ns", "") in ("", None)
            finally:
                conn.close()

    def test_sub_agent_rebuild_from_same_db_restores_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "subagent-rebuild.db"
            role = "coder"
            run_id = "sub-rebuild"
            parent_task_id = None
            thread_id = f"{role}-{run_id}"
            invoke_config = {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": f"{role}:_:{run_id}",
                }
            }
            read_config = {"configurable": {"thread_id": thread_id}}

            cp1, conn1 = _new_sqlite_saver(db_path)
            try:
                app1 = build_sub_agent(
                    role,
                    run_id=run_id,
                    parent_task_id=parent_task_id,
                    checkpointer=cp1,
                    interrupt_before=["planner"],
                )
                app1.invoke(
                    _sub_agent_initial_state(
                        agent_role=role,
                        run_id=run_id,
                        parent_task_id=parent_task_id,
                        user_input="rebuild sub agent",
                    ),
                    config=invoke_config,
                )
                snap1 = app1.get_state(read_config)
                assert "planner" in (snap1.next or ())
            finally:
                conn1.close()

            cp2, conn2 = _new_sqlite_saver(db_path)
            try:
                app2 = build_sub_agent(
                    role,
                    run_id=run_id,
                    parent_task_id=parent_task_id,
                    checkpointer=cp2,
                    interrupt_before=["planner"],
                )
                snap2 = app2.get_state(read_config)
                assert snap2.values.get("user_input") == "rebuild sub agent"
                assert snap2.values.get("agent_role") == role
                assert "planner" in (snap2.next or ())
                assert snap2.config["configurable"].get("checkpoint_ns", "") in ("", None)
            finally:
                conn2.close()
