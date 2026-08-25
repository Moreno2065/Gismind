import pytest
from app.agents.cost import CostTracker, CostExceeded, estimate_tokens


def test_cost_tracker_starts_empty():
    ct = CostTracker(max_tokens=100)
    assert ct.total == 0
    assert ct.remaining == 100


def test_cost_tracker_accumulates():
    ct = CostTracker(max_tokens=100)
    ct.add("planner", 40)
    ct.add("observer", 30)
    assert ct.total == 70
    assert ct.remaining == 30


def test_cost_exceeded_raises():
    ct = CostTracker(max_tokens=50)
    ct.add("planner", 40)
    with pytest.raises(CostExceeded, match="exceeded max_tokens"):
        ct.add("verifier", 20)


def test_estimate_tokens():
    assert estimate_tokens("hello") >= 1
    # ~200 chars -> ~50 tokens
    assert 40 <= estimate_tokens("a" * 200) <= 60
