"""Frozen browser scenarios must cover meaning, not artificial prompt suffixes."""
import dataclasses
import importlib
import re
from collections import Counter

import pytest


@pytest.fixture
def corpus():
    return importlib.import_module('tests.browser.runtime_chinese_corpus')


def test_frozen_inventory_has_exactly_2000_unique_chinese_requests(corpus):
    cases = corpus.load_cases()
    assert len(cases) == len({case.case_id for case in cases}) == len({case.prompt for case in cases}) == 2000
    assert Counter(case.family for case in cases) == {family: 200 for family in corpus.FAMILIES}
    assert len(corpus.FAMILIES) == 10
    for case in cases:
        assert re.search(r'[\u4e00-\u9fff]', case.prompt)
        assert not re.search(r'[A-Za-z]', case.prompt)
        if case.clarification_reply is not None:
            assert re.search(r'[\u4e00-\u9fff]', case.clarification_reply)
            assert not re.search(r'[A-Za-z]', case.clarification_reply)
    assert corpus.corpus_digest(cases) == corpus.CORPUS_SHA256
    assert corpus.corpus_digest() == corpus.CORPUS_SHA256


def test_dataclass_and_repeated_load_are_stable_without_shared_params(corpus):
    first, second = corpus.load_cases(), corpus.load_cases()
    assert first == second
    with pytest.raises(dataclasses.FrozenInstanceError):
        first[0].prompt = 'changed'
    first[0].params['threshold'] = -1
    assert second[0].params['threshold'] != -1
    assert corpus.corpus_digest(first) != corpus.CORPUS_SHA256


def test_profile_changes_business_inputs_without_case_number_suffix(corpus):
    cases = corpus.load_cases()
    for family in corpus.FAMILIES:
        selected = [case for case in cases if case.family == family and case.paraphrase == 0]
        assert len(selected) == len({case.profile for case in selected}) == 20
        assert len({tuple(sorted(case.params.items())) for case in selected}) == 20
        assert len({case.prompt for case in selected}) == 20
        assert all(case.case_id not in case.prompt for case in selected)
        assert all(not re.search(r'(测试|用例|样本|场景)编号', case.prompt) for case in selected)


def test_ambiguity_has_missing_topic_and_exact_followup_for_completion(corpus):
    cases = corpus.load_cases()
    ambiguous = [case for case in cases if case.clarification_reply is not None]
    assert len(ambiguous) == 400
    assert {case.family for case in ambiguous} == {'housing_ambiguous', 'buffer_ambiguous'}
    for case in ambiguous:
        assert case.clarification_terms
        if case.family == 'buffer_ambiguous':
            assert str(case.params['distance_m']) + '米' not in case.prompt
            assert str(case.params['distance_m']) + '米' in case.clarification_reply
            assert '地图' in case.clarification_reply and '地理包' in case.clarification_reply
        else:
            assert str(case.params['price_max']) + '万元' not in case.prompt
            assert str(case.params['area_min']) + '平方米' not in case.prompt
            assert str(case.params['price_max']) + '万元' in case.clarification_reply
            assert str(case.params['area_min']) + '平方米' in case.clarification_reply
    assert all(not case.clarification_terms for case in cases if case.clarification_reply is None)


def test_formats_match_physical_geometry_and_coordinate_contract(corpus):
    cases = corpus.load_cases()
    assert {case.upload_format for case in cases} == {'geojson', 'gpkg', 'zip', 'kml', 'csv', 'tsv', 'tif'}
    for case in cases:
        if case.upload_format in {'csv', 'tsv'}:
            assert case.family in corpus.POINT_FAMILIES
            assert '经度' in case.prompt and '纬度' in case.prompt
            assert case.params['source_epsg'] == 4326
        if case.family == 'raster_calculate':
            assert case.upload_format == 'tif'
            assert '无数据' in case.prompt
            assert str(case.params['factor']) in case.prompt and str(case.params['offset']) in case.prompt
            assert '地图' not in case.prompt
        else:
            assert '地图' in case.prompt and '地理包' in case.prompt


def test_multi_instruction_and_null_semantics_are_explicit(corpus):
    for case in corpus.load_cases():
        if case.family in corpus.POINT_FAMILIES:
            assert str(case.params['threshold']) in case.prompt
            assert '数值' in case.prompt
        if case.family == 'rename_buffer':
            assert '名称' in case.prompt and '新名' in case.prompt
        if case.family == 'sequential_buffers':
            assert str(case.params['first_distance_m']) + '米' in case.prompt
            assert str(case.params['second_distance_m']) + '米' in case.prompt
            assert '上一步' in case.prompt or '第一步' in case.prompt
        if case.family.startswith('housing_'):
            assert '价格' in case.prompt and '面积' in case.prompt
            assert '边界' in case.prompt and '入学' in case.prompt
        if case.family in {'housing_exclude', 'housing_exclude_postfix'}:
            assert '缺失' in case.prompt and ('保留' in case.prompt or '不要' in case.prompt)


def test_digest_includes_reply_format_and_expected_parameters(corpus):
    cases = corpus.load_cases()
    for field, value in [('prompt', '改变需求'), ('clarification_reply', '改变答复'), ('upload_format', 'changed'), ('params', {'threshold': 999})]:
        changed = (dataclasses.replace(cases[0], **{field: value}), *cases[1:])
        assert corpus.corpus_digest(changed) != corpus.CORPUS_SHA256


def test_clarification_terms_cover_business_synonyms(corpus):
    cases = corpus.load_cases()
    housing = next(case for case in cases if case.family == 'housing_ambiguous')
    buffer = next(case for case in cases if case.family == 'buffer_ambiguous')
    assert {'价格', '预算', '总价', '面积'} <= set(housing.clarification_terms)
    assert {'距离', '半径', '多少米', '多远'} <= set(buffer.clarification_terms)


def test_coordinate_and_download_format_are_unambiguous_without_latin(corpus):
    for case in corpus.load_cases():
        if case.family == 'raster_calculate':
            assert case.params['source_epsg'] == case.params['target_epsg'] == 3857
        else:
            assert '地理包空间数据库文件' in case.prompt
            if not case.family.startswith('housing_'):
                assert str(case.params['source_epsg']) in case.prompt
        if case.family == 'vector_reproject_filter':
            assert '4326' in case.prompt and '一九八四' in case.prompt


@pytest.mark.parametrize('family', ['housing_ambiguous', 'buffer_ambiguous'])
def test_half_of_ambiguous_requests_are_natural_without_clarification_hints(corpus, family):
    natural = [case for case in corpus.load_cases() if case.family == family and case.paraphrase >= 5]
    assert len(natural) == 100
    hints = r'缺失|暂缺|先问|先询问|先确认|先澄清|先让|请向我|补问|问清|先把|未指定|未提供|尚未|待定|还没|暂时没有|没定|再执行|确认后|未确定|未量化'
    assert all(not re.search(hints, case.prompt) for case in natural)
    assert all(case.clarification_reply and case.clarification_terms for case in natural)
