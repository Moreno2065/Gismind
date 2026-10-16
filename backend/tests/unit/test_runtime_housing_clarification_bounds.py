"""Offline bounds proofs from housing clarification failures, not live evidence."""
from __future__ import annotations

import pytest

from app.runtime.compiler import AwaitingInputDiagnostic, CompileError
from app.runtime.goal_requirements import build_goal_requirements, numeric_conditions
from tests.unit.test_runtime_goal_negation_scope import housing_artifacts, housing_tasks
from tests.unit.test_runtime_harness_goal_coverage import compile_goal, output, ref, step


UNITS = {'价格': '万元', '面积': '平方米'}
HOUSING_GOAL = (
    '房源的“价格”字段单位为万元，“面积”字段单位为平方米；第二个附件是学区范围。'
    '我想要价格实惠、面积充裕的房源，两方面都合适才要。'
    '房源还必须位于第二个附件的学区多边形内部，落在边界上的房源不选。'
    '在地图上展示最终结果，并将同一结果导出为地理包空间数据库文件供下载。'
)


def predicates(requirements):
    return [item['params'] for item in requirements['conditions'] if not item.get('null_rejection')]


@pytest.mark.parametrize(('price', 'area'), [(260, 72), (270, 74), (280, 76), (290, 78)])
def test_housing_reply_with_named_inclusive_bounds_compiles(price, area):
    answer = (f'价格上限为{price}万元，包含上限；面积下限为{area}平方米，包含下限。'
              '必须同时满足价格与面积条件，价格或面积缺失的房源不保留。')
    plan = compile_goal(HOUSING_GOAL, housing_tasks(price, area, complements=True),
                        artifacts=housing_artifacts(), patch={'clarification': answer})
    assert plan.goal_requirements['missing_facts'] == []
    assert plan.goal_requirements['boolean'] == 'all'
    assert predicates(plan.goal_requirements) == [
        {'field': '价格', 'operator': '<=', 'value': price, 'negate': False},
        {'field': '面积', 'operator': '>=', 'value': area, 'negate': False},
    ]
    assert [item['evidence'] for item in plan.goal_requirements['conditions'][:2]] == [
        f'价格上限为{price}万元，包含上限', f'面积下限为{area}平方米，包含下限']


@pytest.mark.parametrize(('text', 'field', 'operator', 'value'), [
    ('价格上限为0万元，包含上限。', '价格', '<=', 0),
    ('价格上限是260万元。', '价格', '<=', 260),
    ('价格上限为260万元，不包含上限。', '价格', '<', 260),
    ('价格上限为260万元（不含上限）。', '价格', '<', 260),
    ('价格上限为260万元；不含上限。', '价格', '<', 260),
    ('面积下限为72平方米，不包含下限。', '面积', '>', 72),
    ('面积下限为72平方米(不含下限)。', '面积', '>', 72),
    ('面积下限是72平方米。', '面积', '>=', 72),
    ('价格不大于260万元。', '价格', '<=', 260),
    ('面积不小于72平方米。', '面积', '>=', 72),
    ('价格在260万元以内。', '价格', '<=', 260),
    ('面积达到72平方米。', '面积', '>=', 72),
    ('价格上限为2600000元，包含上限。', '价格', '<=', 260),
])
def test_literal_named_and_equivalent_bounds_preserve_direction_units_and_zero(text, field, operator, value):
    conditions = numeric_conditions(text, set(UNITS), UNITS)
    assert [item['params'] for item in conditions] == [
        {'field': field, 'operator': operator, 'value': value, 'negate': False}]


@pytest.mark.parametrize('mutation', ['price', 'area', 'bypass'])
def test_clarified_bounds_cannot_be_changed_or_bypassed(mutation):
    answer = '价格上限为260万元，包含上限；面积下限为72平方米，包含下限。'
    tasks = housing_tasks(260, 72, complements=True)
    if mutation == 'price':
        tasks[0] = step('price', 'attribute.filter', ref(),
                        {'field': '价格', 'operator': '<', 'value': 260})
    elif mutation == 'area':
        tasks[1] = step('area', 'attribute.filter', output('price'),
                        {'field': '面积', 'operator': '>=', 'value': 71})
    else:
        tasks[1] = step('area', 'attribute.filter', ref(),
                        {'field': '面积', 'operator': '>=', 'value': 72})
    with pytest.raises(CompileError, match='GOAL_REQUIREMENT_MISSING|GOAL_LINEAGE_MISMATCH'):
        compile_goal(HOUSING_GOAL, tasks, artifacts=housing_artifacts(), patch={'clarification': answer})


def test_partial_named_correction_preserves_other_bound_and_other_field():
    requirements = build_goal_requirements(
        '价格字段单位为万元，面积字段单位为平方米。价格至少200且价格不超过300，面积至少72。',
        housing_artifacts(), {'clarification': '价格上限改为260万元，包含上限。'})
    assert {(item['field'], item['operator'], item['value']) for item in predicates(requirements)} == {
        ('价格', '>=', 200), ('价格', '<=', 260), ('面积', '>=', 72)}


@pytest.mark.parametrize('answer', [
    '面积下限为72平方米，包含下限。',
    '价格或面积缺失的房源不保留。',
    '不要把价格上限改为260万元，面积下限为72平方米，包含下限。',
    '价格上限不是260万元，面积下限为72平方米，包含下限。',
    '预算上限为260万元，面积下限为72平方米，包含下限。',
])
def test_partial_negated_or_unbound_reply_cannot_supply_missing_price(answer):
    with pytest.raises(AwaitingInputDiagnostic, match='GOAL_FACTS_REQUIRED'):
        compile_goal(HOUSING_GOAL, housing_tasks(260, 72, complements=True),
                     artifacts=housing_artifacts(), patch={'clarification': answer})


def test_named_bound_does_not_assume_physical_price_unit():
    with pytest.raises(AwaitingInputDiagnostic, match='GOAL_FACTS_REQUIRED'):
        compile_goal('筛选价格实惠的房源。', housing_tasks(260, 72, complements=True),
                     artifacts=housing_artifacts(),
                     patch={'clarification': '价格上限为260万元，包含上限。'})


def test_conflicting_endpoint_correction_cannot_reuse_stale_or_new_bound():
    goal = HOUSING_GOAL + '价格不超过300，面积至少72。'
    answer = '价格上限改为260万元，包含上限，但不包含上限。'
    for price in (300, 260):
        with pytest.raises(AwaitingInputDiagnostic, match='GOAL_FACTS_REQUIRED'):
            compile_goal(goal, housing_tasks(price, 72, complements=True),
                         artifacts=housing_artifacts(), patch={'clarification': answer})


def test_negated_named_correction_does_not_replace_known_bound():
    requirements = build_goal_requirements(
        '价格字段单位为万元。价格不超过300且价格至少200。', housing_artifacts(),
        {'clarification': '不要把价格上限改为260万元。'})
    assert {(item['operator'], item['value']) for item in predicates(requirements)} == {('<=', 300), ('>=', 200)}


def test_upper_endpoint_annotation_does_not_change_another_fields_lower_endpoint():
    requirements = build_goal_requirements(HOUSING_GOAL, housing_artifacts(), {'clarification':
        '价格上限为260万元，不包含上限；面积下限为72平方米，包含下限。'})
    assert [(item['field'], item['operator']) for item in predicates(requirements)] == [('价格', '<'), ('面积', '>=')]


@pytest.mark.parametrize('paraphrase', range(10))
@pytest.mark.parametrize('omitted', [None, 'price', 'area'])
def test_each_frozen_housing_phrase_requires_both_clarified_bounds(paraphrase, omitted):
    from tests.browser.runtime_chinese_corpus import load_cases

    case = next(item for item in load_cases() if item.family == 'housing_ambiguous'
                and item.profile == 6 and item.paraphrase == paraphrase)
    tasks = housing_tasks(case.params['price_max'], case.params['area_min'], complements=True)
    if omitted == 'price':
        tasks[0] = step('price', 'attribute.filter', ref(),
                        {'field': '面积', 'operator': '>=', 'value': case.params['area_min']})
    elif omitted == 'area':
        tasks[1] = step('area', 'attribute.filter', output('price'),
                        {'field': '价格', 'operator': '<=', 'value': case.params['price_max']})
    kwargs = {'artifacts': housing_artifacts(), 'patch': {'clarification': case.clarification_reply}}
    if omitted:
        with pytest.raises(CompileError, match='GOAL_REQUIREMENT_MISSING'):
            compile_goal(case.prompt, tasks, **kwargs)
    else:
        plan = compile_goal(case.prompt, tasks, **kwargs)
        assert predicates(plan.goal_requirements) == [
            {'field': '价格', 'operator': '<=', 'value': case.params['price_max'], 'negate': False},
            {'field': '面积', 'operator': '>=', 'value': case.params['area_min'], 'negate': False},
        ]
