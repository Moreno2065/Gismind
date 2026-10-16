"""Offline request-proof regressions; real-provider acceptance is separate."""
import pytest

from app.runtime.compiler import AwaitingInputDiagnostic, CompileError
from app.runtime.goal_requirements import build_goal_requirements
from tests.browser.runtime_chinese_corpus import load_cases
from tests.unit.test_runtime_goal_negation_scope import housing_artifacts, housing_tasks
from tests.unit.test_runtime_harness_goal_coverage import compile_goal


@pytest.mark.parametrize('paraphrase', range(10))
def test_postfixed_exclusion_forms_keep_exact_price_polarity_and_area_bound(paraphrase):
    case = next(item for item in load_cases() if item.family == 'housing_exclude_postfix'
                and item.profile == 3 and item.paraphrase == paraphrase)
    requirements = build_goal_requirements(case.prompt, housing_artifacts())
    assert requirements['missing_facts'] == []
    assert [item['params'] for item in requirements['conditions']] == [
        {'field': '价格', 'operator': '>', 'value': 230, 'negate': True},
        {'field': '面积', 'operator': '>=', 'value': 66, 'negate': False},
    ]
    compile_goal(case.prompt, housing_tasks(230, 66, postfix=True), artifacts=housing_artifacts())
    # Negated > deliberately keeps null prices. <= drops them and is not equivalent.
    with pytest.raises(CompileError, match='GOAL_REQUIREMENT_MISSING'):
        compile_goal(case.prompt, housing_tasks(230, 66, postfix=True, complements=True),
                     artifacts=housing_artifacts())


@pytest.mark.parametrize('suffix', ['的房源请排除', '的全部房源排除', '的房源请去掉',
                                    '的所有记录都剔除', '的记录全部删除掉'])
def test_polite_and_quantified_postfix_action_is_bound_to_its_own_predicate(suffix):
    goal = f'价格高于230{suffix}，面积至少66。'
    requirements = build_goal_requirements(goal, housing_artifacts())
    assert requirements['missing_facts'] == []
    assert requirements['conditions'][0]['params']['negate'] is True
    assert requirements['conditions'][1]['params']['negate'] is False


@pytest.mark.parametrize('goal', [
    '价格高于230的房源和学校请排除，面积至少66。',
    '价格高于230的房源请排除面积低于66的记录。',
    '价格高于230且面积低于66的全部房源排除。',
    '价格高于230或面积低于66的房源请去掉。',
])
def test_extra_objects_and_compound_negation_still_need_scope_clarification(goal):
    with pytest.raises(AwaitingInputDiagnostic, match='GOAL_FACTS_REQUIRED'):
        compile_goal(goal, housing_tasks(230, 66, postfix=True), artifacts=housing_artifacts())


def test_negated_request_to_exclude_rows_does_not_create_an_exclusion_obligation():
    requirements = build_goal_requirements('不要把价格高于230的全部房源排除，只显示地图。', housing_artifacts())
    assert requirements['conditions'] == []
