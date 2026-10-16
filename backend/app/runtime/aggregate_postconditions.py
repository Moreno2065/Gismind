"""Independent, complete result checks for grouping and spatial relations.

No checker calls a GIS handler or constructs a Cartesian distance matrix.
Spatial queries operate one source geometry at a time; expected result rows
are capped by the actual output cardinality and fail on the first extra row.
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict
from functools import lru_cache
from typing import Any

from . import postconditions as pc


def _datum(geometry, converter):
    from shapely.ops import transform

    def convert(x, y, z=None):
        x, y = converter(x, y)
        return (x, y) if z is None else (x, y, z)

    return transform(convert, geometry)


@lru_cache(maxsize=32)
def _projector(source, target):
    from pyproj import Transformer

    return Transformer.from_crs(source, target, always_xy=True).transform


def _project(geometry, source, target):
    from shapely.ops import transform

    if source == target:
        return geometry
    return transform(_projector(source, target), geometry)


def _wgs_records(source):
    from app.tools.geo_transform import gcj02_to_wgs84

    features = pc._features(source)
    if features is None or not pc._geometry_valid(features):
        raise ValueError("invalid source vector")
    records = pc._records(features, fill_fields=True)
    crs = pc._source_crs(source)
    return [(properties, _datum(geometry, gcj02_to_wgs84) if crs == "GCJ02"
             else _project(geometry, crs, "EPSG:4326")) for properties, geometry in records]


def _local_crs(records):
    from shapely.ops import unary_union

    centroid = unary_union([geometry for _, geometry in records]).centroid
    if centroid.is_empty:
        raise ValueError("local projection requires a nonempty source")
    longitude, latitude = centroid.x, centroid.y
    if 73.5 <= longitude <= 135.5:
        return f"EPSG:{4534 + round((round(longitude / 3) * 3 - 75) / 3)}"
    zone = int((longitude + 180) / 6) + 1
    return f"EPSG:{(32600 if latitude >= 0 else 32700) + zone}"


def _output_geometry(geometry, crs, *, physical_crs="EPSG:4326"):
    from app.tools.geo_transform import gcj02_to_wgs84, wgs84_to_gcj02

    # The current deterministic adapters perform a display datum round trip.
    # Reconstruct it explicitly, preserving Z, instead of loosening the check
    # enough to hide spatial errors in China or projected-coordinate outputs.
    wgs = _project(geometry, physical_crs, "EPSG:4326")
    display = _datum(wgs, wgs84_to_gcj02)
    if crs == "GCJ02":
        return display
    return _project(_datum(display, gcj02_to_wgs84), "EPSG:4326", crs)


def _actual(result, source, empty_policy):
    parsed = pc._vector_result(result, empty_policy=empty_policy)
    if parsed is None or not pc._preserves_crs(source, parsed[1]):
        raise ValueError("invalid output vector or CRS")
    # Reject a contradictory explicit CRS even if the physical marker agrees.
    for value in (result, pc._data(result), parsed[1]):
        if isinstance(value, dict) and value.get("crs") is not None:
            declared = pc._declared_crs(value)
            raw = value["crs"]
            if isinstance(raw, dict) and isinstance(raw.get("properties"), dict):
                from pyproj import CRS
                declared = CRS.from_user_input(raw["properties"].get("name")).to_string()
            if declared != pc._source_crs(source):
                raise ValueError("conflicting output CRS")
    for feature in parsed[0]:
        if not isinstance(feature.get("properties"), dict):
            raise ValueError("output properties must be an object")
        pc._property_key(feature["properties"])
    return pc._records(parsed[0])


def _tolerance(crs):
    from pyproj import CRS

    return 1e-8 if crs == "GCJ02" or CRS.from_user_input(crs).is_geographic else 1e-4


def dissolve(result: Any, inputs, params, empty_policy):
    try:
        import pandas as pd
        from shapely.ops import unary_union

        source = inputs["input"]
        actual = _actual(result, source, empty_policy)
        records = _wgs_records(source)
        if not records:
            return not actual
        local, output_crs = _local_crs(records), pc._source_crs(source)
        columns = pd.DataFrame([properties for properties, _ in records])
        by = params.get("by")
        if by is not None and (not isinstance(by, str) or by not in columns):
            return False
        # GeoPandas dissolve drops null grouping keys, and selects the first
        # non-null value independently for each other column (not first row).
        groups = columns.groupby(by, sort=False).indices.values() if by is not None else [range(len(records))]
        expected = []
        for indices in groups:
            if len(expected) >= len(actual):
                return False
            selected = columns.iloc[list(indices)]
            properties = {column: next((value for value in selected[column] if value is not None
                                       and not (isinstance(value, float) and math.isnan(value))), None)
                          for column in columns}
            geometry = unary_union([_project(records[index][1], "EPSG:4326", local) for index in indices])
            expected.append((properties, _output_geometry(geometry, output_crs, physical_crs=local).normalize()))
        return pc._same_records(expected, [(properties, geometry.normalize()) for properties, geometry in actual],
                                tolerance=_tolerance(output_crs))
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def count_points(result: Any, inputs, params, empty_policy):
    try:
        from shapely.strtree import STRtree

        source = inputs["points"]
        actual = _actual(result, source, empty_policy)
        points, polygons = _wgs_records(source), _wgs_records(inputs["polygons"])
        if len(actual) != len(polygons):
            return False
        if any(geometry.geom_type not in {"Point", "MultiPoint"} for _, geometry in points) \
                or any(geometry.geom_type not in {"Polygon", "MultiPolygon"} for _, geometry in polygons):
            return False
        tree = STRtree([geometry for _, geometry in points])
        output_crs = pc._source_crs(source)
        expected = []
        for properties, geometry in polygons:
            properties = {key: value for key, value in properties.items() if key != "_poly_idx"}
            properties["count"] = int(len(tree.query(geometry, predicate="intersects")))
            expected.append((properties, _output_geometry(geometry, output_crs)))
        return pc._same_records(expected, actual, tolerance=_tolerance(output_crs))
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _join_properties(left, right, index, k):
    if k == 1:
        overlap = left.keys() & right.keys()
        properties = {(key + "_left" if key in overlap else key): value for key, value in left.items()}
        properties["index_right"] = index
        properties.update({(key + "_right" if key in overlap else key): value for key, value in right.items()})
    else:
        properties = dict(left)
        for key, value in right.items():
            properties[key if key not in properties else key + "_right"] = value
    return properties


def _same_join_records(expected, actual, tolerance):
    from shapely.geometry import box
    from shapely.strtree import STRtree

    if len(expected) != len(actual):
        return False
    groups = defaultdict(list)
    for index, (properties, _) in enumerate(actual):
        distance = properties.get("distance_m")
        if isinstance(distance, bool) or not isinstance(distance, (int, float)) \
                or not math.isfinite(distance) or distance < 0:
            return False
        groups[pc._property_key({key: value for key, value in properties.items() if key != "distance_m"})].append(index)
    trees = {key: (indices, STRtree([actual[index][1] for index in indices])) for key, indices in groups.items()}
    used = set()
    for properties, geometry in expected:
        key = pc._property_key({key: value for key, value in properties.items() if key != "distance_m"})
        if key not in trees:
            return False
        indices, tree = trees[key]
        x0, y0, x1, y1 = geometry.bounds
        for local_index in tree.query(box(x0 - tolerance, y0 - tolerance, x1 + tolerance, y1 + tolerance)):
            index = indices[int(local_index)]
            if index not in used and math.isclose(properties["distance_m"], actual[index][0]["distance_m"],
                                                  rel_tol=1e-9, abs_tol=1e-6) \
                    and pc._same_geometry(geometry, actual[index][1], tolerance):
                used.add(index)
                break
        else:
            return False
    return True


def join_nearest(result: Any, inputs, params, empty_policy):
    try:
        from shapely.strtree import STRtree

        source = inputs["input"]
        actual = _actual(result, source, empty_policy)
        left, right = _wgs_records(source), _wgs_records(inputs["other"])
        k, maximum = params.get("k", 1), params.get("max_distance")
        if isinstance(k, bool) or not isinstance(k, int) or k < 1:
            return False
        if maximum is not None and (isinstance(maximum, bool) or not isinstance(maximum, (int, float))
                                    or not math.isfinite(maximum) or maximum < 0):
            return False
        if not left or not right:
            return not actual
        local, output_crs = _local_crs(left), pc._source_crs(source)
        projected_right = [_project(geometry, "EPSG:4326", local) for _, geometry in right]
        tree = STRtree(projected_right)
        expected = []
        for properties, geometry in left:
            projected = _project(geometry, "EPSG:4326", local)
            if k == 1:
                candidates = tree.query(projected, predicate="intersects") if maximum == 0 else tree.query_nearest(
                    projected, max_distance=maximum, all_matches=True)
                nearest = [(float(projected.distance(projected_right[int(index)])), int(index)) for index in candidates]
            else:
                # Exact top-k; heapq retains at most k candidates. This scans
                # one right layer per left row, never allocates N x M values,
                # and preserves the handler's lexicographic source-index ties.
                distances = ((float(projected.distance(other)), index) for index, other in enumerate(projected_right))
                nearest = heapq.nsmallest(k, (pair for pair in distances if maximum is None or pair[0] <= maximum),
                                         key=lambda pair: (pair[0], str(pair[1])))
            if len(expected) + len(nearest) > len(actual):
                return False
            output_geometry = _output_geometry(projected, output_crs, physical_crs=local)
            for distance, index in nearest:
                joined = _join_properties(properties, right[index][0], index, k)
                joined["distance_m"] = distance
                expected.append((joined, output_geometry))
        return _same_join_records(expected, actual, _tolerance(output_crs))
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
