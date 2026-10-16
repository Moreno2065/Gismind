"""Deterministic harness boundaries, not model success-rate evidence."""
import json

import pytest

from app.runtime.compiler import AwaitingInputDiagnostic, CompileError, Compiler
from app.runtime.contracts import ArtifactRef, PlannerDraft, PlannerStepDraft, TaskOutputRef, ToolContractRegistry
from app.runtime.llm_planner import OpenAICompatiblePlanner, PlannerRequest, candidate_catalog


def source(artifact_id="houses"):
    digest = "a" * 64
    return {"artifact_id": artifact_id, "type": "Vector", "crs": {"kind": "EPSG", "code": 3857},
            "schema": {"fields": {"总价万元": "float", "面积平方米": "float", "名称": "string"},
                       "geometry_types": ["Polygon"]},
            "content_hash": digest, "storage_uri": "artifacts/sha256/aa/" + digest,
            "size_bytes": 100, "validation_status": "valid"}


def request(goal, artifacts=None):
    return PlannerRequest(run_id="harness-goal", input_revision=1, user_goal=goal,
                          input_artifacts=tuple(artifacts or [source()]), revision_patch={})


def ref(artifact_id="houses"):
    return ArtifactRef(artifact_id=artifact_id, type="Vector", crs={"kind": "EPSG", "code": 3857})


def output(key):
    return TaskOutputRef(task_key=key, port="output")


def step(key, op, input_ref=None, params=None, **inputs):
    if input_ref is not None:
        inputs["vector" if op in {"map.build", "vector.buffer"} else "input"] = input_ref
    return PlannerStepDraft(task_key=key, op=op, inputs=inputs, params=params or {})


def compile_goal(goal, tasks, *, artifacts=None, patch=None):
    artifacts = artifacts or [source()]
    by_id = {a["artifact_id"]: a for a in artifacts}
    return Compiler(ToolContractRegistry.with_builtins(), artifact_lookup=by_id.get,
                    policy={"trusted_user_goal": goal, "visible_artifacts": artifacts,
                            "revision_patch": patch or {}}).compile(
        PlannerDraft(run_id="harness-goal", user_goal=goal, tasks=tasks))


@pytest.mark.parametrize("goal", [
    "每块地选个肯定在它里面的位置放地名标注，不要落到外头。",
    "在每个多边形内部找一个位置当标签点，必须落在自己的多边形里面。",
])
def test_contained_label_intent_retrieves_surface_point_without_technical_word(goal):
    catalog = candidate_catalog(ToolContractRegistry.with_builtins(), request(goal))
    assert "vector.point_on_surface" in catalog.operation_ids


def test_simple_intent_does_not_fill_catalog_with_unrelated_type_matches():
    catalog = candidate_catalog(ToolContractRegistry.with_builtins(), request("做500米缓冲。"))
    assert "vector.buffer" in catalog.operation_ids
    assert len(catalog) <= 8


def test_omitted_buffer_cannot_pass_as_map_success():
    with pytest.raises(CompileError, match="GOAL_REQUIREMENT_MISSING"):
        compile_goal("对这些地块做500米缓冲，并在地图上显示缓冲结果。",
                     [step("map", "map.build", ref())])


def test_unconsumed_buffer_cannot_pass_as_correct_final_map():
    with pytest.raises(CompileError, match="GOAL_LINEAGE_MISMATCH"):
        compile_goal("做500米缓冲，并在地图上显示缓冲结果。", [
            step("buffer", "vector.buffer", ref(), {"distance_m": 500}),
            step("map", "map.build", ref()),
        ])


def test_and_conditions_require_both_filters_not_only_the_first():
    with pytest.raises(CompileError, match="GOAL_REQUIREMENT_MISSING"):
        compile_goal("同时满足总价万元不超过300、面积平方米至少90，在地图上显示筛选结果。", [
            step("price", "attribute.filter", ref(), {"field": "总价万元", "operator": "<=", "value": 300}),
            step("map", "map.build", output("price")),
        ])


def test_parallel_filters_do_not_implement_conjunction():
    with pytest.raises(CompileError, match="GOAL_LINEAGE_MISMATCH"):
        compile_goal("总价万元不超过300且面积平方米至少90，在地图上显示结果。", [
            step("price", "attribute.filter", ref(), {"field": "总价万元", "operator": "<=", "value": 300}),
            step("area", "attribute.filter", ref(), {"field": "面积平方米", "operator": ">=", "value": 90}),
            step("map", "map.build", output("price")),
        ])


def test_connected_and_filters_compile_without_extra_model_decisions():
    plan = compile_goal("总价万元不超过300且面积平方米至少90，在地图上显示结果。", [
        step("price", "attribute.filter", ref(), {"field": "总价万元", "operator": "<=", "value": 300}),
        step("area", "attribute.filter", output("price"), {"field": "面积平方米", "operator": ">=", "value": 90}),
        step("map", "map.build", output("area")),
    ])
    assert [t.op for t in plan.tasks] == ["attribute.filter", "attribute.filter", "map.build"]
    assert "goal_requirements" in json.loads(plan.canonical_json)
    assert plan.goal_requirements == json.loads(plan.canonical_json)["goal_requirements"]


def test_or_conditions_must_not_be_silently_executed_as_and():
    with pytest.raises(CompileError, match="GOAL_BOOLEAN_UNSUPPORTED"):
        compile_goal("总价万元不超过300或者面积平方米至少90，满足任一条件即可。", [
            step("price", "attribute.filter", ref(), {"field": "总价万元", "operator": "<=", "value": 300}),
            step("area", "attribute.filter", output("price"), {"field": "面积平方米", "operator": ">=", "value": 90}),
        ])


def test_vague_school_housing_cannot_be_resolved_by_a_map_or_generic_answer():
    with pytest.raises(AwaitingInputDiagnostic) as error:
        compile_goal("帮我挑学区房，要便宜、上学方便。", [step("map", "map.build", ref())])
    assert error.value.code == "GOAL_FACTS_REQUIRED"
    assert "学区" in error.value.message
    assert "patch_schema" in error.value.details


def test_school_region_and_attribute_conditions_must_all_reach_result():
    zone = source("school_boundary")
    zone["schema"] = {"fields": {"学校": "string"}, "geometry_types": ["Polygon"]}
    goal = "按上传的招生范围挑选学区房，房源在第二个附件范围内，且总价万元不超过300、面积平方米至少90。只做空间筛选，不保证入学。"
    with pytest.raises(CompileError, match="GOAL_REQUIREMENT_MISSING"):
        compile_goal(goal, [
            step("price", "attribute.filter", ref(), {"field": "总价万元", "operator": "<=", "value": 300}),
            step("area", "attribute.filter", output("price"), {"field": "面积平方米", "operator": ">=", "value": 90}),
        ], artifacts=[source(), zone])


def test_negated_operation_and_field_named_operation_are_not_requirements():
    plan = compile_goal("不要做缓冲，只在地图上显示地块。", [step("map", "map.build", ref())])
    assert len(plan.tasks) == 1


def test_model_context_requires_gis_translation_and_conjunctive_dataflow():
    planner = OpenAICompatiblePlanner(api_key="", base_url="https://api.deepseek.com", model="deepseek-flash",
                                     timeout_seconds=1, max_tokens=1536, max_retries=0)
    req = request("帮我挑学区房，要便宜、上学方便。")
    messages = planner._messages(req, candidate_catalog(planner.registry, req))
    payload = json.loads(messages[1]["content"])
    assert "goal_requirements" in payload
    assert payload["goal_requirements"]["missing_facts"]
    assert "不能用常识推理" in messages[0]["content"]


def test_clarification_preserves_constraints_and_does_not_invent_commute_threshold():
    from app.runtime.goal_requirements import build_goal_requirements
    goal = "帮我挑学区房，价格便宜、上学方便，面积平方米至少90。"
    req = build_goal_requirements(goal, [source(), source("zone")],
        {"clarification": "按第二份学区范围筛选，总价万元不超过300。"})
    assert len(req["conditions"]) == 2
    assert any("起终点" in fact for fact in req["missing_facts"])
    revised = build_goal_requirements(goal, [source(), source("zone")],
        {"clarification": "不考虑通学距离或时间，只按学区范围和属性筛选，总价万元不超过300。"})
    assert revised["missing_facts"] == []


def test_multiple_filters_do_not_trigger_single_predicate_guard():
    from app.runtime.llm_planner import _semantic_fact_mismatch
    req = request("总价万元大于200且面积平方米不少于90。")
    tasks = [step("price", "attribute.filter", ref(), {"field": "总价万元", "operator": ">", "value": 200}),
             step("area", "attribute.filter", output("price"), {"field": "面积平方米", "operator": ">=", "value": 90})]
    assert _semantic_fact_mismatch(req, tasks) is None


def test_field_unit_conversion_is_not_silently_lost():
    from app.runtime.goal_requirements import build_goal_requirements
    req = build_goal_requirements("总价万元不超过3000000元且面积平方米至少90。", [source()])
    assert req["conditions"][0]["params"]["value"] == 300


def test_clarification_replaces_only_the_corrected_field():
    from app.runtime.goal_requirements import build_goal_requirements
    req = build_goal_requirements("总价万元不超过300且面积平方米至少90。", [source()],
        {"clarification": "预算改一下，总价万元不超过250。"})
    assert {(item["params"]["field"], item["params"]["value"]) for item in req["conditions"]} == {("总价万元", 250), ("面积平方米", 90)}


def test_plain_chinese_or_is_not_conjunction():
    from app.runtime.goal_requirements import build_goal_requirements
    req = build_goal_requirements("总价万元不超过300或面积平方米至少90。", [source()])
    assert req["boolean"] == "any"


def test_parallel_filter_union_does_not_satisfy_and_lineage():
    with pytest.raises(CompileError, match="GOAL_LINEAGE_MISMATCH"):
        compile_goal("总价万元不超过300且面积平方米至少90，在地图上显示筛选结果。", [
            step("price", "attribute.filter", ref(), {"field": "总价万元", "operator": "<=", "value": 300}),
            step("area", "attribute.filter", ref(), {"field": "面积平方米", "operator": ">=", "value": 90}),
            step("union", "vector.merge", input=[output("price"), output("area")]),
            step("map", "map.build", output("union")),
        ])


@pytest.mark.parametrize("goal", ["请在地图上显示原始地块，但不要做缓冲。", "只在地图上显示原始地块而不用做缓冲。"])
def test_embedded_negated_buffer_is_not_required(goal):
    assert len(compile_goal(goal, [step("map", "map.build", ref())]).tasks) == 1


def test_unknown_column_unit_requires_clarification():
    artifact = source()
    artifact["schema"]["fields"] = {"总价": "float"}
    with pytest.raises(AwaitingInputDiagnostic, match="GOAL_FACTS_REQUIRED"):
        compile_goal("筛选便宜的房源，总价不超过300万元，在地图上展示结果。", [
            step("price", "attribute.filter", ref(), {"field": "总价", "operator": "<=", "value": 300}),
            step("map", "map.build", output("price")),
        ], artifacts=[artifact])


def test_upper_bound_correction_preserves_lower_bound():
    from app.runtime.goal_requirements import build_goal_requirements
    req = build_goal_requirements("总价万元至少200且总价万元不超过300。", [source()],
                                  {"clarification": "把上限改为总价万元不超过250。"})
    assert {(c["params"]["operator"], c["params"]["value"]) for c in req["conditions"]} == {(">=", 200), ("<=", 250)}


def test_thousands_separator_not_truncated_to_three():
    from app.runtime.goal_requirements import build_goal_requirements
    req = build_goal_requirements("总价万元不超过3,000且面积平方米至少90。", [source()])
    assert req["conditions"][0]["params"]["value"] == 3000


def test_stricter_semantics_have_new_contract_identity_not_old_cache_identity():
    registry = ToolContractRegistry.with_builtins()
    for name in ("vector.buffer", "vector.clip", "vector.extract_location", "vector.point_on_surface",
                 "attribute.filter", "attribute.keep_fields", "attribute.rename_field"):
        contract = registry.get(name)
        assert contract.version == ("1.2.0" if name in {"attribute.filter", "attribute.keep_fields"} else "1.1.0")
        assert contract.manifest["supersedes"]["version"] == "1.0.0"


def test_explicit_unit_clarification_unblocks_without_losing_original_limit():
    from app.runtime.goal_requirements import build_goal_requirements
    artifact = source()
    artifact["schema"]["fields"] = {"总价": "float"}
    req = build_goal_requirements("筛选总价不超过300万元的房源。", [artifact],
                                  {"clarification": "总价字段的单位是元。"})
    assert req["missing_facts"] == []
    assert req["conditions"][0]["params"]["value"] == 3000000


def test_unknown_units_are_not_false_verified_predicates():
    from app.runtime.llm_planner import _semantic_fact_mismatch
    artifact = source()
    artifact["schema"]["fields"] = {"总价": "float"}
    req = request("总价不超过300万元。", [artifact])
    assert _semantic_fact_mismatch(req, [step("price", "attribute.filter", ref(),
                                            {"field": "总价", "operator": "<=", "value": 3000000})]) is None


MULTI_STEP = ('请按顺序完成全部步骤：\n1、把上传站点中的site_id字段重命名为“站点编号”。\n'
              '2、为每个站点生成500米缓冲区。\n3、在地图上显示缓冲结果。\n'
              '4、导出同一缓冲结果为GeoPackage（GPKG）供下载。')


def test_all_explicit_common_actions_become_obligations():
    from app.runtime.goal_requirements import build_goal_requirements
    requirements = build_goal_requirements('按顺序：将名称字段重命名为地名；仅保留地名字段；重投影到EPSG:4326；筛选地名等于“甲”的要素；生成500米缓冲区；显示地图；导出GeoJSON。', [source()])
    assert {item['op'] for item in requirements['operations']} >= {
        'attribute.rename_field', 'attribute.keep_fields', 'vector.reproject', 'attribute.filter',
        'vector.buffer', 'map.build', 'file.export'}


def test_numbered_sequential_request_cannot_omit_first_rename():
    with pytest.raises(CompileError, match='GOAL_REQUIREMENT_MISSING'):
        compile_goal(MULTI_STEP, [step('buffer', 'vector.buffer', ref(), {'distance_m': 500}),
            step('map', 'map.build', TaskOutputRef(task_key='buffer', port='vector')),
            step('export', 'file.export', TaskOutputRef(task_key='buffer', port='vector'), {'format': 'gpkg'})])


def test_sequential_request_cannot_skip_middle_keep_fields():
    with pytest.raises(CompileError, match='GOAL_REQUIREMENT_MISSING'):
        compile_goal('先把名称字段改名为地名，然后仅保留地名字段，再显示地图。', [
            step('rename', 'attribute.rename_field', ref(), {'old_name': '名称', 'new_name': '地名'}),
            step('map', 'map.build', output('rename'))])


INDEPENDENT = '1. 对地块做500米缓冲并显示地图。2. 为每个地块生成面内标注点并导出GeoJSON。'


def independent_tasks():
    return [step('buffer', 'vector.buffer', ref(), {'distance_m': 500}),
            step('map', 'map.build', TaskOutputRef(task_key='buffer', port='vector')),
            step('labels', 'vector.point_on_surface', ref()),
            step('export', 'file.export', output('labels'), {'format': 'geojson'})]


def test_explicit_independent_numbered_instructions_accept_separate_final_branches():
    assert len(compile_goal(INDEPENDENT, independent_tasks()).tasks) == 4


def test_only_first_independent_instruction_cannot_pass():
    with pytest.raises(CompileError, match='GOAL_REQUIREMENT_MISSING|INVALID_PARAMETER'):
        compile_goal(INDEPENDENT, independent_tasks()[:2])


def test_independent_group_map_cannot_bypass_its_own_processing():
    tasks = independent_tasks()
    tasks[1] = step('map', 'map.build', ref())
    with pytest.raises(CompileError, match='GOAL_LINEAGE_MISMATCH'):
        compile_goal(INDEPENDENT, tasks)


def test_numbering_with_cross_step_reference_is_one_chain():
    with pytest.raises(CompileError, match='GOAL_LINEAGE_MISMATCH'):
        compile_goal('1、对地块生成500米缓冲区。2、在地图显示上一步缓冲结果。', [
            step('buffer', 'vector.buffer', ref(), {'distance_m': 500}), step('map', 'map.build', ref())])


def test_unscoped_numbered_followup_asks_which_input_instead_of_guessing():
    with pytest.raises(AwaitingInputDiagnostic, match='GOAL_FACTS_REQUIRED'):
        compile_goal('1、对地块生成500米缓冲区。2、生成面内点。', [
            step('buffer', 'vector.buffer', ref(), {'distance_m': 500}), step('labels', 'vector.point_on_surface', ref())])


def test_full_numbered_sequential_chain_preserves_all_four_actions():
    uploaded = source()
    uploaded['schema']['fields'] = {'site_id': 'string'}
    tasks = [step('rename', 'attribute.rename_field', ref(), {'old_name': 'site_id', 'new_name': '站点编号'}),
             step('buffer', 'vector.buffer', output('rename'), {'distance_m': 500}),
             step('map', 'map.build', TaskOutputRef(task_key='buffer', port='vector')),
             step('export', 'file.export', TaskOutputRef(task_key='buffer', port='vector'), {'format': 'gpkg'})]
    assert len(compile_goal(MULTI_STEP, tasks, artifacts=[uploaded]).tasks) == 4


@pytest.mark.parametrize('predicate', ['intersects', 'contains'])
def test_explicit_fully_within_housing_region_rejects_other_predicates(predicate):
    goal = '筛选房源，房源必须完全位于第二个附件的学区范围多边形内，在地图上显示结果。'
    with pytest.raises(CompileError, match='GOAL_REQUIREMENT_MISSING'):
        compile_goal(goal, [step('spatial', 'vector.extract_location', ref(), mask=ref('zone'), params={'predicate': predicate}),
                           step('map', 'map.build', output('spatial'))], artifacts=[source(), source('zone')])


def test_explicit_within_housing_and_numeric_conditions_remain_one_chain():
    goal = '筛选房源，总价万元不超过300且面积平方米至少90，必须位于第二个附件的学区范围多边形内，在地图上展示结果。'
    tasks = [step('price', 'attribute.filter', ref(), {'field': '总价万元', 'operator': '<=', 'value': 300}),
             step('area', 'attribute.filter', output('price'), {'field': '面积平方米', 'operator': '>=', 'value': 90}),
             step('spatial', 'vector.extract_location', output('area'), mask=ref('zone'), params={'predicate': 'within'}),
             step('map', 'map.build', output('spatial'))]
    assert len(compile_goal(goal, tasks, artifacts=[source(), source('zone')]).tasks) == 4


def test_relationship_answer_can_resume_ambiguous_numbered_list():
    goal = '1、对地块生成500米缓冲区。2、生成面内点。'
    tasks = [step('buffer', 'vector.buffer', ref(), {'distance_m': 500}), step('labels', 'vector.point_on_surface', ref())]
    assert len(compile_goal(goal, tasks, patch={'clarification': '各项是相互独立任务，都处理原始输入。'}).tasks) == 2


def test_independent_buffer_distances_are_bound_per_instruction_not_globally():
    goal = '1. 对原始地块做100米缓冲并显示地图。2. 对原始地块做500米缓冲并导出GeoJSON。'
    tasks = [step('small', 'vector.buffer', ref(), {'distance_m': 100}),
             step('map', 'map.build', TaskOutputRef(task_key='small', port='vector')),
             step('large', 'vector.buffer', ref(), {'distance_m': 500}),
             step('export', 'file.export', TaskOutputRef(task_key='large', port='vector'), {'format': 'geojson'})]
    assert len(compile_goal(goal, tasks).tasks) == 4
    with pytest.raises(CompileError, match='GOAL_LINEAGE_MISMATCH'):
        compile_goal(goal, [*tasks[:3], step('export', 'file.export', TaskOutputRef(task_key='small', port='vector'), {'format': 'geojson'})])
    with pytest.raises(CompileError, match='GOAL_REQUIREMENT_MISSING'):
        compile_goal(goal, [*tasks[:2], step('export', 'file.export', TaskOutputRef(task_key='small', port='vector'), {'format': 'geojson'})])


def test_colon_before_inline_numbered_independent_list_preserves_both_distances():
    from app.runtime.goal_requirements import build_goal_requirements
    goal = '分别独立完成：1、对原始地块做100米缓冲并在地图上显示。2、对原始地块做500米缓冲并导出GPKG。'
    groups = build_goal_requirements(goal, [source()]).get('instruction_groups', [])
    assert len(groups) == 2
    assert [next(op['params']['distance_m'] for op in group['operations'] if op['op'] == 'vector.buffer')
            for group in groups] == [100, 500]
    assert next(op['params']['format'] for op in groups[1]['operations'] if op['op'] == 'file.export') == 'gpkg'


def test_repeated_sequential_buffers_preserve_explicit_distances_and_order():
    goal = '按顺序：1、对地块做100米缓冲。2、对上一步结果再做500米缓冲。3、在地图显示最后结果。'
    plan = compile_goal(goal, [step('small', 'vector.buffer', ref(), {'distance_m': 100}),
                           step('large', 'vector.buffer', TaskOutputRef(task_key='small', port='vector'), {'distance_m': 500}),
                           step('map', 'map.build', TaskOutputRef(task_key='large', port='vector'))])
    assert [stage['requirements'][0]['params']['distance_m']
            for stage in plan.goal_requirements['ordered_stages']] == [100, 500]


def test_independent_groups_preserve_global_preamble_on_every_result():
    goal = '两项结果都仅保留名称字段。分别独立完成：' + INDEPENDENT
    with pytest.raises(CompileError, match='GOAL_REQUIREMENT_MISSING'):
        compile_goal(goal, independent_tasks())
    tasks = independent_tasks()
    tasks.insert(1, step('keep_buffer', 'attribute.keep_fields', TaskOutputRef(task_key='buffer', port='vector'), {'fields': ['名称']}))
    tasks[2] = step('map', 'map.build', output('keep_buffer'))
    with pytest.raises(CompileError, match='GOAL_LINEAGE_MISMATCH'):
        compile_goal(goal, tasks)
    tasks.insert(-1, step('keep_labels', 'attribute.keep_fields', output('labels'), {'fields': ['名称']}))
    tasks[-1] = step('export', 'file.export', output('keep_labels'), {'format': 'geojson'})
    assert len(compile_goal(goal, tasks).tasks) == 6


def test_fully_numbered_clarification_applies_only_to_corresponding_group():
    goal = '1. 对原始地块做缓冲并显示地图。2. 为每个原始地块生成面内点并导出GeoJSON。'
    reply = '1、对原始地块做500米缓冲并显示地图。2、为每个原始地块生成面内点并导出GeoJSON。'
    assert len(compile_goal(goal, independent_tasks(), patch={'clarification': reply}).tasks) == 4


@pytest.mark.parametrize('goal', [INDEPENDENT, '分别独立完成：' + INDEPENDENT,
                                  INDEPENDENT.replace('为每个地块', '为每个原始地块')])
def test_original_input_group_cannot_consume_another_groups_buffer(goal):
    tasks = independent_tasks()
    tasks[2] = step('labels', 'vector.point_on_surface', TaskOutputRef(task_key='buffer', port='vector'))
    with pytest.raises(CompileError, match='GOAL_LINEAGE_MISMATCH'):
        compile_goal(goal, tasks)
    assert len(compile_goal(goal, independent_tasks()).tasks) == 4


@pytest.mark.parametrize('use_live_fixture', [False, True])
def test_long_housing_filter_clause_keeps_required_spatial_selection(use_live_fixture):
    from app.runtime.goal_requirements import build_goal_requirements
    from tests.browser.runtime_live_browser_support import HOUSING_PROMPT
    goal = HOUSING_PROMPT if use_live_fixture else ('请从房源附件中筛选总价万元不超过300且面积平方米不少于90的房源，'
        '同时必须位于第二个附件的学区范围多边形内，在地图上展示结果，并导出结果为GPKG。')
    requirements = build_goal_requirements(goal, [source(), source('zone')])
    assert requirements['domain'] == 'housing_selection'
    assert any(op['op'] == 'vector.extract_location' and op.get('params') == {'predicate': 'within'}
               for op in requirements['operations'])
    tasks = [step('price', 'attribute.filter', ref(), {'field': '总价万元', 'operator': '<=', 'value': 300}),
             step('area', 'attribute.filter', output('price'), {'field': '面积平方米', 'operator': '>=', 'value': 90}),
             step('map', 'map.build', output('area')),
             step('export', 'file.export', output('area'), {'format': 'gpkg'})]
    with pytest.raises(CompileError, match='GOAL_REQUIREMENT_MISSING'):
        compile_goal(goal, tasks, artifacts=[source(), source('zone')])


def test_numbered_sequential_order_cannot_reverse_noncommuting_processing():
    goal = '按顺序：1、对原始地块生成面内点。2、对上一步结果做500米缓冲。3、在地图上显示最后结果。'
    wrong = [step('buffer', 'vector.buffer', ref(), {'distance_m': 500}),
             step('labels', 'vector.point_on_surface', TaskOutputRef(task_key='buffer', port='vector')),
             step('map', 'map.build', output('labels'))]
    with pytest.raises(CompileError, match='GOAL_LINEAGE_MISMATCH'):
        compile_goal(goal, wrong)
    right = [step('labels', 'vector.point_on_surface', ref()),
             step('buffer', 'vector.buffer', output('labels'), {'distance_m': 500}),
             step('map', 'map.build', TaskOutputRef(task_key='buffer', port='vector'))]
    assert len(compile_goal(goal, right).tasks) == 3


def test_relationship_answer_original_input_also_blocks_cross_group_processing():
    goal = '1、对地块生成500米缓冲区。2、生成面内点。'
    tasks = [step('buffer', 'vector.buffer', ref(), {'distance_m': 500}),
             step('labels', 'vector.point_on_surface', TaskOutputRef(task_key='buffer', port='vector'))]
    with pytest.raises(CompileError, match='GOAL_LINEAGE_MISMATCH'):
        compile_goal(goal, tasks, patch={'clarification': '各项是相互独立任务，都处理原始输入。'})


def test_original_input_does_not_allow_repeated_same_operation_from_other_group():
    goal = '1、对原始地块做500米缓冲并显示地图。2、对原始地块做500米缓冲并导出GeoJSON。'
    tasks = [step('first', 'vector.buffer', ref(), {'distance_m': 500}),
             step('map', 'map.build', TaskOutputRef(task_key='first', port='vector')),
             step('second', 'vector.buffer', TaskOutputRef(task_key='first', port='vector'), {'distance_m': 500}),
             step('export', 'file.export', TaskOutputRef(task_key='second', port='vector'), {'format': 'geojson'})]
    with pytest.raises(CompileError, match='GOAL_LINEAGE_MISMATCH'):
        compile_goal(goal, tasks)
    tasks[2] = step('second', 'vector.buffer', ref(), {'distance_m': 500})
    assert len(compile_goal(goal, tasks).tasks) == 4


def test_verified_distance_correction_updates_only_the_unique_numbered_buffer_stage():
    goal = '按顺序：1、对原始地块生成面内点。2、对上一步结果做500米缓冲。3、在地图上显示最后结果。'
    patch = {'clarification': '缓冲距离改为300米。'}
    def tasks(distance):
        return [step('labels', 'vector.point_on_surface', ref()),
                step('buffer', 'vector.buffer', output('labels'), {'distance_m': distance}),
                step('map', 'map.build', TaskOutputRef(task_key='buffer', port='vector'))]
    assert len(compile_goal(goal, tasks(300), patch=patch).tasks) == 3
    with pytest.raises(CompileError, match='GOAL_REQUIREMENT_MISSING'):
        compile_goal(goal, tasks(500), patch=patch)


def test_distance_correction_is_not_broadcast_to_repeated_numbered_buffers():
    goal = '按顺序：1、对原始地块做100米缓冲。2、对上一步结果做500米缓冲。3、在地图显示最后结果。'
    tasks = [step('first', 'vector.buffer', ref(), {'distance_m': 300}),
             step('second', 'vector.buffer', TaskOutputRef(task_key='first', port='vector'), {'distance_m': 300}),
             step('map', 'map.build', TaskOutputRef(task_key='second', port='vector'))]
    with pytest.raises(AwaitingInputDiagnostic, match='GOAL_FACTS_REQUIRED'):
        compile_goal(goal, tasks, patch={'clarification': '缓冲距离改为300米。'})
