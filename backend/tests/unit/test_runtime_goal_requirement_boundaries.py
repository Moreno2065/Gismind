"""Clause boundaries prevent row-selection text becoming column obligations."""
from __future__ import annotations

import pytest

from app.runtime.compiler import CompileError
from app.runtime.goal_requirements import build_goal_requirements, validate_goal_requirements


ARTIFACTS = [{'type': 'Vector', 'schema': {'fields': {'名称': 'str', '编号': 'int', '数值': 'float'}}}]


@pytest.mark.parametrize('boundary', ['。', '；', ';', '！', '!', '？', '?', '\n'])
def test_row_filter_cannot_reach_next_sentence_field_rename(boundary):
    goal = ('先只保留数值严格大于14的站点，等于阈值的不要' + boundary
            + '完成筛选后，字段“名称”改名为“新名”，然后给每个站点建立150米的缓冲区。')
    requirements = build_goal_requirements(goal, ARTIFACTS)
    operations = {item['op'] for item in requirements['operations']}
    assert 'attribute.keep_fields' not in operations
    assert 'attribute.rename_field' in operations
    assert 'vector.buffer' in operations


@pytest.mark.parametrize('goal', [
    '先只保留数值大于14的站点。字段名称改名为新名。',
    'Keep only rows with value above 14. Rename the name field.',
    'Keep only rows. Rename fields.',
    'Keep only rows with value above 14; rename the name column.',
    '先只保留数值大于14的站点。不要删除属性。字段名称改名为新名。',
])
def test_cross_sentence_column_words_do_not_create_keep_fields(goal):
    requirements = build_goal_requirements(goal, ARTIFACTS)
    assert not any(item['op'] == 'attribute.keep_fields' for item in requirements['operations'])


@pytest.mark.parametrize('goal', [
    '只保留名称字段。',
    '仅保留名称和编号这两个字段。',
    '只保留名称、编号两个字段。',
    '只保留名称，编号两个字段。',
    '只保留名称,编号两个字段。',
    '保留名称字段，其余属性删除。',
    '保留名称字段，其他字段去掉。',
    'Keep only name and id columns.',
    'Keep only name, id columns.',
    'Keep only fields name, id.',
    'Keep only area.km2, name fields.',
    '先筛选数值大于14的站点。然后只保留名称、编号字段。',
])
def test_explicit_keep_fields_remains_a_hard_compiler_requirement(goal):
    requirements = build_goal_requirements(goal, ARTIFACTS)
    keep = [item for item in requirements['operations'] if item['op'] == 'attribute.keep_fields']
    assert keep, requirements
    with pytest.raises(CompileError) as failure:
        validate_goal_requirements({**requirements, 'operations': keep, 'conditions': []}, [])
    assert failure.value.code == 'GOAL_REQUIREMENT_MISSING'


def test_removed_numbered_parts_alias_has_no_public_or_internal_remnant():
    from app.runtime import goal_requirements
    assert not hasattr(goal_requirements, '_numbered_parts')
