"""Offline guard checks, never counted as real-browser/provider successes."""
import importlib
import importlib.util

import pytest


def runner():
    name = 'tests.browser.runtime_chinese_browser'
    assert importlib.util.find_spec(name), 'the 2000-case real-browser runner is missing'
    return importlib.import_module(name)


def test_full_schedule_is_unique_and_interleaves_families():
    from tests.browser.runtime_chinese_corpus import load_cases
    cases = runner().select_cases(load_cases(), limit=2000)
    assert len(cases) == len({case.case_id for case in cases}) == 2000
    assert len({case.family for case in cases[:10]}) == 10
    assert {case.upload_format for case in cases[:40]} == {'geojson', 'gpkg', 'zip', 'kml', 'csv', 'tsv', 'tif'}
    assert any(case.clarification_reply and case.paraphrase >= 5 for case in cases[:10])
    assert len(runner().select_cases(load_cases(), limit=10)) == 10


def test_selected_retest_does_not_duplicate_or_silently_ignore_ids():
    from tests.browser.runtime_chinese_corpus import load_cases
    cases = load_cases()
    assert runner().select_cases(cases, case_ids=[cases[-1].case_id]) == [cases[-1]]
    with pytest.raises(ValueError, match='unknown'):
        runner().select_cases(cases, case_ids=['does-not-exist'])
    with pytest.raises(ValueError, match='duplicate'):
        runner().select_cases(cases, case_ids=[cases[0].case_id] * 2)


def test_full_gate_never_upgrades_small_or_partial_evidence():
    module = runner()
    cases = [{'case_id': f'case-{i}', 'passed': True, 'real_provider_verified': True} for i in range(2000)]
    assert module.full_gate(cases, source_unchanged=True)
    assert not module.full_gate(cases[:9], source_unchanged=True)
    assert not module.full_gate(cases, source_unchanged=False)
    assert not module.full_gate(cases + [cases[0]], source_unchanged=True)
    assert not module.full_gate([{**cases[0], 'passed': False}] + cases[1:], source_unchanged=True)
    assert not module.full_gate([{**cases[0], 'real_provider_verified': False}] + cases[1:], source_unchanged=True)


def test_clarification_must_ask_for_the_missing_business_parameter():
    module = runner()
    module.verify_clarification({'status': 'awaiting_input', 'tasks': [], 'output_artifacts': [],
                                 'awaiting_input': {'message': '需要多大距离的缓冲？'}}, ('距离', '半径'))
    for value in (
        {'status': 'completed'},
        {'status': 'awaiting_input', 'tasks': [{'status': 'succeeded'}]},
        {'status': 'awaiting_input', 'output_artifacts': [{'artifact_id': 'a'}]},
        {'status': 'awaiting_input', 'tasks': [], 'output_artifacts': [], 'awaiting_input': {'message': '你好吗？'}},
    ):
        with pytest.raises(AssertionError):
            module.verify_clarification(value, ('距离', '半径'))


def test_visible_rows_compare_values_not_only_counts():
    module = runner()
    features = [{'properties': {'编号': '甲', '数值': 3}}, {'properties': {'编号': '乙', '数值': 4}}]
    module.verify_table_rows(['#', '编号', '数值'], [['乙', '4'], ['甲', '3']], features)
    with pytest.raises(AssertionError, match='visible'):
        module.verify_table_rows(['#', '编号', '数值'], [['甲', '3'], ['甲', '3']], features)


def test_clarification_does_not_take_metadata_as_a_question_or_omit_a_condition():
    module = runner()
    with pytest.raises(AssertionError):
        module.verify_clarification({'status': 'awaiting_input', 'awaiting_input': {
            'message': '你好吗？', 'patch_schema': {'description': '距离参数'}}}, ('距离',))
    with pytest.raises(AssertionError):
        module.verify_clarification({'status': 'awaiting_input', 'awaiting_input': {
            'message': '你的价格预算是多少？'}}, ('价格', '预算', '面积'))
    module.verify_clarification({'status': 'awaiting_input', 'awaiting_input': {
        'message': '价格预算和面积要求分别是多少？'}}, ('价格', '预算', '面积'))


def test_binary_upload_request_is_never_decoded_as_utf8():
    class BinaryUpload:
        method = 'POST'
        url = 'http://127.0.0.1/api/artifacts/uploads'

        @property
        def post_data(self):
            raise UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'binary multipart')

    capture = getattr(runner(), 'capture_request', None)
    assert callable(capture), 'binary-safe request observer is required'
    assert capture(BinaryUpload()) == {'method': 'POST', 'path': '/api/artifacts/uploads'}


def test_case_rejects_recovered_history_session():
    check = getattr(runner(), 'verify_run_session', None)
    assert callable(check), 'each case needs an explicit fresh browser session'
    check({'session_id': 'fresh'}, 'fresh')
    with pytest.raises(AssertionError, match='session'):
        check({'session_id': 'old'}, 'fresh')


def test_accepted_run_is_tracked_before_fallible_session_validation():
    from types import SimpleNamespace
    module = runner()
    track = getattr(module, 'track_accepted_run', None)
    assert callable(track), 'accepted runs must be tracked before validating their session'
    report = {}
    assert track(SimpleNamespace(json=lambda: {'run_id': 'accepted'}), report) == 'accepted'
    with pytest.raises(AssertionError, match='session'):
        module.verify_run_session({'session_id': 'old'}, 'fresh')
    assert report['run_id'] == 'accepted'
    assert not report.get('cleanup_incomplete')


@pytest.mark.parametrize('payload', [{}, {'run_id': None}, {'run_id': ''}, {'run_id': 123}])
def test_unidentifiable_accepted_run_stops_case_admission(payload):
    from types import SimpleNamespace
    track = getattr(runner(), 'track_accepted_run', None)
    assert callable(track), 'unknown accepted runs must stop the cohort'
    report = {}
    with pytest.raises(AssertionError):
        track(SimpleNamespace(json=lambda: payload), report)
    assert report['cleanup_incomplete'] is True


def test_malformed_accepted_response_stops_case_admission():
    from types import SimpleNamespace
    track = getattr(runner(), 'track_accepted_run', None)
    assert callable(track), 'malformed accepted responses must stop the cohort'
    report = {}
    def malformed():
        raise ValueError('invalid JSON')
    with pytest.raises(ValueError):
        track(SimpleNamespace(json=malformed), report)
    assert report['cleanup_incomplete'] is True


def test_windows_frontend_process_avoids_maglev_without_changing_browser_or_linux():
    command = getattr(runner(), 'frontend_command', None)
    assert callable(command)
    windows = command('node', 5173, windows=True)
    linux = command('node', 5173, windows=False)
    assert windows[:2] == ['node', '--no-maglev']
    assert '--no-maglev' not in linux
    assert windows[2:] == linux[1:]
    assert windows[-3:] == ['--port', '5173', '--strictPort']
