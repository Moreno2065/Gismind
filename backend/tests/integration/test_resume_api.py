"""Real-wiring tests for POST /api/agent/{session_id}/resume.

Uses temporary SQLite + production build_dispatcher. No mocks.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.agents.dispatcher import build_dispatcher
from app.agents.state import new_root_state
from app.main import create_app


class TestResumeAgentAPI:
    """POST /api/agent/{session_id}/resume against real production graph."""

    def test_resume_not_found_when_no_checkpoint(self, temp_checkpointer):
        cp, _ = temp_checkpointer
        app = create_app(checkpointer=cp)
        client = TestClient(app)
        resp = client.post("/api/agent/sess_resume_missing/resume")
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_found"

    def test_resume_empty_snapshot_is_not_found(self, temp_checkpointer):
        """Missing checkpoint yields empty snapshot, not a resume."""
        cp, _ = temp_checkpointer
        app = create_app(checkpointer=cp)
        client = TestClient(app)
        resp = client.post("/api/agent/sess_resume_empty/resume")
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_found"

    def test_resume_already_stopped(self, temp_checkpointer):
        cp, _ = temp_checkpointer
        session_id = "sess_resume_done"
        graph = build_dispatcher(checkpointer=cp, interrupt_before=["assemble"])
        config = {"configurable": {"thread_id": session_id}}
        # Seed a finished root state via update_state on a started thread.
        # interrupt_before assemble still needs planner_router to run; instead
        # write values with update_state after an interrupt-before entry.
        graph.invoke(
            dict(
                new_root_state(
                    "already done",
                    session_id=session_id,
                    run_id="r-done",
                )
            ),
            config=config,
        )
        # Force should_stop on the interrupted checkpoint without running LLM.
        graph.update_state(
            config,
            {
                "should_stop": True,
                "final_output": {"summary": "已完成"},
            },
        )

        app = create_app(checkpointer=cp)
        client = TestClient(app)
        resp = client.post(f"/api/agent/{session_id}/resume")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "already_stopped"
        assert data["final_output"]["summary"] == "已完成"

    def test_resume_invokes_dispatcher(self, temp_checkpointer):
        """Interrupted production graph can be resumed without re-seeding input."""
        cp, _ = temp_checkpointer
        session_id = "sess_resume_run"
        # interrupt before planner_router so no LLM is required for seed.
        graph = build_dispatcher(
            checkpointer=cp,
            interrupt_before=["planner_router"],
        )
        config = {"configurable": {"thread_id": session_id}}
        graph.invoke(
            dict(
                new_root_state(
                    "resume me",
                    session_id=session_id,
                    run_id="r-resume",
                )
            ),
            config=config,
        )
        snap = graph.get_state(config)
        assert "planner_router" in (snap.next or ())

        # /api/agent/{session}/resume continues from the interrupted checkpoint
        # (invoke(None)). Without a deterministic LLM, planner_router may
        # fail-open into empty plan + should_stop — still a real production path.
        app = create_app(checkpointer=cp)
        client = TestClient(app)
        resp = client.post(f"/api/agent/{session_id}/resume")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "resumed"
        assert data["session_id"] == session_id
        # final_output may be empty/partial depending on LLM availability;
        # contract requires resumed + session_id, not a mocked summary.
        assert "final_output" in data
