"""Deterministic intent/lineage regressions; not live-model success evidence."""
import pytest

from app.runtime.compiler import AwaitingInputDiagnostic, CompileError
from app.runtime.contracts import TaskOutputRef
from app.runtime.goal_requirements import build_goal_requirements
from tests.unit.test_runtime_harness_goal_coverage import compile_goal, output, ref, source, step


def buffer_chain(first=100, second=500):
    return [step('first', 'vector.buffer', ref(), {'distance_m': first}),
            step('second', 'vector.buffer',
                 TaskOutputRef(task_key='first', port='vector'),
                 {'distance_m': second}),
            step('map', 'map.build',
                 TaskOutputRef(task_key='second', port='vector'))]


REPEATED = '按顺序：1、对原始地块做100米缓冲。2、对上一步结果做500米缓冲。3、在地图显示最后结果。'


def test_repeated_ordered_operations_compile_with_distinct_stage_proofs():
    plan = compile_goal(REPEATED, buffer_chain())
    assert len(plan.tasks) == 3
    assert [s['instruction_index'] for s in plan.goal_requirements['ordered_stages']] == [1, 2]


def test_same_parameters_still_require_two_distinct_sequential_operations():
    goal = REPEATED.replace('100米', '500米')
    assert len(compile_goal(goal, buffer_chain(500, 500)).tasks) == 3
    with pytest.raises(CompileError, match='GOAL_LINEAGE_MISMATCH'):
        compile_goal(goal, [step('first', 'vector.buffer', ref(), {'distance_m': 500}),
                            step('map', 'map.build', buffer_chain()[1].inputs['vector'])])


def test_repeated_sequence_cannot_add_an_unrequested_third_buffer():
    tasks = buffer_chain()
    tasks.insert(-1, step('extra', 'vector.buffer', TaskOutputRef(task_key='second', port='vector'), {'distance_m': 500}))
    tasks[-1] = step('map', 'map.build', TaskOutputRef(task_key='extra', port='vector'))
    with pytest.raises(CompileError, match='GOAL_LINEAGE_MISMATCH'):
        compile_goal(REPEATED, tasks)


@pytest.mark.parametrize('mutation', ['reverse', 'original', 'skip_second'])
def test_repeated_sequence_rejects_wrong_order_or_bypass(mutation):
    tasks = buffer_chain()
    if mutation == 'reverse':
        tasks = buffer_chain(500, 100)
    elif mutation == 'original':
        tasks[1] = step('second', 'vector.buffer', ref(), {'distance_m': 500})
    else:
        tasks[-1] = step('map', 'map.build', tasks[1].inputs['vector'])
    with pytest.raises(CompileError, match='GOAL_LINEAGE_MISMATCH'):
        compile_goal(REPEATED, tasks)


def test_scoped_numbered_correction_changes_only_corresponding_stage():
    plan = compile_goal(REPEATED, buffer_chain(100, 300), patch={'clarification': '2、缓冲距离改为300米。'})
    assert plan.tasks[1].params['distance_m'] == 300
    with pytest.raises(CompileError, match='GOAL_REQUIREMENT_MISSING|GOAL_LINEAGE_MISMATCH'):
        compile_goal(REPEATED, buffer_chain(300, 300), patch={'clarification': '2、缓冲距离改为300米。'})


def test_unscoped_repeated_distance_change_requires_explicit_stage():
    with pytest.raises(AwaitingInputDiagnostic, match='GOAL_FACTS_REQUIRED'):
        compile_goal(REPEATED, buffer_chain(300, 300), patch={'clarification': '缓冲距离改为300米。'})


def test_planner_scalar_guard_does_not_broadcast_scoped_correction():
    from app.runtime.contracts import PlannerDraft
    from app.runtime.llm_planner import PlannerRequest
    from app.runtime.queue import RuntimeSupervisor
    request = PlannerRequest(run_id='r', input_revision=2, user_goal=REPEATED,
        input_artifacts=(source(),), revision_patch={'clarification': '2、缓冲距离改为300米。'})
    RuntimeSupervisor._guard_buffer_distance(request, PlannerDraft(tasks=buffer_chain(100, 300)))


@pytest.mark.parametrize(('word', 'comparison', 'expected'), [
    ('排除', '超过', '>'), ('剔除', '不超过', '<='), ('去掉', '不少于', '>='),
    ('删除', '低于', '<'), ('排除', '等于', '='),
])
def test_explicit_exclusion_inverts_condition_instead_of_selecting_excluded_rows(word, comparison, expected):
    goal = f'{word}总价万元{comparison}300的房源，在地图显示剩下的结果。'
    requirements = build_goal_requirements(goal, [source()])
    assert requirements['conditions'][0]['params']['operator'] == expected
    assert requirements['conditions'][0]['params']['negate'] is True
    assert len(compile_goal(goal, [step('filter', 'attribute.filter', ref(),
        {'field': '总价万元', 'operator': expected, 'value': 300, 'negate': True}), step('map', 'map.build', output('filter'))]).tasks) == 2


def test_negating_a_combined_predicate_is_not_silently_changed_to_and_of_complements():
    goal = '排除总价万元超过300且面积平方米低于90的房源，显示地图。'
    with pytest.raises(AwaitingInputDiagnostic, match='GOAL_FACTS_REQUIRED'):
        compile_goal(goal, [step('price', 'attribute.filter', ref(), {'field': '总价万元', 'operator': '<=', 'value': 300}),
                           step('area', 'attribute.filter', output('price'), {'field': '面积平方米', 'operator': '>=', 'value': 90}),
                           step('map', 'map.build', output('area'))])


def test_positive_conjunction_cannot_be_satisfied_with_a_negated_filter():
    goal = '总价万元不超过300且面积平方米至少90，在地图显示结果。'
    with pytest.raises(CompileError, match='GOAL_REQUIREMENT_MISSING'):
        compile_goal(goal, [step('price', 'attribute.filter', ref(),
            {'field': '总价万元', 'operator': '<=', 'value': 300, 'negate': True}),
            step('area', 'attribute.filter', output('price'), {'field': '面积平方米', 'operator': '>=', 'value': 90}),
            step('map', 'map.build', output('area'))])


@pytest.mark.parametrize('modifier', ['所有', '全部', '那些', '其中', '满足', '所有满足'])
def test_exclusion_modifiers_do_not_turn_exclusion_into_positive_selection(modifier):
    requirements = build_goal_requirements(f'排除{modifier}总价万元超过300的房源。', [source()])
    assert requirements['conditions'][0]['params'].get('negate') is True


@pytest.mark.parametrize('goal', [REPEATED.replace('2、', '1、'), REPEATED.replace('1、', '4、')])
def test_duplicate_or_out_of_order_step_numbers_require_clarification(goal):
    with pytest.raises(AwaitingInputDiagnostic, match='GOAL_FACTS_REQUIRED'):
        compile_goal(goal, buffer_chain(500, 100))


def test_exclusion_handler_retains_null_and_duplicate_nonmatching_records():
    import geopandas as gpd
    from shapely.geometry import Point
    from app.tools.spatial_analysis import SpatialAnalyzer
    frame = gpd.GeoDataFrame({'price': [400., 250., None, 250.], 'id': ['A', 'B', 'C', 'B']},
        geometry=[Point(0, 0), Point(1, 1), Point(2, 2), Point(1, 1)], crs=3857)
    result = SpatialAnalyzer().extract_by_attribute(frame, 'price', '>', 300, negate=True, preserve_input_crs=True)
    assert result['status'] == 'success'
    assert result['data']['id'].tolist() == ['B', 'C', 'B']


@pytest.mark.parametrize('dtype', ['float64', 'Float64'])
@pytest.mark.parametrize(('operator', 'expected_ids'), [('!=', ['A', 'C']), ('is_null', ['A', 'B'])])
def test_exclusion_null_semantics_do_not_depend_on_pandas_dtype(dtype, operator, expected_ids):
    import pandas as pd
    import geopandas as gpd
    from shapely.geometry import Point
    from app.tools.spatial_analysis import SpatialAnalyzer
    frame = gpd.GeoDataFrame({'price': pd.Series([300, 250, None], dtype=dtype), 'id': ['A', 'B', 'C']},
        geometry=[Point(i, 0) for i in range(3)], crs=3857)
    result = SpatialAnalyzer().extract_by_attribute(frame, 'price', operator, 300, negate=True, preserve_input_crs=True)
    assert result['status'] == 'success'
    assert result['data']['id'].tolist() == expected_ids


def test_scoped_repeated_distance_prompt_never_advertises_one_global_distance():
    import json
    from app.runtime.llm_planner import OpenAICompatiblePlanner, PlannerRequest, candidate_catalog
    from app.runtime.contracts import ToolContractRegistry
    registry = ToolContractRegistry.with_builtins()
    request = PlannerRequest(run_id='r', input_revision=2, user_goal=REPEATED,
        input_artifacts=(source(),), revision_patch={'clarification': '2、缓冲距离改为300米。'})
    planner = OpenAICompatiblePlanner(api_key='unused', base_url='https://unused.invalid', model='unused', registry=registry,
        timeout_seconds=1, max_tokens=1000, max_retries=0)
    message = json.loads(planner._messages(request, candidate_catalog(registry, request))[-1]['content'])
    assert 'vector.buffer' not in message.get('verified_request_facts', {})
    assert [stage['requirements'][0]['params']['distance_m']
        for stage in message['goal_requirements']['ordered_stages']] == [100, 300]


def test_live_scenarios_cover_actual_exclusion_and_repeated_operations():
    from tests.browser.runtime_live_browser_support import CASES, fixture_uploads, expected_housing
    assert 'sequential_buffers' in CASES and 'housing_exclude' in CASES
    assert fixture_uploads('sequential_buffers')[0][1]['features'][0]['properties'] == {'site_id': 'S1'}
    selected = expected_housing('housing_exclude', seed=73)
    assert len(selected) == 4 and selected['总价万元'].isna().sum() == 1


def test_exclusion_housing_goal_keeps_its_explicit_spatial_obligation():
    from tests.browser.runtime_live_browser_support import CASES
    requirements = build_goal_requirements(CASES['housing_exclude']['prompt'], [source(), source('zone')])
    assert requirements['domain'] == 'housing_selection'
    assert any(item['op'] == 'vector.extract_location' and item.get('params', {}).get('predicate') == 'within'
               for item in requirements['operations'])


@pytest.mark.parametrize('verb', ['排除', '剔除', '去掉', '删除'])
def test_postfix_housing_exclusion_preserves_predicate_and_negates_it(verb):
    from app.runtime.contracts import PlannerDraft
    from app.runtime.llm_planner import PlannerRequest
    from app.runtime.queue import RuntimeSupervisor

    goal = f'把总价万元不高于300的房源{verb}，在地图显示剩下结果。'
    requirements = build_goal_requirements(goal, [source()])
    assert requirements['conditions'][0]['params'] == {
        'field': '总价万元', 'operator': '<=', 'value': 300, 'negate': True}
    tasks = [step('filter', 'attribute.filter', ref(),
        {'field': '总价万元', 'operator': '<=', 'value': 300, 'negate': True}),
        step('map', 'map.build', output('filter'))]
    assert len(compile_goal(goal, tasks).tasks) == 2
    RuntimeSupervisor._guard_semantic_facts(PlannerRequest(run_id='r', input_revision=1,
        user_goal=goal, input_artifacts=(source(),), revision_patch={}), PlannerDraft(tasks=tasks))
    tasks[0] = step('filter', 'attribute.filter', ref(),
        {'field': '总价万元', 'operator': '<=', 'value': 300, 'negate': False})
    with pytest.raises(CompileError, match='GOAL_REQUIREMENT_MISSING'):
        compile_goal(goal, tasks)


@pytest.mark.parametrize('goal', [
    '把总价万元不高于300的房源和学校排除，在地图显示剩下结果。',
    '把总价万元不高于300且面积平方米低于90的房源排除，在地图显示剩下结果。',
])
def test_ambiguous_postfix_exclusion_requests_scope_instead_of_positive_filter(goal):
    with pytest.raises(AwaitingInputDiagnostic, match='GOAL_FACTS_REQUIRED'):
        compile_goal(goal, [step('filter', 'attribute.filter', ref(),
            {'field': '总价万元', 'operator': '<=', 'value': 300}), step('map', 'map.build', output('filter'))])


@pytest.mark.parametrize('independent', [False, True])
def test_scoped_filter_correction_keeps_other_stage_and_suppresses_global_fact(independent):
    import json
    from app.runtime.contracts import PlannerDraft, ToolContractRegistry
    from app.runtime.llm_planner import OpenAICompatiblePlanner, PlannerRequest, candidate_catalog
    from app.runtime.queue import RuntimeSupervisor

    goal = ('分别独立：1、对原始房源筛选总价万元不超过300，在地图显示。'
            '2、对原始房源筛选总价万元不超过250，在地图显示。') if independent else (
            '按顺序：1、筛选总价万元不超过300的房源。'
            '2、对上一步结果筛选总价万元不超过250。3、在地图显示最后结果。')
    patch = {'clarification': '2、总价万元不超过200。'}
    params = lambda value: {'field': '总价万元', 'operator': '<=', 'value': value}
    tasks = [step('first', 'attribute.filter', ref(), params(300)),
             step('second', 'attribute.filter', ref() if independent else output('first'), params(200)),
             step('map', 'map.build', output('second'))]
    if independent:
        tasks.append(step('first_map', 'map.build', output('first')))
    request = PlannerRequest(run_id='r', input_revision=2, user_goal=goal,
        input_artifacts=(source(),), revision_patch=patch)
    RuntimeSupervisor._guard_semantic_facts(request, PlannerDraft(tasks=tasks))
    plan = compile_goal(goal, tasks, patch=patch)
    scoped = plan.goal_requirements.get('instruction_groups', []) if independent else plan.goal_requirements['ordered_stages']
    scoped_conditions = [item for group in scoped for item in group.get('conditions', group.get('requirements', []))
                         if item['op'] == 'attribute.filter' and 'field' in item.get('params', {})]
    assert [item['params']['value'] for item in scoped_conditions] == [300, 200]
    assert [item['params']['value'] for item in plan.goal_requirements['conditions']] == [300, 200]
    registry = ToolContractRegistry.with_builtins()
    planner = OpenAICompatiblePlanner(api_key='unused', base_url='https://unused.invalid', model='unused',
        registry=registry, timeout_seconds=1, max_tokens=1000, max_retries=0)
    message = json.loads(planner._messages(request, candidate_catalog(registry, request))[-1]['content'])
    assert 'attribute.filter' not in message.get('verified_request_facts', {})
    tasks[0] = step('first', 'attribute.filter', ref(), params(200))
    with pytest.raises(CompileError, match='GOAL_REQUIREMENT_MISSING|GOAL_LINEAGE_MISMATCH'):
        compile_goal(goal, tasks, patch=patch)


@pytest.mark.parametrize('negate', [False, True])
def test_single_filter_guard_and_prompt_still_require_exact_polarity(negate):
    import json
    from app.runtime.contracts import PlannerDraft, ToolContractRegistry
    from app.runtime.llm_planner import OpenAICompatiblePlanner, PlannerRequest, candidate_catalog
    from app.runtime.queue import RuntimeSupervisor

    goal = ('把总价万元超过300的房源排除' if negate else '筛选总价万元超过300的房源') + '，在地图显示。'
    request = PlannerRequest(run_id='r', input_revision=1, user_goal=goal,
        input_artifacts=(source(),), revision_patch={})
    params = {'field': '总价万元', 'operator': '>', 'value': 300, 'negate': negate}
    RuntimeSupervisor._guard_semantic_facts(request, PlannerDraft(tasks=[step('f', 'attribute.filter', ref(), params)]))
    with pytest.raises(CompileError, match='REQUEST_FILTER_MISMATCH'):
        RuntimeSupervisor._guard_semantic_facts(request, PlannerDraft(tasks=[step('f', 'attribute.filter', ref(),
            {**params, 'negate': not negate})]))
    registry = ToolContractRegistry.with_builtins()
    planner = OpenAICompatiblePlanner(api_key='unused', base_url='https://unused.invalid', model='unused',
        registry=registry, timeout_seconds=1, max_tokens=1000, max_retries=0)
    message = json.loads(planner._messages(request, candidate_catalog(registry, request))[-1]['content'])
    assert message['verified_request_facts']['attribute.filter'] == params


def test_postfix_exclusion_live_goal_keeps_spatial_and_numeric_obligations():
    from tests.browser.runtime_live_browser_support import CASES

    requirements = build_goal_requirements(CASES['housing_exclude_postfix']['prompt'], [source(), source('zone')])
    assert requirements['missing_facts'] == []
    assert [item['params']['negate'] for item in requirements['conditions']] == [True, False]
    assert any(item['op'] == 'vector.extract_location' and item.get('params', {}).get('predicate') == 'within'
               for item in requirements['operations'])
