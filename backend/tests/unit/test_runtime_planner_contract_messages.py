"""Contract exposure tests; these are not real-provider success evidence."""
import json

from app.runtime.contracts import ArtifactRef, PlannerDraft, PlannerStepDraft, ToolContractRegistry
from app.runtime.llm_planner import OpenAICompatiblePlanner, PlannerRequest, candidate_catalog


def _setup(base_url="https://provider.invalid/v1"):
    registry = ToolContractRegistry.with_builtins()
    planner = OpenAICompatiblePlanner(api_key="test", base_url=base_url, model="test",
        timeout_seconds=1, max_tokens=512, max_retries=0, registry=registry)
    request = PlannerRequest(run_id="run", input_revision=1, user_goal="按分区融合各组面",
        input_artifacts=({"artifact_id": "a", "type": "Vector"},), revision_patch={})
    return planner, request, candidate_catalog(registry, request)


def test_initial_catalog_exposes_contract_generated_named_task_shape():
    planner, request, catalog = _setup()
    payload = json.loads(planner._messages(request, catalog)[1]["content"])
    operation = next(o for o in payload["operation_catalog"]["operations"] if o["op"] == "vector.dissolve")
    template = operation["task_template"]
    assert template["template_only"] is True
    assert set(template["task"]["inputs"]) == {"input"}
    assert template["task"]["params"] == {}


def test_repair_reports_the_specific_wrong_port_without_inventing_aliases():
    planner, request, catalog = _setup()
    rejected = PlannerDraft(run_id="run", input_revision=1, user_goal=request.user_goal,
        tasks=[PlannerStepDraft(task_key="dissolve", op="vector.dissolve",
            inputs={"vector": ArtifactRef(artifact_id="a", type="Vector")}, params={"by": "分区"})])
    payload = json.loads(planner._messages(request, catalog,
        repair={"rejected_draft": rejected.model_dump(mode="json"), "diagnostic": {"code": "PORT_MISMATCH"}})[1]["content"])
    issue = payload["repair"]["contract_diagnostics"][0]
    assert issue["code"] == "PORT_MISMATCH"
    assert issue["details"]["allowed_ports"] == ["input"]
    assert issue["details"]["unexpected_ports"] == ["vector"]
    assert "vector" in payload["repair"]["rejected_draft"]["tasks"][0]["inputs"]


def test_provider_schema_is_catalog_specific_and_not_mislabeled_strict():
    planner, request, catalog = _setup()
    response_format = planner._response_format(planner._messages(request, catalog))
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is False
    schema = response_format["json_schema"]["schema"]
    assert schema["properties"]["tasks"]["items"] == {"$ref": "#/$defs/CatalogTask"}
    alternatives = schema["$defs"]["CatalogTask"]["anyOf"]
    assert {s["properties"]["op"]["const"] for s in alternatives} == set(catalog.operation_ids)
    dissolve = next(s for s in alternatives if s["properties"]["op"]["const"] == "vector.dissolve")
    assert dissolve["properties"]["inputs"]["additionalProperties"] is False
    assert dissolve["properties"]["inputs"]["required"] == ["input"]


def test_known_json_mode_provider_keeps_catalog_in_messages_not_unsupported_schema():
    planner, request, catalog = _setup("https://api.deepseek.com/v1")
    messages = planner._messages(request, catalog)
    assert planner._response_format(messages) == {"type": "json_object"}
    assert "task_template" in messages[1]["content"]


def test_clip_and_spatial_selection_expose_distinct_geometry_effects():
    planner, _, _ = _setup()
    request = PlannerRequest(run_id="run", input_revision=1, user_goal="保留用地在界线里面的几何部分并比较空间筛选",
        input_artifacts=({"artifact_id": "a", "type": "Vector"}, {"artifact_id": "b", "type": "Vector"}), revision_patch={})
    catalog = candidate_catalog(planner.registry, request)
    catalog = catalog.expand(planner.registry, ["vector.clip", "vector.extract_location"], limit=18) if len(set(catalog.operation_ids) | {"vector.clip", "vector.extract_location"}) <= 18 else catalog
    operations = {o["op"]: o for o in catalog.operations}
    assert operations["vector.clip"]["planner_hint"]["geometry_effect"] == "intersect_with_mask"
    assert operations["vector.extract_location"]["planner_hint"]["geometry_effect"] == "select_whole_features_unchanged"
