"""Independent checks for grouped geometry and spatial relation results."""

from copy import deepcopy

import pytest
from shapely.geometry import Point, box, mapping

from app.runtime import ToolContractRegistry, tool_handlers
from app.runtime.postconditions import get_postcondition


def feature(geometry, **properties):
    return {"type": "Feature", "geometry": mapping(geometry), "properties": properties}


def vector(*rows, epsg=4326):
    return {"type": "Vector", "crs": {"kind": "EPSG", "code": epsg},
            "data": {"type": "FeatureCollection", "features": list(rows)}}


def check(operation, output, inputs, params=None):
    return get_postcondition(operation)(output, inputs, params or {}, empty_policy="allow")


def execute(operation, inputs, params=None):
    output = getattr(tool_handlers, "handle_" + operation.replace(".", "_"))(**inputs, **(params or {}))
    assert output["status"] == "success", output
    return output


def dissolve_inputs():
    return {"input": vector(
        feature(box(118.7, 32, 118.702, 32.002), group="a", value=None, keep="first"),
        feature(box(118.701, 32.001, 118.703, 32.003), group="a", value=2, keep="later"),
        feature(box(118.71, 32, 118.712, 32.002), group="b", value=3, keep="other"),
        feature(box(118.72, 32, 118.722, 32.002), group=None, value=4, keep="null-key"),
    )}


@pytest.mark.parametrize("defect", ["unchanged", "wrong_group", "wrong_value", "missing_field", "geometry", "duplicate", "missing", "crs"])
def test_dissolve_rejects_wrong_grouped_union(defect):
    inputs, params = dissolve_inputs(), {"by": "group"}
    output = execute("vector.dissolve", inputs, params)
    rows = output["data"]["features"]
    if defect == "unchanged":
        output["data"]["features"] = deepcopy(inputs["input"]["data"]["features"])
    elif defect == "wrong_group":
        rows[0]["properties"]["group"] = "invented"
    elif defect == "wrong_value":
        rows[0]["properties"]["value"] = 999
    elif defect == "missing_field":
        del rows[0]["properties"]["keep"]
    elif defect == "geometry":
        rows[0]["geometry"] = mapping(box(118.7, 32, 118.704, 32.004))
    elif defect == "duplicate":
        rows[1] = deepcopy(rows[0])
    elif defect == "missing":
        rows.pop()
    else:
        output["data"]["_physical_crs"] = "EPSG:3857"
    assert not check("vector.dissolve", output, inputs, params)


@pytest.mark.parametrize("params", [{}, {"by": "group"}])
def test_dissolve_accepts_real_union_with_first_non_null_fields(params):
    inputs = dissolve_inputs()
    output = execute("vector.dissolve", inputs, params)
    rows = output["data"]["features"]
    assert len(rows) == (2 if params else 1)
    assert rows[0]["properties"]["value"] == 2
    rows.reverse()
    assert check("vector.dissolve", output, inputs, params)


def count_inputs():
    return {"points": vector(
        feature(Point(0, 0), label="boundary"), feature(Point(.5, .5), label="inside"),
        feature(Point(.5, .5), label="duplicate"), feature(Point(8, 8), label="outside"),
    ), "polygons": vector(
        feature(box(0, 0, 1, 1), name="a", count=99, value=None),
        feature(box(-1, -1, .75, .75), name="b", count=88, value=3),
        feature(box(2, 2, 3, 3), name="zero", count=77, value=4),
    )}


@pytest.mark.parametrize("defect", ["count", "boolean_count", "missing_field", "properties", "geometry", "duplicate", "missing", "crs"])
def test_count_points_rejects_wrong_counts_or_polygon_records(defect):
    inputs = count_inputs()
    output = execute("vector.count_points", inputs)
    rows = output["data"]["features"]
    if defect in {"count", "boolean_count"}:
        rows[0]["properties"]["count"] = 2 if defect == "count" else True
    elif defect == "missing_field":
        del rows[0]["properties"]["value"]
    elif defect == "properties":
        rows[0]["properties"]["name"] = "changed"
    elif defect == "geometry":
        rows[0]["geometry"] = mapping(box(0, 0, .9, .9))
    elif defect == "duplicate":
        rows[1] = deepcopy(rows[0])
    elif defect == "missing":
        rows.pop()
    else:
        output["data"]["_crs_label"] = "GCJ02"
    assert not check("vector.count_points", output, inputs)


def test_count_points_accepts_boundary_overlap_duplicate_points_and_zero_count():
    inputs = count_inputs()
    output = execute("vector.count_points", inputs)
    assert [r["properties"]["count"] for r in output["data"]["features"]] == [3, 3, 0]
    output["data"]["features"].reverse()
    assert check("vector.count_points", output, inputs)


def nearest_inputs():
    return {"input": vector(feature(Point(118.7, 32), name="left", value=7)),
            "other": vector(feature(Point(118.701, 32), name="near", right_value=1),
                            feature(Point(118.702, 32), name="middle", right_value=2),
                            feature(Point(118.71, 32), name="far", right_value=3))}


@pytest.mark.parametrize("k", [1, 2])
@pytest.mark.parametrize("defect", ["wrong_neighbor", "distance", "nan_distance", "boolean_distance", "missing_field", "properties", "geometry", "duplicate", "missing", "crs"])
def test_join_nearest_rejects_plausible_wrong_relation(defect, k):
    inputs, params = nearest_inputs(), {"k": k}
    output = execute("vector.join_nearest", inputs, params)
    rows = output["data"]["features"]
    if defect == "wrong_neighbor":
        rows[0]["properties"]["name_right"] = "far"
        rows[0]["properties"]["right_value"] = 3
    elif defect in {"distance", "nan_distance", "boolean_distance"}:
        rows[0]["properties"]["distance_m"] = {"distance": 0, "nan_distance": float("nan"), "boolean_distance": True}[defect]
    elif defect == "missing_field":
        del rows[0]["properties"]["value"]
    elif defect == "properties":
        rows[0]["properties"]["value"] = 8
    elif defect == "geometry":
        rows[0]["geometry"] = mapping(Point(118.8, 32))
    elif defect == "duplicate":
        rows.append(deepcopy(rows[0]))
    elif defect == "missing":
        rows.pop()
    else:
        output["data"]["_physical_crs"] = "EPSG:3857"
    assert not check("vector.join_nearest", output, inputs, params)


@pytest.mark.parametrize("k,expected", [(1, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]), (2, [0, 1]), (3, [0, 1, 10]), (20, [0, 1, 10, 11, 2, 3, 4, 5, 6, 7, 8, 9])])
@pytest.mark.parametrize("maximum", [None, 0])
def test_nearest_ties_follow_k_specific_cardinality_and_stable_index(k, expected, maximum):
    inputs = {"input": vector(feature(Point(0, 0), left="a")),
              "other": vector(*(feature(Point(0, 0), right=i) for i in range(12)))}
    params = {"k": k, **({"max_distance": maximum} if maximum is not None else {})}
    output = execute("vector.join_nearest", inputs, params)
    assert [row["properties"]["right"] for row in output["data"]["features"]] == expected
    assert check("vector.join_nearest", output, inputs, params)
    if k == 3:
        output["data"]["features"][2]["properties"]["right"] = 2
        assert not check("vector.join_nearest", output, inputs, params)


@pytest.mark.parametrize("k", [1, 2])
def test_nearest_max_distance_has_closed_boundary_and_rejects_omitted_matches(k):
    inputs = nearest_inputs()
    output = execute("vector.join_nearest", inputs, {"k": k})
    distance = output["data"]["features"][0]["properties"]["distance_m"]
    params = {"k": k, "max_distance": distance}
    bounded = execute("vector.join_nearest", inputs, params)
    assert len(bounded["data"]["features"]) == 1
    assert check("vector.join_nearest", bounded, inputs, params)
    bounded["data"]["features"] = []
    assert not check("vector.join_nearest", bounded, inputs, params)
    miss = execute("vector.join_nearest", inputs, {"k": k, "max_distance": distance / 2})
    assert check("vector.join_nearest", miss, inputs, {"k": k, "max_distance": distance / 2})


@pytest.mark.parametrize("operation", ["vector.dissolve", "vector.join_nearest", "vector.count_points"])
@pytest.mark.parametrize("crs", [4326, 3857, "GCJ02"])
def test_real_aggregate_outputs_preserve_declared_crs_and_3d(operation, crs):
    from pyproj import Transformer
    from shapely.ops import transform

    point = Point(118.7, 32, 5)
    polygon = box(118.69, 31.99, 118.71, 32.01)
    if crs == 3857:
        projection = Transformer.from_crs(4326, 3857, always_xy=True).transform
        point, polygon = transform(projection, point), transform(projection, polygon)
    def layer(geometry, **properties):
        value = vector(feature(geometry, **properties), epsg=3857 if crs == 3857 else 4326)
        if crs == "GCJ02":
            value["crs"] = {"kind": "GCJ02"}
        return value
    if operation == "vector.count_points":
        inputs = {"points": layer(point), "polygons": layer(polygon, name="zone")}
    else:
        inputs = {"input": layer(point, name="left"), "other": layer(point, name="right")}
        if operation == "vector.dissolve":
            inputs.pop("other")
    output = execute(operation, inputs)
    assert check(operation, output, inputs)


@pytest.mark.parametrize("operation", ["vector.dissolve", "vector.join_nearest", "vector.count_points"])
def test_aggregate_contracts_have_specific_versioned_postconditions(operation):
    contract = ToolContractRegistry.with_builtins().get(operation)
    assert contract.version == "1.1.0"
    assert contract.postconditions == ["app.runtime.postconditions:validate_" + operation.replace(".", "_")]
    assert contract.manifest["supersedes"]["version"] == "1.0.0"


def test_nearest_validation_reuses_bounded_projection_setup(monkeypatch):
    from pyproj import Transformer

    inputs = {"input": vector(*(feature(Point(-122 + i / 1000, 37), left=i) for i in range(24))),
              "other": vector(feature(Point(-122, 37), right=1))}
    output = execute("vector.join_nearest", inputs)
    original, calls = Transformer.from_crs, []
    def counted(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(Transformer, "from_crs", counted)
    assert check("vector.join_nearest", output, inputs)
    assert len(calls) <= 6, "Projection setup must be bounded by CRS pairs, not feature count"


@pytest.mark.parametrize("operation", ["vector.dissolve", "vector.join_nearest", "vector.count_points"])
def test_output_crs_declarations_cannot_contradict_physical_marker(operation):
    inputs = {"vector.dissolve": dissolve_inputs, "vector.join_nearest": nearest_inputs,
              "vector.count_points": count_inputs}[operation]()
    output = execute(operation, inputs)
    output["data"]["crs"] = {"kind": "EPSG", "code": 3857}
    assert not check(operation, output, inputs)


def test_count_points_keeps_duplicate_polygon_rows_and_counts_multipoint_once():
    from shapely.geometry import MultiPoint

    polygon = feature(box(0, 0, 1, 1), name="same", _poly_idx=99)
    inputs = {"points": vector(feature(MultiPoint([(0, 0), (.5, .5)]))),
              "polygons": vector(polygon, deepcopy(polygon))}
    output = execute("vector.count_points", inputs)
    assert [row["properties"]["count"] for row in output["data"]["features"]] == [1, 1]
    assert check("vector.count_points", output, inputs)


@pytest.mark.parametrize("k", [1, 3])
def test_nearest_accepts_right_field_collisions_and_nullable_fields(k):
    inputs = {"input": vector(feature(Point(0, 0), name="a", value=None)),
              "other": vector(feature(Point(0, 0), name="b", value=2), feature(Point(1, 1), name="c"))}
    if k > 1:
        inputs["input"]["data"]["features"][0]["properties"]["name_right"] = "existing"
    output = execute("vector.join_nearest", inputs, {"k": k})
    assert check("vector.join_nearest", output, inputs, {"k": k})


def test_non_wgs_geographic_crs_does_not_use_projected_unit_tolerance():
    inputs = {"input": vector(feature(Point(118.7, 32), name="a"), epsg=4490)}
    output = execute("vector.dissolve", inputs)
    assert check("vector.dissolve", output, inputs)
    output["data"]["features"][0]["geometry"]["coordinates"][0] += 5e-5
    assert not check("vector.dissolve", output, inputs)
