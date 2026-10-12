"""Executable semantic contracts for Runtime-owned vector operations.

These tests intentionally exercise real GeoPandas/Shapely handlers.  They
guard against a plan compiling while its declared parameters or CRS metadata
are silently discarded by the Runtime adapter.
"""

from __future__ import annotations

from app.runtime import ToolContractRegistry
from app.runtime import tool_handlers


def _points(*rows: tuple[float, float, str]) -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": name, "group": name},
                "geometry": {"type": "Point", "coordinates": [x, y]},
            }
            for x, y, name in rows
        ],
    }


def test_attribute_keep_fields_forwards_declared_fields_to_the_handler():
    result = tool_handlers.handle_attribute_keep_fields(
        input=_points((116.4, 39.9, "north")),
        fields=["name"],
    )

    assert result["status"] == "success"
    assert result["data"]["features"][0]["properties"] == {"name": "north"}


def test_attribute_filter_contract_operators_match_the_handler_semantics():
    source = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"name": "north", "value": 1}, "geometry": {"type": "Point", "coordinates": [116.4, 39.9]}},
            {"type": "Feature", "properties": {"name": "south", "value": 2}, "geometry": {"type": "Point", "coordinates": [116.5, 39.9]}},
        ],
    }
    equals = tool_handlers.handle_attribute_filter(input=source, field="value", operator="=", value=2)
    members = tool_handlers.handle_attribute_filter(input=source, field="name", operator="in", value=["south"])

    assert equals["status"] == "success"
    assert [feature["properties"]["name"] for feature in equals["data"]["features"]] == ["south"]
    assert members["status"] == "success"
    assert [feature["properties"]["name"] for feature in members["data"]["features"]] == ["south"]


def test_attribute_filter_contract_advertises_supported_operator_set():
    operators = ToolContractRegistry.with_builtins().get("attribute.filter").parameter_schema["properties"]["operator"]["enum"]
    assert set(operators) >= {"=", "==", "!=", "contains", "in", "is_null"}


def test_vector_dissolve_forwards_group_parameter_without_collapsing_groups():
    result = tool_handlers.handle_vector_dissolve(
        input=_points((116.4, 39.9, "north"), (116.5, 39.9, "south")),
        by="group",
    )

    assert result["status"] == "success"
    assert {feature["properties"]["group"] for feature in result["data"]["features"]} == {"north", "south"}


def test_vector_merge_consumes_every_artifact_bound_to_the_many_port():
    result = tool_handlers.handle_vector_merge(
        input=[
            {"data": _points((116.4, 39.9, "north")), "crs": {"kind": "EPSG", "code": 4326}},
            {"data": _points((116.5, 39.8, "south")), "crs": {"kind": "EPSG", "code": 4326}},
        ],
    )

    assert result["status"] == "success"
    assert {feature["properties"]["name"] for feature in result["data"]["features"]} == {"north", "south"}


def test_vector_join_nearest_honors_k_parameter():
    left = _points((116.4, 39.9, "origin"))
    right = _points((116.401, 39.9, "near"), (116.41, 39.9, "mid"), (116.5, 39.9, "far"))

    result = tool_handlers.handle_vector_join_nearest(input=left, other=right, k=2)

    assert result["status"] == "success"
    assert [feature["properties"]["name_right"] for feature in result["data"]["features"]] == ["near", "mid"]


def test_vector_join_nearest_returns_typed_empty_result_when_no_candidate_is_in_range():
    left = _points((116.4, 39.9, "origin"))
    right = _points((116.5, 39.9, "far"))

    result = tool_handlers.handle_vector_join_nearest(input=left, other=right, max_distance=1, k=2)

    assert result["status"] == "success"
    assert result["data"]["type"] == "FeatureCollection"
    assert result["data"]["features"] == []


def test_vector_check_validity_is_a_table_issue_report_not_a_vector_payload():
    result = tool_handlers.handle_vector_check_validity(input=_points((116.4, 39.9, "valid")))

    assert result["status"] == "success"
    assert result["data"] == {"issues": []}
    contract = ToolContractRegistry.with_builtins().get("vector.check_validity")
    assert contract.output_ports["result"].artifact_type == "Table"


def test_vector_reproject_many_propagates_member_failure_instead_of_publishing_null_output():
    result = tool_handlers.handle_vector_reproject_many(
        vector=[_points((116.4, 39.9, "ok")), {"data": {"not": "a feature collection"}}],
        target_crs={"kind": "EPSG", "code": 3857},
    )

    assert result["status"] == "error"


def test_vector_contracts_expose_every_required_binary_input_and_parameter():
    registry = ToolContractRegistry.with_builtins()

    assert set(registry.get("vector.overlay").input_ports) == {"input", "other"}
    assert set(registry.get("vector.clip").input_ports) == {"input", "mask"}
    assert set(registry.get("vector.snap").input_ports) == {"input", "other"}
    assert set(registry.get("vector.count_points").input_ports) == {"points", "polygons"}
    assert registry.get("vector.simplify").parameter_schema["required"] == ["tolerance"]
    assert registry.get("attribute.keep_fields").parameter_schema["required"] == ["fields"]


def test_runtime_gdf_uses_artifact_crs_instead_of_relabeling_projected_coordinates():
    artifact = {
        "crs": {"kind": "EPSG", "code": 3857},
        "data": _points((12957588.0, 4852834.0, "projected")),
    }

    parsed = tool_handlers._gdf(artifact)

    assert parsed is not None
    assert parsed.crs.to_epsg() == 3857
    assert parsed.geometry.iloc[0].x == 12957588.0


def test_same_crs_coordinate_conversion_is_an_identity():
    result = tool_handlers.handle_geo_transform(
        lng=118.78,
        lat=32.04,
        source_crs={"kind": "WGS84"},
        target_crs={"kind": "WGS84"},
    )

    assert result == {
        "status": "success",
        "data": {
            "location": [118.78, 32.04],
            "source_crs": "WGS84",
            "target_crs": "WGS84",
        },
        "source": "computed",
    }


def test_vector_datum_conversion_transforms_feature_collection_not_a_single_point():
    source = _points((118.78, 32.04, "one"), (118.79, 32.05, "two"))

    result = tool_handlers.handle_vector_convert_datum(
        vector=source,
        source_crs={"kind": "WGS84"},
        target_crs={"kind": "WGS84"},
    )

    assert result["status"] == "success"
    assert result["data"]["type"] == "FeatureCollection"
    assert len(result["data"]["features"]) == 2
    assert result["data"]["features"][0]["geometry"]["coordinates"] == [118.78, 32.04]
