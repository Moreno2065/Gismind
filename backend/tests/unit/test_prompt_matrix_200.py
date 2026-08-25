"""Regression gate for the documented 200-prompt fallback catalog.

This suite is deliberately *not* evidence of Root LLM planning quality.  It
only verifies that a known prompt stays executable if the root model is
unavailable, and it records that path as ``fallback`` or ``guardrail``.
"""

from __future__ import annotations

import json
import runpy
import shutil
from pathlib import Path

import pytest

from app.agents.dispatcher import planner_router_node


MATRIX_PATH = Path(__file__).resolve().parents[3] / "blackbox" / "prompt_matrix_200.py"
REAL_SUITE_PATH = Path(__file__).resolve().parents[3] / "blackbox" / "real_llm_prompt_suite.py"


class _UnavailablePlannerLLM:
    """Simulate an unavailable root model without concealing the fallback path."""

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, *_args, **_kwargs):
        self.calls += 1
        raise RuntimeError("root planner unavailable in fallback-catalog test")


@pytest.fixture(scope="module")
def matrix():
    return runpy.run_path(str(MATRIX_PATH))


def test_prompt_matrix_has_exactly_200_multilingual_boundary_cases(matrix):
    cases = matrix["CASES"]

    assert len(cases) == 200
    assert len({case.id for case in cases}) == 200
    assert len({case.prompt for case in cases}) == 200
    assert {case.language for case in cases} == {"zh", "zh_tw", "en", "es", "ja", "fr"}
    assert sum(case.boundary for case in cases) >= 8


def test_prompt_matrix_covers_every_public_agent_tool(matrix):
    covered = {
        tool_name
        for case in matrix["CASES"]
        for tool_name in case.expected_tools
    }

    assert set(matrix["PUBLIC_AGENT_TOOLS"]).issubset(covered)


def test_real_suite_has_a_deliberately_invalid_polygon_fixture():
    """Validity and repair prompts must exercise real invalid input, not a label."""
    suite = runpy.run_path(str(REAL_SUITE_PATH))
    fixture_root = Path(__file__).resolve().parents[2] / ".fixture-matrix-test"
    try:
        fixture_root.mkdir(exist_ok=True)
        fixture_path = suite["build_fixtures"](fixture_root)["invalid_parcels"]
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        ring = payload["features"][0]["geometry"]["coordinates"][0]

        # A bow-tie is self-intersecting; its two diagonals meet away from vertices.
        assert ring[0] == ring[-1]
        assert ring[0] != ring[2]
        assert ring[1] != ring[3]
    finally:
        shutil.rmtree(fixture_root, ignore_errors=True)


@pytest.mark.parametrize(
    "case",
    runpy.run_path(str(MATRIX_PATH))["CASES"],
    ids=lambda case: case.id,
)
def test_every_matrix_prompt_has_a_labeled_recovery_plan_when_root_llm_is_unavailable(case):
    llm = _UnavailablePlannerLLM()
    result = planner_router_node(
        {
            "user_input": case.prompt,
            "upload_file_ids": [f"file_{name}" for name in case.upload_names],
            "messages": [],
        },
        llm=llm,
    )

    planned_tools = {task["tool_name"] for task in result["task_plan"]["tasks"]}
    assert result["planner_source"] in {"guardrail", "fallback"}
    if result["planner_source"] == "guardrail":
        assert llm.calls == 0
    else:
        assert llm.calls >= 1
    assert set(case.expected_tools).issubset(planned_tools)
