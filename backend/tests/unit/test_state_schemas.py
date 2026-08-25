"""Tests for agent state and schema models (Phase 1 / Task 1.3)."""

import pytest
from app.agents.schemas import (
    PlanInstruction,
    SubTask,
    TaskPlan,
    VerifierOutput,
    SubAgentOutcome,
    RefineNote,
)
from app.agents.errors import ErrorCode


class TestSubTask:
    def test_has_required_fields(self):
        st = SubTask(
            id="t1",
            agent_role="poi",
            goal="500m 内蜜雪冰城",
            depends_on=[],
        )
        assert st.id == "t1"
        assert st.agent_role == "poi"
        assert st.expected_artifacts == []

    def test_defaults(self):
        st = SubTask(id="t2", agent_role="geo", goal="解析地名")
        assert st.depends_on == []
        assert st.expected_artifacts == []


class TestTaskPlan:
    def test_same_role_tool_chain_is_a_first_class_dag(self):
        plan = TaskPlan(tasks=[
            SubTask(id="fix", agent_role="geometer", goal="修复几何", tool_name="fix_geometries"),
            SubTask(id="project", agent_role="geometer", goal="重投影", tool_name="reproject_layer", depends_on=["fix"]),
            SubTask(id="buffer", agent_role="geometer", goal="缓冲", tool_name="buffer", depends_on=["project"]),
            SubTask(id="dissolve", agent_role="geometer", goal="融合", tool_name="dissolve_layer", depends_on=["buffer"]),
            SubTask(id="export", agent_role="geometer", goal="导出", tool_name="export_result", depends_on=["dissolve"]),
        ])

        assert [task.tool_name for task in plan.tasks] == [
            "fix_geometries",
            "reproject_layer",
            "buffer",
            "dissolve_layer",
            "export_result",
        ]
        assert plan.tasks[-1].depends_on == ["dissolve"]

    def test_multi_instruction_dag_requires_every_instruction_to_be_covered(self):
        plan = TaskPlan(
            instructions=[
                PlanInstruction(id="i1", text="查询地铁站"),
                PlanInstruction(id="i2", text="绘制缓冲区"),
            ],
            tasks=[
                SubTask(id="geo", agent_role="geo", goal="解析夫子庙", instruction_id="i1"),
                SubTask(id="poi", agent_role="poi", goal="查询地铁站", depends_on=["geo"], instruction_id="i1"),
                SubTask(id="buffer", agent_role="geometer", goal="绘制缓冲区", depends_on=["geo"], instruction_id="i2"),
            ],
        )

        assert [item.id for item in plan.instructions] == ["i1", "i2"]
        assert plan.tasks[2].instruction_id == "i2"

    def test_multi_instruction_dag_rejects_uncovered_instruction(self):
        with pytest.raises(Exception, match="not covered"):
            TaskPlan(
                instructions=[
                    PlanInstruction(id="i1", text="查询地铁站"),
                    PlanInstruction(id="i2", text="绘制缓冲区"),
                ],
                tasks=[
                    SubTask(id="poi", agent_role="poi", goal="查询地铁站", instruction_id="i1"),
                ],
            )

    def test_dag_rejects_unknown_dependency(self):
        with pytest.raises(Exception, match="unknown task"):
            TaskPlan(tasks=[
                SubTask(id="poi", agent_role="poi", goal="查询", depends_on=["missing"]),
            ])

    def test_dag_rejects_cycle_before_dispatch(self):
        with pytest.raises(Exception, match="cycle"):
            TaskPlan(tasks=[
                SubTask(id="a", agent_role="geo", goal="A", depends_on=["b"]),
                SubTask(id="b", agent_role="poi", goal="B", depends_on=["a"]),
            ])

    def test_serializes_roundtrip(self):
        plan = TaskPlan(tasks=[
            SubTask(id="t1", agent_role="geo", goal="解析地名"),
            SubTask(id="t2", agent_role="poi", goal="POI 查询", depends_on=["t1"]),
        ])
        data = plan.model_dump()
        restored = TaskPlan.model_validate(data)
        assert len(restored.tasks) == 2
        assert restored.tasks[1].depends_on == ["t1"]

    def test_empty_tasks(self):
        plan = TaskPlan(tasks=[])
        assert plan.tasks == []


class TestVerifierOutput:
    def test_confidence_bounds(self):
        vo = VerifierOutput(approved=True, reason="ok", confidence=0.92)
        assert vo.approved is True
        assert vo.confidence == 0.92

    def test_confidence_above_1_raises(self):
        with pytest.raises(Exception):
            VerifierOutput.model_validate(
                {"approved": True, "reason": "x", "confidence": 1.5}
            )

    def test_confidence_below_0_raises(self):
        with pytest.raises(Exception):
            VerifierOutput.model_validate(
                {"approved": True, "reason": "x", "confidence": -0.1}
            )

    def test_default_confidence(self):
        vo = VerifierOutput(approved=False, reason="failed")
        assert vo.confidence == 1.0

    def test_refinement_hints_default(self):
        vo = VerifierOutput(approved=True, reason="ok")
        assert vo.refinement_hints == []


class TestRefineNote:
    def test_has_all_fields(self):
        note = RefineNote(
            iteration=1,
            verifier_reason="精度不足",
            refinement_hints=["增加采样点"],
            applied=True,
        )
        assert note.iteration == 1
        assert note.verifier_reason == "精度不足"
        assert note.applied is True


class TestSubAgentOutcome:
    def test_success_outcome(self):
        outcome = SubAgentOutcome(
            task_id="t1",
            run_id="run-001",
            agent_role="poi",
            status="success",
            artifacts={"count": 15},
            duration_ms=1200,
            iteration_used=1,
        )
        assert outcome.status == "success"
        assert outcome.artifacts["count"] == 15
        assert outcome.error_code is None

    def test_failed_outcome_with_error(self):
        outcome = SubAgentOutcome(
            task_id="t1",
            run_id="run-002",
            agent_role="geo",
            status="failed",
            error_code=ErrorCode.GEOCODE_FAILED.value,
            error_message="高德 API 超时",
        )
        assert outcome.status == "failed"
        assert outcome.error_code == "GEOCODE_FAILED"

    def test_refined_outcome_has_verifier(self):
        outcome = SubAgentOutcome(
            task_id="t1",
            run_id="run-003",
            agent_role="poi",
            status="refined",
            iteration_used=2,
            verifier_output=VerifierOutput(
                approved=True, reason="二次验证通过", confidence=0.95
            ),
        )
        assert outcome.status == "refined"
        assert outcome.iteration_used == 2
        assert outcome.verifier_output is not None
        assert outcome.verifier_output.approved is True


class TestStateImports:
    def test_sub_agent_state_keys(self):
        from app.agents.state import SubAgentState

        # Verify it's a TypedDict with expected keys
        assert "messages" in SubAgentState.__annotations__
        assert "agent_role" in SubAgentState.__annotations__
        assert "run_id" in SubAgentState.__annotations__
        assert "parent_task_id" in SubAgentState.__annotations__

    def test_agent_root_state_keys(self):
        from app.agents.state import AgentRootState

        assert "messages" in AgentRootState.__annotations__
        assert "task_plan" in AgentRootState.__annotations__
        assert "dispatched" in AgentRootState.__annotations__

    def test_new_root_state(self):
        from app.agents.state import new_root_state

        state = new_root_state("查询北京故宫附近酒店")
        assert state["user_input"] == "查询北京故宫附近酒店"
        assert state["iteration"] == 0
        assert state["should_stop"] is False
        assert state["task_plan"] == {"tasks": []}
        assert state["dispatched"] == {}
