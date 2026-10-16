"""Buffer ring representation regressions; deterministic GIS, no model calls."""
from copy import deepcopy

import pytest
from shapely.affinity import translate
from shapely.geometry import MultiPolygon, Point, Polygon, box, mapping, shape

from app.runtime.postconditions import _same_buffer_geometry, validate_vector_buffer
from app.runtime.tool_handlers import handle_vector_buffer
from tests.unit.test_runtime_operation_postconditions import feature, vector


def repeated_buffer():
    # Coordinates from the published input to the real failing 30 m -> 95 m
    # sequence. Fixtures are generated here; no live evidence file is required.
    source = vector(*[feature(Point(x, y), 编号=index, 数值=value) for index, (x, y, value) in enumerate([
        (118.74658819537805, 32.00591804927527, 13.),
        (118.75880142243199, 32.00435556807357, 17.),
        (118.73414490071912, 32.00613505318673, 12.001),
    ])])
    first = handle_vector_buffer(vector=source, distance_m=30)
    assert first["status"] == "success"
    intermediate = {**source, "data": first["data"]}
    second = handle_vector_buffer(vector=intermediate, distance_m=95)
    assert second["status"] == "success"
    return intermediate, second


def test_real_sequential_buffer_survives_roundtrip_change_of_normalized_ring_anchor():
    source, result = repeated_buffer()
    assert validate_vector_buffer(result, {"vector": source}, {"distance_m": 95})


@pytest.mark.parametrize("representation", ["rotate", "reverse", "reorder_records"])
def test_buffer_preserves_semantics_across_ring_and_record_representation(representation):
    source, result = repeated_buffer()
    if representation == "reorder_records":
        result["data"]["features"].reverse()
    else:
        for row in result["data"]["features"]:
            ring = list(shape(row["geometry"]).exterior.coords)[:-1]
            ring = ring[17:] + ring[:17] if representation == "rotate" else ring[::-1]
            row["geometry"] = mapping(Polygon(ring))
    assert validate_vector_buffer(result, {"vector": source}, {"distance_m": 95})


@pytest.mark.parametrize("defect", [
    "wrong_radius", "moved", "property", "missing_record", "duplicate_record",
    "missing_vertex", "extra_hole", "extra_z", "wrong_crs",
])
def test_tolerating_ring_anchor_does_not_accept_corrupted_buffer(defect):
    source, result = repeated_buffer()
    row = result["data"]["features"][0]
    geometry = shape(row["geometry"])
    if defect == "wrong_radius":
        result = handle_vector_buffer(vector=source, distance_m=96)
    elif defect == "moved":
        row["geometry"] = mapping(translate(geometry, xoff=.0001))
    elif defect == "property":
        row["properties"]["编号"] = -1
    elif defect == "missing_record":
        result["data"]["features"].pop()
    elif defect == "duplicate_record":
        result["data"]["features"][1] = deepcopy(row)
    elif defect == "missing_vertex":
        row["geometry"] = mapping(Polygon(list(geometry.exterior.coords)[1:-1]))
    elif defect == "extra_hole":
        hole = geometry.centroid.buffer(.00001)
        row["geometry"] = mapping(Polygon(geometry.exterior.coords, [hole.exterior.coords]))
    elif defect == "extra_z":
        row["geometry"] = mapping(Polygon([(x, y, 0.) for x, y in geometry.exterior.coords]))
    else:
        result["data"]["_physical_crs"] = "EPSG:3857"
    assert not validate_vector_buffer(result, {"vector": source}, {"distance_m": 95})


@pytest.mark.parametrize("multipart", [False, True])
def test_buffer_holes_and_multipart_members_remain_valid_with_reordered_rings(multipart):
    polygon = Polygon(box(500000, 3500000, 500100, 3500100).exterior.coords,
                      [box(500030, 3500030, 500070, 3500070).exterior.coords])
    geometry = MultiPolygon([polygon, translate(polygon, xoff=300)]) if multipart else polygon
    source = vector(feature(geometry, 编号=1), epsg=4549)
    result = handle_vector_buffer(vector=source, distance_m=5)
    assert result["status"] == "success"
    output = shape(result["data"]["features"][0]["geometry"])
    def reorder(polygon):
        exterior = list(polygon.exterior.coords)[:-1]
        return Polygon(exterior[7:] + exterior[:7],
                       [list(ring.coords)[::-1] for ring in reversed(polygon.interiors)])
    output = MultiPolygon([reorder(part) for part in reversed(output.geoms)]) if multipart else reorder(output)
    result["data"]["features"][0]["geometry"] = mapping(output)
    assert validate_vector_buffer(result, {"vector": source}, {"distance_m": 5})


@pytest.mark.parametrize(("dx", "dy", "accepted"), [
    (.019, 0., True), (.021, 0., False), (.014, .014, True), (.015, .015, False),
])
def test_buffer_retains_two_centimetre_euclidean_tolerance(dx, dy, accepted):
    source = vector(feature(Point(500000, 3500000), 编号=1), epsg=4549)
    result = handle_vector_buffer(vector=source, distance_m=5)
    row = result["data"]["features"][0]
    row["geometry"] = mapping(translate(shape(row["geometry"]), xoff=dx, yoff=dy))
    assert validate_vector_buffer(result, {"vector": source}, {"distance_m": 5}) is accepted


@pytest.mark.parametrize("geometry", [Polygon(), MultiPolygon([])])
def test_internal_buffer_comparator_preserves_same_type_empty_equality(geometry):
    assert _same_buffer_geometry(geometry, geometry, .02)
    other_type = MultiPolygon([]) if geometry.geom_type == "Polygon" else Polygon()
    assert not _same_buffer_geometry(geometry, other_type, .02)
    assert not _same_buffer_geometry(geometry, box(0, 0, 1, 1), .02)


def test_zero_distance_nonempty_polygon_still_validates():
    source = vector(feature(box(500000, 3500000, 500100, 3500100), 编号=1), epsg=4549)
    result = handle_vector_buffer(vector=source, distance_m=0)
    assert validate_vector_buffer(result, {"vector": source}, {"distance_m": 0})


@pytest.mark.parametrize("policy", ["allow", "error"])
def test_genuinely_empty_zero_distance_result_preserves_empty_policy(policy):
    source = vector()
    result = {"status": "empty", "data": source["data"]}
    assert validate_vector_buffer(result, {"vector": source}, {"distance_m": 0},
                                  empty_policy=policy) is (policy == "allow")


def test_zero_distance_point_cannot_publish_empty_or_missing_geometry():
    source = vector(feature(Point(118.7, 32), 编号=1))
    empty_geometry = {"status": "success", "data": {"type": "FeatureCollection",
        "features": [feature(Polygon(), 编号=1)]}}
    assert not validate_vector_buffer(empty_geometry, {"vector": source}, {"distance_m": 0})
    missing = {"status": "success", "data": {"type": "FeatureCollection", "features": []}}
    assert not validate_vector_buffer(missing, {"vector": source}, {"distance_m": 0})
