"""Independent, audit-only comparison of projected source and SDK geometries.

Version 2026-10-14.1. This module was authored after the original cohort; it is
not part of that cohort's sealed test source or an LLM-success substitute.
It imports only the standard library and performs no I/O. The caller must read
independent source geometries, explicitly project them to GCJ02, observe actual
SDK objects, and close implicit SDK rings before invoking this comparison.

Scope: two-dimensional Point and Polygon (outer ring plus optional holes).
Multi-geometries, lines, empty geometries, and other kinds fail explicitly.
"""

from collections import Counter, deque
import math


ORACLE_VERSION = "2026-10-14.1"
SUPPORTED_GEOMETRY_KINDS = ("Point", "Polygon")


def _finite_number(value, path):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path}: expected a finite numeric coordinate")
    try:
        number = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError(f"{path}: expected a finite numeric coordinate") from error
    if not math.isfinite(number):
        raise ValueError(f"{path}: expected a finite numeric coordinate")
    return number


def _position(coordinates, path):
    if (
        not isinstance(coordinates, list)
        or len(coordinates) != 2
        or any(isinstance(value, (list, tuple, dict)) for value in coordinates)
    ):
        raise ValueError(f"{path}: expected a two-dimensional position [longitude, latitude]")
    return tuple(_finite_number(value, f"{path}[{axis}]") for axis, value in enumerate(coordinates))


def _geometry(kind, coordinates, path):
    if not isinstance(kind, str) or kind not in SUPPORTED_GEOMETRY_KINDS:
        raise ValueError(f"{path}: unsupported geometry kind {kind!r}; supported kinds are Point and Polygon")
    coordinate_path = f"{path}.coordinates"
    if kind == "Point":
        return kind, _position(coordinates, coordinate_path)
    if not isinstance(coordinates, list) or not coordinates:
        raise ValueError(f"{coordinate_path}: Polygon requires a nonempty list of closed rings")
    rings = []
    for ring_index, coordinates_ring in enumerate(coordinates):
        ring_path = f"{coordinate_path}[{ring_index}]"
        if not isinstance(coordinates_ring, list) or len(coordinates_ring) < 4:
            raise ValueError(f"{ring_path}: closed ring requires at least four positions")
        ring = tuple(_position(position, f"{ring_path}[{index}]") for index, position in enumerate(coordinates_ring))
        if ring[0] != ring[-1]:
            raise ValueError(f"{ring_path}: ring is not closed; final position must equal its first position")
        if len(set(ring[:-1])) < 3:
            raise ValueError(f"{ring_path}: ring requires at least three distinct positions")
        # Remove exactly the final closure position, retaining repeated vertices.
        rings.append(ring[:-1])
    return kind, tuple(rings)


def _expected_feature(value, index):
    path = f"expected_features[{index}]"
    if not isinstance(value, dict) or value.get("type") != "Feature":
        raise ValueError(f"{path}: expected a GeoJSON Feature object")
    geometry = value.get("geometry")
    if not isinstance(geometry, dict):
        raise ValueError(f"{path}.geometry: expected a nonempty GeoJSON geometry object")
    if "coordinates" not in geometry:
        raise ValueError(f"{path}.geometry.coordinates: missing coordinates")
    return _geometry(geometry.get("type"), geometry["coordinates"], f"{path}.geometry")


def _observed_overlay(value, index):
    path = f"observed_overlays[{index}]"
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected an SDK overlay observation object")
    if "coordinates" not in value:
        raise ValueError(f"{path}.coordinates: missing coordinates")
    if "overlay_index" in value:
        overlay_index = value["overlay_index"]
        if isinstance(overlay_index, bool) or not isinstance(overlay_index, int) or overlay_index < 0:
            raise ValueError(f"{path}.overlay_index: expected a nonnegative integer")
    return _geometry(value.get("kind"), value["coordinates"], path)


def _position_error(expected, observed):
    return max(abs(expected[axis] - observed[axis]) for axis in (0, 1))


def _ring_error(expected, observed, tolerance):
    """Minimum maximum component error over cyclic and reversed vertex order."""
    if len(expected) != len(observed):
        return None
    size = len(expected)
    best_error = None
    for offset in range(size):
        first_error = _position_error(expected[0], observed[offset])
        if first_error > tolerance:
            continue
        for direction in (1, -1):
            maximum_error = first_error
            for index in range(1, size):
                maximum_error = max(maximum_error, _position_error(expected[index], observed[(offset + direction * index) % size]))
                if maximum_error > tolerance:
                    break
            else:
                if best_error is None or maximum_error < best_error:
                    best_error = maximum_error
                    if best_error == 0.0:
                        return best_error
    return best_error


def _bipartite_match(edges, observed_count):
    """Find a maximum one-to-one matching using iterative augmenting paths."""
    expected_matches = [-1] * len(edges)
    observed_owners = [-1] * observed_count
    for starting_expected in range(len(edges)):
        queue = deque([starting_expected])
        seen_expected = {starting_expected}
        parent_for_observed = {}
        free_observed = None
        while queue and free_observed is None:
            expected_index = queue.popleft()
            for observed_index in edges[expected_index]:
                if observed_index in parent_for_observed:
                    continue
                parent_for_observed[observed_index] = expected_index
                owner = observed_owners[observed_index]
                if owner == -1:
                    free_observed = observed_index
                    break
                if owner not in seen_expected:
                    seen_expected.add(owner)
                    queue.append(owner)
        while free_observed is not None and free_observed != -1:
            expected_index = parent_for_observed[free_observed]
            previous_observed = expected_matches[expected_index]
            expected_matches[expected_index] = free_observed
            observed_owners[free_observed] = expected_index
            free_observed = previous_observed
    return expected_matches


def _ordered_edges(errors):
    # Ordering affects the chosen valid pairing, never whether one exists.
    return [index for index in sorted(errors, key=lambda index: (errors[index], index))]


def _geometry_error(expected, observed, tolerance):
    expected_kind, expected_coordinates = expected
    observed_kind, observed_coordinates = observed
    if expected_kind != observed_kind:
        return None
    if expected_kind == "Point":
        error = _position_error(expected_coordinates, observed_coordinates)
        return error if error <= tolerance else None
    if len(expected_coordinates) != len(observed_coordinates):
        return None
    outer_error = _ring_error(expected_coordinates[0], observed_coordinates[0], tolerance)
    if outer_error is None:
        return None
    expected_holes, observed_holes = expected_coordinates[1:], observed_coordinates[1:]
    if not expected_holes:
        return outer_error
    hole_errors = []
    for expected_hole in expected_holes:
        row = {}
        for observed_index, observed_hole in enumerate(observed_holes):
            error = _ring_error(expected_hole, observed_hole, tolerance)
            if error is not None:
                row[observed_index] = error
        hole_errors.append(row)
    hole_pairs = _bipartite_match([_ordered_edges(row) for row in hole_errors], len(observed_holes))
    if -1 in hole_pairs:
        return None
    return max([outer_error] + [hole_errors[index][observed_index] for index, observed_index in enumerate(hole_pairs)])


def _structure(geometry):
    kind, coordinates = geometry
    if kind == "Point":
        return {"kind": kind}
    return {"kind": kind, "ring_positions": [len(ring) + 1 for ring in coordinates]}


def verify_sdk_geometry(expected_features, observed_overlays, tolerance_degrees=2e-6):
    """Verify exact geometry multiset structure within a coordinate tolerance.

    expected_features must be a nonempty list of GeoJSON Features containing
    independent source geometry already projected into the SDK's GCJ02 space.
    observed_overlays must be a list of {kind, coordinates, overlay_index?}
    observations of actual SDK objects; coordinates use closed GeoJSON rings.
    Polygon outer rings cannot substitute for holes. Hole order, ring start,
    winding, and feature order may differ; every vertex and ring is checked.

    Return JSON-serializable passed/pairs/max_error_degrees, with both pair
    indexes addressing their input list positions. The error is the maximum
    absolute longitude or latitude component difference in degrees, not a
    distance in metres. Optional overlay_index is validated source metadata.
    A matching is valid but need not globally minimize the bottleneck error.
    Raise ValueError with input paths or matching diagnostics on any failure.
    """
    if isinstance(tolerance_degrees, bool) or not isinstance(tolerance_degrees, (int, float)):
        raise ValueError("tolerance_degrees must be a finite nonnegative number")
    try:
        tolerance = float(tolerance_degrees)
    except (ValueError, OverflowError) as error:
        raise ValueError("tolerance_degrees must be a finite nonnegative number") from error
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("tolerance_degrees must be a finite nonnegative number")
    if not isinstance(expected_features, list) or not expected_features:
        raise ValueError("expected_features must be a nonempty list of GeoJSON Features")
    if not isinstance(observed_overlays, list):
        raise ValueError("observed_overlays must be a list of SDK overlay observations")
    expected = [_expected_feature(value, index) for index, value in enumerate(expected_features)]
    observed = [_observed_overlay(value, index) for index, value in enumerate(observed_overlays)]
    if len(expected) != len(observed):
        raise ValueError(f"feature/overlay cardinality mismatch: expected={len(expected)}, observed={len(observed)}")
    expected_counts = Counter(geometry[0] for geometry in expected)
    observed_counts = Counter(geometry[0] for geometry in observed)
    if expected_counts != observed_counts:
        raise ValueError(f"geometry type cardinality mismatch: expected={dict(expected_counts)}, observed={dict(observed_counts)}")
    errors = []
    for expected_geometry in expected:
        row = {}
        for observed_index, observed_geometry in enumerate(observed):
            error = _geometry_error(expected_geometry, observed_geometry, tolerance)
            if error is not None:
                row[observed_index] = error
        errors.append(row)
    matches = _bipartite_match([_ordered_edges(row) for row in errors], len(observed))
    unmatched = [index for index, observed_index in enumerate(matches) if observed_index == -1]
    if unmatched:
        candidates = {index: list(errors[index]) for index in unmatched}
        raise ValueError(
            f"no one-to-one geometry match within tolerance_degrees={tolerance:g}; "
            f"unmatched expected_indices={unmatched}; candidate observed_indices={candidates}; "
            f"expected_structures={[_structure(value) for value in expected]}; "
            f"observed_structures={[_structure(value) for value in observed]}"
        )
    pairs = [
        {"expected_index": index, "observed_index": observed_index, "max_error_degrees": errors[index][observed_index]}
        for index, observed_index in enumerate(matches)
    ]
    return {"passed": True, "pairs": pairs, "max_error_degrees": max(pair["max_error_degrees"] for pair in pairs)}
