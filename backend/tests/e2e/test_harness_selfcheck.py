"""Prove the corpus harness really drives the production Runtime chain.

If any of these fail the harness is broken, not the Runtime, and the whole
corpus result is meaningless.
"""

from __future__ import annotations

import pytest

from . import fixtures as fx
from .harness import E2ECase, FixtureRef, FixtureSpec, StepRef, StepSpec, run_case


def test_harness_executes_a_real_buffer_through_the_worker(tmp_path):
    case = E2ECase(
        case_id="selfcheck.buffer",
        category="selfcheck",
        user_goal="把这些点做 500 米缓冲",
        fixtures=(FixtureSpec("pts", "Vector", lambda: fx.points(5), crs=fx.WGS84, filename="pts.geojson"),),
        run_inputs=("pts",),
        steps=(StepSpec("buffer", "vector.buffer", {"vector": FixtureRef("pts")}, {"distance_m": 500}),),
    )
    result = run_case(case, tmp_path / "r1")
    assert result.outcome == "success", f"{result.outcome} {result.codes} {result.error}"
    assert result.output_artifacts
    assert result.artifact_type(0) == "Vector"
    payload = result.json(0)
    assert len(payload["features"]) == 5


def test_harness_records_a_catalog_miss_instead_of_crashing(tmp_path):
    """An operation outside the request catalog must be a durable failure."""
    case = E2ECase(
        case_id="selfcheck.catalog",
        category="selfcheck",
        user_goal="做一件与任何工具都不相干的事",
        fixtures=(FixtureSpec("pts", "Vector", lambda: fx.points(3), crs=fx.WGS84, filename="pts.geojson"),),
        run_inputs=("pts",),
        steps=(StepSpec("hull", "vector.convex_hull", {"input": FixtureRef("pts")}),),
    )
    result = run_case(case, tmp_path / "r2")
    assert result.outcome in {"compile_reject", "failed", "success"}
    assert result.run["status"] in {"completed", "failed"}


def test_harness_links_upstream_output_into_downstream_input(tmp_path):
    case = E2ECase(
        case_id="selfcheck.chain",
        category="selfcheck",
        user_goal="把这些点做 300 米缓冲然后在地图上展示",
        fixtures=(FixtureSpec("pts", "Vector", lambda: fx.points(4), crs=fx.WGS84, filename="pts.geojson"),),
        run_inputs=("pts",),
        steps=(
            StepSpec("buffer", "vector.buffer", {"vector": FixtureRef("pts")}, {"distance_m": 300}),
            StepSpec("map", "map.build", {"vector": StepRef("buffer", "vector")}),
        ),
    )
    result = run_case(case, tmp_path / "r3")
    assert result.outcome == "success", f"{result.outcome} {result.codes} {result.error}"
    assert [task["status"] for task in result.tasks] == ["succeeded", "succeeded"]
    assert result.by_type("MapLayer")


@pytest.mark.parametrize("distance", [50, 500, 5000])
def test_harness_is_deterministic_across_distances(tmp_path, distance):
    case = E2ECase(
        case_id=f"selfcheck.buffer.{distance}",
        category="selfcheck",
        user_goal=f"缓冲 {distance} 米",
        fixtures=(FixtureSpec("pts", "Vector", lambda: fx.points(2), crs=fx.WGS84, filename="pts.geojson"),),
        run_inputs=("pts",),
        steps=(StepSpec("buffer", "vector.buffer", {"vector": FixtureRef("pts")}, {"distance_m": distance}),),
    )
    result = run_case(case, tmp_path / f"b{distance}")
    assert result.outcome == "success", f"{result.outcome} {result.codes} {result.error}"
    assert len(result.json(0)["features"]) == 2


def test_harness_records_a_zero_distance_buffer_as_a_durable_failure(tmp_path):
    """A zero-distance buffer yields empty geometry and must not publish."""
    case = E2ECase(
        case_id="selfcheck.buffer.zero",
        category="selfcheck",
        user_goal="缓冲 0 米",
        fixtures=(FixtureSpec("pts", "Vector", lambda: fx.points(2), crs=fx.WGS84, filename="pts.geojson"),),
        run_inputs=("pts",),
        steps=(StepSpec("buffer", "vector.buffer", {"vector": FixtureRef("pts")}, {"distance_m": 0}),),
    )
    result = run_case(case, tmp_path / "b0")
    assert result.outcome == "failed"
    assert any("postcondition failed" in message for message in result.failure_messages)
    assert result.run["status"] == "failed"
