"""Offline mutation checks for real-provider robustness smoke oracles."""

from __future__ import annotations

from copy import deepcopy

import geopandas as gpd
import pytest
from shapely.geometry import Polygon

from tests.live import runtime_robustness_smoke as smoke


def test_inventory_is_fixed_and_contains_no_oracle_answer_in_prompt():
    assert set(smoke.CASES) == {"rename_resume", "distance_resume", "crs_resume", "clip_roles", "filename_injection"}
    assert smoke.CASES["rename_resume"]["reply"] not in smoke.CASES["rename_resume"]["prompt"]
    assert "500" not in smoke.CASES["distance_resume"]["prompt"]
    assert "3857" not in smoke.CASES["crs_resume"]["prompt"]


def test_source_freeze_covers_executable_runtime_upload_and_locked_dependencies():
    names = {path.replace("\\", "/").split("/backend/")[-1] for path in smoke._source_hashes()}
    assert "app/runtime/queue.py" in names
    assert "app/runtime/postconditions.py" in names
    assert "app/api/runtime.py" in names
    assert "app/runtime/contracts/public_operations_manifest.json" in names
    assert "tests/live/real_export_format_smoke.py" in names
    assert "requirements-ci.lock" in names


def test_clip_plan_rejects_swapped_input_roles():
    plan, [first, second] = _clip_plan(keep_fields=False)
    plan["tasks"][-1]["inputs"]["input"]["task_key"] = "load_mask"
    plan["tasks"][-1]["inputs"]["mask"]["task_key"] = "load_source"
    with pytest.raises(AssertionError, match="role"):
        smoke.assert_plan_semantics("clip_roles", plan, [first, second], source_fields={"name", "quantity"})


def _clip_plan(*, keep_fields: bool = True):
    first, second = "source-file", "mask-file"
    tasks = [
        {"task_key": "load_source", "op": "io.load_vector", "inputs": {
            "file": {"kind": "artifact", "artifact_id": first}}, "outputs": [{"port": "vector"}]},
        {"task_key": "load_mask", "op": "io.load_vector", "inputs": {
            "file": {"kind": "artifact", "artifact_id": second}}, "outputs": [{"port": "vector"}]},
        {"task_key": "clip", "op": "vector.clip", "inputs": {
            "input": {"kind": "task_output", "task_key": "load_source", "port": "vector"},
            "mask": {"kind": "task_output", "task_key": "load_mask", "port": "vector"},
        }, "outputs": [{"port": "output"}]},
    ]
    if keep_fields:
        tasks.append({"task_key": "keep", "op": "attribute.keep_fields", "inputs": {
            "input": {"kind": "task_output", "task_key": "clip", "port": "output"},
        }, "params": {"fields": ["name", "quantity"]}, "outputs": [{"port": "output"}]})
    return {"tasks": tasks}, [first, second]


@pytest.mark.parametrize("with_keep,expected_sink", [(False, "clip"), (True, "keep")])
def test_clip_accepts_only_semantically_equivalent_terminal_paths(with_keep, expected_sink):
    plan, uploads = _clip_plan(keep_fields=with_keep)
    assert smoke.assert_plan_semantics("clip_roles", plan, uploads,
                                      source_fields={"name", "quantity"}) == expected_sink


@pytest.mark.parametrize("mutation,expected_error", [
    ("drop_source_field", "fields"),
    ("include_mask_field", "fields"),
    ("duplicate_field", "fields"),
    ("bypass_clip", "clip"),
    ("wrong_port", "port"),
    ("extra_side_effect", "side effect|unexpected|forbidden"),
    ("orphan_task", "unexpected|terminal"),
    ("extra_keep_fields", "unexpected|terminal"),
])
def test_clip_rejects_non_equivalent_keep_fields_mutations(mutation, expected_error):
    plan, uploads = _clip_plan()
    tasks = plan["tasks"]
    keep = tasks[-1]
    if mutation == "drop_source_field":
        keep["params"]["fields"] = ["name"]
    elif mutation == "include_mask_field":
        keep["params"]["fields"] = ["name", "quantity", "mask_id"]
    elif mutation == "duplicate_field":
        keep["params"]["fields"] = ["name", "quantity", "quantity"]
    elif mutation == "bypass_clip":
        keep["inputs"]["input"]["task_key"] = "load_source"
        keep["inputs"]["input"]["port"] = "vector"
    elif mutation == "wrong_port":
        keep["inputs"]["input"]["port"] = "vector"
    elif mutation == "extra_side_effect":
        keep["side_effect"] = True
    elif mutation == "orphan_task":
        tasks.append({"task_key": "orphan", "op": "io.load_vector", "inputs": {
            "file": {"kind": "artifact", "artifact_id": uploads[0]}}})
    elif mutation == "extra_keep_fields":
        duplicate = deepcopy(keep)
        duplicate["task_key"] = "extra_keep"
        tasks.append(duplicate)
    with pytest.raises(AssertionError, match=expected_error):
        smoke.assert_plan_semantics("clip_roles", plan, uploads,
                                    source_fields={"name", "quantity"})


def test_clip_uses_terminal_artifact_even_if_intermediate_clip_is_good():
    plan, uploads = _clip_plan()
    sink = smoke.assert_plan_semantics("clip_roles", plan, uploads,
                                       source_fields={"name", "quantity"})
    chosen = smoke.select_terminal_artifact_id([
        {"plan_id": "active-plan", "task_key": "clip", "artifact_id": "good-clip"},
        {"plan_id": "active-plan", "task_key": "keep", "artifact_id": "bad-terminal"},
    ], sink, "active-plan")
    assert chosen == "bad-terminal"
    source = gpd.GeoDataFrame.from_features(smoke.SOURCE_POLYGON["features"], crs="EPSG:4326")
    mask = gpd.GeoDataFrame.from_features(smoke.MASK_POLYGON["features"], crs="EPSG:4326")
    good_clip = source.copy()
    good_clip.geometry = source.geometry.intersection(mask.geometry.iloc[0])
    bad_terminal = source.copy()
    artifacts = {"good-clip": good_clip, "bad-terminal": bad_terminal}
    smoke.assert_output_semantics("clip_roles", artifacts["good-clip"], [source, mask])
    with pytest.raises(AssertionError, match="clip geometry"):
        smoke.assert_output_semantics("clip_roles", artifacts[chosen], [source, mask])


def test_terminal_artifact_selection_ignores_same_key_in_superseded_plan():
    rows = [
        {"plan_id": "old-plan", "task_key": "keep", "artifact_id": "old-result"},
        {"plan_id": "active-plan", "task_key": "keep", "artifact_id": "new-result"},
    ]
    assert smoke.select_terminal_artifact_id(rows, "keep", "active-plan") == "new-result"


def test_clip_output_oracle_rejects_geometry_that_ignores_mask():
    source = gpd.GeoDataFrame({"name": ["source"], "quantity": [7]}, geometry=[Polygon([(-120, 40), (-119.9, 40), (-119.9, 40.1), (-120, 40.1)])], crs="EPSG:4326")
    mask = gpd.GeoDataFrame({"mask_id": ["inner"]}, geometry=[Polygon([(-119.99, 40.01), (-119.95, 40.01), (-119.95, 40.05), (-119.99, 40.05)])], crs="EPSG:4326")
    with pytest.raises(AssertionError, match="clip geometry"):
        smoke.assert_output_semantics("clip_roles", source, [source, mask])


def test_clip_output_oracle_rejects_extra_non_source_field():
    source = gpd.GeoDataFrame.from_features(smoke.SOURCE_POLYGON["features"], crs="EPSG:4326")
    mask = gpd.GeoDataFrame.from_features(smoke.MASK_POLYGON["features"], crs="EPSG:4326")
    output = source.copy()
    output.geometry = source.geometry.intersection(mask.geometry.iloc[0])
    output["unexpected"] = "not a source field"
    with pytest.raises(AssertionError, match="output fields"):
        smoke.assert_output_semantics("clip_roles", output, [source, mask])


def test_clip_output_oracle_rejects_fractional_change_hidden_by_int_cast():
    source = gpd.GeoDataFrame.from_features(smoke.SOURCE_POLYGON["features"], crs="EPSG:4326")
    mask = gpd.GeoDataFrame.from_features(smoke.MASK_POLYGON["features"], crs="EPSG:4326")
    output = source.copy()
    output.geometry = source.geometry.intersection(mask.geometry.iloc[0])
    output["quantity"] = 7.1
    with pytest.raises(AssertionError, match="source attribute"):
        smoke.assert_output_semantics("clip_roles", output, [source, mask])


def test_gis_reader_excludes_only_proven_top_level_geojson_feature_id():
    source = gpd.GeoDataFrame.from_features(smoke.SOURCE_POLYGON["features"], crs="EPSG:4326")
    mask = gpd.GeoDataFrame.from_features(smoke.MASK_POLYGON["features"], crs="EPSG:4326")
    output = source.copy()
    output.geometry = source.geometry.intersection(mask.geometry.iloc[0])
    output["id"] = "0"  # GDAL exposes GeoJSON Feature.id as a column.
    feature = {"id": "0", "properties": {"name": "source", "quantity": 7}}
    normalized = smoke.normalize_geojson_reader_properties(output, feature)
    smoke.assert_output_semantics("clip_roles", normalized, [source, mask])
    feature["properties"]["id"] = "0"  # A real property may not be hidden.
    with pytest.raises(AssertionError, match="output fields"):
        smoke.assert_output_semantics("clip_roles", smoke.normalize_geojson_reader_properties(output, feature), [source, mask])


def test_gis_reader_rejects_raw_property_disagreement():
    output = gpd.GeoDataFrame.from_features(smoke.SOURCE_POLYGON["features"], crs="EPSG:4326")
    feature = {"properties": {"name": "source", "quantity": 7.1}}
    with pytest.raises(AssertionError, match="GeoJSON properties"):
        smoke.normalize_geojson_reader_properties(output, feature)


@pytest.mark.parametrize("mutation", ["changed_quantity", "extra_field"])
def test_rename_output_oracle_preserves_all_other_source_attributes(mutation):
    source = gpd.GeoDataFrame.from_features(smoke.POINTS["features"], crs="EPSG:4326")
    output = source.rename(columns={"old_name": "new_name"})
    if mutation == "changed_quantity":
        output["quantity"] = 7.1
    else:
        output["surprise"] = "extra"
    with pytest.raises(AssertionError, match="source attribute|output fields"):
        smoke.assert_output_semantics("rename_resume", output, [source])


@pytest.mark.parametrize("mutation", ["missing_quantity", "changed_quantity"])
def test_centroid_output_oracle_preserves_all_source_attributes(mutation):
    source = gpd.GeoDataFrame.from_features(smoke.SOURCE_POLYGON["features"], crs="EPSG:4326")
    output = source.copy()
    metric_crs = source.estimate_utm_crs()
    output.geometry = source.to_crs(metric_crs).centroid.to_crs(source.crs)
    if mutation == "missing_quantity":
        output = output.drop(columns=["quantity"])
    else:
        output["quantity"] = 7.1
    with pytest.raises(AssertionError, match="source attribute|output fields"):
        smoke.assert_output_semantics("filename_injection", output, [source])


@pytest.mark.parametrize("case_id", ["rename_resume", "clip_roles", "filename_injection"])
def test_crs_preserving_operation_rejects_wrong_crs_label(case_id):
    if case_id == "rename_resume":
        source = gpd.GeoDataFrame.from_features(smoke.POINTS["features"], crs="EPSG:4326")
        output = source.rename(columns={"old_name": "new_name"})
        sources = [source]
    else:
        source = gpd.GeoDataFrame.from_features(smoke.SOURCE_POLYGON["features"], crs="EPSG:4326")
        if case_id == "clip_roles":
            mask = gpd.GeoDataFrame.from_features(smoke.MASK_POLYGON["features"], crs="EPSG:4326")
            output = source.copy()
            output.geometry = source.geometry.intersection(mask.geometry.iloc[0])
            sources = [source, mask]
        else:
            output = source.copy()
            metric_crs = source.estimate_utm_crs()
            output.geometry = source.to_crs(metric_crs).centroid.to_crs(source.crs)
            sources = [source]
    wrong_label = output.set_crs(3857, allow_override=True)
    with pytest.raises(AssertionError, match="source CRS"):
        smoke.assert_output_semantics(case_id, wrong_label, sources)


def test_injection_plan_rejects_side_effect_even_with_correct_centroid():
    plan = {"tasks": [
        {"task_key": "centroid", "op": "vector.centroid", "inputs": {}},
        {"task_key": "publish", "op": "publish_file", "inputs": {}},
    ]}
    with pytest.raises(AssertionError, match="forbidden"):
        smoke.assert_plan_semantics("filename_injection", plan, ["source-file"])


@pytest.mark.parametrize("radius,accepted", [(500, True), (525, False)])
def test_buffer_oracle_rejects_five_percent_numeric_drift(radius, accepted):
    source = gpd.GeoDataFrame.from_features(smoke.POINTS["features"], crs="EPSG:4326")
    metric_crs = source.estimate_utm_crs()
    output = source.to_crs(metric_crs)
    output.geometry = output.geometry.buffer(radius)
    output = output.to_crs(source.crs)
    if accepted:
        smoke.assert_output_semantics("distance_resume", output, [source])
    else:
        with pytest.raises(AssertionError, match="500 metres"):
            smoke.assert_output_semantics("distance_resume", output, [source])
