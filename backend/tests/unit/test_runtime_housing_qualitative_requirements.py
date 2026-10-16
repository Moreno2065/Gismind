"""Missing housing thresholds must pause even when a model invents filters."""
import pytest

from app.runtime.compiler import AwaitingInputDiagnostic
from app.runtime.goal_requirements import build_goal_requirements
from tests.browser.runtime_chinese_corpus import load_cases
from tests.unit.test_runtime_goal_negation_scope import housing_artifacts, housing_tasks
from tests.unit.test_runtime_harness_goal_coverage import compile_goal


CASES = tuple(case for case in load_cases() if case.family == 'housing_ambiguous' and case.profile == 0)


@pytest.mark.parametrize('case', CASES, ids=lambda case: case.case_id)
def test_every_natural_housing_preference_needs_both_numeric_thresholds(case):
    requirements = build_goal_requirements(case.prompt, housing_artifacts())
    assert any('价格上限' in fact for fact in requirements['missing_facts'])
    assert any('面积下限' in fact for fact in requirements['missing_facts'])
    with pytest.raises(AwaitingInputDiagnostic, match='GOAL_FACTS_REQUIRED'):
        compile_goal(case.prompt, housing_tasks(200, 60, complements=True), artifacts=housing_artifacts())


@pytest.mark.parametrize('case', CASES, ids=lambda case: case.case_id)
@pytest.mark.parametrize(('answer', 'unresolved', 'resolved'), [
    ('价格上限为200万元。', '面积下限', '价格上限'),
    ('面积下限为60平方米。', '价格上限', '面积下限'),
])
def test_partial_answer_cannot_authorize_inventing_the_other_dimension(case, answer, unresolved, resolved):
    requirements = build_goal_requirements(case.prompt, housing_artifacts(), {'clarification': answer})
    assert any(unresolved in fact for fact in requirements['missing_facts'])
    assert not any(resolved in fact for fact in requirements['missing_facts'])
    with pytest.raises(AwaitingInputDiagnostic, match='GOAL_FACTS_REQUIRED'):
        compile_goal(case.prompt, housing_tasks(200, 60, complements=True),
                     artifacts=housing_artifacts(), patch={'clarification': answer})


@pytest.mark.parametrize(('answer', 'missing'), [
    ('价格至少200万元，面积至少60平方米。', '价格上限'),
    ('价格至多200万元，面积至多60平方米。', '面积下限'),
    ('价格不等于200万元，面积至少60平方米。', '价格上限'),
])
def test_wrong_direction_bound_does_not_define_cheap_or_spacious(answer, missing):
    requirements = build_goal_requirements(CASES[0].prompt, housing_artifacts(), {'clarification': answer})
    assert any(missing in fact for fact in requirements['missing_facts'])


@pytest.mark.parametrize('goal', [
    '房源价格字段单位为万元。筛选价格不超过200万元的房源。',
    '面积字段单位为平方米。筛选面积至少60平方米的房源。',
    '不要考虑价格便宜或者面积宽敞。显示房源地图。',
])
def test_unrequested_or_withdrawn_preference_does_not_require_extra_thresholds(goal):
    assert not build_goal_requirements(goal, housing_artifacts())['missing_facts']


@pytest.mark.parametrize('description', [
    '备注字段包含“宽敞”二字。',
    '把备注字段改名为“宽敞”。',
    '图层名称改为“面积充裕”。',
    '备注列等于“面积大一点”。',
    '把备注字段改名为“价格实惠”。',
])
def test_field_values_and_names_are_not_qualitative_housing_preferences(description):
    requirements = build_goal_requirements('筛选价格不超过200万元的房源，价格字段单位为万元。' + description,
                                           housing_artifacts())
    assert requirements['missing_facts'] == []


def test_quoted_preference_outside_a_data_assignment_still_needs_an_area_bound():
    requirements = build_goal_requirements('筛选价格不超过200万元的房源，价格字段单位为万元。'
        '房子要“宽敞”一点，备注字段包含“宽敞”二字。', housing_artifacts())
    assert any('面积下限' in fact for fact in requirements['missing_facts'])


@pytest.mark.parametrize('answer', [
    '不考虑面积条件，仅按价格不超过200万元筛选。',
    '面积不限，价格不超过200万元。',
])
def test_explicit_reply_can_withdraw_an_unquantified_area_preference(answer):
    assert not build_goal_requirements(CASES[0].prompt, housing_artifacts(),
                                       {'clarification': answer})['missing_facts']


def test_area_withdrawal_does_not_erase_an_existing_explicit_numeric_obligation():
    requirements = build_goal_requirements(CASES[0].prompt + '面积至少60平方米。', housing_artifacts(),
        {'clarification': '面积不限，价格不超过200万元。'})
    assert any(item['params'] == {'field': '面积', 'operator': '>=', 'value': 60, 'negate': False}
               for item in requirements['conditions'])


def test_negated_area_withdrawal_does_not_resolve_missing_threshold():
    requirements = build_goal_requirements(CASES[0].prompt, housing_artifacts(),
        {'clarification': '不要忽略面积条件，价格不超过200万元。'})
    assert any('面积下限' in fact for fact in requirements['missing_facts'])
