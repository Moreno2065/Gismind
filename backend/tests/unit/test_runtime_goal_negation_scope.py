"""Bounded request negation proofs; these are offline compiler regressions."""
from __future__ import annotations

import pytest

from app.runtime.compiler import AwaitingInputDiagnostic, CompileError
from app.runtime.contracts import ArtifactRef, TaskOutputRef
from app.runtime.goal_requirements import build_goal_requirements
from tests.unit.test_runtime_harness_goal_coverage import compile_goal, output, ref, source, step


def housing_artifacts():
    houses, zone = source(), source('zone')
    houses['schema']['fields'] = {'价格': 'float', '面积': 'float', '名称': 'str'}
    return [houses, zone]


def housing_goal(price, area, *, postfix=False):
    criteria = (f'把价格高于{price}万元的记录删除，价格缺失不要因此丢掉；随后面积必须至少{area}平方米。'
                if postfix else f'价格高于{price}万元的不要，面积低于{area}平方米的也不要；'
                '价格或面积缺失的房源也不保留。')
    return ('房源的“价格”字段单位为万元，“面积”字段单位为平方米；第二个附件是学区范围。'
            + criteria + '房源还必须位于第二个附件的学区多边形内部，落在边界上的房源不选。'
            '学区仅作空间筛选，不承诺入学资格。'
            '在地图上展示最终结果，并将同一结果导出为地理包空间数据库文件供下载。保留未要求修改的原始属性。')


def housing_tasks(price, area, *, complements=False, reject_nulls=True, postfix=False, crs_code=3857):
    def input_ref(identity='houses'):
        return ArtifactRef(artifact_id=identity, type='Vector', crs={'kind': 'EPSG', 'code': crs_code})
    price_params = {'field': '价格', 'operator': '<=' if complements else '>',
                    'value': price, 'negate': not complements}
    area_params = {'field': '面积', 'operator': '>=' if complements or postfix else '<',
                   'value': area, 'negate': not (complements or postfix)}
    tasks = [step('price', 'attribute.filter', input_ref(), price_params),
             step('area', 'attribute.filter', output('price'), area_params)]
    previous = 'area'
    if reject_nulls and not complements and not postfix:
        for index, field in enumerate(('价格', '面积')):
            key = f'not_null_{index}'
            tasks.append(step(key, 'attribute.filter', output(previous),
                              {'field': field, 'operator': 'is_null', 'value': None, 'negate': True}))
            previous = key
    tasks.extend([
        step('within', 'vector.extract_location', output(previous), {'predicate': 'within'}, mask=input_ref('zone')),
        step('map', 'map.build', output('within')),
        step('export', 'file.export', output('within'), {'format': 'gpkg'}),
    ])
    return tasks


@pytest.mark.parametrize(('price', 'area'), [(230, 66), (240, 68), (250, 70)])
@pytest.mark.parametrize('complements', [False, True])
def test_separate_postfix_negations_and_null_or_accept_equivalent_valid_chains(price, area, complements):
    goal = housing_goal(price, area)
    plan = compile_goal(goal, housing_tasks(price, area, complements=complements), artifacts=housing_artifacts())
    assert plan.goal_requirements['boolean'] == 'all'
    assert plan.goal_requirements['missing_facts'] == []


@pytest.mark.parametrize(('price', 'area'), [(230, 66), (240, 68), (250, 70)])
def test_negated_numeric_filters_without_required_null_rejection_fail(price, area):
    with pytest.raises(CompileError, match='GOAL_REQUIREMENT_MISSING'):
        compile_goal(housing_goal(price, area), housing_tasks(price, area, reject_nulls=False),
                     artifacts=housing_artifacts())


@pytest.mark.parametrize(('price', 'area'), [(250, 70), (260, 72)])
def test_record_deletion_postfix_preserves_unknown_price_and_independent_area(price, area):
    goal = housing_goal(price, area, postfix=True)
    plan = compile_goal(goal, housing_tasks(price, area, postfix=True), artifacts=housing_artifacts())
    assert plan.goal_requirements['conditions'][0]['params'] == {
        'field': '价格', 'operator': '>', 'value': price, 'negate': True}
    with pytest.raises(CompileError, match='GOAL_REQUIREMENT_MISSING'):
        compile_goal(goal, housing_tasks(price, area, complements=True, postfix=True), artifacts=housing_artifacts())


@pytest.mark.parametrize('goal', [
    '排除价格高于250且面积低于70的房源。',
    '把价格高于250且面积低于70的记录删除。',
    '把价格高于250的房源和学校排除。',
    '价格高于250或面积低于70的房源不要。',
])
def test_unresolved_compound_negation_still_requests_scope(goal):
    with pytest.raises(AwaitingInputDiagnostic, match='GOAL_FACTS_REQUIRED'):
        compile_goal(goal, housing_tasks(250, 70), artifacts=housing_artifacts())


@pytest.mark.parametrize('goal', [
    '价格不超过250或面积至少70。',
    '价格不超过250，或者面积至少70，满足任一条件即可。',
])
def test_positive_or_still_cannot_be_proved_by_serial_filters(goal):
    with pytest.raises(CompileError, match='GOAL_BOOLEAN_UNSUPPORTED'):
        compile_goal(goal, housing_tasks(250, 70, complements=True), artifacts=housing_artifacts())


def test_negated_action_does_not_become_filter_or_operation_obligation():
    requirements = build_goal_requirements('不要筛选价格高于250的房源，不用做缓冲，只在地图显示。', housing_artifacts())
    assert requirements['conditions'] == []
    assert [item['op'] for item in requirements['operations']] == ['map.build']


@pytest.mark.parametrize('dtype', ['float64', 'Float64'])
def test_equivalent_exclusion_chains_preserve_thresholds_duplicates_and_null_semantics(dtype):
    import geopandas as gpd
    import pandas as pd
    from shapely.geometry import Point
    from app.tools.spatial_analysis import SpatialAnalyzer

    frame = gpd.GeoDataFrame({
        '价格': pd.Series([250, 251, 249, None, 250, 250], dtype=dtype),
        '面积': pd.Series([70, 70, 69, 70, None, 70], dtype=dtype),
        'id': ['boundary', 'too_expensive', 'too_small', 'null_price', 'null_area', 'boundary'],
    }, geometry=[Point(index, 0) for index in range(6)], crs=3857)
    analyzer = SpatialAnalyzer()
    for complements in (False, True):
        tasks = housing_tasks(250, 70, complements=complements)
        compile_goal(housing_goal(250, 70), tasks, artifacts=housing_artifacts())
        result = frame
        for task in tasks:
            if task.op == 'attribute.filter':
                extracted = analyzer.extract_by_attribute(result, **task.params, preserve_input_crs=True)
                assert extracted['status'] == 'success'
                result = extracted['data']
        assert result['id'].tolist() == ['boundary', 'boundary']


def test_null_rejection_on_parallel_branch_does_not_prove_final_delivery():
    tasks = housing_tasks(250, 70)
    tasks[2] = step('not_null_0', 'attribute.filter', ref(),
                    {'field': '价格', 'operator': 'is_null', 'value': None, 'negate': True})
    with pytest.raises(CompileError, match='GOAL_LINEAGE_MISMATCH'):
        compile_goal(housing_goal(250, 70), tasks, artifacts=housing_artifacts())


RENAME_BEFORE_BUFFER = '对选中数据先更改字段名：把“名称”改成“新名”，再按200米生成各站点缓冲面。在地图显示最终结果。'


def test_explicit_field_change_creates_bound_rename_then_buffer_requirements():
    requirements = build_goal_requirements(RENAME_BEFORE_BUFFER, [source()])
    renames = [item for item in requirements['operations'] if item['op'] == 'attribute.rename_field']
    assert len(renames) == 1
    rename = renames[0]
    assert rename['params'] == {'old_name': '名称', 'new_name': '新名'}
    assert [stage['requirements'][0]['op'] for stage in requirements['ordered_stages']] == [
        'attribute.rename_field', 'vector.buffer']
    plan = compile_goal(RENAME_BEFORE_BUFFER, [
        step('rename', 'attribute.rename_field', ref(), rename['params']),
        step('buffer', 'vector.buffer', output('rename'), {'distance_m': 200}),
        step('map', 'map.build', TaskOutputRef(task_key='buffer', port='vector')),
    ])
    assert len(plan.tasks) == 3


@pytest.mark.parametrize('mutation', ['missing', 'wrong_target', 'reversed', 'bypass'])
def test_named_rename_before_buffer_cannot_be_omitted_changed_reversed_or_bypassed(mutation):
    tasks = [
        step('rename', 'attribute.rename_field', ref(), {'old_name': '名称', 'new_name': '新名'}),
        step('buffer', 'vector.buffer', output('rename'), {'distance_m': 200}),
        step('map', 'map.build', TaskOutputRef(task_key='buffer', port='vector')),
    ]
    if mutation == 'missing':
        tasks[0] = step('rename', 'attribute.keep_fields', ref(), {'fields': ['名称', '总价万元', '面积平方米']})
    elif mutation == 'wrong_target':
        tasks[0] = step('rename', 'attribute.rename_field', ref(), {'old_name': '名称', 'new_name': 'wrong_name'})
    elif mutation == 'reversed':
        tasks = [step('buffer', 'vector.buffer', ref(), {'distance_m': 200}),
                 step('rename', 'attribute.rename_field', TaskOutputRef(task_key='buffer', port='vector'),
                      {'old_name': '名称', 'new_name': '新名'}),
                 step('map', 'map.build', output('rename'))]
    else:
        tasks[1] = step('buffer', 'vector.buffer', ref(), {'distance_m': 200})
    with pytest.raises(CompileError, match='GOAL_REQUIREMENT_MISSING|GOAL_LINEAGE_MISMATCH'):
        compile_goal(RENAME_BEFORE_BUFFER, tasks)


def test_unordered_rename_and_buffer_do_not_invent_a_sequence():
    requirements = build_goal_requirements('把“名称”改成“新名”，做200米缓冲。在地图显示结果。', [source()])
    assert 'ordered_stages' not in requirements


FILTER_RENAME_BUFFER = ('先只保留总价万元严格大于18的站点，等于阈值的不要。'
                       '对选中数据先更改字段名：把“总价万元”改成“新值”，再按200米生成各站点缓冲面。'
                       '在地图显示最终结果。')


def filter_rename_buffer_tasks(*, reverse=False):
    if reverse:
        first = [step('rename', 'attribute.rename_field', ref(), {'old_name': '总价万元', 'new_name': '新值'}),
                 step('filter', 'attribute.filter', output('rename'), {'field': '新值', 'operator': '>', 'value': 18})]
    else:
        first = [step('filter', 'attribute.filter', ref(), {'field': '总价万元', 'operator': '>', 'value': 18}),
                 step('rename', 'attribute.rename_field', output('filter'), {'old_name': '总价万元', 'new_name': '新值'})]
    return [*first, step('buffer', 'vector.buffer', output(first[-1].task_key), {'distance_m': 200}),
            step('map', 'map.build', TaskOutputRef(task_key='buffer', port='vector'))]


def test_explicit_selected_data_chain_requires_filter_then_rename_then_buffer():
    plan = compile_goal(FILTER_RENAME_BUFFER, filter_rename_buffer_tasks())
    stages = plan.goal_requirements['ordered_stages']
    assert [stage['requirements'][0]['op'] for stage in stages] == [
        'attribute.filter', 'attribute.rename_field', 'vector.buffer']


@pytest.mark.parametrize('mutation', ['rename_before_filter', 'include_threshold'])
def test_selected_data_chain_rejects_wrong_column_or_inclusive_threshold(mutation):
    tasks = filter_rename_buffer_tasks(reverse=mutation == 'rename_before_filter')
    if mutation == 'include_threshold':
        tasks[0] = step('filter', 'attribute.filter', ref(), {'field': '总价万元', 'operator': '>=', 'value': 18})
    with pytest.raises(CompileError, match='GOAL_REQUIREMENT_MISSING|GOAL_LINEAGE_MISMATCH'):
        compile_goal(FILTER_RENAME_BUFFER, tasks)


@pytest.mark.parametrize(('comparison', 'operator'), [('严格大于', '>'), ('严格超过', '>'), ('严格小于', '<')])
def test_explicit_strict_comparators_preserve_boundary(comparison, operator):
    requirements = build_goal_requirements(f'先只保留总价万元{comparison}18的站点。', [source()])
    assert [item['params'] for item in requirements['conditions']] == [
        {'field': '总价万元', 'operator': operator, 'value': 18, 'negate': False}]


def test_strict_qualifier_does_not_convert_a_non_strict_phrase_to_a_strict_bound():
    requirements = build_goal_requirements('总价万元严格不超过18。', [source()])
    assert all(item['params']['operator'] != '<' for item in requirements['conditions'])


def test_later_null_retention_cannot_certify_stale_null_rejection():
    goal = '价格高于250的不要，价格缺失的房源不保留。'
    tasks = [step('price', 'attribute.filter', ref(), {'field': '价格', 'operator': '<=', 'value': 250})]
    with pytest.raises(AwaitingInputDiagnostic, match='GOAL_FACTS_REQUIRED'):
        compile_goal(goal, tasks, artifacts=housing_artifacts(),
                     patch={'clarification': '价格缺失的房源保留。'})


@pytest.fixture(params=[
    '浏览器-housing_multi-03-04', '浏览器-housing_multi-04-04', '浏览器-housing_multi-05-04',
    '浏览器-housing_exclude_postfix-05-06', '浏览器-housing_exclude_postfix-06-06',
])
def inspected_frozen_housing_case(request, tmp_path):
    from app.runtime.upload_inspection import inspect_file
    from tests.browser.runtime_chinese_corpus import load_cases
    from tests.browser.runtime_chinese_fixtures import build_uploads

    case = next(item for item in load_cases() if item.case_id == request.param)
    artifacts = []
    for identity, upload in zip(('houses', 'zone'), build_uploads(case, tmp_path)):
        path = tmp_path / upload.filename
        path.write_bytes(upload.content_bytes)
        inspected = inspect_file(path, upload.filename)
        artifact = source(identity)
        artifact['crs'] = inspected['crs']
        artifact['schema'] = {'fields': inspected['fields'], 'geometry_types': inspected['geometry_types'],
                              'inspection': inspected}
        artifacts.append(artifact)
    return case, artifacts


@pytest.mark.parametrize('mutation', ['valid', 'missing', 'intersects', 'bypass'])
def test_frozen_complete_housing_requests_require_strict_spatial_selection(inspected_frozen_housing_case, mutation):
    case, artifacts = inspected_frozen_housing_case
    tasks = housing_tasks(case.params['price_max'], case.params['area_min'],
                          postfix=case.family == 'housing_exclude_postfix', crs_code=4326)
    previous = tasks[-4].task_key
    if mutation == 'missing':
        tasks.pop(-3)
    elif mutation == 'intersects':
        tasks[-3] = step('within', 'vector.extract_location', output(previous), {'predicate': 'intersects'},
                          mask=ArtifactRef(artifact_id='zone', type='Vector', crs={'kind': 'EPSG', 'code': 4326}))
    if mutation in {'missing', 'bypass'}:
        tasks[-2:] = [step('map', 'map.build', output(previous)),
                      step('export', 'file.export', output(previous), {'format': 'gpkg'})]
    if mutation == 'valid':
        plan = compile_goal(case.prompt, tasks, artifacts=artifacts)
        spatial = [item for item in plan.goal_requirements['operations'] if item['op'] == 'vector.extract_location']
        assert len(spatial) == 1
        assert spatial[0]['params'] == {'predicate': 'within'}
    else:
        with pytest.raises(CompileError, match='GOAL_REQUIREMENT_MISSING|GOAL_LINEAGE_MISMATCH'):
            compile_goal(case.prompt, tasks, artifacts=artifacts)


def test_attachment_description_without_requested_spatial_relation_does_not_create_selection():
    goal = '第二个附件是学区范围。价格高于250的不要，面积低于70的也不要。在地图显示。'
    requirements = build_goal_requirements(goal, housing_artifacts())
    assert not any(item['op'] == 'vector.extract_location' for item in requirements['operations'])


@pytest.mark.parametrize('paraphrase', range(10))
def test_frozen_housing_conjunction_phrases_do_not_acquire_spurious_or(paraphrase):
    from tests.browser.runtime_chinese_corpus import load_cases
    case = next(item for item in load_cases() if item.family == 'housing_multi'
                and item.profile == 3 and item.paraphrase == paraphrase)
    requirements = build_goal_requirements(case.prompt, housing_artifacts())
    assert requirements['missing_facts'] == []
    assert requirements['boolean'] == 'all'
    if paraphrase == 2:
        numeric = [item['params'] for item in requirements['conditions'] if not item.get('null_rejection')]
        assert numeric == [
            {'field': '价格', 'operator': '<=', 'value': 230, 'negate': False},
            {'field': '面积', 'operator': '>=', 'value': 66, 'negate': False},
        ]


def test_null_policy_clarification_does_not_supply_a_missing_price_threshold():
    from tests.browser.runtime_chinese_corpus import load_cases
    case = next(item for item in load_cases() if item.family == 'housing_ambiguous'
                and item.profile == 3 and item.paraphrase == 0)
    requirements = build_goal_requirements(case.prompt, housing_artifacts(),
        {'clarification': '价格或面积缺失的房源不保留。'})
    assert any('价格上限' in fact for fact in requirements['missing_facts'])
