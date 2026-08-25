from app.agents.metrics import AgentMetrics


def test_metrics_properties():
    m = AgentMetrics()
    m.verifier_calls = 10
    m.verifier_approvals = 8
    assert m.verify_hit_rate == 0.8


def test_metrics_empty_defaults():
    m = AgentMetrics()
    assert m.verify_hit_rate == 0.0

    assert m.avg_sub_task_duration_ms == 0.0


def test_avg_sub_task_duration():
    m = AgentMetrics()
    m.sub_task_durations_ms = [100, 200, 300]
    assert m.avg_sub_task_duration_ms == 200.0
