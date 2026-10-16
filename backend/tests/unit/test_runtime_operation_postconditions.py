"""Independent result checks reject plausible but incorrect vector outputs."""

from copy import deepcopy

import pytest
from shapely.geometry import Point, box, mapping

from app.runtime import tool_handlers
from app.runtime.postconditions import get_postcondition


def feature(geometry, **properties):
    return {"type": "Feature", "geometry": mapping(geometry), "properties": properties}


def vector(*features, epsg=4326):
    return {"type": "Vector", "crs": {"kind": "EPSG", "code": epsg},
            "data": {"type": "FeatureCollection", "features": list(features)}}


def result(*features):
    return {"status": "success", "data": {"type": "FeatureCollection", "features": list(features)}}


def check(operation, output, inputs, params=None):
    return get_postcondition(operation)(output, inputs, params or {}, empty_policy="allow")


@pytest.mark.parametrize("defect", ["unchanged", "wrong_value", "lost_column", "moved_geometry", "lost_record"])
def test_rename_rejects_incomplete_or_corrupted_transformation(defect):
    source = vector(feature(Point(118.7, 32), old=7, keep="same"))
    output = result(feature(Point(118.7, 32), new=7, keep="same"))
    row = output["data"]["features"][0]
    if defect == "unchanged":
        row["properties"] = {"old": 7, "keep": "same"}
    elif defect == "wrong_value":
        row["properties"]["new"] = 8
    elif defect == "lost_column":
        del row["properties"]["keep"]
    elif defect == "moved_geometry":
        row["geometry"] = mapping(Point(118.8, 32))
    else:
        output["data"]["features"] = []
    assert not check("attribute.rename_field", output, {"input": source}, {"old_name": "old", "new_name": "new"})


@pytest.mark.parametrize("defect", ["extra_column", "wrong_value", "missing_record"])
def test_keep_fields_checks_values_and_complete_record_set(defect):
    source = vector(feature(Point(0, 0), keep=7, remove=8))
    output = result(feature(Point(0, 0), keep=7))
    if defect == "extra_column":
        output["data"]["features"][0]["properties"]["remove"] = 8
    elif defect == "wrong_value":
        output["data"]["features"][0]["properties"]["keep"] = 8
    else:
        output["data"]["features"] = []
    assert not check("attribute.keep_fields", output, {"input": source}, {"fields": ["keep"]})


@pytest.mark.parametrize("defect", ["unfiltered", "missing_match", "duplicate", "changed_property"])
def test_filter_checks_exact_selection_including_omitted_matches(defect):
    rows = [feature(Point(i, 0), value=i, name=f"r{i}") for i in (1, 2, 3)]
    output = result(*deepcopy(rows[1:]))
    if defect == "unfiltered":
        output = result(*rows)
    elif defect == "missing_match":
        output["data"]["features"].pop()
    elif defect == "duplicate":
        output["data"]["features"].append(deepcopy(rows[1]))
    else:
        output["data"]["features"][0]["properties"]["name"] = "changed"
    assert not check("attribute.filter", output, {"input": vector(*rows)}, {"field": "value", "operator": ">", "value": 1})


@pytest.mark.parametrize("operator,value,selected", [
    ("=", 2, [1]), ("==", 2, [1]), ("!=", 2, [0, 2]), (">", 2, []),
    (">=", 2, [1]), ("<", 2, [0]), ("<=", 2, [0, 1]), ("in", [1, 2], [0, 1]),
    ("is_null", None, [2]),
])
def test_filter_supported_operators_and_nulls(operator, value, selected):
    rows = [feature(Point(i, 0), value=v) for i, v in enumerate([1, 2, None])]
    source = vector(*rows)
    params = {"field": "value", "operator": operator, "value": value}
    assert check("attribute.filter", result(*(rows[i] for i in selected)), {"input": source}, params)
    real = tool_handlers.handle_attribute_filter(input=source, **params)
    assert check("attribute.filter", real, {"input": source}, params), real


def test_filter_contains_preserves_existing_regex_semantics():
    rows = [feature(Point(i, 0), name=name) for i, name in enumerate(["school", "housing", "other"])]
    params = {"field": "name", "operator": "contains", "value": "school|housing"}
    source = vector(*rows)
    assert check("attribute.filter", result(*rows[:2]), {"input": source}, params)
    assert not check("attribute.filter", result(*rows), {"input": source}, params)


def test_filter_negate_excludes_only_matches_and_keeps_null_records():
    rows = [feature(Point(i, 0), value=value) for i, value in enumerate([1, 5, None])]
    source = vector(*rows)
    params = {"field": "value", "operator": ">", "value": 2, "negate": True}
    assert check("attribute.filter", result(rows[0], rows[2]), {"input": source}, params)
    assert not check("attribute.filter", result(rows[0]), {"input": source}, params)
    assert not check("attribute.filter", result(rows[1]), {"input": source}, params)


def test_filter_negation_has_versioned_contract_parameter():
    from app.runtime import ToolContractRegistry

    contract = ToolContractRegistry.with_builtins().get("attribute.filter")
    assert contract.version == "1.2.0"
    assert contract.parameter_schema["properties"]["negate"] == {"type": "boolean", "default": False}


@pytest.mark.parametrize("dtype", ["float64", "Float64"])
@pytest.mark.parametrize("operator,value,selected", [("!=", 300, [0, 2]), ("is_null", None, [0, 1])])
def test_filter_negation_normalizes_unknown_matches_across_pandas_dtypes(monkeypatch, dtype, operator, value, selected):
    import pandas as pd

    rows = [feature(Point(i, 0), value=value) for i, value in enumerate([300, 250, None])]
    source = vector(*rows)
    original = pd.DataFrame
    # Exercise both real pandas comparison masks with identical JSON records.
    # GeoJSON normally infers float64; nullable frames produce a different
    # intermediate mask for NaN != value and must have the same complement.
    def typed_frame(*args, **kwargs):
        return original(*args, **kwargs, dtype=dtype)
    monkeypatch.setattr(pd, "DataFrame", typed_frame)
    params = {"field": "value", "operator": operator, "value": value, "negate": True}
    assert check("attribute.filter", result(*(rows[i] for i in selected)), {"input": source}, params)
    assert not check("attribute.filter", result(rows[0]), {"input": source}, params)


def test_filter_without_negation_retains_existing_nan_inequality_semantics():
    rows = [feature(Point(i, 0), value=value) for i, value in enumerate([300, 250, None])]
    params = {"field": "value", "operator": "!=", "value": 300, "negate": False}
    assert check("attribute.filter", result(rows[1], rows[2]), {"input": vector(*rows)}, params)


def test_chained_filters_enforce_both_conditions():
    rows = [feature(Point(i, 0), price=p, rooms=r) for i, (p, r) in enumerate([(90, 1), (90, 3), (150, 3)])]
    source = vector(*rows)
    first_params = {"field": "price", "operator": "<", "value": 100}
    first = tool_handlers.handle_attribute_filter(input=source, **first_params)
    assert check("attribute.filter", first, {"input": source}, first_params)
    intermediate = {**source, "data": first["data"]}
    second_params = {"field": "rooms", "operator": ">=", "value": 2}
    second = tool_handlers.handle_attribute_filter(input=intermediate, **second_params)
    assert check("attribute.filter", second, {"input": intermediate}, second_params)
    assert not check("attribute.filter", result(*rows[1:]), {"input": intermediate}, second_params)


@pytest.mark.parametrize("defect", ["unclipped", "lost_match", "wrong_properties", "outside_mask"])
def test_clip_checks_intersection_and_complete_set(defect):
    source = vector(feature(box(0, 0, 2, 2), name="a"), feature(box(3, 0, 4, 1), name="b"), epsg=3857)
    mask = vector(feature(box(1, -1, 5, 3)), epsg=3857)
    output = result(feature(box(1, 0, 2, 2), name="a"), feature(box(3, 0, 4, 1), name="b"))
    if defect == "unclipped":
        output = {"status": "success", "data": source["data"]}
    elif defect == "lost_match":
        output["data"]["features"].pop()
    elif defect == "wrong_properties":
        output["data"]["features"][0]["properties"]["name"] = "bad"
    else:
        output["data"]["features"][0]["geometry"] = mapping(box(7, 0, 8, 2))
    assert not check("vector.clip", output, {"input": source, "mask": mask})


def test_clip_accepts_equivalent_ring_order_and_real_output():
    source = vector(feature(box(0, 0, 2, 2), name="a"), epsg=3857)
    mask = vector(feature(box(1, -1, 5, 3)), epsg=3857)
    assert check("vector.clip", result(feature(box(1, 0, 2, 2), name="a")), {"input": source, "mask": mask})
    real = tool_handlers.handle_vector_clip(input=source, mask=mask)
    assert check("vector.clip", real, {"input": source, "mask": mask}), real


def test_spatial_selection_preserves_original_records_once_for_multiple_masks():
    rows = [feature(Point(1, 1), name="housing", value=5), feature(Point(9, 9), name="far", value=8)]
    source = vector(*rows, epsg=3857)
    mask = vector(feature(box(0, 0, 2, 2), name="school1"), feature(box(0, 0, 3, 3), name="school2"), epsg=3857)
    inputs = {"input": source, "mask": mask}
    assert check("vector.extract_location", result(rows[0]), inputs)
    assert not check("vector.extract_location", result(*rows), inputs)
    assert not check("vector.extract_location", result(), inputs)
    assert not check("vector.extract_location", result(rows[0], rows[0]), inputs)
    real = tool_handlers.handle_vector_extract_location(**inputs)
    assert real["status"] == "success", real
    assert len(real["data"]["features"]) == 1
    assert real["data"]["features"][0]["properties"] == rows[0]["properties"]
    assert check("vector.extract_location", real, inputs)


@pytest.mark.parametrize("defect", ["outside", "polygon", "missing", "wrong_properties"])
def test_surface_point_requires_one_point_on_each_source_feature(defect):
    source = vector(feature(box(0, 0, 4, 4), name="a"), epsg=3857)
    output = result(feature(Point(1, 1), name="a"))
    if defect == "outside":
        output["data"]["features"][0]["geometry"] = mapping(Point(9, 9))
    elif defect == "polygon":
        output["data"]["features"][0]["geometry"] = mapping(box(1, 1, 2, 2))
    elif defect == "missing":
        output = result()
    else:
        output["data"]["features"][0]["properties"]["name"] = "b"
    assert not check("vector.point_on_surface", output, {"input": source})


@pytest.mark.parametrize("epsg,point", [(4326, Point(118.78, 32.04)), (3857, Point(13222425, 3768681))])
def test_real_surface_point_and_attribute_operations_preserve_crs(epsg, point):
    source = vector(feature(point.buffer(.001 if epsg == 4326 else 100), old=7, keep="x"), epsg=epsg)
    for operation, params in [("attribute.rename_field", {"old_name": "old", "new_name": "new"}),
                              ("attribute.keep_fields", {"fields": ["old"]}),
                              ("vector.point_on_surface", {})]:
        output = getattr(tool_handlers, "handle_" + operation.replace(".", "_"))(input=source, **params)
        assert check(operation, output, {"input": source}, params), (operation, output)


@pytest.mark.parametrize("defect", ["unchanged", "wrong_distance", "changed_properties", "duplicate"])
def test_buffer_checks_geometry_distance_independently_of_claimed_metadata(defect):
    source = vector(feature(Point(118.78, 32.04), name="school"))
    params = {"distance_m": 100}
    output = tool_handlers.handle_vector_buffer(vector=source, distance_m=10 if defect == "wrong_distance" else 100)
    output["data"]["buffer_distance_m"] = 100
    if defect == "unchanged":
        output = result(*source["data"]["features"])
    elif defect == "changed_properties":
        output["data"]["features"][0]["properties"]["name"] = "other"
    elif defect == "duplicate":
        output["data"]["features"] *= 2
    assert not check("vector.buffer", output, {"vector": source}, params)


@pytest.mark.parametrize("epsg,point", [(4326, Point(118.78, 32.04)), (3857, Point(13222425, 3768681)), (4326, Point(-73.9, 40.7))])
def test_real_buffer_passes_independent_distance_check(epsg, point):
    source = vector(feature(point, name="school"), epsg=epsg)
    output = tool_handlers.handle_vector_buffer(vector=source, distance_m=100)
    assert check("vector.buffer", output, {"vector": source}, {"distance_m": 100}), output


@pytest.mark.parametrize("operation,params", [
    ("attribute.rename_field", {"old_name": "old", "new_name": "new"}),
    ("attribute.keep_fields", {"fields": ["old"]}),
    ("attribute.filter", {"field": "old", "operator": ">", "value": 0}),
])
def test_attribute_handlers_do_not_move_coordinates(operation, params):
    source = vector(feature(Point(118.78, 32.04), old=7))
    output = getattr(tool_handlers, "handle_" + operation.replace(".", "_"))(input=source, **params)
    from shapely.geometry import shape
    assert shape(output["data"]["features"][0]["geometry"]).equals_exact(Point(118.78, 32.04), 0)


@pytest.mark.parametrize("operation,params", [
    ("attribute.rename_field", {"old_name": "old", "new_name": "new"}),
    ("attribute.keep_fields", {"fields": ["old"]}),
    ("attribute.filter", {"field": "old", "operator": ">", "value": 0}),
    ("vector.point_on_surface", {}), ("vector.buffer", {"distance_m": 100}),
])
def test_native_operations_and_buffer_accept_gcj02(operation, params):
    source = vector(feature(Point(118.78, 32.04), old=7))
    source["crs"] = {"kind": "GCJ02"}
    inputs = {"vector" if operation == "vector.buffer" else "input": source}
    output = getattr(tool_handlers, "handle_" + operation.replace(".", "_"))(**inputs, **params)
    assert check(operation, output, inputs, params), output


@pytest.mark.parametrize("operation,params", [
    ("attribute.rename_field", {"old_name": "old", "new_name": "new"}),
    ("attribute.keep_fields", {"fields": ["old"]}),
    ("attribute.filter", {"field": "old", "operator": ">", "value": 100}),
    ("vector.clip", {}), ("vector.extract_location", {}), ("vector.point_on_surface", {}),
])
def test_semantic_checks_allow_genuine_empty_outputs(operation, params):
    source = vector()
    inputs = {"input": source, "mask": vector(feature(box(0, 0, 1, 1)))}
    assert check(operation, {"status": "empty", "data": source["data"]}, inputs, params)
    assert not get_postcondition(operation)({"status": "empty", "data": source["data"]}, inputs, params, empty_policy="error")


def test_semantic_checks_preserve_nullable_records_and_duplicates():
    rows = [feature(Point(0, 0), old=None, name="a"), feature(Point(1, 1), old=7), feature(Point(1, 1), old=7)]
    source = vector(*rows)
    params = {"old_name": "old", "new_name": "new"}
    output = tool_handlers.handle_attribute_rename_field(input=source, **params)
    output["data"]["features"].reverse()
    assert check("attribute.rename_field", output, {"input": source}, params)
    output["data"]["features"].pop()
    assert not check("attribute.rename_field", output, {"input": source}, params)


@pytest.mark.parametrize("predicate,selected", [("intersects", [0, 1]), ("within", [0]), ("contains", [1])])
def test_spatial_predicates_are_applied_to_source_geometry(predicate, selected):
    rows = [feature(Point(1, 1), id=0), feature(box(-1, -1, 3, 3), id=1), feature(Point(9, 9), id=2)]
    inputs = {"input": vector(*rows, epsg=3857), "mask": vector(feature(box(0, 0, 2, 2)), epsg=3857)}
    params = {"predicate": predicate}
    assert check("vector.extract_location", result(*(rows[i] for i in selected)), inputs, params)
    real = tool_handlers.handle_vector_extract_location(**inputs, **params)
    assert check("vector.extract_location", real, inputs, params), real
