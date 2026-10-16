"""Synthetic controls for independently versioned, audit-only geometry tooling.

These tests neither call a provider nor authenticate historical LLM results.
"""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest


ORACLE_PATH = Path(__file__).with_name("sdk_geometry_oracle.py")


def verify(expected, observed, **kwargs):
    # An assertion, rather than an import error, supplies the initial TDD red.
    assert ORACLE_PATH.is_file(), "the independent SDK geometry oracle is missing"
    spec = importlib.util.spec_from_file_location("sdk_geometry_oracle", ORACLE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.verify_sdk_geometry(expected, observed, **kwargs)


def feature(kind, coordinates):
    return {
        "type": "Feature",
        "properties": {"source": "independent-geometry-fixture"},
        "geometry": {"type": kind, "coordinates": coordinates},
    }


def overlay(kind, coordinates, **metadata):
    return {"kind": kind, "coordinates": coordinates, **metadata}


def square(x=0.0, y=0.0, size=4.0):
    return [[x, y], [x + size, y], [x + size, y + size], [x, y + size], [x, y]]


def shifted_ring(ring, offset, reverse=False):
    body = deepcopy(ring[:-1])
    if reverse:
        body.reverse()
    body = body[offset:] + body[:offset]
    return body + [deepcopy(body[0])]


def assert_mismatch(expected, observed, match="no one-to-one geometry match", **kwargs):
    with pytest.raises(ValueError, match=match):
        verify(expected, observed, **kwargs)


def test_point_result_is_json_serializable_and_uses_list_position():
    result = verify(
        [feature("Point", [116.123, 39.456])],
        [overlay("Point", [116.123, 39.456], overlay_index=93)],
    )
    assert result == {
        "passed": True,
        "pairs": [{"expected_index": 0, "observed_index": 0, "max_error_degrees": 0.0}],
        "max_error_degrees": 0.0,
    }
    assert json.loads(json.dumps(result, allow_nan=False)) == result


def test_mixed_feature_order_is_ignored_and_inputs_remain_unchanged():
    expected = [feature("Point", [2, 3]), feature("Polygon", [square()])]
    observed = [overlay("Polygon", [square()], overlay_index=9), overlay("Point", [2, 3])]
    before = deepcopy((expected, observed))
    result = verify(expected, observed)
    assert [(p["expected_index"], p["observed_index"]) for p in result["pairs"]] == [(0, 1), (1, 0)]
    assert (expected, observed) == before


@pytest.mark.parametrize("offset", range(4))
@pytest.mark.parametrize("reverse", [False, True])
def test_closed_ring_rotation_and_reversal_are_equivalent(offset, reverse):
    result = verify(
        [feature("Polygon", [square()])],
        [overlay("Polygon", [shifted_ring(square(), offset, reverse)])],
    )
    assert result["passed"] is True
    assert result["max_error_degrees"] == 0.0


def test_holes_are_matched_as_multiset_separate_from_outer_ring():
    outer = square(size=20)
    first_hole = square(2, 2, 2)
    second_hole = square(10, 10, 3)
    result = verify(
        [feature("Polygon", [outer, first_hole, second_hole])],
        [overlay("Polygon", [shifted_ring(outer, 2, True), shifted_ring(second_hole, 3), shifted_ring(first_hole, 1, True)])],
    )
    assert result["passed"] is True


def test_outer_and_hole_cannot_swap_roles():
    outer, hole = square(size=20), square(2, 2, 2)
    assert_mismatch(
        [feature("Polygon", [outer, hole])],
        [overlay("Polygon", [hole, outer])],
    )


def test_same_bbox_interior_vertex_mutation_is_rejected():
    ring = [[0, 0], [4, 0], [4, 4], [2, 2], [0, 4], [0, 0]]
    mutation = deepcopy(ring)
    mutation[3] = [2, 3]
    assert min(p[0] for p in ring) == min(p[0] for p in mutation)
    assert max(p[1] for p in ring) == max(p[1] for p in mutation)
    assert_mismatch([feature("Polygon", [ring])], [overlay("Polygon", [mutation])])


def test_missing_vertex_cannot_pass_by_same_bbox():
    ring = [[0, 0], [4, 0], [4, 4], [2, 2], [0, 4], [0, 0]]
    assert_mismatch([feature("Polygon", [ring])], [overlay("Polygon", [square()])])


def test_extra_repeated_vertex_is_not_silently_simplified():
    ring = square()
    extra = deepcopy(ring)
    extra.insert(2, deepcopy(extra[1]))
    assert_mismatch([feature("Polygon", [ring])], [overlay("Polygon", [extra])])


@pytest.mark.parametrize("observed_rings", [[square(size=20)], [square(size=20), square(2, 2, 2), square(10, 10, 2)]])
def test_missing_or_extra_hole_is_rejected(observed_rings):
    assert_mismatch(
        [feature("Polygon", [square(size=20), square(2, 2, 2)])],
        [overlay("Polygon", observed_rings)],
    )


def test_hole_interior_position_mutation_is_rejected():
    outer = square(size=20)
    hole = [[2, 2], [6, 2], [6, 6], [4, 4], [2, 6], [2, 2]]
    changed = deepcopy(hole)
    changed[3] = [4, 5]
    assert_mismatch([feature("Polygon", [outer, hole])], [overlay("Polygon", [outer, changed])])


def test_changed_point_is_rejected():
    assert_mismatch([feature("Point", [116, 39])], [overlay("Point", [116.00001, 39])])


@pytest.mark.parametrize("observed", [[], [overlay("Point", [1, 2]), overlay("Point", [1, 2])]])
def test_missing_or_extra_overlay_is_rejected(observed):
    assert_mismatch([feature("Point", [1, 2])], observed, match="feature/overlay cardinality mismatch")


def test_different_geometry_type_counts_are_rejected():
    assert_mismatch(
        [feature("Point", [1, 2])],
        [overlay("Polygon", [square()])],
        match="geometry type cardinality mismatch",
    )


def test_duplicate_overlay_cannot_substitute_for_missing_distinct_point():
    assert_mismatch(
        [feature("Point", [1, 2]), feature("Point", [3, 4])],
        [overlay("Point", [1, 2]), overlay("Point", [1, 2])],
    )


def test_legitimate_duplicate_geometries_remain_distinct_pairs():
    result = verify(
        [feature("Point", [1, 2]), feature("Point", [1, 2])],
        [overlay("Point", [1, 2]), overlay("Point", [1, 2])],
    )
    assert len(result["pairs"]) == 2
    assert {pair["observed_index"] for pair in result["pairs"]} == {0, 1}


def test_point_matching_reassigns_candidates_instead_of_greedy_false_failure():
    result = verify(
        [feature("Point", [0, 0]), feature("Point", [1.4e-6, 0])],
        [overlay("Point", [0.5e-6, 0]), overlay("Point", [-1e-6, 0])],
        tolerance_degrees=1e-6,
    )
    assert [(p["expected_index"], p["observed_index"]) for p in result["pairs"]] == [(0, 1), (1, 0)]
    assert result["max_error_degrees"] == pytest.approx(1e-6)


def test_hole_matching_reassigns_candidates_instead_of_greedy_false_failure():
    outer = square(size=20)
    first_hole = square(2, 2, 1)
    second_hole = square(2 + 1.5e-6, 2, 1)
    observed_first = square(2 + 0.5e-6, 2, 1)
    observed_second = square(2 - 1e-6, 2, 1)
    result = verify(
        [feature("Polygon", [outer, first_hole, second_hole])],
        [overlay("Polygon", [outer, observed_first, observed_second])],
        tolerance_degrees=1.000001e-6,
    )
    assert result["passed"] is True
    assert result["max_error_degrees"] == pytest.approx(1e-6)


def test_error_is_maximum_coordinate_component_in_degrees():
    result = verify(
        [feature("Point", [0, 0])],
        [overlay("Point", [1.5e-6, -0.9e-6])],
    )
    assert result["pairs"][0]["max_error_degrees"] == pytest.approx(1.5e-6)
    assert result["max_error_degrees"] == pytest.approx(1.5e-6)


def test_ring_reports_maximum_position_error():
    expected = square()
    observed = deepcopy(expected)
    observed[2][1] += 1e-6
    result = verify([feature("Polygon", [expected])], [overlay("Polygon", [observed])])
    assert result["pairs"][0]["max_error_degrees"] == pytest.approx(1e-6)


def test_tolerance_boundary_is_inclusive():
    result = verify([feature("Point", [0, 0])], [overlay("Point", [2e-6, 0])])
    assert result["max_error_degrees"] == 2e-6


def test_tolerance_zero_requires_exact_coordinates():
    assert verify([feature("Point", [1, 2])], [overlay("Point", [1, 2])], tolerance_degrees=0)["passed"]
    assert_mismatch([feature("Point", [1, 2])], [overlay("Point", [1.0000000001, 2])], tolerance_degrees=0)


@pytest.mark.parametrize("tolerance", [-1, float("nan"), float("inf"), float("-inf"), True, "0.1", None])
def test_invalid_tolerance_is_rejected(tolerance):
    assert_mismatch([feature("Point", [1, 2])], [overlay("Point", [1, 2])], match="tolerance_degrees", tolerance_degrees=tolerance)


@pytest.mark.parametrize("bad_coordinate", [float("nan"), float("inf"), float("-inf"), True, "116", None])
@pytest.mark.parametrize("side", ["expected", "observed"])
def test_nonfinite_or_nonnumeric_point_coordinate_is_rejected(side, bad_coordinate):
    expected = [feature("Point", [116, 39])]
    observed = [overlay("Point", [116, 39])]
    if side == "expected":
        expected[0]["geometry"]["coordinates"][0] = bad_coordinate
    else:
        observed[0]["coordinates"][1] = bad_coordinate
    assert_mismatch(expected, observed, match=side + r".*coordinates.*finite numeric")


@pytest.mark.parametrize("coordinates", [[], [1], [1, 2, 3], [[1, 2]], [[1], [2]], (1, 2), {"x": 1, "y": 2}])
@pytest.mark.parametrize("side", ["expected", "observed"])
def test_malformed_point_nesting_or_dimension_is_rejected(side, coordinates):
    expected = [feature("Point", coordinates if side == "expected" else [1, 2])]
    observed = [overlay("Point", coordinates if side == "observed" else [1, 2])]
    assert_mismatch(expected, observed, match=side + r".*coordinates.*two-dimensional position")


@pytest.mark.parametrize("coordinates", [[], square(), [[square()]], [[]], [square()[:-1]], [[[0, 0], [1, 1], [0, 0]]], [[[0, 0], [0, 0], [0, 0], [0, 0]]], [square(), []]])
@pytest.mark.parametrize("side", ["expected", "observed"])
def test_empty_open_degenerate_or_incompatible_polygon_nesting_is_rejected(side, coordinates):
    expected = [feature("Polygon", coordinates if side == "expected" else [square()])]
    observed = [overlay("Polygon", coordinates if side == "observed" else [square()])]
    assert_mismatch(expected, observed, match=side + r".*coordinates")


@pytest.mark.parametrize("side", ["expected", "observed"])
def test_nonfinite_polygon_vertex_is_rejected(side):
    rings = [square()]
    rings[0][2][1] = float("nan")
    expected = [feature("Polygon", rings if side == "expected" else [square()])]
    observed = [overlay("Polygon", rings if side == "observed" else [square()])]
    assert_mismatch(expected, observed, match=side + r".*coordinates.*finite numeric")


@pytest.mark.parametrize("kind", ["MultiPoint", "MultiPolygon", "LineString", "MultiLineString", "GeometryCollection", "Unknown", None])
@pytest.mark.parametrize("side", ["expected", "observed"])
def test_unsupported_geometry_kinds_are_explicitly_rejected(side, kind):
    expected = [feature(kind if side == "expected" else "Point", [1, 2])]
    observed = [overlay(kind if side == "observed" else "Point", [1, 2])]
    assert_mismatch(expected, observed, match=side + r".*unsupported geometry kind")


@pytest.mark.parametrize("expected", [None, {}, (), [], [None], [{"type": "Point", "coordinates": [1, 2]}], [{"type": "Feature", "geometry": None}], [{"type": "Feature"}], [{"type": "Feature", "geometry": {"type": "Point"}}]])
def test_malformed_expected_features_are_rejected(expected):
    assert_mismatch(expected, [overlay("Point", [1, 2])], match="expected")


@pytest.mark.parametrize("observed", [None, {}, (), [None], [{}], [{"kind": "Point"}]])
def test_malformed_observed_overlays_are_rejected(observed):
    assert_mismatch([feature("Point", [1, 2])], observed, match="observed")


@pytest.mark.parametrize("overlay_index", [-1, 1.5, True, "4", None])
def test_invalid_optional_overlay_index_is_rejected(overlay_index):
    assert_mismatch(
        [feature("Point", [1, 2])],
        [overlay("Point", [1, 2], overlay_index=overlay_index)],
        match="observed.*overlay_index.*nonnegative integer",
    )


def test_unmatched_error_identifies_expected_index_and_tolerance():
    with pytest.raises(ValueError) as caught:
        verify([feature("Point", [1, 2]), feature("Point", [3, 4])], [overlay("Point", [1, 2]), overlay("Point", [5, 6])])
    assert "unmatched expected_indices=[1]" in str(caught.value)
    assert "tolerance_degrees=2e-06" in str(caught.value)
