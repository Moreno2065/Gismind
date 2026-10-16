"""Invalid model text and missing deliveries must have visible terminal facts."""
import json

import pytest

from app.api.runtime import _snapshot
from app.runtime import RuntimeDB, RuntimeStore
from app.runtime.llm_planner import OpenAICompatiblePlanner
from tests.integration.test_runtime_planning_control import add_run, setup_runtime


@pytest.mark.parametrize("content", ["我会先缓冲然后导出并展示地图。", "{}", '{"tasks":[]}', ""])
def test_invalid_real_planner_reply_finishes_bounded_attempts_and_exposes_diagnostic(tmp_path, monkeypatch, content):
    planner = OpenAICompatiblePlanner(api_key="offline", base_url="https://unused.invalid", model="offline",
                                      timeout_seconds=1, max_tokens=128, max_retries=0)
    calls = []

    def wire(messages):
        calls.append(messages)
        return {"choices": [{"message": {"content": content}}]}, 1

    monkeypatch.setattr(planner, "_call", wire)
    runtime, supervisor = setup_runtime(tmp_path, planner)
    add_run(runtime, "invalid")
    assert supervisor.worker_loop("worker", max_tasks=1) == 1
    assert runtime.get_run("invalid")["status"] == "failed"
    assert 1 <= len(calls) <= 2
    attempts = runtime._read(lambda c: c.execute("SELECT status,error_code FROM planner_attempts").fetchall())
    assert all(row[0] == "failed" and row[1] for row in attempts)
    assert runtime._read(lambda c: c.execute("SELECT COUNT(*) FROM plans").fetchone()[0]) == 0
    snapshot = _snapshot(runtime, "invalid")
    assert snapshot["diagnostic"]["code"] == attempts[-1][1]
    assert snapshot["diagnostic"]["message"]
    terminal = json.loads(runtime.list_events("invalid")[-1]["payload_json"])
    assert terminal["diagnostic"] == snapshot["diagnostic"]


@pytest.mark.parametrize("outputs,empty_policy,expected", [
    ([{"port": "vector", "artifact_type": "Vector", "min_items": 1}], "allow", "failed"),
    ([], "fail", "failed"),
    ([], "allow", "completed"),
    ([{"port": "results", "artifact_type": "Table", "min_items": 0}], "allow", "completed"),
])
def test_terminal_aggregation_requires_declared_outputs_unless_contract_explicitly_allows_none(tmp_path, outputs, empty_policy, expected):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s")
    runtime.create_run("r", "s")
    runtime.create_plan("p", "r")
    runtime.create_task("t", "p", "task", task={"outputs": outputs, "empty_policy": empty_policy})
    runtime._write(lambda c: c.execute("UPDATE runs SET active_plan_id='p',status='running' WHERE run_id='r'"))
    runtime._write(lambda c: c.execute("UPDATE tasks SET status='succeeded' WHERE task_id='t'"))
    assert runtime.aggregate_run_terminal("r") == expected
    snapshot = _snapshot(runtime, "r")
    if expected == "failed":
        assert snapshot["diagnostic"]["code"] == "RUN_OUTPUTS_MISSING"
        assert snapshot["diagnostic"]["message"]
        assert not any(event["event_type"] == "run.completed" for event in runtime.list_events("r"))
