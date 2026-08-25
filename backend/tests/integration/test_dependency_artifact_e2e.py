"""Real graph regression for cross-sub-agent artifact delivery."""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver

from app.agents.tool_execution import run_react_loop
from tests.support import DeterministicLLM


def _approve() -> dict:
    return {
        "approved": True,
        "reason": "结果正确",
        "refinement_hints": [],
        "confidence": 0.95,
    }


def _finish() -> dict:
    return {"decision": "FINISH", "reason": "任务完成"}


def test_real_geo_to_viz_dependency_produces_nonempty_map() -> None:
    root_llm = DeterministicLLM(planner=[
        '{"task_plan":{"tasks":['
        '{"id":"source","agent_role":"coder","goal":"构造测试空间要素","depends_on":[]},'
        '{"id":"render","agent_role":"viz","goal":"渲染上游空间要素","depends_on":["source"]}'
        ']}}',
        '{"reply":"地图已生成"}',
    ])
    sub_llm = DeterministicLLM(
        planner=[
            "feature = {'type': 'Feature', 'geometry': {'type': 'Point', "
            "'coordinates': [118.78, 32.04]}, 'properties': {'name': 'test'}}\n"
            "features = [feature]\n"
            "__result__ = {'features': features}",
            {"name": "map_layer_build", "args": {"geometry_from": 0}},
        ],
        verifier=[_approve()],
        judge=[_finish(), _finish()],
    )

    result = run_react_loop(
        user_input="把测试点画出来",
        session_id="dependency-artifact-e2e",
        trace_id="trace-dependency",
        checkpointer=MemorySaver(),
        dispatcher_llm=root_llm,
        sub_agent_llm=sub_llm,
    )

    final_output = result["final_output"]
    assert final_output.get("status") != "failed"
    assert final_output["layers"][0]["features"][0]["properties"]["name"] == "test"
    # Assembly now preserves the rendered-layer fact directly, so it no longer
    # needs a second root-model call to synthesize a fallback reply.
    assert root_llm.calls == ["planner"]
    assert sub_llm.calls == [
        "planner", "judge",
        "planner",
    ]
