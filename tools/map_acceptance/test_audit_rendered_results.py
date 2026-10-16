"""Observer negative controls are independent unit checks, never LLM successes."""
from copy import deepcopy
import ast
import asyncio
import hashlib
import importlib.util
import inspect
import io
import json
import math
from pathlib import Path
import subprocess
import sys
import time
import zipfile
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

TOOLING = Path(__file__).resolve().parent


def test_request_owner_survives_case_switch_and_same_url_requests(observer, tmp_path):
    class Request:
        url = 'https://restapi.amap.com/v3/log/init?key=not-a-real-key'
    owner = observer.ChromeObserver(None, {}, tmp_path)
    first, second = Request(), Request()
    owner.current_case, owner.current_case_scope = 'case19', {'case_epoch': 'epoch19', 'artifact_ids': ['art19']}
    owner.capture_request_owner(first)
    owner.current_case, owner.current_case_scope = 'case20', {'case_epoch': 'epoch20', 'artifact_ids': ['art20']}
    owner.capture_request_owner(second)
    owner.capture_request_owner(first)
    assert owner.request_owner_scope(first)['case_epoch'] == 'epoch19'
    assert owner.request_owner_scope(second)['case_epoch'] == 'epoch20'
    copy = owner.request_owner_scope(first); copy['artifact_ids'].append('foreign')
    assert owner.request_owner_scope(first)['artifact_ids'] == ['art19']
    with pytest.raises(ValueError, match='originating Request'):
        owner.request_owner_scope(Request())


@pytest.mark.parametrize('requested', [[], ['a', 'a'], ['unknown'], [True], {'a': True}])
def test_named_retest_rejects_invalid_selection(observer, tmp_path, requested):
    path = tmp_path / 'case-ids.json'; path.write_text(json.dumps(requested))
    with pytest.raises(ValueError):
        observer.select_original_cases([{'case_id': 'a'}, {'case_id': 'b'}], case_ids_file=path)


def test_named_retest_uses_original_order_and_never_a_prefix(observer, tmp_path):
    cases = [{'case_id': value} for value in ('a', 'b', 'c', 'd')]
    path = tmp_path / 'case-ids.json'; path.write_text('["d", "b"]')
    assert observer.select_original_cases(cases, case_ids_file=path) == [cases[1], cases[3]]
    with pytest.raises(ValueError):
        observer.select_original_cases(cases, case_ids_file=path, limit=1)
    with pytest.raises(SystemExit):
        observer.parse_cli(['--original', 'a', '--output', 'b', '--server-map', 'c',
                            '--case-ids-file', str(path), '--limit', '1'])


def test_json_publication_retries_transient_windows_sharing_error(observer, monkeypatch, tmp_path):
    target = tmp_path / 'sequence.json'
    target.write_text('{"previous":true}', encoding='utf8')
    replace = observer.os.replace
    calls = []
    pauses = []
    def temporarily_locked(source, destination):
        calls.append(Path(source))
        if len(calls) <= 2:
            assert json.loads(target.read_text()) == {'previous': True}
            error = PermissionError('synthetic Windows sharing conflict')
            error.winerror = 32
            raise error
        replace(source, destination)
    monkeypatch.setattr(observer.os, 'replace', temporarily_locked)
    monkeypatch.setattr(observer.time, 'sleep', pauses.append)
    observer.write_json(target, {'complete': [1, 2, 3]})
    assert json.loads(target.read_text()) == {'complete': [1, 2, 3]}
    assert len(calls) == 3 and len(set(calls)) == 1 and pauses == [.05, .05]
    assert not list(tmp_path.glob('*.pending'))
    assert observer.EVIDENCE_PUBLICATION_RETRIES[-1]['retries'] == 2


def test_json_publication_keeps_old_content_and_fails_on_persistent_lock(observer, monkeypatch, tmp_path):
    target = tmp_path / 'sequence.json'
    target.write_text('{"previous":true}', encoding='utf8')
    clock = {'now': 0.0}
    def locked(source, destination):
        error = PermissionError('synthetic persistent Windows access conflict')
        error.winerror = 5
        raise error
    monkeypatch.setattr(observer.os, 'replace', locked)
    monkeypatch.setattr(observer.time, 'monotonic', lambda: clock['now'])
    monkeypatch.setattr(observer.time, 'sleep', lambda seconds: clock.update(now=clock['now'] + seconds))
    with pytest.raises(PermissionError, match='persistent'):
        observer.write_json(target, {'complete': True})
    assert clock['now'] == 2.0
    assert json.loads(target.read_text()) == {'previous': True}
    assert not list(tmp_path.glob('*.pending'))


@pytest.mark.asyncio
async def test_browser_cases_share_one_capture_slot(observer):
    lock = asyncio.Lock()
    active = 0
    maximum = 0
    calls = []

    async def audit(case):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        calls.append(case)
        await asyncio.sleep(0)
        active -= 1
        return case

    results = await asyncio.gather(*(observer.observe_case_without_capture_contention(
        case, audit, lock) for case in range(4)))
    assert results == calls == list(range(4))
    assert maximum == 1 and not lock.locked()


@pytest.mark.asyncio
async def test_browser_capture_slot_released_after_case_failure(observer):
    lock = asyncio.Lock()

    async def failed(case):
        raise ValueError('actual case failure remains a failure')

    with pytest.raises(ValueError, match='case failure'):
        await observer.observe_case_without_capture_contention(1, failed, lock)
    assert not lock.locked()

    async def next_case(case):
        return case

    assert await observer.observe_case_without_capture_contention(2, next_case, lock) == 2


def capture_control_snapshot():
    """Synthetic adapter input only, never a browser or model success."""
    return {'view': {'center': [117.0, 34.0], 'zoom': 15, 'pitch': 0, 'rotation': 0,
        'size': [24, 24], 'rect': {'x': 0, 'y': 0, 'width': 24, 'height': 24}, 'dpr': 1},
        'overlays': [{'overlay_index': 0, 'kind': 'Point', 'coordinates': [117.0, 34.0],
            'visibility_observable': True, 'visible': True}]}


def capture_control_png(value=0):
    stream = io.BytesIO()
    Image.fromarray(np.full((24, 24, 3), value, dtype=np.uint8)).save(stream, format='PNG')
    return stream.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize('expiry_stage', ['visibility', 'animation_frames', 'before_snapshot', 'screenshot', 'after_snapshot'])
async def test_frame_checks_fixed_deadline_after_every_await(observer, monkeypatch, tmp_path, expiry_stage):
    clock, calls = {'now': 0.0}, []
    monkeypatch.setattr(observer, 'time', SimpleNamespace(monotonic=lambda: clock['now']))
    snapshot = capture_control_snapshot()
    class ControlledPage:
        async def evaluate(self, script, arg=None):
            stage = 'animation_frames' if 'requestAnimationFrame' in script else 'visibility'
            calls.append(stage)
            if stage == expiry_stage:
                clock['now'] = 2.0
        async def screenshot(self, **kwargs):
            calls.append('screenshot')
            assert clock['now'] < 1.0 and kwargs['timeout'] > 0
            if expiry_stage == 'screenshot':
                clock['now'] = 2.0
            return capture_control_png()
    instance = observer.ChromeObserver(None, {}, tmp_path)
    instance.page = ControlledPage()
    async def controlled_snapshot():
        stage = 'after_snapshot' if 'screenshot' in calls else 'before_snapshot'
        calls.append(stage)
        if stage == expiry_stage:
            clock['now'] = 2.0
        return deepcopy(snapshot)
    instance.snapshot = controlled_snapshot
    with pytest.raises(ValueError, match='paint budget exhausted') as caught:
        await instance.frame(observer.view_signature(snapshot), observer._geometry_signature(snapshot), 1.0,
            index=None, shown=True)
    diagnostic = caught.value.diagnostics
    assert diagnostic['failure_kind'] == 'paint_budget_exhausted'
    assert diagnostic['deadline_monotonic'] == 1.0 and diagnostic['deadline_remaining_seconds'] == 0
    assert diagnostic['operations'][-1]['stage'] == expiry_stage
    if expiry_stage in ('visibility', 'animation_frames', 'before_snapshot'):
        assert 'screenshot' not in calls
    if expiry_stage == 'screenshot':
        assert 'after_snapshot' not in calls


@pytest.mark.asyncio
async def test_expired_frame_has_zero_page_operations(observer, monkeypatch, tmp_path):
    monkeypatch.setattr(observer, 'time', SimpleNamespace(monotonic=lambda: 2.0))
    instance = observer.ChromeObserver(None, {}, tmp_path)
    instance.page = None
    snapshot = capture_control_snapshot()
    with pytest.raises(ValueError, match='paint budget exhausted') as caught:
        await instance.frame(observer.view_signature(snapshot), observer._geometry_signature(snapshot), 1.0,
            index=None, shown=True, diagnostic_output=tmp_path / 'expired')
    assert caught.value.diagnostics['operations'] == []


@pytest.mark.asyncio
async def test_frame_retains_bounded_actual_unstable_png_samples(observer, monkeypatch, tmp_path):
    clock = {'now': 0.0, 'captures': 0}
    monkeypatch.setattr(observer, 'time', SimpleNamespace(monotonic=lambda: clock['now']))
    snapshot = capture_control_snapshot()
    class ControlledPage:
        async def evaluate(self, script, arg=None):
            pass
        async def screenshot(self, **kwargs):
            clock['captures'] += 1
            clock['now'] += .2
            return capture_control_png(255 if clock['captures'] % 2 else 0)
    instance = observer.ChromeObserver(None, {}, tmp_path); instance.page = ControlledPage()
    async def controlled_snapshot():
        return deepcopy(snapshot)
    instance.snapshot = controlled_snapshot
    with pytest.raises(ValueError) as caught:
        await instance.frame(observer.view_signature(snapshot), observer._geometry_signature(snapshot), 1.1,
            index=None, shown=True, diagnostic_output=tmp_path / 'unstable')
    diagnostic = caught.value.diagnostics
    assert diagnostic['passed'] is False and diagnostic['last_global_drift'] == 1.0
    assert diagnostic['observed_pixels_unstable'] is True and diagnostic['attempts'] >= 5
    assert 1 <= len(diagnostic['raw_png_samples']) <= 3
    files = list((tmp_path / 'unstable').glob('sample-*.png'))
    assert len(files) == len(diagnostic['raw_png_samples'])
    for sample in diagnostic['raw_png_samples']:
        path = tmp_path / sample['capture_path']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == sample['capture_sha256']
    assert all('elapsed_seconds' in operation and 'remaining_after_seconds' in operation
        for operation in diagnostic['operations'])


@pytest.mark.asyncio
async def test_frame_classifies_font_capture_timeout_without_claiming_unstable_pixels(observer, tmp_path):
    snapshot = capture_control_snapshot()
    class ControlledPage:
        async def evaluate(self, script, arg=None):
            pass
        async def screenshot(self, **kwargs):
            raise TimeoutError('Page.screenshot: Timeout 100ms exceeded; waiting for fonts to load')
    instance = observer.ChromeObserver(None, {}, tmp_path); instance.page = ControlledPage()
    async def controlled_snapshot():
        return deepcopy(snapshot)
    instance.snapshot = controlled_snapshot
    with pytest.raises(ValueError) as caught:
        await instance.frame(observer.view_signature(snapshot), observer._geometry_signature(snapshot), time.monotonic()+1,
            index=None, shown=True, diagnostic_output=tmp_path / 'fonts')
    assert caught.value.diagnostics['failure_kind'] == 'font_capture_timeout'
    assert caught.value.diagnostics['attempts'] == 0
    assert caught.value.diagnostics['observed_pixels_unstable'] is False
    assert 'waiting for fonts' in caught.value.diagnostics['original_error']


@pytest.mark.asyncio
async def test_failed_final_phase_preserves_complete_partial_journal(observer, tmp_path):
    instance = observer.ChromeObserver(None, {}, tmp_path)
    calls = []
    async def controlled_frame(signature, geometry, deadline, *, index, shown, diagnostic_output):
        calls.append(shown)
        if len(calls) == 4:
            raise ValueError('synthetic final shown-again failure')
        data = capture_control_png(255 if shown else 0)
        return {'bytes': data, 'pixels': np.zeros((24, 24, 3), dtype=np.uint8),
            'snapshot': capture_control_snapshot(), 'attempts': 3, 'last_global_drift': 0.0}
    instance.frame = controlled_frame
    with pytest.raises(ValueError) as caught:
        await instance.capture_phase_sequence('view', 'geometry', time.monotonic()+10, index=None,
            capture_output=tmp_path / 'final', prefix='final-all-', sequence_kind='final')
    journal = json.loads((tmp_path / 'final' / 'sequence.json').read_text())
    assert journal['paint_readiness'] == 'incomplete' and journal['failed_phase'] == 'shown-again'
    assert len(journal['frames']) == 3 and journal['sequence_kind'] == 'final'
    assert caught.value.capture_sequence_metadata == journal
    assert not (tmp_path / 'final' / 'final-all-shown-again.png').exists()


def test_max_failures_cli_is_explicit_positive_limit(observer):
    required = ['--original', 'original', '--output', 'new', '--server-map', 'servers.json']
    assert observer.parse_cli(required + ['--max-failures', '5']).max_failures == 5
    assert observer.parse_cli(required).max_failures is None
    for invalid in ['0', '-1']:
        with pytest.raises(SystemExit):
            observer.parse_cli(required + ['--max-failures', invalid])


@pytest.mark.asyncio
async def test_cooperative_fail_fast_stops_new_cases_at_five_red_records(observer):
    budget = observer.CooperativeFailureBudget(5)
    cases = [{'case_id': f'case-{index}'} for index in range(10)]
    async def audit(case):
        return {**case, 'passed': False, 'error': 'synthetic negative control'}
    records = [record async for record in observer.observe_scheduled_cases(cases, audit, budget)]
    assert len(records) == 5
    status = budget.report(cases)
    assert status['triggered'] is True and status['failure_count'] == 5
    assert status['unscheduled_case_ids'] == [f'case-{index}' for index in range(5, 10)]
    gate = observer.coverage_gate([f'art-{index}' for index in range(1800)], records,
        raster_excluded=200, source_unchanged=True, limit=None, evidence_seal_verified=True)
    assert not gate['full_map_gate']


@pytest.mark.asyncio
async def test_cooperative_fail_fast_drains_four_inflight_without_new_scheduling(observer):
    budget = observer.CooperativeFailureBudget(1)
    release = asyncio.Event(); started, completed, closed = [], [], []
    assignments = [[{'case_id': f'{worker}-{index}'} for index in range(2)] for worker in range(4)]
    async def audit(case):
        started.append(case['case_id'])
        if len(started) == 4:
            release.set()
        await release.wait()
        completed.append(case['case_id'])
        return {**case, 'passed': False}
    async def worker(index):
        try:
            return [item async for item in observer.observe_scheduled_cases(assignments[index], audit, budget)]
        finally:
            closed.append(index)
    records = await asyncio.gather(*(worker(index) for index in range(4)))
    assert len(started) == len(completed) == 4 and sorted(closed) == list(range(4))
    assert all(len(items) == 1 for items in records)
    assert budget.report(sum(assignments, []))['unscheduled_case_count'] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize('threshold', [None, 5])
async def test_cooperative_schedule_keeps_all_green_cases_and_reports_worker_failure(observer, threshold):
    budget = observer.CooperativeFailureBudget(threshold)
    async def audit(case):
        if case['case_id'] == 'broken':
            raise RuntimeError('synthetic worker failure')
        return {**case, 'passed': True}
    cases = [{'case_id': str(index)} for index in range(7)]
    assert len([record async for record in observer.observe_scheduled_cases(cases, audit, budget)]) == 7
    with pytest.raises(RuntimeError):
        _ = [record async for record in observer.observe_scheduled_cases([{'case_id': 'broken'}], audit, budget)]
    assert budget.report(cases + [{'case_id': 'broken'}])['failure_count'] == 1


@pytest.mark.asyncio
async def test_paint_oracle_success_after_fixed_deadline_is_not_ready(observer, monkeypatch):
    clock = {'now': 0.0}
    monkeypatch.setattr(observer, 'time', SimpleNamespace(monotonic=lambda: clock['now']))
    masks = observer.build_masks('Point', [50, 50], (100, 100))
    hidden = np.zeros((100, 100, 3), dtype=np.uint8)
    shown = hidden.copy(); shown[masks['core_mask']] = [138, 150, 166]
    actual_oracle = observer.verify_visible_paint
    def delayed_oracle(*args, **kwargs):
        result = actual_oracle(*args, **kwargs)
        clock['now'] = 2.0
        return result
    monkeypatch.setattr(observer, 'verify_visible_paint', delayed_oracle)
    async def measurement(index):
        return [hidden, shown, hidden, shown], {'source': 'synthetic negative timing control'}
    attempts = []
    with pytest.raises(ValueError, match='fixed paint readiness deadline'):
        await observer.wait_for_paint_ready(measurement, deadline=1.0, attempts=attempts,
            core_mask=masks['core_mask'], contour_mask=None, allowed_mask=masks['allowed_mask'])
    assert attempts[0]['paint_readiness'] == 'not_ready'
    assert attempts[0]['statistics']['passed'] is True


@pytest.mark.asyncio
async def test_final_phase_evidence_persistence_cannot_extend_fixed_budget(observer, monkeypatch, tmp_path):
    clock = {'now': 0.0}
    monkeypatch.setattr(observer, 'time', SimpleNamespace(monotonic=lambda: clock['now']))
    instance = observer.ChromeObserver(None, {}, tmp_path)
    async def controlled_frame(*args, **kwargs):
        return {'bytes': capture_control_png(), 'pixels': np.zeros((24, 24, 3), dtype=np.uint8),
            'snapshot': capture_control_snapshot(), 'attempts': 3, 'last_global_drift': 0.0}
    instance.frame = controlled_frame
    actual_write_json = observer.write_json
    def delayed_write(path, value):
        actual_write_json(path, value)
        if path.name == 'sequence.json' and len(value['frames']) == 4:
            clock['now'] = 2.0
    monkeypatch.setattr(observer, 'write_json', delayed_write)
    with pytest.raises(ValueError, match='paint budget exhausted') as caught:
        await instance.capture_phase_sequence('view', 'geometry', 1.0, index=None,
            capture_output=tmp_path / 'final', sequence_kind='final')
    assert caught.value.capture_sequence_metadata['paint_readiness'] == 'incomplete'
    assert len(caught.value.capture_sequence_metadata['frames']) == 4


@pytest.mark.asyncio
async def test_stable_frame_preserves_positive_fixed_budget_and_operation_receipts(observer, monkeypatch, tmp_path):
    clock = {'now': 0.0}
    monkeypatch.setattr(observer, 'time', SimpleNamespace(monotonic=lambda: clock['now']))
    snapshot = capture_control_snapshot()
    class ControlledPage:
        async def evaluate(self, script, arg=None):
            pass
        async def screenshot(self, **kwargs):
            assert 0 < kwargs['timeout'] <= 1000
            clock['now'] += .1
            return capture_control_png()
    instance = observer.ChromeObserver(None, {}, tmp_path); instance.page = ControlledPage()
    async def controlled_snapshot():
        return deepcopy(snapshot)
    instance.snapshot = controlled_snapshot
    result = await instance.frame(observer.view_signature(snapshot), observer._geometry_signature(snapshot), 1.0,
        index=None, shown=True, diagnostic_output=tmp_path / 'stable')
    assert result['attempts'] == 3 and result['stable_consecutive_comparisons'] == 2
    receipt = json.loads((tmp_path / 'stable' / 'diagnostic.json').read_text())
    assert receipt['passed'] is True and receipt['deadline_extended'] is False
    assert receipt['deadline_monotonic'] == 1.0 and receipt['deadline_remaining_seconds'] > 0
    assert receipt['operation_totals']['screenshot']['count'] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('cleanup_fails', [False, True])
@pytest.mark.parametrize('export_fails', [False, True, 'unprintable', 'sensitive'])
async def test_fail_fast_run_exports_summary_and_all_raw_ledgers_without_browser(observer, monkeypatch, tmp_path, cleanup_fails, export_fails):
    """Entire negative orchestration; no browser/network/LLM success fixture."""
    import playwright.async_api
    # Successful closes also exercise repeated context rollover and complete
    # ledger aggregation. Cleanup-failure controls retain their final-close scope.
    monkeypatch.setattr(observer, 'MAX_CASES_PER_CONTEXT', 25 if cleanup_fails else 2)
    original = tmp_path / 'synthetic-original'; original.mkdir()
    for name in ('source-manifest.json', 'corpus.json', 'summary.json', 'results.jsonl', 'schedule.json', 'evidence-manifest.json'):
        (original / name).write_text('{}', encoding='utf8')
    server_path = tmp_path / 'servers.json'; server_path.write_text('{}', encoding='utf8')
    cases = [{'case_id': f'negative-{index}', 'shard': 'shard-1', 'session_id': f'session-{index}', 'run_id': f'run-{index}',
        'map_artifact': {'artifact_id': f'art-{index}'}} for index in range(8)]
    monkeypatch.setattr(observer, 'load_original', lambda root: (cases, [None] * 200))
    monkeypatch.setattr(observer, 'validate_server_map', lambda root, value: {'shard-1': {}})
    monkeypatch.setattr(observer, 'source_hashes', lambda: {})
    monkeypatch.setattr(observer, 'verify_evidence_seal', lambda root:
        {'passed': True, 'verified_file_count': 21515, 'basis': 'synthetic orchestration unit only'})
    class NegativeObserver(observer.ChromeObserver):
        async def initialize(self, case):
            pass
        async def audit_case(self, case):
            self.case_sequence += 1
            (self.output / case['shard'] / case['case_id']).mkdir(parents=True)
            for ledger in (self.network_request_starts, self.network_extra_infos, self.native_sdk_responses,
                    self.native_sdk_body_receipts, self.network_finished, self.network_response_extra_infos,
                    self.paused_requests, self.network_loading_failures):
                ledger.append({'request_id': case['case_id'], 'source': 'synthetic negative ledger control'})
            self.sdk_initial_200[case['case_id']] = {'request_id': case['case_id'], 'source': 'synthetic negative prior ledger control'}
            self.sdk_redirects.append({'request_id': case['case_id'], 'source': 'synthetic negative redirect ledger control'})
            record = {**case, 'map_artifact_id': case['map_artifact']['artifact_id'], 'passed': False,
                'error': 'synthetic negative case', 'sdk_resources': []}
            observer.write_json(self.output / case['shard'] / case['case_id'] / 'observation.json',
                self.receipt_authority.compact_record(record))
            return record
        async def close(self):
            if cleanup_fails:
                raise RuntimeError('synthetic cleanup failure with raw ledgers retained')
    monkeypatch.setattr(observer, 'ChromeObserver', NegativeObserver)
    class NoBrowser:
        version = 'synthetic unit, not Chrome'
        async def close(self):
            pass
    class NoPlaywright:
        chromium = None
        async def __aenter__(self):
            self.chromium = self
            return self
        async def __aexit__(self, *args):
            pass
        async def launch(self, **kwargs):
            return NoBrowser()
    monkeypatch.setattr(playwright.async_api, 'async_playwright', NoPlaywright)
    formatter_calls = []
    class NoStringError(Exception):
        def __str__(self):
            formatter_calls.append('original_str_called')
            raise RuntimeError('token=FORMATTER_SECRET_MUST_NOT_APPEAR')
    normal_validation_error = ValueError('synthetic terminal export validation failure')
    sensitive_validation_error = ValueError('key=NORMAL_SECRET_MUST_NOT_APPEAR '
        'https://webapi.amap.com/maps?key=QUERY_SECRET_MUST_NOT_APPEAR&securityJsCode=SDK_SECRET_MUST_NOT_APPEAR')
    if export_fails:
        def fail_terminal_validation(*args):
            if export_fails == 'unprintable':
                raise NoStringError('key=ORIGINAL_SECRET_MUST_NOT_APPEAR')
            if export_fails == 'sensitive':
                raise sensitive_validation_error
            raise normal_validation_error
        monkeypatch.setattr(observer, 'validate_evidence_export', fail_terminal_validation)
    output = tmp_path / 'synthetic-new'
    args = observer.parse_cli(['--original', str(original), '--output', str(output),
        '--server-map', str(server_path), '--concurrency', '1', '--max-failures', '5'])
    assert await observer.run_observer(args) == 1
    report = json.loads((output / 'audit-summary.json').read_text())
    assert report['full_map_gate'] is False and report['pilot_passed'] is False
    assert len(report['records']) == 5 and report['cooperative_fail_fast']['triggered'] is True
    assert report['cooperative_fail_fast']['unscheduled_case_count'] == 3
    start = json.loads((output / 'audit-start.json').read_text())
    assert start['cooperative_fail_fast']['max_failures'] == 5 and start['paint_timeout_seconds'] == 30
    for field in ('network_request_start_observations', 'network_request_extra_info_observations',
            'native_sdk_response_observations', 'native_sdk_actual_body_receipts',
            'network_loading_finished_observations', 'network_response_extra_info_observations',
            'fetch_request_paused_observations', 'network_loading_failed_observations'):
        assert len(report[field]) == 5
    assert bool(report['worker_errors']) == bool(cleanup_fails or export_fails)
    assert report['evidence_completeness']['complete'] is (not bool(export_fails))
    assert bool(report['evidence_completeness']['errors']) is bool(export_fails)
    assert len(report['native_sdk_initial_http200_receipts']) == 5 and len(report['sdk_redirects']) == 5
    if export_fails:
        with pytest.raises(ValueError, match='incomplete'): observer.expand_evidence_report(report, output)
        diagnostic = report['evidence_completeness']['errors'][0]
        serialized = json.dumps(report)
        for secret in ('FORMATTER_SECRET_MUST_NOT_APPEAR', 'ORIGINAL_SECRET_MUST_NOT_APPEAR',
                'NORMAL_SECRET_MUST_NOT_APPEAR', 'QUERY_SECRET_MUST_NOT_APPEAR', 'SDK_SECRET_MUST_NOT_APPEAR'):
            assert secret not in serialized
        if export_fails == 'unprintable':
            assert formatter_calls == ['original_str_called']  # No fallback formats either exception again.
            assert diagnostic['error_type'] == 'NoStringError'
            assert diagnostic['formatter_failure_type'] == 'RuntimeError'
            assert diagnostic['error_text_available'] is False and len(json.dumps(diagnostic)) < 600
        else:
            error = sensitive_validation_error if export_fails == 'sensitive' else normal_validation_error
            assert diagnostic == {'stage': 'terminal_evidence_incomplete', 'error': observer.safe_error(error)}
    assert len(report['network_reconciliation_statistics']) == (1 if cleanup_fails else 3)
    assert report['network_reconciliation_statistics'][0]['counts']['event_skips'] == 0
    assert report['network_reconciliation_statistics'][0]['counts']['full_calls'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('expired_stage', ['visibility', 'snapshot', 'release_refs'])
async def test_cleanup_has_separate_fixed_budget_and_revokes_old_restored_flag(observer, monkeypatch, tmp_path, expired_stage):
    clock, calls = {'now': 10.0}, []
    monkeypatch.setattr(observer, 'time', SimpleNamespace(monotonic=lambda: clock['now']))
    snapshot = capture_control_snapshot()
    class ControlledPage:
        async def evaluate(self, script, arg=None):
            stage = 'release_refs' if 'delete window' in script else 'visibility'
            calls.append(stage)
            if stage == expired_stage:
                clock['now'] = 41.0
            return True
        async def screenshot(self, **kwargs):
            raise AssertionError('cleanup must never capture or repair paint evidence')
    instance = observer.ChromeObserver(None, {}, tmp_path); instance.page = ControlledPage()
    async def controlled_snapshot():
        calls.append('snapshot')
        if expired_stage == 'snapshot':
            clock['now'] = 41.0
        return deepcopy(snapshot)
    instance.snapshot = controlled_snapshot
    record = {'passed': True, 'restored_all_shown': True,
        'paint_work_budget': {'deadline_monotonic': 1.0},
        'independent_expected_gcj02_features': [{'type': 'Feature', 'properties': {},
            'geometry': {'type': 'Point', 'coordinates': [117.0, 34.0]}}]}
    await instance.cleanup_overlays(record, observer._geometry_signature(snapshot))
    assert record['passed'] is False and record['restored_all_shown'] is False
    assert record['cleanup_diagnostics']['deadline_monotonic'] == 40.0
    assert record['cleanup_diagnostics']['deadline_extended'] is False
    assert record['cleanup_diagnostics']['failed_stage'] == expired_stage
    assert record['paint_work_budget']['deadline_monotonic'] == 1.0
    assert calls == ['visibility', 'snapshot', 'release_refs'][:['visibility', 'snapshot', 'release_refs'].index(expired_stage)+1]


@pytest.mark.asyncio
async def test_cleanup_after_expired_paint_releases_refs_but_missing_final_paint_stays_red(observer, monkeypatch, tmp_path):
    clock, calls = {'now': 10.0}, []
    monkeypatch.setattr(observer, 'time', SimpleNamespace(monotonic=lambda: clock['now']))
    snapshot = capture_control_snapshot()
    class ControlledPage:
        async def evaluate(self, script, arg=None):
            calls.append('release_refs' if 'delete window' in script else 'visibility')
            return True
        async def screenshot(self, **kwargs):
            raise AssertionError('cleanup must not add screenshots')
    instance = observer.ChromeObserver(None, {}, tmp_path); instance.page = ControlledPage()
    async def controlled_snapshot():
        calls.append('snapshot'); return deepcopy(snapshot)
    instance.snapshot = controlled_snapshot
    record = {'passed': True, 'restored_all_shown': True, 'paint_work_budget': {'deadline_monotonic': 1.0},
        'independent_expected_gcj02_features': [{'type': 'Feature', 'properties': {},
            'geometry': {'type': 'Point', 'coordinates': [117.0, 34.0]}}]}
    await instance.cleanup_overlays(record, observer._geometry_signature(snapshot))
    assert calls == ['visibility', 'snapshot', 'release_refs']
    assert record['cleanup_geometry_oracle']['passed'] is True
    assert record['cleanup_observer_refs_released'] is True
    assert record['restored_all_shown'] is False and record['passed'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', [None, 'geometry', 'view', 'visibility'])
async def test_cleanup_restored_requires_existing_complete_causal_proof_and_unchanged_real_snapshot(observer, tmp_path, changed):
    final = capture_control_snapshot()
    actual = deepcopy(final)
    if changed == 'geometry':
        actual['overlays'][0]['coordinates'][0] += .01
    elif changed == 'view':
        actual['view']['zoom'] += 1
    elif changed == 'visibility':
        actual['overlays'][0]['visible'] = False
    class ControlledPage:
        async def evaluate(self, script, arg=None):
            return True
        async def screenshot(self, **kwargs):
            raise AssertionError('cleanup cannot create causal paint evidence')
    instance = observer.ChromeObserver(None, {}, tmp_path); instance.page = ControlledPage()
    async def controlled_snapshot():
        return deepcopy(actual)
    instance.snapshot = controlled_snapshot
    expected = [{'type': 'Feature', 'properties': {}, 'geometry': {'type': 'Point', 'coordinates': [117.0, 34.0]}}]
    masks = observer.build_masks('Point', [50, 50], (100, 100))
    hidden = np.zeros((100, 100, 3), dtype=np.uint8)
    shown = hidden.copy(); shown[masks['core_mask']] = [138, 150, 166]
    statistics = observer.verify_visible_paint(hidden, shown, hidden, shown,
        core_mask=masks['core_mask'], contour_mask=None, allowed_mask=masks['allowed_mask'])
    record = {'passed': True, 'restored_all_shown': True, 'independent_expected_gcj02_features': expected,
        'final_sdk_snapshot': final, 'final_geometry_oracle': observer.verify_sdk_geometry(expected, final['overlays']),
        'final_all_shown_causal_paint_oracles': [statistics],
        'final_all_phases': [{'phase': phase} for phase in ('hidden-before', 'shown-first', 'hidden-after', 'shown-again')]}
    await instance.cleanup_overlays(record, observer._geometry_signature(final))
    assert record['restored_all_shown'] is (changed is None)
    assert record['passed'] is (changed is None)
    assert record['cleanup_observer_refs_released'] is True
sys.path.insert(0, str(TOOLING))


@pytest.fixture
def observer():
    path = TOOLING / 'audit_rendered_results.py'
    if not path.is_file():
        class MissingImplementation:
            def __getattr__(self, name):
                raise AssertionError(f'independent historical observer feature is missing: {name}')
        return MissingImplementation()
    spec = importlib.util.spec_from_file_location('audit_rendered_results', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def polygon():
    ring = [[40 + 25 * math.cos(i * math.tau / 64), 40 + 25 * math.sin(i * math.tau / 64)] for i in range(64)]
    return {'type': 'Polygon', 'coordinates': [ring + [ring[0]]]}


def original_case(tmp_path):
    path = tmp_path / 'shard-1' / 'case-a'
    path.mkdir(parents=True)
    content = {'crs': {'kind': 'EPSG', 'code': 4326}, 'layers': [{'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'properties': {}, 'geometry': polygon()}]}]}
    map_bytes = json.dumps(content).encode()
    file_bytes = b'SQLite format 3\x00' + b'unit-binding-only'
    (path / 'art_map.json').write_bytes(map_bytes)
    (path / 'art_file.gpkg').write_bytes(file_bytes)
    artifacts = [
        {'artifact_id': 'art_map', 'type': 'MapLayer', 'content_hash': hashlib.sha256(map_bytes).hexdigest(), 'crs': content['crs']},
        {'artifact_id': 'art_file', 'type': 'File', 'content_hash': hashlib.sha256(file_bytes).hexdigest(),
         'schema': {'file': {'extension': '.gpkg', 'format': 'gpkg'}}}]
    row = {'case_id': 'case-a', 'run_id': 'run-a', 'passed': True, 'real_provider_verified': True, 'evidence_path': str(path)}
    result = {'case_id': 'case-a', 'run_id': 'run-a', 'session_id': 'session-a', 'passed': True,
              'real_provider_verified': True, 'snapshot': {'run_id': 'run-a', 'session_id': 'session-a',
              'status': 'completed', 'output_artifacts': artifacts, 'final_artifact_ids': ['art_map', 'art_file']}}
    return row, result, path


def test_binding_uses_final_identity_and_physical_download_hash(observer, tmp_path):
    row, result, path = original_case(tmp_path)
    bound = observer.bind_case(row, result, path)
    assert bound['map_artifact']['artifact_id'] == 'art_map'
    assert bound['gpkg_path'] == path / 'art_file.gpkg'
    assert bound['session_id'] == 'session-a'
    assert bound['shard'] == 'shard-1'


@pytest.mark.parametrize('field', ['case_id', 'run_id', 'session_id'])
def test_binding_rejects_cross_case_run_or_session(observer, tmp_path, field):
    row, result, path = original_case(tmp_path)
    if field == 'session_id':
        result['snapshot'][field] = 'other'
    else:
        result[field] = 'other'
    with pytest.raises(ValueError, match='binding'):
        observer.bind_case(row, result, path)


def test_binding_rejects_nonfinal_map_and_changed_gpkg(observer, tmp_path):
    row, result, path = original_case(tmp_path)
    result['snapshot']['final_artifact_ids'] = ['art_file']
    with pytest.raises(ValueError, match='final'):
        observer.bind_case(row, result, path)
    result['snapshot']['final_artifact_ids'].append('art_map')
    (path / 'art_file.gpkg').write_bytes(b'SQLite format 3\x00changed')
    with pytest.raises(ValueError, match='hash'):
        observer.bind_case(row, result, path)


def test_scope_rejects_style_source_holes_lines_and_wrong_crs(observer):
    content = {'crs': {'kind': 'EPSG', 'code': 4326}, 'layers': [{'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'properties': {}, 'geometry': polygon()}]}]}
    assert len(observer.validate_map_scope(content)) == 1
    candidates = []
    altered = deepcopy(content); altered['layers'][0]['style'] = {}; candidates.append(altered)
    altered = deepcopy(content); altered['layers'][0]['features'][0]['properties']['_source'] = 'Upload'; candidates.append(altered)
    altered = deepcopy(content); altered['layers'][0]['features'][0]['geometry']['coordinates'].append(polygon()['coordinates'][0]); candidates.append(altered)
    altered = deepcopy(content); altered['layers'][0]['features'][0]['geometry']['type'] = 'LineString'; candidates.append(altered)
    altered = deepcopy(content); altered['crs']['code'] = 3857; candidates.append(altered)
    for altered in candidates:
        with pytest.raises(ValueError):
            observer.validate_map_scope(altered)


def test_implicit_ring_closure_preserves_raw_path(observer):
    ring = [[1, 2], [3, 2], [3, 4]]
    raw = deepcopy(ring)
    got = observer.normalize_sdk_overlay({'kind': 'Polygon', 'raw_coordinates': ring})
    assert got['coordinates'] == [[*ring, ring[0]]]
    assert got['raw_coordinates'] == raw
    assert ring == raw
    with pytest.raises(ValueError):
        observer.normalize_sdk_overlay({'kind': 'LineString', 'raw_coordinates': ring})


def test_masks_point_known_offset_and_polygon_eroded_core(observer):
    point = observer.build_masks('Point', [50, 50], (100, 100))
    assert point['core_mask'][42, 42]
    assert not point['core_mask'][50, 50]
    assert not point['allowed_mask'][70, 70]
    masks = observer.build_masks('Polygon', polygon()['coordinates'], (100, 100))
    assert masks['core_mask'][40, 40]
    assert not np.any(masks['core_mask'] & masks['contour_mask'])
    assert np.all((masks['core_mask'] | masks['contour_mask']) <= masks['allowed_mask'])


def test_masks_fail_closed_for_crop_and_tiny_geometry(observer):
    with pytest.raises(ValueError, match='crop'):
        observer.build_masks('Point', [8, 8], (100, 100))
    with pytest.raises(ValueError, match='core'):
        observer.build_masks('Polygon', [[[40, 40], [42, 40], [42, 42], [40, 40]]], (100, 100))


def test_synthetic_missing_pixels_fail_locked_real_oracle(observer):
    masks = observer.build_masks('Point', [50, 50], (100, 100))
    hidden = np.zeros((100, 100, 3), dtype=np.uint8)
    with pytest.raises(ValueError, match='core coverage'):
        observer.verify_visible_paint(hidden, hidden, hidden, hidden,
            core_mask=masks['core_mask'], contour_mask=None, allowed_mask=masks['allowed_mask'])


def test_full_gate_exact_1800_unique_map_ids_and_no_pilot(observer):
    ids = [f'art_{i}' for i in range(1800)]
    records = [{'map_artifact_id': value, 'passed': True} for value in ids]
    assert observer.coverage_gate(ids, records, raster_excluded=200, source_unchanged=True, limit=None, evidence_seal_verified=True)['full_map_gate']
    for changed, limit, source, raster in [(records[:-1], None, True, 200),
            (records[:-1] + [records[0]], None, True, 200), (records, 1800, True, 200),
            (records, None, False, 200), (records, None, True, 199)]:
        assert not observer.coverage_gate(ids, changed, raster_excluded=raster, source_unchanged=source, limit=limit, evidence_seal_verified=True)['full_map_gate']
    failed = deepcopy(records); failed[-1]['passed'] = False
    assert not observer.coverage_gate(ids, failed, raster_excluded=200, source_unchanged=True, limit=None, evidence_seal_verified=True)['full_map_gate']


def test_route_guard_blocks_proxy_writes_and_provider(observer):
    origins = ['http://127.0.0.1:5173', 'http://127.0.0.1:8101']
    assert observer.blocked_request('POST', origins[0] + '/api/sessions', origins)
    assert observer.blocked_request('DELETE', origins[1] + '/api/artifacts/art_a', origins)
    assert observer.blocked_request('GET', 'https://api.deepseek.com/v1/models', origins)
    assert not observer.blocked_request('GET', origins[0] + '/api/runs/run_a', origins)
    assert not observer.blocked_request('GET', 'https://webapi.amap.com/maps?v=2.0&key=secret', origins)


def test_trace_extracts_only_single_actual_user_id(observer, tmp_path):
    path = tmp_path / 'trace.zip'
    request = {'method': 'GET', 'url': 'http://localhost/api/runs/run-a',
        'headers': [{'name': 'Authorization', 'value': 'must-never-export'}, {'name': 'X-User-Id', 'value': 'actual-user'}]}
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('trace.network', json.dumps({'type': 'resource-snapshot', 'snapshot': {'request': request}}) + '\n')
    assert observer.trace_user_id(path) == 'actual-user'


def test_url_identity_redacts_credentials_and_key_tokens(observer):
    value = observer.redact_url('https://user:pass@webapi.amap.com/maps?v=2.0&key=SECRET&securityJsCode=CODE&token=TOKEN')
    assert 'SECRET' not in value and 'CODE' not in value and 'TOKEN' not in value
    assert 'user' not in value and 'pass' not in value
    assert 'v=2.0' in value


def test_output_must_be_new_sibling_outside_original(observer, tmp_path):
    original = tmp_path / 'original'; original.mkdir()
    assert observer.validate_output(original, tmp_path / 'observer-new') == tmp_path / 'observer-new'
    for wrong in [original, original / 'child', tmp_path]:
        with pytest.raises(ValueError):
            observer.validate_output(original, wrong)


def test_style_scope_rejects_altered_marker_and_polygon(observer):
    point = {'kind': 'Point', 'options': {'anchor': 'center', 'offset': [-8, -8]},
        'content': '<div style="width:16px;height:16px;border-radius:50%;background:#8a96a6;box-shadow:0 0 0 3px #5b677633,0 0 8px #8a96a6aa;"></div>'}
    observer.validate_overlay_style(point)
    bad = deepcopy(point); bad['options']['offset'] = [0, 0]
    with pytest.raises(ValueError, match='style'):
        observer.validate_overlay_style(bad)
    poly = {'kind': 'Polygon', 'options': {'fillColor': '#8a96a6', 'strokeColor': '#8a96a6', 'fillOpacity': .24, 'strokeWeight': 1.5, 'strokeOpacity': .9}}
    observer.validate_overlay_style(poly)
    bad = deepcopy(poly); bad['options']['fillOpacity'] = .8
    with pytest.raises(ValueError, match='style'):
        observer.validate_overlay_style(bad)


def test_independent_expected_reads_real_gpkg_and_explicit_gcj(observer, tmp_path):
    import geopandas as gpd
    from shapely.geometry import Point
    from app.tools.geo_transform import wgs84_to_gcj02
    path = tmp_path / 'physical.gpkg'
    gpd.GeoDataFrame({'name': ['a']}, geometry=[Point(118.7, 32.0)], crs=4326).to_file(path, driver='GPKG')
    value = observer.independent_expected_features(path)
    assert value[0]['geometry']['coordinates'] == list(wgs84_to_gcj02(118.7, 32.0))
    assert value[0]['geometry']['coordinates'] != [118.7, 32.0]
    assert value[0]['properties'] == {}


def test_clone_snapshot_must_equal_original_binding(observer, tmp_path):
    row, result, path = original_case(tmp_path)
    bound = observer.bind_case(row, result, path)
    observer.verify_clone_snapshot(bound, result['snapshot'])
    for field in ['run_id', 'session_id', 'final_artifact_ids']:
        wrong = deepcopy(result['snapshot']); wrong[field] = [] if field == 'final_artifact_ids' else 'other'
        with pytest.raises(ValueError, match='clone'):
            observer.verify_clone_snapshot(bound, wrong)
    wrong = deepcopy(result['snapshot']); wrong['output_artifacts'][-1]['content_hash'] = 'wrong'
    with pytest.raises(ValueError, match='clone'):
        observer.verify_clone_snapshot(bound, wrong)


def test_original_summary_requires_explicit_terminal_full_and_no_stubs(observer):
    summary = {'scheduled': 2000, 'executed': 2000, 'passed_cases': 2000,
               'passed': True, 'full_2000_gate': True, 'network_stubs': False, 'source_unchanged': True}
    observer.validate_original_summary(summary)
    for field, replacement in [('scheduled', 1999), ('executed', 1999), ('passed_cases', 1999),
            ('network_stubs', True), ('source_unchanged', False), ('full_2000_gate', False)]:
        wrong = deepcopy(summary); wrong[field] = replacement
        with pytest.raises(ValueError, match='original'):
            observer.validate_original_summary(wrong)


def test_sdk_observation_program_rejects_detached_map_and_never_wraps_sdk(observer):
    program = observer.SDK_ATTACH_JS
    assert 'this.filter' in program and 'isConnected' in program and 'sourceArtifactId' in program
    assert 'getAllOverlays' in program and 'getMap() === map' in program
    assert 'new AMap' not in program and 'prototype =' not in program and 'setMap(' not in program
    assert 'getOptions' in observer.SDK_SNAPSHOT_JS


def test_camera_signature_fail_closed_on_layout_or_view_movement(observer):
    snapshot = {'view': {'center': [118, 32], 'zoom': 12, 'pitch': 0, 'rotation': 0,
        'size': [800, 160], 'rect': {'x': 100, 'y': 100, 'width': 800, 'height': 160}, 'dpr': 1}}
    sig = observer.view_signature(snapshot)
    assert sig == observer.view_signature(deepcopy(snapshot))
    snapshot['view']['rect']['y'] = 101
    with pytest.raises(ValueError, match='camera/layout'):
        observer.require_fixed_view(sig, snapshot)


def test_cli_accepts_only_readonly_observer_inputs(observer, tmp_path):
    args = observer.parse_cli(['--original', str(tmp_path / 'original'), '--output', str(tmp_path / 'audit'),
        '--server-map', str(tmp_path / 'servers.json'), '--concurrency', '4', '--limit', '8'])
    assert args.concurrency == 4 and args.limit == 8
    with pytest.raises(SystemExit):
        observer.parse_cli(['--original', 'a', '--output', 'b', '--server-map', 'c', '--limit', '0'])
    with pytest.raises(SystemExit):
        observer.parse_cli(['--original', 'a', '--output', 'b', '--server-map', 'c', '--concurrency', '5'])


def test_guard_failure_and_late_case_errors_cannot_be_success(observer):
    assert not observer.observation_passed(geometry=True, pixels=True, restored=True, sdk_identity=True, forbidden_requests=[{'method': 'POST'}], session_projection_verified=True)
    assert not observer.observation_passed(geometry=True, pixels=True, restored=False, sdk_identity=True, forbidden_requests=[], session_projection_verified=True)
    assert not observer.observation_passed(geometry=True, pixels=True, restored=True, sdk_identity=False, forbidden_requests=[], session_projection_verified=True)
    assert observer.observation_passed(geometry=True, pixels=True, restored=True, sdk_identity=True, forbidden_requests=[], session_projection_verified=True)


def test_error_evidence_cannot_expose_sdk_query_secrets(observer):
    value = observer.safe_error(ValueError('failed https://webapi.amap.com/maps?key=SECRET&securityJsCode=CODE token=TOKEN'))
    assert 'SECRET' not in value and 'CODE' not in value and 'TOKEN' not in value


def test_worker_assignment_keeps_each_shard_context_and_every_case_once(observer):
    cases = [{'case_id': f'case-{i}', 'shard': f'shard-{(i % 4) + 1}'} for i in range(12)]
    work = observer.assign_workers(cases, 4)
    assert len(work) == 4
    assert all(len({case['shard'] for case in assigned}) == 1 for assigned in work)
    assert sorted(case['case_id'] for assigned in work for case in assigned) == sorted(case['case_id'] for case in cases)


def test_clone_snapshot_rejects_changed_nonfinal_output(observer, tmp_path):
    row, result, path = original_case(tmp_path)
    result['snapshot']['output_artifacts'].append({'artifact_id': 'art_intermediate', 'type': 'Vector', 'content_hash': 'original'})
    bound = observer.bind_case(row, result, path)
    wrong = deepcopy(result['snapshot']); wrong['output_artifacts'][-1]['content_hash'] = 'changed'
    with pytest.raises(ValueError, match='clone'):
        observer.verify_clone_snapshot(bound, wrong)


def test_camera_signature_rejects_unobservable_sdk_size_or_crop(observer):
    snapshot = {'view': {'center': [118, 32], 'zoom': 12, 'pitch': 0, 'rotation': 0,
        'size': [None, None], 'rect': {'x': 100, 'y': 100, 'width': 800, 'height': 160}, 'dpr': 1}}
    with pytest.raises(ValueError, match='camera/layout'):
        observer.view_signature(snapshot)
    snapshot['view']['size'] = [800, 160]
    snapshot['view']['rect']['width'] = 0
    with pytest.raises(ValueError, match='camera/layout'):
        observer.view_signature(snapshot)


def test_history_projection_header_only_on_local_readonly_session_collection(observer):
    origins = ['http://127.0.0.1:6211', 'http://127.0.0.1:6201']
    request = {'method': 'GET', 'url': origins[0] + '/api/sessions',
               'headers': {'X-User-Id': 'actual-user', 'x-history-observer-session': 'stale'}}
    headers = observer.history_projection_headers(request, origins, 'session-original')
    indexed = {value['name'].lower(): value['value'] for value in headers}
    assert indexed['x-history-observer-session'] == 'session-original'
    assert indexed['x-user-id'] == 'actual-user'
    assert len([value for value in headers if value['name'].lower() == 'x-history-observer-session']) == 1
    for method, url in [('POST', request['url']), ('GET', origins[0] + '/api/sessions/session-a/runs'),
            ('GET', 'https://webapi.amap.com/maps?key=actual-sdk-key'), ('GET', 'http://other.example/api/sessions')]:
        candidate = {**request, 'method': method, 'url': url}
        assert observer.history_projection_headers(candidate, origins, 'session-original') is None


@pytest.mark.asyncio
async def test_interception_task_exception_retrieved_and_persisted_after_done(observer):
    pending, errors = set(), []
    async def failed_protocol_operation():
        raise RuntimeError('CDPSession.send: Protocol error (Fetch.continueRequest): Invalid InterceptionId.')
    task = observer.track_task(failed_protocol_operation(), pending, errors,
        {'case_id': 'case-a', 'method': 'GET', 'url': 'http://127.0.0.1:6211/api/sessions', 'task_kind': 'request_guard'})
    await asyncio.gather(task, return_exceptions=True)
    assert not pending and len(errors) == 1
    assert errors[0]['case_id'] == 'case-a'
    assert errors[0]['classification'] == 'invalid_interception_id'
    assert errors[0]['request_cancellation_confirmed'] is False
    assert errors[0]['gate_failure'] is True


@pytest.mark.asyncio
async def test_cancelled_background_task_is_recorded_not_dropped(observer):
    pending, errors = set(), []
    future = asyncio.get_running_loop().create_future()
    async def wait_for_real_future():
        await future
    task = observer.track_task(wait_for_real_future(), pending, errors, {'task_kind': 'request_guard', 'case_id': 'case-a'})
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not pending and errors[0]['classification'] == 'observer_task_cancelled'
    assert errors[0]['gate_failure'] is True


def test_guard_errors_block_case_and_exact_full_coverage_gate(observer):
    errors = [{'task_kind': 'request_guard', 'error': 'Invalid InterceptionId', 'gate_failure': True}]
    assert not observer.observation_passed(geometry=True, pixels=True, restored=True, sdk_identity=True,
        forbidden_requests=[], background_task_errors=errors, session_projection_verified=True)
    ids = [f'art_{i}' for i in range(1800)]
    records = [{'map_artifact_id': value, 'passed': True} for value in ids]
    result = observer.coverage_gate(ids, records, raster_excluded=200, source_unchanged=True,
        limit=None, background_task_errors=errors, evidence_seal_verified=True)
    assert not result['full_map_gate']
    assert result['background_task_error_count'] == 1


def run_js(function, statement):
    result = subprocess.run(['node', '-e', f'const fn=({function}); {statement}'],
        capture_output=True, text=True, check=True)
    return result.stdout


def test_real_offset_adapter_accepts_strict_array_and_pixel_forms(observer):
    program = observer.SDK_PIXEL_FUNCTION_JS
    statement = """const values=[[-8,-8],{x:-8,y:-8},{getX:()=>-8,getY:()=>-8}];
      const bad=[[1],[1,2,3],[true,2],[NaN,2],[Infinity,2],['1',2]];
      const valid=values.map(fn);const invalid=bad.map(x=>{try{fn(x);return false}catch{return true}});
      process.stdout.write(JSON.stringify({valid,invalid}));"""
    result = json.loads(run_js(program, statement))
    assert result['valid'] == [[-8, -8]] * 3
    assert result['invalid'] == [True] * 6


def test_live_sdk_membership_rejects_added_replaced_or_wrong_type_refs(observer):
    program = observer.SDK_MEMBERSHIP_FUNCTION_JS
    statement = """const a={},b={},extra={};const expected=[a,b],kinds=['Point','Polygon'];
      fn(expected,kinds,[a],[b],[a,b]);
      const invalid=[[[a],[b],[a,b,extra]],[[extra],[b],[extra,b]],[[b],[a],[a,b]],[[a,a],[b],[a,a,b]]]
        .map(v=>{try{fn(expected,kinds,...v);return false}catch{return true}});
      process.stdout.write(JSON.stringify(invalid));"""
    assert json.loads(run_js(program, statement)) == [True] * 4
    assert "map.getAllOverlays('marker')" in observer.SDK_SNAPSHOT_JS
    assert "map.getAllOverlays('polygon')" in observer.SDK_SNAPSHOT_JS


def test_full_original_seal_rejects_changed_db_added_file_or_missing_trace(observer, tmp_path):
    root = tmp_path / 'sealed'; root.mkdir()
    (root / 'runtime.db').write_bytes(b'original-db')
    (root / 'trace.zip').write_bytes(b'original-trace')
    manifest = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in ['runtime.db', 'trace.zip']}
    (root / 'evidence-manifest.json').write_text(json.dumps(manifest), encoding='utf8')
    assert observer.verify_evidence_seal(root)['verified_file_count'] == 2
    (root / 'runtime.db').write_bytes(b'changed-db')
    with pytest.raises((AssertionError, ValueError), match='sealed|seal'):
        observer.verify_evidence_seal(root)
    (root / 'runtime.db').write_bytes(b'original-db')
    extra = root / 'extra.bin'; extra.write_bytes(b'new')
    with pytest.raises((AssertionError, ValueError), match='sealed|seal'):
        observer.verify_evidence_seal(root)
    extra.unlink()
    (root / 'trace.zip').unlink()
    with pytest.raises((AssertionError, ValueError), match='sealed|seal'):
        observer.verify_evidence_seal(root)


def test_complete_map_counts_without_complete_original_seal_never_green(observer):
    ids = [f'art_{i}' for i in range(1800)]
    records = [{'map_artifact_id': value, 'passed': True} for value in ids]
    assert not observer.coverage_gate(ids, records, raster_excluded=200, source_unchanged=True,
        limit=None, evidence_seal_verified=False)['full_map_gate']


def test_late_guard_exception_rewrites_only_matching_observer_record_status(observer):
    records = [{'case_id': 'case-a', 'passed': True}, {'case_id': 'case-b', 'passed': True}]
    errors = [{'case_id': 'case-a', 'task_kind': 'request_guard', 'gate_failure': True}]
    observer.finalize_background_errors(records, errors)
    assert records[0]['passed'] is False and records[0]['background_task_errors'] == errors
    assert records[1]['passed'] is True


@pytest.mark.asyncio
async def test_transient_blank_attempt_retained_then_actual_pixel_oracle_ready(observer):
    masks = observer.build_masks('Point', [50, 50], (100, 100))
    hidden = np.zeros((100, 100, 3), dtype=np.uint8)
    shown = hidden.copy(); shown[masks['core_mask']] = [138, 150, 166]
    attempts = []
    async def independent_synthetic_measurement(index):
        images = [hidden, hidden, hidden, hidden] if index == 0 else [hidden, shown, hidden, shown]
        return images, {'source': 'synthetic unit pixels only', 'index': index}
    result = await observer.wait_for_paint_ready(independent_synthetic_measurement,
        deadline=time.monotonic() + .2, attempts=attempts, core_mask=masks['core_mask'],
        contour_mask=None, allowed_mask=masks['allowed_mask'])
    assert result['passed'] is True and len(attempts) == 2
    assert attempts[0]['paint_readiness'] == 'not_ready' and 'core coverage' in attempts[0]['error']
    assert attempts[1]['paint_readiness'] == 'ready'


@pytest.mark.asyncio
async def test_persistent_blank_rejects_at_same_fixed_deadline(observer):
    masks = observer.build_masks('Point', [50, 50], (100, 100))
    blank = np.zeros((100, 100, 3), dtype=np.uint8)
    attempts = []
    async def independent_blank_measurement(index):
        return [blank] * 4, {'source': 'synthetic unit blank only', 'index': index}
    started = time.monotonic(); deadline = started + .03
    with pytest.raises(ValueError, match='fixed paint readiness deadline'):
        await observer.wait_for_paint_ready(independent_blank_measurement,
            deadline=deadline, attempts=attempts, core_mask=masks['core_mask'],
            contour_mask=None, allowed_mask=masks['allowed_mask'])
    assert time.monotonic() - started < .3
    assert attempts and all(value['paint_readiness'] == 'not_ready' for value in attempts)
    original_count = len(attempts)
    with pytest.raises(ValueError, match='fixed paint readiness deadline'):
        await observer.wait_for_paint_ready(independent_blank_measurement,
            deadline=deadline, attempts=attempts, core_mask=masks['core_mask'],
            contour_mask=None, allowed_mask=masks['allowed_mask'])
    assert len(attempts) == original_count


def projection_reference_fixture():
    recent = [{'id': f'session_{i:032x}', 'title': '新会话', 'created_at': f'created-{i}', 'updated_at': f'updated-{i}'} for i in range(200)]
    selected = {'id': 'session_ffffffffffffffffffffffffffffffff', 'title': '新会话', 'created_at': 'original-created', 'updated_at': 'original-updated'}
    base = {'selector': selected['id'], 'frontend_origin': 'http://127.0.0.1:6211', 'backend_origin': 'http://127.0.0.1:6201',
        'stock_url': 'http://127.0.0.1:6201/api/sessions', 'stock_status': 200,
        'stock_body': json.dumps({'items': recent}).encode(),
        'selected_url': 'http://127.0.0.1:6201/api/sessions/' + selected['id'], 'selected_status': 200,
        'selected_body': json.dumps(selected).encode()}
    response = {'url': 'http://127.0.0.1:6211/api/sessions', 'status': 200,
        'headers': {'X-History-Observer-Policy': 'history-readonly-observer-v2', 'X-History-Observer-Session': selected['id']},
        'body': json.dumps({'items': [selected, *recent]}).encode()}
    return base, response, recent, selected


def test_projection_binds_actual_policy_echo_selected_and_original_recent(observer):
    base, response, recent, selected = projection_reference_fixture()
    reference = observer.make_projection_reference(**base)
    receipt = observer.verify_session_projection(reference, **response)
    assert receipt['passed'] is True
    assert receipt['selector_session_id'] == selected['id']
    assert receipt['selected_actual_item'] == selected
    assert receipt['original_recent_count'] == 200
    assert receipt['original_recent_ids'] == [item['id'] for item in recent]
    assert receipt['projected_count'] == 201
    assert receipt['response_body_sha256'] == hashlib.sha256(response['body']).hexdigest()
    assert receipt['stock_recent_body_sha256'] == hashlib.sha256(base['stock_body']).hexdigest()
    assert len(receipt['original_recent_membership_sha256']) == 64


def test_projection_allows_order_change_and_deduplicates_selected_already_recent(observer):
    base, response, recent, selected = projection_reference_fixture()
    base['selector'] = recent[12]['id']
    base['selected_url'] = base['backend_origin'] + '/api/sessions/' + base['selector']
    base['selected_body'] = json.dumps(recent[12]).encode()
    response['headers']['X-History-Observer-Session'] = base['selector']
    response['body'] = json.dumps({'items': list(reversed(recent))}).encode()
    receipt = observer.verify_session_projection(observer.make_projection_reference(**base), **response)
    assert receipt['projected_count'] == 200 and receipt['passed']


@pytest.mark.parametrize('change', ['missing_policy', 'wrong_policy', 'missing_echo', 'wrong_echo', 'http_error',
    'wrong_endpoint', 'changed_selected', 'changed_recent', 'missing_recent', 'extra_item', 'duplicate_item'])
def test_projection_rejects_unproved_policy_wrong_case_or_changed_rows(observer, change):
    base, response, recent, selected = projection_reference_fixture()
    reference = observer.make_projection_reference(**base)
    items = [deepcopy(selected), *deepcopy(recent)]
    if change == 'missing_policy': response['headers'].pop('X-History-Observer-Policy')
    if change == 'wrong_policy': response['headers']['X-History-Observer-Policy'] = 'stock-ui'
    if change == 'missing_echo': response['headers'].pop('X-History-Observer-Session')
    if change == 'wrong_echo': response['headers']['X-History-Observer-Session'] = recent[0]['id']
    if change == 'http_error': response['status'] = 404
    if change == 'wrong_endpoint': response['url'] = base['backend_origin'] + '/api/sessions'
    if change == 'changed_selected': items[0]['updated_at'] = 'fabricated'
    if change == 'changed_recent': items[10]['title'] = 'fabricated'
    if change == 'missing_recent': items.pop()
    if change == 'extra_item': items.append({**selected, 'id': 'session_extra'})
    if change == 'duplicate_item': items.append(deepcopy(items[1]))
    response['body'] = json.dumps({'items': items}).encode()
    with pytest.raises(ValueError, match='projection'):
        observer.verify_session_projection(reference, **response)


@pytest.mark.parametrize('change', ['stock_wrong_endpoint', 'stock_wrong_count', 'stock_duplicates', 'selected_wrong_endpoint', 'selected_wrong_id'])
def test_projection_reference_rejects_unmatched_stock_or_selected_endpoint(observer, change):
    base, response, recent, selected = projection_reference_fixture()
    if change == 'stock_wrong_endpoint': base['stock_url'] += '?selector=other'
    if change == 'stock_wrong_count': base['stock_body'] = json.dumps({'items': recent[:-1]}).encode()
    if change == 'stock_duplicates': base['stock_body'] = json.dumps({'items': [*recent[:-1], recent[0]]}).encode()
    if change == 'selected_wrong_endpoint': base['selected_url'] = base['backend_origin'] + '/api/sessions/' + recent[0]['id']
    if change == 'selected_wrong_id': base['selected_body'] = json.dumps(recent[0]).encode()
    with pytest.raises(ValueError, match='projection'):
        observer.make_projection_reference(**base)


def test_projection_provenance_requires_actual_matching_verified_response(observer):
    base, response, recent, selected = projection_reference_fixture()
    receipt = observer.verify_session_projection(observer.make_projection_reference(**base), **response)
    assert observer.projection_observations_verified([receipt], selected['id'])
    assert not observer.projection_observations_verified([], selected['id'])
    assert not observer.projection_observations_verified([{'passed': True}], selected['id'])
    bad = deepcopy(receipt); bad['passed'] = False
    assert not observer.projection_observations_verified([receipt, bad], selected['id'])
    assert not observer.projection_observations_verified([receipt], recent[0]['id'])
    assert not observer.observation_passed(geometry=True, pixels=True, restored=True, sdk_identity=True,
        forbidden_requests=[], session_projection_verified=False)


def test_polygon_default_stroke_opacity_is_exact_observed_point_nine(observer):
    poly = {'kind': 'Polygon', 'options': {'fillColor': '#8a96a6', 'strokeColor': '#8a96a6',
        'fillOpacity': .24, 'strokeWeight': 1.5, 'strokeOpacity': .9}}
    observer.validate_overlay_style(poly)
    for value in [.1, 1, None, '0.9', False]:
        bad = deepcopy(poly); bad['options']['strokeOpacity'] = value
        with pytest.raises(ValueError, match='style'):
            observer.validate_overlay_style(bad)
    missing = deepcopy(poly); missing['options'].pop('strokeOpacity')
    with pytest.raises(ValueError, match='style'):
        observer.validate_overlay_style(missing)


@pytest.mark.asyncio
async def test_sdk_redirect_records_chain_without_reading_unavailable_body(observer):
    async def must_not_read_redirect_body():
        raise AssertionError('redirect response body must not be requested')
    source = 'https://webapi.amap.com/maps?v=2.0&key=SECRET'
    target = '/maps/main.js?v=2.3.5.6&key=SECRET&token=TOKEN'
    result = await observer.observe_sdk_script_response(url=source, status=302,
        headers={'Location': target}, redirect_chain=[source], body_reader=must_not_read_redirect_body)
    assert result['kind'] == 'redirect' and result['status'] == 302
    assert result['body_read'] is False and result['sha256'] is None
    assert result['redirect_chain'] == [observer.redact_url(source)]
    assert 'SECRET' not in result['redirect_target'] and 'TOKEN' not in result['redirect_target']


@pytest.mark.asyncio
async def test_final_sdk_loaded_200_requires_real_body_hash_and_redirect_chain(observer):
    source = 'https://webapi.amap.com/maps?v=2.0&key=SECRET'
    target = 'https://webapi.amap.com/maps/main.js?v=2.3.5.6&key=SECRET'
    data = b'unit transport control bytes only; not an actual SDK success'
    async def read_unit_bytes():
        return data
    result = await observer.observe_sdk_script_response(url=target, status=200, headers={},
        redirect_chain=[source, target], body_reader=read_unit_bytes)
    assert result['kind'] == 'loaded' and result['body_read'] is True
    assert result['sha256'] == hashlib.sha256(data).hexdigest() and result['size_bytes'] == len(data)
    assert result['redirect_chain'] == [observer.redact_url(source), observer.redact_url(target)]


@pytest.mark.asyncio
async def test_sdk_final_error_or_body_exception_cannot_be_ignored(observer):
    async def body_unavailable():
        raise RuntimeError('final response body unavailable')
    url = 'https://webapi.amap.com/maps/main.js?v=2.0'
    with pytest.raises(ValueError, match='SDK'):
        await observer.observe_sdk_script_response(url=url, status=404, headers={},
            redirect_chain=[url], body_reader=body_unavailable)
    with pytest.raises(RuntimeError, match='final response body'):
        await observer.observe_sdk_script_response(url=url, status=200, headers={},
            redirect_chain=[url], body_reader=body_unavailable)
    with pytest.raises(ValueError, match='redirect'):
        await observer.observe_sdk_script_response(url=url, status=302, headers={},
            redirect_chain=[url], body_reader=body_unavailable)


@pytest.mark.asyncio
async def test_sdk_body_scope_is_received_case_not_case_after_await(observer):
    scope = {'case_id': 'case-old', 'case_epoch': 'epoch-old',
        'cdp_scope_id': 'cdp-1', 'page_target_id': 'page-1'}
    received = deepcopy(scope)
    async def delayed_transport_control():
        scope.update({'case_id': 'case-new', 'case_epoch': 'epoch-new'})
        return b'unit delayed body control only; not an actual SDK success'
    url = 'https://webapi.amap.com/maps/main.js?v=2.0'
    observed = await observer.observe_sdk_script_response(url=url, status=200, headers={},
        redirect_chain=[url], body_reader=delayed_transport_control, observation_scope=scope)
    assert {name: observed[name] for name in received} == received
    assert scope['case_id'] == 'case-new'


def test_per_case_sdk_receipts_mark_real_context_inheritance_and_do_not_share_redirects(observer):
    old = {'case_id': 'case-old', 'case_epoch': 'epoch-old',
        'cdp_scope_id': 'cdp-1', 'page_target_id': 'page-1'}
    current = {**old, 'case_id': 'case-new', 'case_epoch': 'epoch-new'}
    resources = [{'kind': 'loaded', 'status': 200, 'url': 'https://webapi.amap.com/main.js',
        'sha256': 'a' * 64, 'size_bytes': 10, **old}]
    redirects = [{'kind': 'redirect', 'status': 302, **old},
        {'kind': 'redirect', 'status': 302, **current}]
    receipts = observer.sdk_receipts_for_case(resources, redirects, current)
    inherited = receipts['sdk_resources'][0]
    assert inherited['case_id'] == 'case-old' and inherited['case_epoch'] == 'epoch-old'
    assert inherited['used_in_case_id'] == 'case-new' and inherited['used_in_case_epoch'] == 'epoch-new'
    assert inherited['inherited_from_same_cdp_context'] is True
    assert receipts['sdk_redirects'] == [redirects[1]]
    redirects[1]['case_id'] = 'case-later'
    resources[0]['sha256'] = 'b' * 64
    assert receipts['sdk_redirects'][0]['case_id'] == 'case-new'
    assert inherited['sha256'] == 'a' * 64


def canceled_read_fixture():
    origins = ['http://127.0.0.1:6211', 'http://127.0.0.1:6201']
    paused = {'case_id': 'case-original', 'case_epoch': 'epoch-1', 'cdp_scope_id': 'cdp-1', 'page_target_id': 'page-1',
        'session_id': 'session_original', 'run_id': 'run_original', 'artifact_ids': ['art_original'],
        'request_id': 'fetch-1', 'network_id': 'network-1', 'method': 'GET',
        'url': origins[0] + '/api/sessions/session_original/messages',
        'operation': 'Fetch.continueRequest', 'received_sequence': 1}
    error = {**deepcopy(paused), 'task_kind': 'request_guard', 'gate_failure': True,
        'classification': 'invalid_interception_id', 'request_cancellation_confirmed': False,
        'error': 'Error: CDPSession.send: Protocol error (Fetch.continueRequest): Invalid InterceptionId.'}
    failure = {key: paused[key] for key in ['case_id', 'case_epoch', 'cdp_scope_id', 'page_target_id']}
    failure.update({'requestId': 'network-1', 'network_id': 'network-1', 'canceled': True,
        'errorText': 'net::ERR_ABORTED', 'type': 'Fetch', 'blockedReason': None,
        'timestamp': -5000, 'received_sequence': 2})
    failure['native_event'] = {key: failure[key] for key in ['requestId', 'canceled', 'errorText', 'type', 'blockedReason', 'timestamp']}
    failure['cors_status_field_present'] = False
    failure['blocked_reason_field_present'] = True
    return origins, paused, error, failure


@pytest.mark.parametrize('event_kind,record', [
    ('start', {'request_id': 'different-network'}),
    ('paused', {'request_id': 'different-fetch', 'network_id': 'different-network'}),
    ('native', {'requestId': 'different-network', 'network_id': 'different-alias'})])
def test_relation_gating_only_skips_provably_unrelated_ids(observer, event_kind, record):
    _, _, error, _ = canceled_read_fixture()
    record = {'cdp_scope_id': 'cdp-1', 'page_target_id': 'page-1', **record}
    assert observer.event_relation_requires_full(event_kind, record, [error]) is False
    assert observer.event_relation_requires_full(event_kind, record, []) is False


@pytest.mark.parametrize('event_kind,field,value', [
    ('start', 'request_id', 'network-1'), ('paused', 'request_id', 'fetch-1'),
    ('paused', 'network_id', 'network-1'), ('native', 'requestId', 'network-1'),
    ('native', 'network_id', 'network-1')])
@pytest.mark.parametrize('classification', ['invalid_interception_id', 'confirmed_canceled_read', 'unknown'])
def test_relation_gating_preserves_every_or_alias_without_case_type_or_state_filter(observer, event_kind, field, value, classification):
    _, _, error, _ = canceled_read_fixture()
    error['classification'] = classification
    error['task_kind'] = 'any-task-kind'
    record = {'cdp_scope_id': 'foreign-cdp', 'page_target_id': 'foreign-page', 'case_id': 'other-case',
        'case_epoch': 'other-epoch', 'method': 'POST', 'resource_type': 'Image', 'url': 'https://other.invalid',
        'request_id': 'unrelated-fetch', 'requestId': 'unrelated-native', 'network_id': 'unrelated-network', field: value}
    assert observer.event_relation_requires_full(event_kind, record, [error]) is True


@pytest.mark.parametrize('change', ['unknown_kind', 'missing_event_id', 'null_event_id', 'bool_event_id',
    'list_event_id', 'str_subclass_event_id', 'missing_native_alias', 'missing_event_scope',
    'missing_error_id', 'bad_error_id', 'missing_error_scope', 'bad_error_record', 'bad_event_record', 'bad_errors'])
def test_relation_gating_unknown_or_uncomparable_fields_forces_full(observer, change):
    _, _, error, _ = canceled_read_fixture()
    kind = 'paused'
    record = {'cdp_scope_id': 'cdp-1', 'page_target_id': 'page-1', 'request_id': 'unrelated-fetch', 'network_id': 'unrelated-network'}
    errors = [error]
    class UnsafeString(str):
        def __eq__(self, other):
            raise AssertionError('unknown value must never participate in equality')
    if change == 'unknown_kind': kind = 'unknown'
    elif change == 'missing_event_id': record.pop('request_id')
    elif change == 'null_event_id': record['network_id'] = None
    elif change == 'bool_event_id': record['network_id'] = False
    elif change == 'list_event_id': record['network_id'] = []
    elif change == 'str_subclass_event_id': record['network_id'] = UnsafeString('unrelated')
    elif change == 'missing_native_alias': kind = 'native'; record = {**record, 'requestId': 'unrelated'}; record.pop('network_id')
    elif change == 'missing_event_scope': record.pop('page_target_id')
    elif change == 'missing_error_id': error.pop('network_id')
    elif change == 'bad_error_id': error['request_id'] = 1
    elif change == 'missing_error_scope': error.pop('cdp_scope_id')
    elif change == 'bad_error_record': errors = [None]
    elif change == 'bad_event_record': record = None
    elif change == 'bad_errors': errors = None
    assert observer.event_relation_requires_full(kind, record, errors) is True


@pytest.mark.asyncio
async def test_task_exception_after_last_native_event_immediately_full_replays_all_authority(observer, tmp_path):
    origins, paused, template, native, start = early_start_fixture()
    instance = observer.ChromeObserver(None, {}, tmp_path)
    instance.origins = origins
    instance.paused_requests = [paused]; instance.network_loading_failures = [native]
    instance.network_request_starts = [start]
    async def failed_read():
        raise RuntimeError('CDPSession.send: Protocol error (Fetch.continueRequest): Invalid InterceptionId.')
    coroutine = failed_read()
    try:
        task = observer.track_task(coroutine, instance.guard_tasks, instance.background_task_errors,
            {**paused, 'task_kind': 'request_guard'}, on_error=instance.reconcile_network_diagnostics)
    except TypeError:
        coroutine.close()
        raise
    await asyncio.gather(task, return_exceptions=True)
    assert not instance.guard_tasks and len(instance.background_task_errors) == 1
    actual = instance.background_task_errors[0]
    assert actual['classification'] == 'confirmed_canceled_read'
    assert actual['derived_native_scope']['case_epoch'] == paused['case_epoch']
    assert native['case_epoch'] is None and actual['request_success_claimed'] is False


@pytest.mark.asyncio
async def test_task_error_reconcile_callback_exception_is_durable_blocking(observer):
    pending, errors = set(), []
    async def failed_read():
        raise RuntimeError('Invalid InterceptionId')
    def broken_callback():
        raise RuntimeError('synthetic callback failure')
    coroutine = failed_read()
    try:
        task = observer.track_task(coroutine, pending, errors, {'task_kind': 'request_guard', 'case_id': 'case-a'},
            on_error=broken_callback)
    except TypeError:
        coroutine.close()
        raise
    await asyncio.gather(task, return_exceptions=True)
    assert not pending and len(errors) == 2
    assert errors[-1]['classification'] == 'observer_error_callback_exception'
    assert errors[-1]['gate_failure'] is True and 'synthetic callback failure' in errors[-1]['error']
    assert observer.gate_task_errors(errors) == errors


@pytest.mark.parametrize('late_kind', ['same_fetch_other_network', 'same_network_other_fetch', 'duplicate_native',
    'native_request_alias', 'native_network_alias', 'cross_case', 'contradictory_native'])
def test_relation_gated_late_evidence_revokes_green_and_truncated_inputs_never_resurrect(observer, tmp_path, late_kind):
    origins, paused, error, native = canceled_read_fixture()
    instance = observer.ChromeObserver(None, {}, tmp_path); instance.origins = origins
    instance.paused_requests = [paused]; instance.network_loading_failures = [native]
    instance.background_task_errors = [error]
    instance.reconcile_network_diagnostics()
    assert observer.gate_task_errors([error]) == []
    if late_kind in ('same_fetch_other_network', 'same_network_other_fetch', 'cross_case'):
        kind, late = 'paused', deepcopy(paused)
        if late_kind == 'same_fetch_other_network': late['network_id'] = 'other-network'
        else: late['request_id'] = 'other-fetch'
        if late_kind == 'cross_case': late.update({'case_id': 'other-case', 'case_epoch': 'other-epoch'})
        instance.paused_requests.append(late)
    else:
        kind, late = 'native', deepcopy(native)
        if late_kind == 'native_request_alias': late['network_id'] = 'foreign-alias'
        if late_kind == 'native_network_alias': late['requestId'] = 'foreign-alias'
        if late_kind == 'contradictory_native':
            late['canceled'] = late['native_event']['canceled'] = False
        instance.network_loading_failures.append(late)
    instance.reconcile_network_event(kind, late)
    assert observer.gate_task_errors([error]) == [error]
    assert error['cancellation_confirmation_revoked'] is True
    assert len(error['authoritative_paused_records']) == (2 if kind == 'paused' else 1)
    assert len(error['authoritative_native_records']) == (2 if kind == 'native' else 1)
    instance.paused_requests = [paused]; instance.network_loading_failures = [native]
    instance.reconcile_network_diagnostics()
    assert observer.gate_task_errors([error]) == [error]


async def event_callback_control(observer, tmp_path):
    """No transport/browser: exercise the real observer callback registration."""
    class ProtocolBoundary:
        def __init__(self):
            self.callbacks = {}; self.continue_error = False
        def on(self, name, callback):
            self.callbacks[name] = callback
        async def send(self, name, params=None):
            if name == 'Target.getTargetInfo': return {'targetInfo': {'targetId': 'page-1'}}
            if name == 'Fetch.continueRequest' and self.continue_error:
                raise RuntimeError('CDPSession.send: Protocol error (Fetch.continueRequest): Invalid InterceptionId.')
            return {}
    class PageBoundary:
        def __init__(self):
            self.callbacks = {}
        def set_default_timeout(self, value):
            pass
        def on(self, name, callback):
            self.callbacks[name] = callback
    class ContextBoundary:
        async def add_init_script(self, value):
            pass
        async def new_page(self): return PageBoundary()
        async def new_cdp_session(self, page): return protocol
        async def close(self):
            pass
    class BrowserBoundary:
        async def new_context(self, **kwargs): return ContextBoundary()
    protocol = ProtocolBoundary()
    instance = observer.ChromeObserver(BrowserBoundary(),
        {'frontend_url': 'http://127.0.0.1:6211', 'backend_url': 'http://127.0.0.1:6201'}, tmp_path)
    instance.cdp_scope_id = 'cdp-1'
    await instance.initialize({'session_id': 'session_original', 'user_id': 'unit-no-network'})
    return instance, protocol


@pytest.mark.asyncio
@pytest.mark.parametrize('observed_request', [True, False])
async def test_actual_sdk_response_callback_uses_request_owner_or_fails_closed(observer, tmp_path, observed_request):
    instance, protocol = await event_callback_control(observer, tmp_path)
    class Request:
        url = 'https://restapi.amap.com/v3/log/init?key=synthetic-not-live'
        method, resource_type, redirected_from = 'GET', 'script', None
    request = Request()
    instance.current_case, instance.current_case_scope = 'case19', {'case_epoch': 'epoch19'}
    if observed_request:
        instance.page.callbacks['request'](request)
    instance.current_case, instance.current_case_scope = 'case20', {'case_epoch': 'epoch20'}
    calls = []
    async def native(url, status, scope, **kwargs):
        calls.append(scope)
        raise ValueError('intentional synthetic stop before network body')
    instance.native_sdk_body = native
    response = SimpleNamespace(url=request.url, request=request, status=200)
    instance.page.callbacks['response'](response)
    await instance.drain_tasks()
    if observed_request:
        assert len(calls) == 1 and calls[0]['case_id'] == 'case19' and calls[0]['case_epoch'] == 'epoch19'
    else:
        assert not calls and 'originating Request' in instance.background_task_errors[0]['error']
    assert observer.gate_task_errors(instance.background_task_errors)


@pytest.mark.asyncio
async def test_actual_event_callback_appends_unrelated_raw_before_skip_and_gates_stay_full(observer, tmp_path):
    instance, protocol = await event_callback_control(observer, tmp_path)
    origins, paused, error, native = canceled_read_fixture()
    instance.paused_requests = [paused]; instance.network_loading_failures = [native]
    instance.background_task_errors = [error]; instance.reconcile_network_diagnostics()
    full_before = instance.network_reconciliation_counts['full_calls']
    protocol.callbacks['Network.requestWillBeSent']({'requestId': 'unrelated-sdk-1',
        'request': {'method': 'GET', 'url': 'https://webapi.amap.com/unit?key=UNIT_SECRET'},
        'frameId': 'frame-unit', 'loaderId': 'loader-unit', 'type': 'Image', 'timestamp': -99})
    assert instance.network_request_starts[-1]['request_id'] == 'unrelated-sdk-1'
    assert 'UNIT_SECRET' not in instance.network_request_starts[-1]['url']
    assert instance.network_reconciliation_counts['full_calls'] == full_before
    assert instance.network_reconciliation_counts['event_skips'] == 1
    await instance.drain_tasks()
    assert instance.network_reconciliation_counts['full_calls'] == full_before+1
    await instance.close()
    assert instance.network_reconciliation_counts['full_calls'] >= full_before+4


@pytest.mark.asyncio
async def test_actual_callback_early_native_then_late_task_error_uses_complete_raw_ledgers(observer, tmp_path):
    instance, protocol = await event_callback_control(observer, tmp_path)
    origins, paused, error, native, start = early_start_fixture()
    instance.current_case = paused['case_id']; instance.current_session = paused['session_id']
    instance.current_case_scope = {key: paused[key] for key in ('case_epoch', 'session_id', 'run_id', 'artifact_ids')}
    protocol.callbacks['Network.requestWillBeSent']({'requestId': paused['network_id'],
        'request': {'method': 'GET', 'url': paused['url']}, 'frameId': 'frame-1', 'loaderId': 'loader-1', 'type': 'Fetch'})
    protocol.callbacks['Network.loadingFailed'](deepcopy(native['native_event']))
    assert instance.network_loading_failures[0]['case_epoch'] is None
    protocol.continue_error = True
    protocol.callbacks['Fetch.requestPaused']({'requestId': paused['request_id'], 'networkId': paused['network_id'],
        'frameId': 'frame-1', 'request': {'method': 'GET', 'url': paused['url']}})
    await asyncio.gather(*tuple(instance.guard_tasks), return_exceptions=True)
    actual = instance.background_task_errors[0]
    assert actual['classification'] == 'confirmed_canceled_read'
    assert actual['derived_native_scope']['case_epoch'] == paused['case_epoch']
    assert observer.gate_task_errors(instance.background_task_errors) == []
    assert instance.network_loading_failures[0]['case_epoch'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('event_name,event', [('Network.requestWillBeSent', {}), ('Network.loadingFailed', None),
    ('Fetch.requestPaused', {})])
async def test_native_and_fetch_callback_exceptions_are_durable_fail_closed(observer, tmp_path, event_name, event):
    instance, protocol = await event_callback_control(observer, tmp_path)
    protocol.callbacks[event_name](event)
    assert instance.background_task_errors
    assert instance.background_task_errors[-1]['native_event_name'] == event_name
    assert instance.background_task_errors[-1]['gate_failure'] is True
    assert instance.network_reconciliation_counts['full_calls'] >= 1
    assert observer.gate_task_errors(instance.background_task_errors)


def test_callback_diagnostic_formatting_failure_is_not_an_unrecorded_exception(observer, tmp_path):
    class UnprintableError(RuntimeError):
        def __str__(self):
            raise AssertionError('synthetic exception formatter failed')
    instance = observer.ChromeObserver(None, {}, tmp_path)
    instance.origins = ['http://127.0.0.1:6211']
    instance.record_callback_error(UnprintableError(), 'unit-callback')
    assert instance.background_task_errors[-1]['classification'] == 'observer_native_event_exception'
    assert 'UnprintableError' in instance.background_task_errors[-1]['error']
    assert instance.network_reconciliation_counts['full_calls'] == 1
    assert observer.gate_task_errors(instance.background_task_errors)


@pytest.mark.asyncio
async def test_task_unprintable_original_error_is_preserved_before_immediate_full_replay(observer, tmp_path):
    class OriginalUnprintableTaskError(RuntimeError):
        def __str__(self):
            raise ValueError('synthetic formatter error, not the original task error')
    instance = observer.ChromeObserver(None, {}, tmp_path); instance.origins = []
    async def failed_operation():
        raise OriginalUnprintableTaskError()
    task = observer.track_task(failed_operation(), instance.guard_tasks, instance.background_task_errors,
        {'case_id': 'unit-negative', 'task_kind': 'request_guard'}, on_error=instance.reconcile_network_diagnostics)
    await asyncio.gather(task, return_exceptions=True)
    assert not instance.guard_tasks and len(instance.background_task_errors) == 1
    error = instance.background_task_errors[0]
    assert error['error'] == 'OriginalUnprintableTaskError: exception formatting unavailable'
    assert error['classification'] == 'observer_background_task_exception'
    assert error['gate_failure'] is True and error['request_cancellation_confirmed'] is False
    assert instance.network_reconciliation_counts['full_calls'] == 1
    assert observer.gate_task_errors(instance.background_task_errors) == [error]


@pytest.mark.asyncio
@pytest.mark.parametrize('exception,classification', [
    (RuntimeError('CDPSession.send: Protocol error (Fetch.continueRequest): Invalid InterceptionId.'), 'invalid_interception_id'),
    (ValueError('synthetic normal formatting error'), 'observer_background_task_exception'),
    (KeyError('unit-field'), 'observer_background_task_exception')])
async def test_normal_task_exception_entire_json_receipt_is_unchanged(observer, exception, classification):
    pending, errors, callback_records = set(), [], []
    metadata = {'case_id': 'unit-negative', 'task_kind': 'request_guard', 'method': 'GET', 'request_id': 'fetch-unit'}
    async def failed_operation():
        raise exception
    task = observer.track_task(failed_operation(), pending, errors, metadata,
        on_error=lambda: callback_records.append(deepcopy(errors[-1])))
    await asyncio.gather(task, return_exceptions=True)
    expected = {**metadata, 'error': observer.safe_error(exception), 'classification': classification,
        'request_cancellation_confirmed': False, 'gate_failure': True}
    assert errors == callback_records == [expected]
    assert json.dumps(errors, sort_keys=True) == json.dumps([expected], sort_keys=True)


def test_related_event_always_replays_all_errors_and_all_original_ledger_objects(observer, monkeypatch, tmp_path):
    origins, paused, error, native = canceled_read_fixture()
    second_error = deepcopy(error); second_error.update({'request_id': 'fetch-2', 'network_id': 'network-2'})
    instance = observer.ChromeObserver(None, {}, tmp_path); instance.origins = origins
    instance.background_task_errors = [error, second_error]
    instance.paused_requests = [paused]
    instance.network_loading_failures = [native]
    calls = []
    original_replay = observer.reconcile_canceled_read_errors
    def inspect_full(errors, paused_ledger, native_ledger, allowed, *, request_starts):
        assert errors is instance.background_task_errors and paused_ledger is instance.paused_requests
        assert native_ledger is instance.network_loading_failures and request_starts is instance.network_request_starts
        calls.append(len(errors))
        return original_replay(errors, paused_ledger, native_ledger, allowed, request_starts=request_starts)
    monkeypatch.setattr(observer, 'reconcile_canceled_read_errors', inspect_full)
    instance.reconcile_network_event('native', native)
    assert calls == [2]
    assert error['classification'] == 'confirmed_canceled_read' and second_error['gate_failure'] is True


def test_only_native_identity_bound_cancelled_local_read_is_nonblocking(observer):
    origins, paused, error, failure = canceled_read_fixture()
    original_exception = error['error']
    observer.reconcile_canceled_read_errors([error], [paused], [failure], origins)
    assert error['classification'] == 'confirmed_canceled_read'
    assert error['gate_failure'] is False and error['request_cancellation_confirmed'] is True
    assert error['original_interception_exception'] == original_exception
    assert error['native_loading_failed'] == failure and error['fetch_request_paused'] == paused
    assert observer.gate_task_errors([error]) == []
    records = [{'case_id': 'case-original', 'passed': True}]
    observer.finalize_background_errors(records, [error])
    assert records[0]['passed'] is True and records[0]['background_task_errors'] == [error]


@pytest.mark.parametrize('change', ['missing_network_id', 'wrong_fetch_id', 'wrong_network_id', 'wrong_case',
    'wrong_epoch', 'wrong_cdp', 'wrong_page', 'post', 'provider', 'wrong_session', 'other_operation',
    'not_cancelled', 'cancelled_integer', 'wrong_error_text', 'not_fetch', 'blocked', 'cors',
    'ambiguous_paused', 'ambiguous_failure', 'not_request_guard'])
def test_unproved_or_different_cancel_event_remains_blocking(observer, change):
    origins, paused, error, failure = canceled_read_fixture()
    paused_events, failures = [paused], [failure]
    if change == 'missing_network_id': error.pop('network_id')
    if change == 'wrong_fetch_id': error['request_id'] = 'other-fetch'
    if change == 'wrong_network_id': failure['requestId'] = failure['network_id'] = 'other-network'
    if change == 'wrong_case': failure['case_id'] = 'other-case'
    if change == 'wrong_epoch': failure['case_epoch'] = 'other-epoch'
    if change == 'wrong_cdp': failure['cdp_scope_id'] = 'other-cdp'
    if change == 'wrong_page': failure['page_target_id'] = 'other-page'
    if change == 'post': error['method'] = paused['method'] = 'POST'
    if change == 'provider': error['url'] = paused['url'] = 'https://api.deepseek.com/v1/chat/completions'
    if change == 'wrong_session': error['url'] = paused['url'] = origins[0] + '/api/sessions/session_other/messages'
    if change == 'other_operation': error['operation'] = paused['operation'] = 'Fetch.failRequest'
    if change == 'not_cancelled': failure['canceled'] = False
    if change == 'cancelled_integer': failure['canceled'] = 1
    if change == 'wrong_error_text': failure['errorText'] = 'net::ERR_FAILED'
    if change == 'not_fetch': failure['type'] = 'Script'
    if change == 'blocked': failure['blockedReason'] = 'inspector'
    if change == 'cors': failure['corsErrorStatus'] = {'corsError': 'InvalidResponse'}
    if change == 'ambiguous_paused': paused_events.append(deepcopy(paused))
    if change == 'ambiguous_failure': failures.append(deepcopy(failure))
    if change == 'not_request_guard': error['task_kind'] = 'sdk_resource_body'
    observer.reconcile_canceled_read_errors([error], paused_events, failures, origins)
    assert error['gate_failure'] is True and error['classification'] == 'invalid_interception_id'
    assert observer.gate_task_errors([error]) == [error]


def test_late_native_cancel_event_can_be_correlated_without_timing_guess(observer):
    origins, paused, error, failure = canceled_read_fixture()
    observer.reconcile_canceled_read_errors([error], [paused], [], origins)
    assert error['gate_failure'] is True
    observer.reconcile_canceled_read_errors([error], [paused], [failure], origins)
    assert error['gate_failure'] is False
    assert error['native_loading_failed']['timestamp'] == -5000


def test_cancel_diagnostics_do_not_replace_required_paint_or_projection(observer):
    origins, paused, error, failure = canceled_read_fixture()
    observer.reconcile_canceled_read_errors([error], [paused], [failure], origins)
    assert observer.observation_passed(geometry=True, pixels=True, restored=True, sdk_identity=True,
        forbidden_requests=[], background_task_errors=[error], session_projection_verified=True)
    assert not observer.observation_passed(geometry=True, pixels=False, restored=True, sdk_identity=True,
        forbidden_requests=[], background_task_errors=[error], session_projection_verified=True)
    assert not observer.observation_passed(geometry=True, pixels=True, restored=True, sdk_identity=True,
        forbidden_requests=[], background_task_errors=[error], session_projection_verified=False)
    ids = [f'art_{i}' for i in range(1800)]
    records = [{'map_artifact_id': value, 'passed': True} for value in ids]
    coverage = observer.coverage_gate(ids, records, raster_excluded=200, source_unchanged=True,
        limit=None, evidence_seal_verified=True, background_task_errors=[error])
    assert coverage['full_map_gate'] and coverage['blocking_background_task_error_count'] == 0
    assert coverage['confirmed_canceled_read_count'] == 1


@pytest.mark.parametrize('record', [{'gate_failure': False},
    {'gate_failure': False, 'classification': 'confirmed_canceled_read'},
    {'gate_failure': False, 'classification': 'invalid_interception_id'},
    {'gate_failure': False, 'classification': 'observer_background_task_exception'}])
def test_foreign_false_flag_without_complete_revalidated_proof_is_blocking(observer, record):
    assert observer.gate_task_errors([record]) == [record]


@pytest.mark.parametrize('change', ['native_canceled_false', 'native_cors', 'missing_cors_presence', 'fake_provider_origin'])
def test_confirmed_read_proof_is_revalidated_at_every_gate(observer, change):
    origins, paused, error, failure = canceled_read_fixture()
    observer.reconcile_canceled_read_errors([error], [paused], [failure], origins)
    assert error['gate_failure'] is False
    if change == 'native_canceled_false': error['native_loading_failed']['native_event']['canceled'] = False
    if change == 'native_cors': error['native_loading_failed']['native_event']['corsErrorStatus'] = {'corsError': 'Failed'}
    if change == 'missing_cors_presence': error['native_loading_failed'].pop('cors_status_field_present')
    if change == 'fake_provider_origin':
        error['allowed_local_origins'] = ['https://api.deepseek.com']
        error['url'] = error['fetch_request_paused']['url'] = 'https://api.deepseek.com/api/sessions/session_original/messages'
    assert observer.gate_task_errors([error]) == [error]


@pytest.mark.parametrize('late_kind', ['second_fetch', 'contradictory_native', 'duplicate_native', 'cross_case_fetch', 'cross_case_native'])
def test_late_ambiguity_revokes_prior_confirmed_read_using_full_ledgers(observer, late_kind):
    origins, paused, error, failure = canceled_read_fixture()
    paused_ledger, native_ledger = [paused], [failure]
    original_exception = error['error']
    observer.reconcile_canceled_read_errors([error], paused_ledger, native_ledger, origins)
    assert observer.gate_task_errors([error]) == []
    if late_kind in {'second_fetch', 'cross_case_fetch'}:
        late = deepcopy(paused); late['request_id'] = 'fetch-2'; late['received_sequence'] = 3
        if late_kind == 'cross_case_fetch': late.update({'case_id': 'other-case', 'case_epoch': 'other-epoch'})
        paused_ledger.append(late)
    else:
        late = deepcopy(failure); late['received_sequence'] = 3
        if late_kind == 'contradictory_native':
            late['canceled'] = False; late['native_event']['canceled'] = False
        if late_kind == 'cross_case_native': late.update({'case_id': 'other-case', 'case_epoch': 'other-epoch'})
        native_ledger.append(late)
    observer.reconcile_canceled_read_errors([error], paused_ledger, native_ledger, origins)
    assert observer.gate_task_errors([error]) == [error]
    assert error['gate_failure'] is True and error['cancellation_confirmation_revoked'] is True
    assert error['original_interception_exception'] == original_exception
    assert len(error['authoritative_paused_records']) == len(paused_ledger)
    assert len(error['authoritative_native_records']) == len(native_ledger)
    assert error['cancellation_reconciliation_history'][-1]['state'] == 'confirmation_revoked'
    # A later accidentally truncated input cannot erase already observed facts.
    observer.reconcile_canceled_read_errors([error], [paused], [failure], origins)
    assert observer.gate_task_errors([error]) == [error]
    records = [{'case_id': 'case-original', 'passed': True}]
    observer.finalize_background_errors(records, [error])
    assert not records[0]['passed']


def test_legal_late_unique_evidence_confirms_but_unrelated_network_cannot_change_proof(observer):
    origins, paused, error, failure = canceled_read_fixture()
    observer.reconcile_canceled_read_errors([error], [paused], [], origins)
    assert observer.gate_task_errors([error]) == [error]
    observer.reconcile_canceled_read_errors([error], [paused], [failure], origins)
    assert observer.gate_task_errors([error]) == []
    other_paused = deepcopy(paused); other_paused.update({'request_id': 'other-fetch', 'network_id': 'other-network'})
    other_native = deepcopy(failure); other_native.update({'requestId': 'other-network', 'network_id': 'other-network'})
    other_native['native_event']['requestId'] = 'other-network'
    observer.reconcile_canceled_read_errors([error], [paused, other_paused], [failure, other_native], origins)
    assert observer.gate_task_errors([error]) == []
    assert len(error['authoritative_paused_records']) == 1 and len(error['authoritative_native_records']) == 1


def cache_witness_fixture():
    url = 'https://webapi.amap.com/maps?v=2.0&key=UNIT_SECRET'
    data = b'unit cache transport bytes only; not actual SDK success'
    url_hash = hashlib.sha256(url.encode()).hexdigest()
    validator = hashlib.sha256(b'unit-etag').hexdigest()
    starts, responses, bodies = [], [], []
    for index, status in [(1, 200), (2, 304)]:
        scope = {'case_id': f'case-{index}', 'case_epoch': f'epoch-{index}',
            'cdp_scope_id': 'cdp-1', 'page_target_id': 'page-1', 'frame_id': 'frame-1', 'loader_id': f'loader-{index}'}
        start = {**scope, 'request_id': f'sdk-{index}', 'raw_url_sha256': url_hash,
            'method': 'GET', 'resource_type': 'Script', 'redirect_hop': False, 'received_sequence': index * 10}
        start['native_identity'] = {key: start[key] for key in ('request_id', 'raw_url_sha256', 'method', 'frame_id', 'loader_id', 'resource_type', 'redirect_hop')}
        starts.append(start)
        responses.append({**scope, 'request_id': start['request_id'], 'raw_url_sha256': url_hash,
            'status': status, 'resource_type': 'Script', 'etag_sha256': validator, 'received_sequence': index * 10 + 2})
        bodies.append({**scope, 'request_id': start['request_id'], 'raw_url_sha256': url_hash,
            'operation': 'Network.getResponseBody', 'success': True, 'size_bytes': len(data),
            'sha256': hashlib.sha256(data).hexdigest(), 'received_sequence': index * 10 + 4})
    prior = {**deepcopy(starts[0]), 'kind': 'loaded', 'status': 200, 'sha256': bodies[0]['sha256'],
        'size_bytes': len(data), 'redirect_chain_raw_url_sha256': [url_hash]}
    extra = {'request_id': 'sdk-2', 'cdp_scope_id': 'cdp-1', 'page_target_id': 'page-1',
        'if_none_match_present': True, 'if_none_match_sha256': validator}
    finished = [{'request_id': start['request_id'], 'cdp_scope_id': start['cdp_scope_id'],
        'page_target_id': start['page_target_id'], 'received_sequence': start['received_sequence'] + 3,
        'native_timestamp': float(index), 'encoded_data_length': 10,
        'native_event': {'requestId': start['request_id'], 'timestamp': float(index), 'encodedDataLength': 10},
        'request_start_candidates': [deepcopy(start)]} for index, start in enumerate(starts, 1)]
    for body, completion in zip(bodies, finished):
        body['completion_proof'] = deepcopy(completion)
    response_extra = [{'request_id': response['request_id'], 'cdp_scope_id': response['cdp_scope_id'],
        'page_target_id': response['page_target_id'], 'status_code': response['status'], 'etag_sha256': validator,
        'native_identity': {'request_id': response['request_id'], 'status_code': response['status'], 'etag_sha256': validator}}
        for response in responses]
    initial_extra = {'request_id': 'sdk-1', 'cdp_scope_id': 'cdp-1', 'page_target_id': 'page-1',
        'if_none_match_present': False, 'if_none_match_sha256': None}
    witness = {'starts': starts, 'responses': responses, 'bodies': bodies, 'extra_infos': [extra, initial_extra],
        'prior_receipts': [prior], 'finished': finished, 'response_extra_infos': response_extra}
    return url, data, {**deepcopy(starts[1]), 'redirect_chain_raw_url_sha256': [url_hash]}, witness


@pytest.mark.asyncio
async def test_attested_304_reads_current_body_and_keeps_actual_wire_status(observer):
    url, data, scope, witness = cache_witness_fixture()
    calls = []
    async def current_native_body_control():
        calls.append('current sdk-2 body, not old receipt bytes')
        return data
    receipt = await observer.observe_sdk_script_response(url=url, status=304, headers={},
        redirect_chain=[url], body_reader=current_native_body_control, observation_scope=scope, cache_witness=witness)
    assert calls == ['current sdk-2 body, not old receipt bytes']
    assert receipt['status'] == 304 and receipt['kind'] == 'cache_served_representation'
    assert receipt['sha256'] == hashlib.sha256(data).hexdigest() and receipt['body_read'] is True
    assert receipt['native_cache_proof']['current_request_id'] == 'sdk-2'
    assert receipt['native_cache_proof']['prior_request_id'] == 'sdk-1'
    assert receipt['native_cache_proof']['starts'][0]['loader_id'] != scope['loader_id']
    assert 'UNIT_SECRET' not in json.dumps(receipt)


@pytest.mark.parametrize('change', ['empty_bytes', 'different_bytes', 'missing_extra', 'duplicate_extra',
    'missing_validator', 'validator_false_bool', 'wrong_validator', 'wrong_raw_url', 'wrong_context',
    'wrong_epoch', 'wrong_request_id', 'wrong_loader', 'wrong_frame', 'duplicate_start', 'duplicate_response',
    'duplicate_body', 'failed_body', 'wrong_body_operation', 'wrong_body_size', 'missing_prior', 'duplicate_prior', 'redirect_hop'])
def test_unknown_or_conflicting_304_native_witness_is_rejected(observer, change):
    url, data, scope, witness = cache_witness_fixture()
    if change == 'empty_bytes': data = b''
    if change == 'different_bytes': data += b'different'
    if change == 'missing_extra': witness['extra_infos'] = []
    if change == 'duplicate_extra': witness['extra_infos'] *= 2
    if change == 'missing_validator': witness['extra_infos'][0]['if_none_match_sha256'] = None
    if change == 'validator_false_bool': witness['extra_infos'][0]['if_none_match_present'] = 1
    if change == 'wrong_validator': witness['responses'][1]['etag_sha256'] = 'b'*64
    if change == 'wrong_raw_url': witness['starts'][1]['raw_url_sha256'] = 'b'*64
    if change == 'wrong_context': witness['bodies'][1]['cdp_scope_id'] = 'other-cdp'
    if change == 'wrong_epoch': witness['responses'][1]['case_epoch'] = 'other-epoch'
    if change == 'wrong_request_id': witness['bodies'][1]['request_id'] = 'other-request'
    if change == 'wrong_loader': witness['responses'][1]['loader_id'] = 'other-loader'
    if change == 'wrong_frame': witness['responses'][1]['frame_id'] = 'other-frame'
    if change == 'duplicate_start': witness['starts'].append(deepcopy(witness['starts'][1]))
    if change == 'duplicate_response': witness['responses'].append(deepcopy(witness['responses'][1]))
    if change == 'duplicate_body': witness['bodies'].append(deepcopy(witness['bodies'][1]))
    if change == 'failed_body': witness['bodies'][1]['success'] = False
    if change == 'wrong_body_operation': witness['bodies'][1]['operation'] = 'old_receipt_copy'
    if change == 'wrong_body_size': witness['bodies'][1]['size_bytes'] += 1
    if change == 'missing_prior': witness['prior_receipts'] = []
    if change == 'duplicate_prior': witness['prior_receipts'] *= 2
    if change == 'redirect_hop': witness['starts'][1]['redirect_hop'] = True
    with pytest.raises(ValueError, match='SDK cache'):
        observer.verify_sdk_cache_representation(witness, scope=scope, raw_url_sha256=hashlib.sha256(url.encode()).hexdigest(), actual_bytes=data)


def append_sdk_200_history(witness, request_id, start_sequence):
    """Another native synthetic transaction, independent of physical array order."""
    def rebind(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ('request_id', 'requestId') and child == 'sdk-1':
                    value[key] = request_id
                elif key == 'received_sequence' and type(child) is int:
                    value[key] = child + start_sequence - 10
                else:
                    rebind(child)
        elif isinstance(value, list):
            for child in value:
                rebind(child)
    for name in ('starts', 'responses', 'bodies', 'extra_infos', 'response_extra_infos', 'finished', 'prior_receipts'):
        item = deepcopy(next(row for row in witness[name] if row.get('request_id') == 'sdk-1'))
        rebind(item)
        witness[name].append(item)


def test_sdk_200_history_retains_each_request_and_excludes_cache_receipts(observer):
    _, _, _, witness = cache_witness_fixture()
    owner = observer.ChromeObserver(None, {}, Path('synthetic-no-access'))
    first = deepcopy(witness['prior_receipts'][0])
    second = {**deepcopy(first), 'request_id': 'another-native-200'}
    owner.retain_sdk_initial_200(first)
    owner.retain_sdk_initial_200(second)
    owner.retain_sdk_initial_200({**second, 'request_id': 'cached', 'status': 304, 'kind': 'cache_served_representation'})
    assert list(owner.sdk_initial_200) == [(first['raw_url_sha256'], 'sdk-1'), (first['raw_url_sha256'], 'another-native-200')]
    assert [row['request_id'] for row in owner.sdk_witness()['prior_receipts']] == ['sdk-1', 'another-native-200']
    second['sha256'] = 'changed-after-capture'
    assert owner.sdk_witness()['prior_receipts'][1]['sha256'] != second['sha256']


@pytest.mark.parametrize('reverse', [False, True])
def test_sdk_200_history_chooses_latest_proved_predecessor_not_first_or_later(observer, reverse):
    _, data, scope, witness = cache_witness_fixture()
    append_sdk_200_history(witness, 'middle-200', 15)
    append_sdk_200_history(witness, 'later-200', 30)
    for name in ('responses', 'response_extra_infos'):
        for row in witness[name]:
            if row['request_id'] in ('sdk-1', 'later-200'):
                row['etag_sha256'] = 'b' * 64
                if 'native_identity' in row:
                    row['native_identity']['etag_sha256'] = 'b' * 64
    for name in ('bodies', 'prior_receipts'):
        for row in witness[name]:
            if row['request_id'] in ('sdk-1', 'later-200'):
                row['sha256'] = 'c' * 64
    if reverse:
        for rows in witness.values():
            rows.reverse()
    proof = observer.verify_sdk_cache_representation(witness, scope=scope,
        raw_url_sha256=scope['raw_url_sha256'], actual_bytes=data)
    assert proof['prior_request_id'] == 'middle-200'
    resource = {**scope, 'kind': 'cache_served_representation', 'status': 304,
        'sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data), 'native_cache_proof': proof}
    assert observer.sdk_resource_identity_verified(resource, witness)
    assert observer.verify_sdk_resources_indexed([resource], witness)['results'] == [True]


@pytest.mark.parametrize('mismatch', ['validator', 'body'])
def test_sdk_200_history_never_falls_back_to_a_matching_older_answer(observer, mismatch):
    _, data, scope, witness = cache_witness_fixture()
    append_sdk_200_history(witness, 'newest-but-different', 15)
    for name in (('responses', 'response_extra_infos') if mismatch == 'validator' else ('bodies', 'prior_receipts')):
        row = witness[name][-1]
        row['etag_sha256' if mismatch == 'validator' else 'sha256'] = 'f' * 64
        if 'native_identity' in row and mismatch == 'validator':
            row['native_identity']['etag_sha256'] = 'f' * 64
    with pytest.raises(ValueError, match='prior validator mismatch|prior/current representation differs'):
        observer.verify_sdk_cache_representation(witness, scope=scope,
            raw_url_sha256=scope['raw_url_sha256'], actual_bytes=data)


@pytest.mark.parametrize('change', ['tied', 'body_missing', 'body_bool', 'body_overlaps', 'start_unknown', 'duplicate_body'])
def test_sdk_200_history_rejects_ambiguous_or_unorderable_predecessors(observer, change):
    _, data, scope, witness = cache_witness_fixture()
    append_sdk_200_history(witness, 'other-200', 10 if change == 'tied' else 15)
    if change == 'body_missing':
        witness['bodies'][-1].pop('received_sequence')
    elif change == 'body_bool':
        witness['bodies'][-1]['received_sequence'] = True
    elif change == 'body_overlaps':
        witness['bodies'][-1]['received_sequence'] = 21
    elif change == 'start_unknown':
        witness['starts'][-1]['received_sequence'] = None
    elif change == 'duplicate_body':
        witness['bodies'].append(deepcopy(witness['bodies'][-1]))
    with pytest.raises(ValueError, match='SDK cache'):
        observer.verify_sdk_cache_representation(witness, scope=scope,
            raw_url_sha256=scope['raw_url_sha256'], actual_bytes=data)


def test_sdk_200_history_old_cached_proof_replays_with_later_history_and_revokes_on_late_predecessor(observer):
    _, _, _, witness, resource = resource_from_cache_witness(observer)
    append_sdk_200_history(witness, 'later-200', 30)
    assert observer.sdk_resource_identity_verified(resource, witness)
    assert observer.verify_sdk_resources_indexed([resource], witness)['results'] == [True]
    append_sdk_200_history(witness, 'late-arriving-predecessor', 15)
    assert not observer.sdk_resource_identity_verified(resource, witness)
    assert observer.verify_sdk_resources_indexed([resource], witness)['results'] == [False]


def early_start_fixture():
    origins, paused, error, native = canceled_read_fixture()
    paused['frame_id'] = error['frame_id'] = 'frame-1'
    paused['raw_url_sha256'] = error['raw_url_sha256'] = hashlib.sha256(paused['url'].encode()).hexdigest()
    paused['received_sequence'] = error['received_sequence'] = 4
    native['case_id'] = native['case_epoch'] = None
    native['received_sequence'] = 3
    start = {**deepcopy(paused), 'request_id': paused['network_id'], 'resource_type': 'Fetch',
        'loader_id': 'loader-1', 'redirect_hop': False, 'received_sequence': 2}
    start['native_identity'] = {key: start[key] for key in ('request_id', 'raw_url_sha256', 'method', 'frame_id', 'loader_id', 'resource_type', 'redirect_hop')}
    return origins, paused, error, native, start


def test_early_native_uses_independent_start_scope_without_mutating_original_null(observer):
    origins, paused, error, native, start = early_start_fixture()
    original = deepcopy(native)
    assert start['received_sequence'] < native['received_sequence'] < paused['received_sequence']
    observer.reconcile_canceled_read_errors([error], [paused], [native], origins, request_starts=[start])
    assert observer.gate_task_errors([error]) == []
    assert native == original and error['native_loading_failed']['case_epoch'] is None
    assert error['derived_native_scope']['case_epoch'] == 'epoch-1'
    assert error['request_start_proof'] == start
    records = [{'case_id': error['case_id'], 'passed': True}]
    observer.finalize_background_errors(records, [error])
    assert records[0]['passed']


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'wrong_method', 'wrong_url', 'wrong_cdp', 'wrong_page',
    'wrong_case', 'wrong_epoch', 'wrong_frame', 'wrong_loader', 'missing_loader', 'hop', 'after_native', 'wrong_request_id'])
def test_early_native_missing_or_conflicting_start_proof_blocks(observer, change):
    origins, paused, error, native, start = early_start_fixture()
    starts = [start]
    if change == 'missing': starts = []
    if change == 'duplicate': starts *= 2
    if change == 'wrong_method': start['method'] = 'POST'
    if change == 'wrong_url': start['raw_url_sha256'] = 'b'*64
    if change == 'wrong_cdp': start['cdp_scope_id'] = 'other-cdp'
    if change == 'wrong_page': start['page_target_id'] = 'other-page'
    if change == 'wrong_case': start['case_id'] = 'other-case'
    if change == 'wrong_epoch': start['case_epoch'] = 'other-epoch'
    if change == 'wrong_frame': start['frame_id'] = 'other-frame'
    if change == 'wrong_loader': start['loader_id'] = 'other-loader'
    if change == 'missing_loader': start.pop('loader_id')
    if change == 'hop': start['redirect_hop'] = True
    if change == 'after_native': start['received_sequence'] = 4
    if change == 'wrong_request_id': start['request_id'] = 'other-network'
    observer.reconcile_canceled_read_errors([error], [paused], [native], origins, request_starts=starts)
    assert observer.gate_task_errors([error]) == [error]


def test_late_start_ambiguity_revokes_early_derived_scope_and_cannot_be_truncated(observer):
    origins, paused, error, native, start = early_start_fixture()
    observer.reconcile_canceled_read_errors([error], [paused], [native], origins, request_starts=[start])
    assert observer.gate_task_errors([error]) == []
    late = deepcopy(start); late['case_epoch'] = 'other-epoch'
    observer.reconcile_canceled_read_errors([error], [paused], [native], origins, request_starts=[start, late])
    assert observer.gate_task_errors([error]) == [error] and error['cancellation_confirmation_revoked'] is True
    observer.reconcile_canceled_read_errors([error], [paused], [native], origins, request_starts=[start])
    assert observer.gate_task_errors([error]) == [error]


def test_conditional_validator_comes_from_extra_info_not_absent_regular_headers(observer):
    actual = observer.sdk_conditional_headers({'If-None-Match': 'unit-etag'})
    assert actual['if_none_match_present'] is True
    assert actual['if_none_match_sha256'] == hashlib.sha256(b'unit-etag').hexdigest()
    assert observer.sdk_conditional_headers({})['if_none_match_sha256'] is None
    assert 'unit-etag' not in json.dumps(actual)


@pytest.mark.asyncio
async def test_default304_without_witness_and_failed_current_cdp_body_stay_red(observer):
    url, _, scope, witness = cache_witness_fixture()
    async def failed_current_body():
        raise RuntimeError('actual current CDP body unavailable')
    with pytest.raises(ValueError, match='SDK'):
        await observer.observe_sdk_script_response(url=url, status=304, headers={}, redirect_chain=[url],
            body_reader=failed_current_body, observation_scope=scope)
    with pytest.raises(RuntimeError, match='current CDP'):
        await observer.observe_sdk_script_response(url=url, status=304, headers={}, redirect_chain=[url],
            body_reader=failed_current_body, observation_scope=scope, cache_witness=witness)


@pytest.mark.parametrize('late', ['start', 'extra', 'response', 'body'])
def test_final_sdk_gate_replays_full_current_native_ledgers_not_old_cached_green(observer, late):
    url, data, scope, witness = cache_witness_fixture()
    proof = observer.verify_sdk_cache_representation(witness, scope=scope,
        raw_url_sha256=hashlib.sha256(url.encode()).hexdigest(), actual_bytes=data)
    resource = {**deepcopy(scope), 'url': observer.redact_url(url), 'raw_url_sha256': proof['raw_url_sha256'],
        'kind': 'cache_served_representation', 'status': 304, 'sha256': hashlib.sha256(data).hexdigest(),
        'size_bytes': len(data), 'native_cache_proof': proof}
    assert observer.sdk_resource_identity_verified(resource, witness)
    field = {'start': 'starts', 'extra': 'extra_infos', 'response': 'responses', 'body': 'bodies'}[late]
    witness[field].append(deepcopy(witness[field][-1]))
    assert not observer.sdk_resource_identity_verified(resource, witness)


@pytest.mark.parametrize('change', ['late_prior_body', 'body_before_response', 'prior_bool_size', 'empty_loaded_scope'])
def test_native_sdk_metadata_unknown_or_not_preexisting_cannot_pass_final_gate(observer, change):
    url, data, scope, witness = cache_witness_fixture()
    if change == 'late_prior_body': witness['bodies'][0]['received_sequence'] = 25
    if change == 'body_before_response': witness['bodies'][1]['received_sequence'] = 21
    if change == 'prior_bool_size':
        data = b'x'
        for value in witness['bodies']:
            value.update({'size_bytes': 1, 'sha256': hashlib.sha256(data).hexdigest()})
        witness['prior_receipts'][0].update({'size_bytes': True, 'sha256': hashlib.sha256(data).hexdigest()})
    if change == 'empty_loaded_scope':
        prior = witness['prior_receipts'][0]
        for value in (prior, witness['starts'][0], witness['responses'][0], witness['bodies'][0]):
            value['case_id'] = value['case_epoch'] = value['frame_id'] = value['loader_id'] = None
        assert not observer.sdk_resource_identity_verified(prior, witness)
    else:
        with pytest.raises(ValueError, match='SDK cache'):
            observer.verify_sdk_cache_representation(witness, scope=scope,
                raw_url_sha256=hashlib.sha256(url.encode()).hexdigest(), actual_bytes=data)


def test_final_case_sdk_replay_blocks_coverage_when_late_native_extra_is_ambiguous(observer):
    url, data, scope, witness = cache_witness_fixture()
    proof = observer.verify_sdk_cache_representation(witness, scope=scope,
        raw_url_sha256=hashlib.sha256(url.encode()).hexdigest(), actual_bytes=data)
    resource = {**deepcopy(scope), 'url': observer.redact_url(url), 'raw_url_sha256': proof['raw_url_sha256'],
        'kind': 'cache_served_representation', 'status': 304, 'sha256': hashlib.sha256(data).hexdigest(),
        'size_bytes': len(data), 'native_cache_proof': proof}
    ids = [f'art_{i}' for i in range(1800)]
    records = [{'case_id': f'case-{index}', 'map_artifact_id': value, 'passed': True,
        'sdk_resources': [resource]} for index, value in enumerate(ids)]
    observer.finalize_sdk_resources(records, witness)
    assert all(record['passed'] for record in records)
    witness['extra_infos'].append(deepcopy(witness['extra_infos'][0]))
    observer.finalize_sdk_resources(records, witness)
    gate = observer.coverage_gate(ids, records, raster_excluded=200, source_unchanged=True,
        limit=None, evidence_seal_verified=True)
    assert not gate['full_map_gate'] and gate['passed_map_count'] == 0


def resource_from_cache_witness(observer):
    url, data, scope, witness = cache_witness_fixture()
    proof = observer.verify_sdk_cache_representation(witness, scope=scope, raw_url_sha256=scope['raw_url_sha256'], actual_bytes=data)
    resource = {**deepcopy(scope), 'url': observer.redact_url(url), 'kind': 'cache_served_representation',
        'status': 304, 'sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data), 'native_cache_proof': proof}
    return url, data, scope, witness, resource


@pytest.mark.parametrize('target', ['current', 'prior'])
@pytest.mark.parametrize('change', ['missing', 'duplicate', 'wrong_scope', 'wrong_page', 'wrong_native_id',
    'wrong_phase', 'wrong_source_frame', 'wrong_source_loader', 'unknown_raw_packet'])
def test_full_native_completion_ledger_is_replayed_for_current_and_prior(observer, target, change):
    _, _, _, witness, resource = resource_from_cache_witness(observer)
    assert observer.sdk_resource_identity_verified(resource, witness)
    index = 1 if target == 'current' else 0
    completion = witness['finished'][index]
    if change == 'missing': witness['finished'].pop(index)
    if change == 'duplicate': witness['finished'].append(deepcopy(completion))
    if change == 'wrong_scope': completion['cdp_scope_id'] = 'foreign-cdp'
    if change == 'wrong_page': completion['page_target_id'] = 'foreign-page'
    if change == 'wrong_native_id': completion['native_event']['requestId'] = 'foreign-native-id'
    if change == 'wrong_phase': completion['received_sequence'] = witness['bodies'][index]['received_sequence'] + 1
    if change == 'wrong_source_frame': completion['request_start_candidates'][0]['frame_id'] = 'foreign-frame'
    if change == 'wrong_source_loader': completion['request_start_candidates'][0]['loader_id'] = 'foreign-loader'
    if change == 'unknown_raw_packet': completion.pop('native_event')
    assert not observer.sdk_resource_identity_verified(resource, witness)
    records = [{'case_id': 'unit-only', 'passed': True, 'sdk_resources': [resource],
        'sdk_resource_native_identity_verified': True}]
    observer.finalize_sdk_resources(records, witness)
    assert not records[0]['passed']
    assert records[0]['sdk_resource_identity_revoked'] is True
    assert records[0]['sdk_resource_revocation_evidence']['resources'][0]['authority']['finished'] == witness['finished']


@pytest.mark.asyncio
async def test_late_completion_during_actual_body_boundary_is_rejected_and_raw_facts_retained(observer):
    url, data, scope, witness, _ = resource_from_cache_witness(observer)
    owner = observer.ChromeObserver(None, {}, Path('synthetic-no-access'))
    owner.network_request_starts = witness['starts']; owner.native_sdk_responses = witness['responses']
    owner.network_extra_infos = witness['extra_infos']; owner.network_finished = witness['finished']
    owner.network_response_extra_infos = witness['response_extra_infos']
    # A transport-only negative boundary: no HTTP, SDK or Provider success.
    class UnitBodyBoundary:
        async def send(self, method, parameters):
            assert method == 'Network.getResponseBody' and parameters['requestId'] == 'sdk-2'
            owner.network_finished.append(deepcopy(owner.network_finished[1]))
            return {'base64Encoded': False, 'body': data.decode()}
    owner.cdp = UnitBodyBoundary()
    with pytest.raises(ValueError, match='completion'):
        await owner.native_sdk_body(url, 304, scope, redirect_chain_raw_url_sha256=scope['redirect_chain_raw_url_sha256'])
    assert len(owner.network_finished) == 3
    assert owner.native_sdk_body_receipts[0]['sha256'] == hashlib.sha256(data).hexdigest()


def test_completion_callback_preserves_packet_and_source_without_current_case_fill(observer):
    _, _, _, witness = cache_witness_fixture()
    start = witness['starts'][1]
    packet = {'requestId': 'sdk-2', 'timestamp': 10.5, 'encodedDataLength': 617}
    captured = observer.capture_native_completion(packet, cdp_scope_id='cdp-1', page_target_id='page-1',
        received_sequence=23, request_starts=witness['starts'])
    assert captured['native_event'] == packet
    assert 'frame_id' not in captured['native_event'] and 'case_epoch' not in captured['native_event']
    assert captured['request_start_candidates'] == [start]
    packet['requestId'] = 'changed-later'
    assert captured['native_event']['requestId'] == 'sdk-2'


@pytest.mark.parametrize('field', ['raw_url_sha256', 'frame_id', 'artifact_ids', 'session_id', 'run_id'])
def test_actual_retained_fetch_must_agree_with_derived_native_start_and_exception(observer, field):
    origins, paused, error, native, start = early_start_fixture()
    paused[field] = ['foreign-artifact'] if field == 'artifact_ids' else 'foreign-identity'
    observer.reconcile_canceled_read_errors([error], [paused], [native], origins, request_starts=[start])
    assert observer.gate_task_errors([error]) == [error]


@pytest.mark.parametrize('target', ['current', 'prior'])
@pytest.mark.parametrize('change', ['missing', 'duplicate', 'wrong_wire_status', 'wrong_etag', 'wrong_native_id', 'wrong_context'])
def test_wire_response_extra_info_cannot_be_missing_ambiguous_or_inconsistent(observer, target, change):
    _, _, _, witness, resource = resource_from_cache_witness(observer)
    index = 1 if target == 'current' else 0
    wire = witness['response_extra_infos'][index]
    if change == 'missing': witness['response_extra_infos'].pop(index)
    if change == 'duplicate': witness['response_extra_infos'].append(deepcopy(wire))
    if change == 'wrong_wire_status': wire['status_code'] = 200 if index == 1 else 304
    if change == 'wrong_etag': wire['etag_sha256'] = 'b'*64
    if change == 'wrong_native_id': wire['native_identity']['request_id'] = 'foreign-id'
    if change == 'wrong_context': wire['cdp_scope_id'] = 'foreign-cdp'
    assert not observer.sdk_resource_identity_verified(resource, witness)


def test_effective200_wire304_is_cache_representation_and_cannot_seed_prior_real200(observer):
    _, data, scope, witness = cache_witness_fixture()
    witness['responses'][1]['status'] = 200
    proof = observer.verify_sdk_cache_representation(witness, scope=scope,
        raw_url_sha256=scope['raw_url_sha256'], actual_bytes=data)
    assert proof['wire_status'] == 304 and proof['effective_status'] == 200
    prior = witness['prior_receipts'][0]
    witness['response_extra_infos'][0]['status_code'] = 304
    assert not observer.sdk_resource_identity_verified(prior, witness)
    with pytest.raises(ValueError, match='SDK cache'):
        observer.verify_sdk_cache_representation(witness, scope=scope,
            raw_url_sha256=scope['raw_url_sha256'], actual_bytes=data)


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'false_flag_hash', 'unknown_bool'])
def test_prior_actual200_request_extra_info_is_part_of_full_authority(observer, change):
    _, _, _, witness, resource = resource_from_cache_witness(observer)
    initial = witness['extra_infos'][1]
    if change == 'missing': witness['extra_infos'].pop(1)
    if change == 'duplicate': witness['extra_infos'].append(deepcopy(initial))
    if change == 'false_flag_hash': initial['if_none_match_sha256'] = 'b'*64
    if change == 'unknown_bool': initial['if_none_match_present'] = 0
    assert not observer.sdk_resource_identity_verified(resource, witness)


@pytest.mark.parametrize('target', ['finished', 'response_extra_infos'])
def test_late_completion_or_wire_ambiguity_revokes_full_coverage_and_retains_evidence(observer, target):
    _, _, _, witness, resource = resource_from_cache_witness(observer)
    ids = [f'art_{index}' for index in range(1800)]
    records = [{'case_id': f'case-{index}', 'map_artifact_id': value, 'passed': True,
        'sdk_resources': [resource], 'sdk_resource_native_identity_verified': True} for index, value in enumerate(ids)]
    witness[target].append(deepcopy(witness[target][-1]))
    observer.finalize_sdk_resources(records, witness)
    gate = observer.coverage_gate(ids, records, raster_excluded=200, source_unchanged=True,
        limit=None, evidence_seal_verified=True)
    assert not gate['full_map_gate'] and gate['passed_map_count'] == 0
    assert all(record['sdk_resource_identity_revoked'] is True for record in records)


def ordinary_two_hop_fixture(observer):
    # Unit-only complete metadata mirroring probe08's actual 302->200 shape.
    # Synthetic Location/byte controls are NOT missing probe08 fields backfilled.
    initial = 'https://webapi.amap.com/maps/checkredirect?key=UNIT_SECRET'
    final = 'https://mapplugin.amap.com/maps/checkredirectcdn?auth_key=UNIT_TOKEN'
    chain = [hashlib.sha256(value.encode()).hexdigest() for value in (initial, final)]
    location_hash = hashlib.sha256(final.encode()).hexdigest()
    scope = {'case_id': 'unit-hop-case', 'case_epoch': 'unit-hop-epoch', 'cdp_scope_id': 'cdp-1',
        'page_target_id': 'page-1', 'frame_id': 'frame-1', 'loader_id': 'loader-1', 'request_id': 'hop-request'}
    starts = []
    for index, raw_hash in enumerate(chain):
        host = 'webapi.amap.com' if index == 0 else 'mapplugin.amap.com'
        start = {**scope, 'raw_url_sha256': raw_hash, 'method': 'GET', 'resource_type': 'Script',
            'redirect_hop': bool(index), 'received_sequence': 10 + index*3,
            'request_authority_sha256': hashlib.sha256(host.encode()).hexdigest()}
        if index:
            start['redirect_source'] = {'previous_raw_url_sha256': chain[0], 'status': 302,
                'location_value_sha256': location_hash, 'resolved_location_raw_url_sha256': chain[1]}
        start['native_identity'] = {key: deepcopy(start[key]) for key in
            ('request_id', 'raw_url_sha256', 'method', 'frame_id', 'loader_id', 'resource_type', 'redirect_hop')}
        if index: start['native_identity']['redirect_source'] = deepcopy(start['redirect_source'])
        starts.append(start)
    response = {**deepcopy(starts[1]), 'status': 200, 'received_sequence': 15}
    completion = observer.capture_native_completion({'requestId': scope['request_id'], 'timestamp': 4.5,
        'encodedDataLength': 35}, cdp_scope_id='cdp-1', page_target_id='page-1', received_sequence=16, request_starts=starts)
    data = b'unit-only final body transport, not SDK or LLM success'
    body = {**deepcopy(starts[1]), 'operation': 'Network.getResponseBody', 'success': True,
        'sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data), 'received_sequence': 17,
        'completion_proof': deepcopy(completion)}
    wires, extras = [], []
    for index, status in enumerate((302, 200)):
        wire = {'request_id': scope['request_id'], 'cdp_scope_id': 'cdp-1', 'page_target_id': 'page-1',
            'status_code': status, 'etag_sha256': None, 'location_value_sha256': location_hash if index == 0 else None}
        wire['native_identity'] = {name: wire[name] for name in ('request_id', 'status_code', 'etag_sha256', 'location_value_sha256')}
        wires.append(wire)
        extras.append({'request_id': scope['request_id'], 'cdp_scope_id': 'cdp-1', 'page_target_id': 'page-1',
            'if_none_match_present': False, 'if_none_match_sha256': None,
            'request_authority_sha256': starts[index]['request_authority_sha256']})
    resource = {**deepcopy(starts[1]), 'url': observer.redact_url(final), 'kind': 'loaded', 'status': 200,
        'effective_status': 200, 'wire_status': 200, 'sha256': body['sha256'], 'size_bytes': len(data),
        'redirect_chain_raw_url_sha256': chain}
    witness = {'starts': starts, 'responses': [response], 'bodies': [body], 'extra_infos': extras,
        'response_extra_infos': wires, 'finished': [completion], 'prior_receipts': []}
    return witness, resource


def test_known_ordinary302_to200_hop_has_full_exact_source_but_is_not_cache304(observer):
    witness, resource = ordinary_two_hop_fixture(observer)
    assert observer.sdk_resource_identity_verified(resource, witness)
    assert observer.native_completion_verified(witness['finished'][0], witness['starts'][1],
        source_candidates=witness['starts'])
    resource.update({'kind': 'cache_served_representation', 'status': 304})
    assert not observer.sdk_resource_identity_verified(resource, witness)


def cache_witness_with_later_two_hop(observer, change=None):
    _, _, _, witness, cached = resource_from_cache_witness(observer)
    later, resource = ordinary_two_hop_fixture(observer)
    final_hash = resource['raw_url_sha256']
    offset = 0 if change == 'before_current' else 30
    def rebind(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == 'received_sequence' and type(child) is int:
                    value[key] = 19 if change == 'one_start_before' and child == 10 else child + offset
                else:
                    value[key] = rebind(child)
        elif isinstance(value, list):
            value[:] = [rebind(child) for child in value]
        elif value == final_hash:
            return cached['raw_url_sha256']
        return value
    rebind(later)
    rebind(resource)
    if change == 'wrong_identity':
        later['starts'][0]['frame_id'] = 'foreign-frame'
    elif change == 'missing_start':
        later['starts'].pop(0)
    for name, rows in later.items():
        witness[name].extend(rows)
    witness['prior_receipts'].append(resource)
    return witness, cached


def test_sdk_200_history_old_cache_proof_replays_with_proven_future_two_hop(observer):
    witness, cached = cache_witness_with_later_two_hop(observer)
    assert observer.sdk_resource_identity_verified(cached, witness)
    indexed = observer.verify_sdk_resources_indexed([cached], witness, collect_positions=True)
    assert indexed['results'] == [True]
    assert indexed['selected_positions'][0]['starts'] == [0, 1, 2, 3]
    assert cached['native_cache_proof']['prior_request_id'] == 'sdk-1'


@pytest.mark.parametrize('change', ['one_start_before', 'before_current', 'wrong_identity', 'missing_start'])
def test_sdk_200_history_unknown_or_nonfuture_two_hop_still_revokes_cache(observer, change):
    witness, cached = cache_witness_with_later_two_hop(observer, change)
    assert not observer.sdk_resource_identity_verified(cached, witness)
    assert observer.verify_sdk_resources_indexed([cached], witness)['results'] == [False]


@pytest.mark.parametrize('change', ['duplicate_start', 'missing_hop', 'wrong_previous_url', 'wrong_location',
    'wrong_playwright_chain', 'extra_finished', 'wrong_frame', 'wrong_loader', 'wrong_epoch', 'wrong_wire',
    'duplicate_wire', 'missing_wire', 'wrong_request_authority', 'duplicate_request_extra', 'wrong_native_link'])
def test_known_hop_source_unknown_extra_or_conflicting_facts_stay_blocking(observer, change):
    witness, resource = ordinary_two_hop_fixture(observer)
    if change == 'duplicate_start': witness['starts'].append(deepcopy(witness['starts'][1]))
    if change == 'missing_hop': witness['starts'].pop(0)
    if change == 'wrong_previous_url': witness['starts'][1]['redirect_source']['previous_raw_url_sha256'] = 'b'*64
    if change == 'wrong_location': witness['response_extra_infos'][0]['location_value_sha256'] = 'b'*64
    if change == 'wrong_playwright_chain': resource['redirect_chain_raw_url_sha256'].reverse()
    if change == 'extra_finished': witness['finished'].append(deepcopy(witness['finished'][0]))
    if change == 'wrong_frame': witness['starts'][0]['frame_id'] = 'foreign-frame'
    if change == 'wrong_loader': witness['starts'][0]['loader_id'] = 'foreign-loader'
    if change == 'wrong_epoch': witness['starts'][0]['case_epoch'] = 'foreign-epoch'
    if change == 'wrong_wire': witness['response_extra_infos'][0]['status_code'] = 307
    if change == 'duplicate_wire': witness['response_extra_infos'].append(deepcopy(witness['response_extra_infos'][0]))
    if change == 'missing_wire': witness['response_extra_infos'].pop(0)
    if change == 'wrong_request_authority': witness['extra_infos'][0]['request_authority_sha256'] = 'b'*64
    if change == 'duplicate_request_extra': witness['extra_infos'].append(deepcopy(witness['extra_infos'][0]))
    if change == 'wrong_native_link': witness['starts'][1]['native_identity']['redirect_source']['status'] = 307
    assert not observer.sdk_resource_identity_verified(resource, witness)


@pytest.mark.asyncio
@pytest.mark.parametrize('late_wire', [False, True])
async def test_ordinary_hop_getter_uses_actual_chain_and_rechecks_full_authority_after_await(observer, late_wire):
    witness, resource = ordinary_two_hop_fixture(observer)
    owner = observer.ChromeObserver(None, {}, Path('synthetic-no-access'))
    owner.network_request_starts = witness['starts']; owner.native_sdk_responses = witness['responses']
    owner.network_extra_infos = witness['extra_infos']; owner.network_finished = witness['finished']
    owner.network_response_extra_infos = witness['response_extra_infos']; owner.received_sequence = 16
    class UnitBodyBoundary:
        async def send(self, method, parameters):
            assert method == 'Network.getResponseBody' and parameters['requestId'] == 'hop-request'
            if late_wire: owner.network_response_extra_infos.append(deepcopy(owner.network_response_extra_infos[1]))
            return {'base64Encoded': False, 'body': 'unit-only final body transport, not SDK or LLM success'}
    owner.cdp = UnitBodyBoundary()
    call = owner.native_sdk_body(resource['url'], 200, resource,
        redirect_chain_raw_url_sha256=resource['redirect_chain_raw_url_sha256'])
    # The URL is already redacted in this synthetic control; bind its same hash.
    final_hash = hashlib.sha256(resource['url'].encode()).hexdigest()
    for value in (owner.network_request_starts[1], owner.native_sdk_responses[0], resource):
        value['raw_url_sha256'] = final_hash
    owner.network_request_starts[1]['native_identity']['raw_url_sha256'] = final_hash
    owner.network_request_starts[1]['redirect_source']['resolved_location_raw_url_sha256'] = final_hash
    owner.network_request_starts[1]['native_identity']['redirect_source']['resolved_location_raw_url_sha256'] = final_hash
    owner.network_finished[0]['request_start_candidates'] = deepcopy(owner.network_request_starts)
    resource['redirect_chain_raw_url_sha256'][1] = final_hash
    if late_wire:
        with pytest.raises(ValueError, match='authority'):
            await call
    else:
        data, actual_scope = await call
        assert hashlib.sha256(data).hexdigest() == resource['sha256']
        assert actual_scope['wire_status'] == 200 and len(actual_scope['redirect_chain_raw_url_sha256']) == 2


def test_cache304_final_gate_rejects_a_contradictory_actual_playwright_raw_chain(observer):
    _, _, _, witness, resource = resource_from_cache_witness(observer)
    resource['redirect_chain_raw_url_sha256'] = ['b'*64, resource['raw_url_sha256']]
    assert not observer.sdk_resource_identity_verified(resource, witness)


def malformed_sdk_chain(kind, current):
    return {'missing': None, 'empty': [], 'three': ['a'*64, 'b'*64, current],
        'wrong_single': ['b'*64], 'tuple': (current,), 'bad_hash': ['not-a-full-raw-hash'],
        'repeated_two': [current, current], 'unmatched_two': ['b'*64, current]}[kind]


@pytest.mark.parametrize('kind', ['missing', 'empty', 'three', 'wrong_single', 'tuple', 'bad_hash', 'repeated_two', 'unmatched_two'])
@pytest.mark.asyncio
async def test_initial_sdk_getter_rejects_unproved_chain_shape_before_cdp_body(observer, kind):
    url, _, _, witness = cache_witness_fixture()
    scope = deepcopy(witness['prior_receipts'][0])
    owner = observer.ChromeObserver(None, {}, Path('synthetic-no-access'))
    owner.network_request_starts = witness['starts']; owner.native_sdk_responses = witness['responses']
    owner.network_extra_infos = witness['extra_infos']; owner.network_response_extra_infos = witness['response_extra_infos']
    owner.network_finished = witness['finished']
    class MustNotReadBody:
        async def send(self, *args):
            raise AssertionError('unproved actual chain must reject before any CDP body call')
    owner.cdp = MustNotReadBody()
    with pytest.raises(ValueError, match='chain'):
        await asyncio.wait_for(owner.native_sdk_body(url, 200, scope,
            redirect_chain_raw_url_sha256=malformed_sdk_chain(kind, scope['raw_url_sha256'])), timeout=.1)


@pytest.mark.parametrize('kind', ['missing', 'empty', 'three', 'wrong_single', 'tuple', 'bad_hash', 'repeated_two', 'unmatched_two'])
@pytest.mark.parametrize('resource_kind', ['loaded200', 'cache304'])
def test_final_sdk_identity_rejects_missing_or_unproved_chain_shape(observer, kind, resource_kind):
    _, _, _, witness, cached = resource_from_cache_witness(observer)
    resource = deepcopy(witness['prior_receipts'][0]) if resource_kind == 'loaded200' else cached
    if kind == 'missing': resource.pop('redirect_chain_raw_url_sha256', None)
    else: resource['redirect_chain_raw_url_sha256'] = malformed_sdk_chain(kind, resource['raw_url_sha256'])
    assert not observer.sdk_resource_identity_verified(resource, witness)


@pytest.mark.parametrize('value', [True, 1, 1.0, None, {'missing': None}, {'float': -0.0}])
def test_phase12_typed_receipt_roundtrip_and_usage_are_lossless(observer, tmp_path, value):
    pool = observer.SdkReceiptAuthority(tmp_path)
    payload = {'case_id': 'owner', 'case_epoch': 'epoch', 'request_id': 'native', 'extra': value}
    first = {**payload, 'used_in_case_id': 'consumer', 'inherited_from_same_cdp_context': False}
    second = {**payload, 'used_in_case_id': None, 'used_in_case_epoch': 'second'}
    refs = [pool.reference(first, 0), pool.reference(second, 1), pool.reference(first, 2)]
    assert len(pool.table()) == 1
    assert [pool.expand(ref) for ref in refs] == [first, second, first]
    assert [ref['position'] for ref in refs] == [0, 1, 2]


def test_phase12_bool_int_float_are_distinct_payloads(observer, tmp_path):
    pool = observer.SdkReceiptAuthority(tmp_path)
    refs = [pool.reference({'value': value}, index) for index, value in enumerate([True, 1, 1.0])]
    assert len({ref['payload_sha256'] for ref in refs}) == 3


@pytest.mark.parametrize('value', [float('nan'), float('inf'), (1, 2), {1: 'non-string-key'}, object()])
def test_phase12_unknown_typed_shape_is_rejected(observer, value):
    with pytest.raises(ValueError):
        observer.strict_json_bytes(value)


@pytest.mark.parametrize('change', ['digest', 'owner', 'usage', 'position', 'missing_file'])
def test_phase12_refs_fail_closed_on_tampering(observer, tmp_path, change):
    pool = observer.SdkReceiptAuthority(tmp_path)
    ref = pool.reference({'case_id': 'owner', 'used_in_case_id': 'consumer'}, 0)
    if change == 'digest': ref['payload_sha256'] = '0'*64
    if change == 'owner': ref['owner']['case_id'] = 'foreign'
    if change == 'usage': ref['usage']['invented'] = True
    if change == 'position': ref['position'] = True
    if change == 'missing_file': next((tmp_path/'sdk-receipts').glob('*.json')).unlink()
    with pytest.raises(ValueError): pool.expand(ref)


@pytest.mark.parametrize('ledger', ['starts', 'responses', 'bodies', 'extra_infos', 'response_extra_infos', 'finished', 'prior_receipts'])
def test_phase12_index_preserves_unrelated_global304_malformed(observer, ledger):
    _, _, _, witness, resource = resource_from_cache_witness(observer)
    witness[ledger].append(None)
    assert observer.verify_sdk_resources_indexed([resource], witness)['results'] == [False]


@pytest.mark.parametrize('alias', ['outer', 'native'])
def test_phase12_finished_cartesian_or_and_duplicate_order(observer, alias):
    _, _, _, witness, resource = resource_from_cache_witness(observer)
    late = deepcopy(witness['finished'][1]); late.update(cdp_scope_id='foreign', page_target_id='foreign')
    late['request_id'] = 'sdk-2' if alias == 'outer' else 'foreign-id'
    late['native_event']['requestId'] = 'foreign-id' if alias == 'outer' else 'sdk-2'
    witness['finished'].append(late)
    result = observer.verify_sdk_resources_indexed([resource], witness, collect_positions=True)
    assert result['results'] == [False]
    assert result['selected_positions'][0]['finished'] == [0, 1, 2]  # Prior and both current candidates.


def test_phase12_prior_unknown_id_ambiguity_is_not_hidden(observer):
    _, _, _, witness, resource = resource_from_cache_witness(observer)
    prior = deepcopy(witness['prior_receipts'][0]); prior['request_id'] = 'unknown-other-prior'
    witness['prior_receipts'].append(prior)
    assert observer.verify_sdk_resources_indexed([resource], witness)['results'] == [False]


def test_phase12_closed_local_memo_propagates_true_and_false_without_usage_merge(observer, monkeypatch):
    _, _, _, witness, resource = resource_from_cache_witness(observer)
    invalid = {**deepcopy(resource), 'sha256': '0'*64}
    original = observer.sdk_resource_identity_verified; calls = []
    def spy(value, selected):
        calls.append(observer.strict_json_bytes(value)); return original(value, selected)
    monkeypatch.setattr(observer, 'sdk_resource_identity_verified', spy)
    inputs = [resource, {**resource, 'used_in_case_id': 'another'}, invalid, deepcopy(invalid)]
    result = observer.verify_sdk_resources_indexed(inputs, witness, closed=True)
    assert result['results'] == [True, True, False, False] and len(calls) == 2
    observer.verify_sdk_resources_indexed(inputs, witness, closed=True)
    assert len(calls) == 4  # Never shared across calls/witnesses.


def test_phase12_mutation_during_predicate_revokes_all_provisional_results(observer, monkeypatch):
    _, _, _, witness, resource = resource_from_cache_witness(observer)
    original = observer.sdk_resource_identity_verified
    def mutate(value, selected):
        good = original(value, selected)
        witness['finished'].append(deepcopy(witness['finished'][1]))
        return good
    monkeypatch.setattr(observer, 'sdk_resource_identity_verified', mutate)
    result = observer.verify_sdk_resources_indexed([resource], witness, closed=True)
    assert result['results'] == [False] and result['witness_drift']
    assert len(result['witness_drift']['after']['finished']) == 3


def test_phase12_atomic_stream_preserves_last_complete_file_on_partial_failure(observer, tmp_path):
    target = tmp_path/'last.json'; observer.write_json(target, {'complete': True})
    before = target.read_bytes()
    with pytest.raises(ValueError): observer.write_json(target, {'first': 'valid', 'late': float('nan')})
    assert target.read_bytes() == before


def test_phase12_compact_case_and_thin_loader_roundtrip_full_old_payload(observer, tmp_path):
    pool = observer.SdkReceiptAuthority(tmp_path)
    old = {'case_id': 'case', 'shard': 'shard-1', 'run_id': 'run', 'session_id': 'session',
        'map_artifact_id': 'map', 'passed': True, 'sdk_resources': [{'case_id': 'owner', 'used_in_case_id': 'case'}],
        'background_task_errors': [], 'nested': {'all_original_facts': [1, True, 1.0, None]}}
    directory = tmp_path/'shard-1'/'case'; directory.mkdir(parents=True)
    compact = pool.compact_record(old)
    observer.write_json(directory/'observation.json', compact)
    stub = observer.thin_case_record(compact, tmp_path)
    assert observer.load_thin_case(stub, tmp_path) == compact
    assert pool.expand_record(compact) == old
    assert 'nested' not in stub and stub['passed'] is True
    stub['run_id'] = 'other'
    with pytest.raises(ValueError): observer.load_thin_case(stub, tmp_path)


@pytest.mark.parametrize('ledger', ['starts', 'responses', 'bodies', 'extra_infos', 'response_extra_infos', 'finished'])
@pytest.mark.parametrize('target', [0, 1])
def test_phase12_every_current_prior_duplicate_matches_original_predicate(observer, ledger, target):
    _, _, _, witness, resource = resource_from_cache_witness(observer)
    witness[ledger].append(deepcopy(witness[ledger][target]))
    expected = observer.sdk_resource_identity_verified(resource, witness)
    assert expected is False
    assert observer.verify_sdk_resources_indexed([resource], witness)['results'] == [expected]


@pytest.mark.parametrize('kind', ['dict', 'list', 'int', 'str', 'float'])
def test_phase12_json_subclasses_cannot_alias_native_values(observer, kind):
    parent = {'dict': dict, 'list': list, 'int': int, 'str': str, 'float': float}[kind]
    class Unknown(parent): pass
    value = Unknown() if kind in ('dict', 'list') else Unknown(1)
    with pytest.raises(ValueError): observer.strict_json_bytes(value)


def test_phase12_hash_collision_compares_entire_typed_payload(observer, tmp_path, monkeypatch):
    pool = observer.SdkReceiptAuthority(tmp_path)
    real = observer.hashlib.sha256
    class Collision:
        def hexdigest(self): return 'f'*64
    monkeypatch.setattr(observer.hashlib, 'sha256', lambda *args, **kwargs: Collision())
    pool.reference({'value': True}, 0)
    with pytest.raises(ValueError, match='collision'): pool.reference({'value': 1}, 1)
    monkeypatch.setattr(observer.hashlib, 'sha256', real)


@pytest.mark.parametrize('ledger', ['responses', 'finished', 'extra_infos', 'response_extra_infos'])
@pytest.mark.asyncio
async def test_phase12_initial_body_selector_still_rejects_foreign_scope_same_id(observer, ledger):
    url, _, scope, witness = cache_witness_fixture()
    owner = observer.ChromeObserver(None, {}, Path('synthetic-no-access'))
    owner.network_request_starts = witness['starts']; owner.native_sdk_responses = witness['responses']
    owner.network_extra_infos = witness['extra_infos']; owner.network_response_extra_infos = witness['response_extra_infos']
    owner.network_finished = witness['finished']
    late = deepcopy(next(value for value in witness[ledger] if value['request_id'] == scope['request_id']))
    late.update(cdp_scope_id='foreign', page_target_id='foreign')
    witness[ledger].append(late)
    class MustNotRead:
        async def send(self, *args): raise AssertionError('producer must reject full same-ID ambiguity before CDP')
    owner.cdp = MustNotRead()
    with pytest.raises(ValueError, match='ambiguous'):
        await owner.native_sdk_body(url, 304, scope, redirect_chain_raw_url_sha256=[scope['raw_url_sha256']])


def phase12_case_record(resource):
    return {'case_id': 'consumer', 'shard': 'shard-1', 'run_id': 'run', 'session_id': 'session',
        'map_artifact_id': 'map', 'passed': True, 'sdk_resource_native_identity_verified': True,
        'sdk_resources': [{**deepcopy(resource), 'used_in_case_id': 'consumer', 'used_in_case_epoch': 'consumer-epoch',
            'inherited_from_same_cdp_context': True}],
        'sdk_resource_consumer_scope': {'case_id': 'consumer', 'case_epoch': 'consumer-epoch',
            'cdp_scope_id': resource['cdp_scope_id'], 'page_target_id': resource['page_target_id'],
            'run_id': 'run', 'session_id': 'session', 'artifact_ids': ['map']}}


@pytest.mark.parametrize('change', ['cdp', 'page', 'epoch', 'id', 'missing_usage', 'null_usage', 'int_inherited'])
def test_phase12_consumer_binding_is_not_replaceable_by_valid_foreign_receipt(observer, change):
    _, _, _, _, resource = resource_from_cache_witness(observer)
    record = phase12_case_record(resource)
    if change == 'cdp': record['sdk_resource_consumer_scope']['cdp_scope_id'] = 'foreign'
    if change == 'page': record['sdk_resource_consumer_scope']['page_target_id'] = 'foreign'
    if change == 'epoch': record['sdk_resources'][0]['used_in_case_epoch'] = 'foreign'
    if change == 'id': record['sdk_resources'][0]['used_in_case_id'] = 'foreign'
    if change == 'missing_usage': record['sdk_resources'][0].pop('used_in_case_id')
    if change == 'null_usage': record['sdk_resources'][0]['used_in_case_id'] = None
    if change == 'int_inherited': record['sdk_resources'][0]['inherited_from_same_cdp_context'] = 1
    assert observer.sdk_consumer_binding_verified(record) is False


def test_phase12_final_thin_flags_come_from_hydrated_late_red_case(observer, tmp_path):
    _, _, _, witness, resource = resource_from_cache_witness(observer)
    pool = observer.SdkReceiptAuthority(tmp_path)
    record = phase12_case_record(resource)
    directory = tmp_path/'shard-1'/'consumer'; directory.mkdir(parents=True)
    compact = pool.compact_record(record); observer.write_json(directory/'observation.json', compact)
    stubs = [observer.thin_case_record(compact, tmp_path)]
    witness['finished'].append(deepcopy(witness['finished'][1]))
    stats = observer.finalize_durable_cases(stubs, tmp_path, pool, witness, [], [])
    final = pool.expand_record(observer.load_thin_case(stubs[0], tmp_path))
    assert stats['complete_case_count'] == 1
    assert not stubs[0]['passed'] and not final['passed'] and final['sdk_resource_identity_revoked']
    assert not stubs[0]['sdk_resource_native_identity_verified']


def test_phase12_deferred_missing_case_does_not_destroy_other_complete_records(observer, tmp_path):
    _, _, _, witness, resource = resource_from_cache_witness(observer)
    pool = observer.SdkReceiptAuthority(tmp_path); record = phase12_case_record(resource)
    directory = tmp_path/'shard-1'/'consumer'; directory.mkdir(parents=True)
    compact = pool.compact_record(record); observer.write_json(directory/'observation.json', compact)
    stub = observer.thin_case_record(compact, tmp_path)
    (directory/'observation.json').unlink()
    stats = observer.finalize_durable_cases([stub], tmp_path, pool, witness, [], [])
    assert not stub['passed'] and 'case_record_error' in stub and stats['errors']


def test_phase12_whole_report_format_expands_exactly_without_omitted_receipts(observer, tmp_path):
    pool = observer.SdkReceiptAuthority(tmp_path)
    old = {'header': {'unchanged': [True, 1, 1.0]}, 'sdk_resources': [{'case_id': 'historical-owner', 'request_id': 'earlier'}],
        'records': [{'case_id': 'consumer', 'shard': 'shard-1', 'run_id': 'run', 'session_id': 'session',
            'map_artifact_id': 'map', 'passed': False, 'error': 'actual original error retained',
            'sdk_resources': [{'case_id': 'historical-owner', 'request_id': 'earlier', 'used_in_case_id': 'consumer'},
                {'case_id': 'last-owner', 'request_id': 'new'}, {'case_id': 'historical-owner', 'request_id': 'earlier', 'used_in_case_id': 'consumer'}],
            'partial_phases': ['hide', 'show'], 'restored_all_shown': False}]}
    compact_record = pool.compact_record(old['records'][0])
    path = tmp_path/'shard-1'/'consumer'; path.mkdir(parents=True)
    observer.write_json(path/'observation.json', compact_record)
    encoded = {**old, 'evidence_schema': observer.COMPACT_EVIDENCE_SCHEMA,
        'records': [observer.thin_case_record(compact_record, tmp_path)],
        'sdk_resources': [pool.reference(value, index) for index, value in enumerate(old['sdk_resources'])],
        'sdk_receipt_authority': pool.table()}
    assert len(encoded['sdk_receipt_authority']) == 2
    assert observer.strict_json_bytes(observer.expand_evidence_report(encoded, tmp_path)) == observer.strict_json_bytes(old)
    encoded['sdk_receipt_authority'].pop()
    with pytest.raises(ValueError): observer.expand_evidence_report(encoded, tmp_path)


def test_phase12_atomic_stream_late_chunk_failure_does_not_publish_partial_green(observer, tmp_path, monkeypatch):
    target = tmp_path/'audit-summary.json'; observer.write_json(target, {'complete': 'previous'})
    previous = target.read_bytes()
    def broken(self, value, **kwargs):
        yield '{"pilot_passed": true, '
        raise ValueError('synthetic late serialization failure')
    monkeypatch.setattr(observer.json.JSONEncoder, 'iterencode', broken)
    with pytest.raises(ValueError): observer.write_json(target, {'pilot_passed': True})
    assert target.read_bytes() == previous and not list(tmp_path.glob('*.pending'))


def test_phase12_same_length_nested_authority_drift_revokes_cached_true(observer, monkeypatch):
    _, _, _, witness, resource = resource_from_cache_witness(observer)
    original = observer.sdk_resource_identity_verified
    def mutate(value, selected):
        result = original(value, selected)
        witness['finished'][1]['native_event']['requestId'] = 'changed-same-length'
        return result
    monkeypatch.setattr(observer, 'sdk_resource_identity_verified', mutate)
    result = observer.verify_sdk_resources_indexed([resource, deepcopy(resource)], witness, closed=True)
    assert result['results'] == [False, False] and result['witness_drift']


@pytest.mark.parametrize('change', ['wrong_hash', 'missing', 'cross_root', 'wrong_case', 'nonfinite'])
def test_phase12_durable_case_loader_never_falls_back_or_trusts_stale_stub(observer, tmp_path, change):
    pool = observer.SdkReceiptAuthority(tmp_path)
    record = {'shard': 'shard-1', 'case_id': 'case', 'run_id': 'run', 'session_id': 'session', 'map_artifact_id': 'map', 'passed': True}
    directory = tmp_path/'shard-1'/'case'; directory.mkdir(parents=True)
    observer.write_json(directory/'observation.json', record); stub = observer.thin_case_record(record, tmp_path)
    if change == 'wrong_hash': stub['_case_record_ref']['file_sha256'] = '0'*64
    if change == 'missing': (directory/'observation.json').unlink()
    if change == 'cross_root': stub['_case_record_ref']['relative_path'] = '../case/observation.json'
    if change == 'wrong_case': stub['_case_record_ref']['binding']['case_id'] = 'foreign'
    if change == 'nonfinite': stub['_case_record_ref']['binding']['case_id'] = float('nan')
    with pytest.raises(ValueError): observer.load_thin_case(stub, tmp_path)


@pytest.mark.parametrize('resource_kind', ['single200', 'cache304', 'twohop200'])
def test_phase12_indexed_candidates_exactly_match_original_related_order(observer, resource_kind):
    if resource_kind == 'twohop200': witness, resource = ordinary_two_hop_fixture(observer)
    else:
        _, _, _, witness, resource = resource_from_cache_witness(observer)
        if resource_kind == 'single200': resource = deepcopy(witness['prior_receipts'][0])
    verification = observer.SdkVerificationPass(witness)
    selected, positions = verification.selected(resource)
    ids = {resource['request_id']}
    if resource_kind == 'cache304': ids.add(resource['native_cache_proof']['prior_request_id'])
    for name in observer.SDK_LEDGER_NAMES:
        expected = [index for index, value in enumerate(witness[name]) if observer.sdk_authority_related(value, ids, resource, name)]
        assert positions[name] == expected
        assert selected[name] == [witness[name][index] for index in expected]
    assert verification.verify(resource) == observer.sdk_resource_identity_verified(resource, witness)
    assert verification.finish()


def test_phase12_new_receipt_version_is_retained_after_same_url_slot_replacement(observer, tmp_path):
    pool = observer.SdkReceiptAuthority(tmp_path)
    first = {'raw_url_sha256': 'a'*64, 'request_id': 'old', 'case_id': 'owner-1', 'sha256': 'b'*64}
    second = {**first, 'request_id': 'new', 'case_id': 'owner-2'}
    context_slot = {'a'*64: first}
    old_ref = pool.reference(context_slot['a'*64], 0)
    context_slot['a'*64] = second
    new_ref = pool.reference(context_slot['a'*64], 1)
    assert len(pool.table()) == 2 and pool.expand(old_ref) == first and pool.expand(new_ref) == second


def test_phase12_export_refuses_stale_thin_passed_even_when_bound_file_is_valid(observer, tmp_path):
    pool = observer.SdkReceiptAuthority(tmp_path)
    record = {'shard': 'shard-1', 'case_id': 'case', 'run_id': 'run', 'session_id': 'session', 'map_artifact_id': 'map', 'passed': False}
    directory = tmp_path/'shard-1'/'case'; directory.mkdir(parents=True)
    observer.write_json(directory/'observation.json', record); stub = observer.thin_case_record(record, tmp_path)
    stub['passed'] = True
    with pytest.raises(ValueError, match='terminal scalar'): observer.validate_evidence_export([stub], pool, [])


def test_phase12_mutating_resource_not_only_witness_cannot_keep_provisional_green(observer, monkeypatch):
    _, _, _, witness, resource = resource_from_cache_witness(observer)
    original = observer.sdk_resource_identity_verified
    def mutate(value, selected):
        good = original(value, selected); value['sha256'] = '0'*64; return good
    monkeypatch.setattr(observer, 'sdk_resource_identity_verified', mutate)
    result = observer.verify_sdk_resources_indexed([resource], witness, closed=True)
    assert result['results'] == [False]
    assert result['resource_drift'][0]['before']['sha256'] != result['resource_drift'][0]['after']['sha256']


@pytest.mark.parametrize('change', ['run', 'session', 'artifacts'])
def test_phase12_consumer_scope_run_session_and_artifact_binding_cannot_drift(observer, change):
    _, _, _, _, resource = resource_from_cache_witness(observer)
    record = phase12_case_record(resource)
    assert observer.sdk_consumer_binding_verified(record)
    scope = record['sdk_resource_consumer_scope']
    if change == 'run': scope['run_id'] = 'foreign'
    if change == 'session': scope['session_id'] = 'foreign'
    if change == 'artifacts': scope['artifact_ids'] = ['foreign']
    assert not observer.sdk_consumer_binding_verified(record)


def test_phase12_production_final_pass_does_not_retain_per_usage_diagnostic_graphs(observer, monkeypatch):
    _, _, _, witness, resource = resource_from_cache_witness(observer)
    verification = observer.SdkVerificationPass(witness, closed=True)
    selections = []; original = verification.selected
    def spy(value): selections.append(1); return original(value)
    monkeypatch.setattr(verification, 'selected', spy)
    for index in range(100):
        assert verification.verify({**resource, 'used_in_case_id': f'consumer-{index}'})
    assert verification.finish() and verification.predicate_calls == 1
    assert verification.positions == [] and len(selections) == 1


def phase12_A_durable_report(observer, tmp_path, record):
    pool = observer.SdkReceiptAuthority(tmp_path)
    compact = pool.compact_record(record)
    target = tmp_path/record['shard']/record['case_id']/'observation.json'; target.parent.mkdir(parents=True)
    observer.write_json(target, compact)
    stub = observer.thin_case_record(compact, tmp_path)
    report = {'evidence_schema': observer.COMPACT_EVIDENCE_SCHEMA, 'sdk_receipt_authority': pool.table(),
        'sdk_resources': [], 'records': [stub], 'pilot_passed': False, 'full_map_gate': False}
    return pool, target, stub, report


def test_phase12_A_publication_failure_cannot_expand_prior_green_as_terminal(observer, monkeypatch, tmp_path):
    """Reviewer exact mechanism: real durable file + injected write OSError, no SDK/browser mock."""
    _, _, _, witness, resource = resource_from_cache_witness(observer)
    record = phase12_case_record(resource)
    pool, target, stub, report = phase12_A_durable_report(observer, tmp_path, record)
    before = target.read_bytes()
    witness['finished'].append(deepcopy(witness['finished'][1]))
    original_write = observer.write_json
    def fail_publication(path, value):
        if Path(path) == target: raise OSError('synthetic isolated final case publication failure')
        return original_write(path, value)
    monkeypatch.setattr(observer, 'write_json', fail_publication)
    stats = observer.finalize_durable_cases([stub], tmp_path, pool, witness, [], [], closed_barrier=True)
    assert stats['errors'] and not stub['passed'] and stub['case_record_error']
    assert target.read_bytes() == before and observer.strict_read_json(target)['passed'] is True
    # Internal hydration may still inspect the bound prior file for cleanup/revocation.
    assert observer.load_thin_case(stub, tmp_path)['passed'] is True
    with pytest.raises(ValueError, match='incomplete|terminal'):
        observer.expand_evidence_report(report, tmp_path)
    with pytest.raises(ValueError, match='incomplete|terminal'):
        observer.validate_evidence_export([stub], pool, [])


@pytest.mark.parametrize('change', ['passed', 'extra_null', 'missing_null', 'bool_int', 'int_float', 'extra_nonscalar'])
@pytest.mark.parametrize('operation', ['expand', 'validate'])
def test_phase12_A_external_terminal_consistency_requires_presence_and_exact_types(observer, tmp_path, change, operation):
    record = {'shard': 'shard-1', 'case_id': 'case', 'run_id': 'run', 'session_id': 'session',
        'map_artifact_id': 'map', 'passed': True, 'explicit_null': None, 'number': 1}
    pool, _, stub, report = phase12_A_durable_report(observer, tmp_path, record)
    if change == 'passed': stub['passed'] = False
    if change == 'extra_null': stub['absent_in_file'] = None
    if change == 'missing_null': stub.pop('explicit_null')
    if change == 'bool_int': stub['passed'] = 1
    if change == 'int_float': stub['number'] = 1.0
    if change == 'extra_nonscalar': stub['unknown'] = {'value': True}
    with pytest.raises(ValueError, match='terminal'):
        if operation == 'expand': observer.expand_evidence_report(report, tmp_path)
        else: observer.validate_evidence_export([stub], pool, [])


@pytest.mark.parametrize('change', ['hash', 'owner', 'path'])
def test_phase12_A_external_owner_path_hash_guards_stay_closed(observer, tmp_path, change):
    record = {'shard': 'shard-1', 'case_id': 'case', 'run_id': 'run', 'session_id': 'session', 'map_artifact_id': 'map', 'passed': False}
    pool, _, stub, report = phase12_A_durable_report(observer, tmp_path, record)
    if change == 'hash': stub['_case_record_ref']['file_sha256'] = '0'*64
    if change == 'owner': stub['_case_record_ref']['binding']['run_id'] = 'foreign'
    if change == 'path': stub['_case_record_ref']['relative_path'] = '../foreign.json'
    with pytest.raises(ValueError): observer.expand_evidence_report(report, tmp_path)
    with pytest.raises(ValueError): observer.validate_evidence_export([stub], pool, [])


def test_phase12_A_completed_failed_record_remains_exactly_reconstructible(observer, tmp_path):
    record = {'shard': 'shard-1', 'case_id': 'case', 'run_id': 'run', 'session_id': 'session',
        'map_artifact_id': 'map', 'passed': False, 'error': 'complete actual failed result',
        'sdk_resource_identity_revoked': True, 'restored_all_shown': False, 'explicit_null': None,
        'sdk_resources': [], 'late_evidence': {'source': [False, 0, None]}}
    pool, _, stub, report = phase12_A_durable_report(observer, tmp_path, record)
    assert observer.validate_evidence_export([stub], pool, []) is True
    assert observer.expand_evidence_report(report, tmp_path)['records'][0] == record


def test_phase12_A_explicit_report_incompleteness_cannot_expand_as_complete(observer, tmp_path):
    record = {'shard': 'shard-1', 'case_id': 'case', 'run_id': 'run', 'session_id': 'session', 'map_artifact_id': 'map', 'passed': False}
    _, _, _, report = phase12_A_durable_report(observer, tmp_path, record)
    report['evidence_completeness'] = {'complete': False, 'errors': [{'reason': 'durable final case incomplete'}]}
    with pytest.raises(ValueError, match='incomplete'): observer.expand_evidence_report(report, tmp_path)


@pytest.mark.parametrize('error_value', [None, '', False, 0])
def test_phase12_A_error_stub_presence_is_incomplete_even_if_error_value_falsey(observer, tmp_path, error_value):
    record = {'shard': 'shard-1', 'case_id': 'case', 'run_id': 'run', 'session_id': 'session', 'map_artifact_id': 'map', 'passed': False}
    pool, _, stub, report = phase12_A_durable_report(observer, tmp_path, record)
    stub['case_record_error'] = error_value
    with pytest.raises(ValueError, match='incomplete'): observer.validate_evidence_export([stub], pool, [])
    with pytest.raises(ValueError, match='incomplete'): observer.expand_evidence_report(report, tmp_path)


def test_phase12_A_internal_drift_rehydrates_prior_green_then_publishes_complete_red(observer, monkeypatch, tmp_path):
    _, _, _, witness, resource = resource_from_cache_witness(observer)
    record = phase12_case_record(resource)
    base, response, _, selected = projection_reference_fixture()
    record['session_id'] = record['sdk_resource_consumer_scope']['session_id'] = selected['id']
    projection = observer.verify_session_projection(observer.make_projection_reference(**base), **response)
    projection['case_id'] = record['case_id']
    pool, _, stub, report = phase12_A_durable_report(observer, tmp_path, record)
    original_finish = observer.SdkVerificationPass.finish
    def drift_after_first_publication(verification):
        witness['finished'].append(deepcopy(witness['finished'][1]))
        return original_finish(verification)
    monkeypatch.setattr(observer.SdkVerificationPass, 'finish', drift_after_first_publication)
    stats = observer.finalize_durable_cases([stub], tmp_path, pool, witness, [], [projection], closed_barrier=True)
    assert not stats['errors'] and stats['witness_drift'] and not stub['passed']
    assert 'case_record_error' not in stub
    record = pool.expand_record(observer.load_thin_case(stub, tmp_path))
    assert not record['passed'] and record['sdk_resource_identity_revoked'] and record['sdk_authority_drift']
    report['sdk_receipt_authority'] = pool.table()
    assert observer.validate_evidence_export([stub], pool, []) is True
    assert observer.expand_evidence_report(report, tmp_path)['records'][0] == record


@pytest.mark.parametrize('completeness', [None, {'complete': 1, 'errors': []}, {'complete': True},
    {'complete': True, 'errors': None}, {'complete': True, 'errors': [{'contradiction': 'present'}]}])
def test_phase12_A_unknown_or_conflicting_completeness_shape_is_not_a_true_claim(observer, tmp_path, completeness):
    record = {'shard': 'shard-1', 'case_id': 'case', 'run_id': 'run', 'session_id': 'session', 'map_artifact_id': 'map', 'passed': False}
    _, _, _, report = phase12_A_durable_report(observer, tmp_path, record)
    report['evidence_completeness'] = completeness
    with pytest.raises(ValueError, match='incomplete'): observer.expand_evidence_report(report, tmp_path)


# Phase13: source-bound .12 oracle and the independently observed native RED.
# These controls are offline argument/CPU checks, never new map/model successes.
def sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
        separators=(',', ':')).encode('utf8')


def save_json(path, value):
    Path(path).write_bytes(json.dumps(value, sort_keys=True, ensure_ascii=False,
        allow_nan=False, indent=2).encode('utf8') + b'\n')


@pytest.fixture(scope='session')
def phase13_v12():
    path = TOOLING/'fixtures/cancellation_v12.py'
    assert sha_file(path) == '3670e9ea3f110cfbf894e6ed1723d95cc951a29b9fbdc51ea85ed44dec42417f'
    return phase13_load_source(path, 'phase13_immutable_v12_oracle')


def phase13_load_source(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    # Deliberately bypass bytecode-writing loader execution. The immutable .12
    # archive stays read-only even if a surrounding runner did not use -B.
    exec(compile(path.read_bytes(), str(path), 'exec'), module.__dict__)
    return module

def native_cancel_workload(error_count=24, unrelated_per_ledger=768):
    """Exact builtin inputs; ordinary artifact_ids intentionally shares a list."""
    origins = ['http://127.0.0.1:6201']
    shared_artifacts = ['art_synthetic_offline_shared']

    def one(index):
        url = origins[0] + f'/api/sessions/session_{index}/messages'
        scope = {'case_id': f'case-{index}', 'case_epoch': f'epoch-{index}',
            'cdp_scope_id': 'cdp-native-offline', 'page_target_id': 'page-native-offline',
            'session_id': f'session_{index}', 'run_id': f'run_{index}',
            'artifact_ids': shared_artifacts, 'method': 'GET', 'url': url,
            'raw_url_sha256': hashlib.sha256(url.encode()).hexdigest(), 'frame_id': 'frame-native-offline'}
        paused = {**scope, 'request_id': f'fetch-{index}', 'network_id': f'network-{index}',
            'operation': 'Fetch.continueRequest', 'received_sequence': index * 10 + 2}
        error = {**paused, 'task_kind': 'request_guard', 'gate_failure': True,
            'classification': 'invalid_interception_id', 'request_cancellation_confirmed': False,
            'error': 'Error: CDPSession.send: Protocol error (Fetch.continueRequest): Invalid InterceptionId.'}
        native = {**scope, 'requestId': f'network-{index}', 'network_id': f'network-{index}',
            'canceled': True, 'errorText': 'net::ERR_ABORTED', 'type': 'Fetch', 'blockedReason': None,
            'timestamp': -5000, 'received_sequence': index * 10 + 4,
            'cors_status_field_present': False, 'blocked_reason_field_present': True}
        native['native_event'] = {key: native[key] for key in
            ('requestId', 'canceled', 'errorText', 'type', 'blockedReason', 'timestamp')}
        start = {**scope, 'request_id': f'network-{index}', 'loader_id': 'loader-native-offline',
            'resource_type': 'Fetch', 'redirect_hop': False, 'received_sequence': index * 10 + 1}
        start['native_identity'] = {key: start[key] for key in
            ('request_id', 'raw_url_sha256', 'method', 'frame_id', 'loader_id', 'resource_type', 'redirect_hop')}
        return paused, error, native, start

    paused_raw, native_raw, starts_raw, errors = [], [], [], []
    for index in range(error_count + unrelated_per_ledger):
        paused, error, native, start = one(index)
        paused_raw.append(paused); native_raw.append(native); starts_raw.append(start)
        if index < error_count:
            errors.append(error)
    return origins, errors, paused_raw, native_raw, starts_raw, shared_artifacts


def assert_native_value(value):
    if type(value) is dict:
        assert all(type(key) is str for key in value)
        for child in value.values():
            assert_native_value(child)
    elif type(value) is list:
        for child in value:
            assert_native_value(child)
    else:
        assert type(value) in (str, int, float, bool, type(None))
        if type(value) is float:
            assert math.isfinite(value)


def test_cancel_relation_selectors_have_linear_raw_dict_get_budget(observer, tmp_path):
    origins, errors, paused, native, starts, shared_artifacts = native_cancel_workload()
    raw_rows = paused + native + starts
    assert_native_value([origins, errors, paused, native, starts])
    assert all(row['artifact_ids'] is shared_artifacts for row in raw_rows + errors)
    input_before = canonical([origins, paused, native, starts])
    raw_identities = {id(row) for row in raw_rows}
    module_filename = observer.reconcile_canceled_read_errors.__code__.co_filename
    counts = {'raw_dict_get_c_calls': 0, 'same_context_python_calls': 0}

    def count_real_calls(frame, event, argument):
        if frame.f_code.co_filename != module_filename:
            return
        if event == 'call' and frame.f_code.co_name == 'same_context':
            counts['same_context_python_calls'] += 1
        if (event == 'c_call' and getattr(argument, '__name__', None) == 'get'
                and type(getattr(argument, '__self__', None)) is dict
                and id(argument.__self__) in raw_identities):
            counts['raw_dict_get_c_calls'] += 1

    previous_profiler = sys.getprofile()
    try:
        sys.setprofile(count_real_calls)
        result = observer.reconcile_canceled_read_errors(errors, paused, native, origins, request_starts=starts)
    finally:
        sys.setprofile(previous_profiler)
    restored = sys.getprofile() is previous_profiler
    semantics = {
        'return_is_none': result is None,
        'all_24_confirmed': len(errors) == 24 and all(row['classification'] == 'confirmed_canceled_read' for row in errors),
        'all_24_nonblocking': all(row['gate_failure'] is False for row in errors),
        'all_24_start_proofs_bound': all(row['request_start_proof']['request_id'] == row['network_id'] for row in errors),
        'gate_full_proof_replay_no_blocking': observer.gate_task_errors(errors) == [],
        'raw_and_origins_values_unchanged': input_before == canonical([origins, paused, native, starts]),
        'ordinary_shared_artifact_list_preserved': all(row['artifact_ids'] is shared_artifacts for row in raw_rows + errors),
        'profiler_restored_in_finally': restored,
    }
    source_sha = sha_file(Path(observer.__file__))
    function_sha = hashlib.sha256(inspect.getsource(observer.reconcile_canceled_read_errors).encode()).hexdigest()
    budget = 16 * (len(raw_rows) + len(errors))
    metrics = {'kind': 'synthetic_offline_deterministic_cost_control', 'source_module_sha256': source_sha,
        'source_function_sha256': function_sha, 'error_count': len(errors),
        'raw_ledger_rows': {'paused': len(paused), 'native': len(native), 'starts': len(starts)},
        'unrelated_rows_per_ledger': 768, 'counts': counts, 'linear_raw_get_budget': budget,
        'old_nested_relation_product': len(errors) * len(raw_rows), 'semantics': semantics,
        'wall_time_threshold_used': False, 'native_input_subclasses_or_hooks': False,
        'new_live_or_model_successes': 0}
    save_json(tmp_path / 'deterministic-cost-metrics.json', metrics)
    save_json(tmp_path / 'reconciliation-result.json', {
        'kind': 'complete_synthetic_offline_reconciliation_result', 'source_module_sha256': source_sha,
        'source_function_sha256': function_sha, 'return_value': result, 'errors_after': errors,
        'input_values_sha256_before_and_after': hashlib.sha256(input_before).hexdigest(), 'semantics': semantics,
    })
    assert all(semantics.values()), semantics
    assert counts['raw_dict_get_c_calls'] <= budget, (
        'Repeated raw ledger reads exceed a conservative linear selection budget: '
        f"raw_dict_get={counts['raw_dict_get_c_calls']} > linear_budget={budget}; "
        f"same_context_calls={counts['same_context_python_calls']}, "
        f'errors={len(errors)}, raw_rows={len(raw_rows)}. '
        'This is a deterministic CPU-access regression, not a wall-time or missing-helper failure.'
    )




def phase13_graph():
    origins, paused, error, native, start = early_start_fixture()
    native['case_id'], native['case_epoch'] = error['case_id'], error['case_epoch']
    return {'errors': [error], 'paused': [paused], 'native': [native], 'starts': [start], 'origins': origins}


def phase13_typed_graph(value, seen=None):
    """Presence, native scalar types, order, multiplicity and aliases, without hooks."""
    seen = {} if seen is None else seen
    kind = type(value)
    name = (kind.__module__, kind.__qualname__)
    if value is None or kind is str or kind is bool or kind is int:
        return [name, value]
    if isinstance(value, str):
        return [name, str.__str__(value)]
    if kind is float:
        return [name, value.hex()]
    if kind is bytes:
        return [name, value.hex()]
    identity = id(value)
    if identity in seen:
        return ['reference', seen[identity]]
    seen[identity] = len(seen)
    if isinstance(value, dict):
        return [name, seen[identity], [[phase13_typed_graph(k, seen), phase13_typed_graph(v, seen)]
            for k, v in dict.items(value)]]
    if isinstance(value, list):
        return [name, seen[identity], [phase13_typed_graph(v, seen) for v in list.__iter__(value)]]
    if isinstance(value, tuple):
        return [name, seen[identity], [phase13_typed_graph(v, seen) for v in tuple.__iter__(value)]]
    try:
        state = object.__getattribute__(value, '__dict__')
    except AttributeError:
        state = None
    return [name, seen[identity], phase13_typed_graph(state, seen)]


def phase13_execute(module, graph, trace):
    exception = None
    try:
        result = module.reconcile_canceled_read_errors(graph['errors'], graph['paused'], graph['native'],
            graph['origins'], request_starts=graph['starts'])
    except Exception as error:
        result = None
        exception = [type(error).__module__, type(error).__qualname__, error.args]
    return phase13_typed_graph({'graph': graph, 'trace': trace, 'exception': exception, 'return': result})


def phase13_equivalent(observer, old, factory):
    # Factories for hook inputs must construct fresh graphs. Native factories
    # below deepcopy the WHOLE argument graph once, never one argument at a time.
    graph, trace = factory()
    expected = phase13_execute(old, graph, trace)
    graph, trace = factory()
    actual = phase13_execute(observer, graph, trace)
    assert actual == expected
    return graph, trace


def phase13_native_factory(graph):
    return lambda: (deepcopy(graph), [])


@pytest.mark.parametrize('eligible', [False, True])
def test_phase13_T01_T02_lazy_boundary_original_mutation_and_failure(observer, phase13_v12, eligible):
    def factory():
        trace = []
        class NeverIterate:
            def __iter__(self):
                trace.append('unexpected raw/origins iteration')
                raise AssertionError('original must not touch these without eligible errors')
        if not eligible:
            return {'errors': [{'classification': 'unknown', 'task_kind': 'request_guard'}],
                'paused': NeverIterate(), 'native': NeverIterate(), 'starts': NeverIterate(),
                'origins': NeverIterate()}, trace
        graph = phase13_graph()
        graph['errors'].insert(0, {'classification': 'unknown', 'task_kind': 'request_guard'})
        graph['paused'] = [None]
        return graph, trace
    graph, trace = phase13_equivalent(observer, phase13_v12, factory)
    assert trace == []
    if eligible:
        assert graph['errors'][1]['original_classification'] == 'invalid_interception_id'
        assert graph['errors'][1]['gate_failure'] is True
    else:
        graph = {'errors': [], 'paused': None, 'native': None, 'starts': None, 'origins': None}
        phase13_equivalent(observer, phase13_v12, phase13_native_factory(graph))


@pytest.mark.parametrize('argument', ['errors', 'paused', 'native', 'starts', 'origins'])
@pytest.mark.parametrize('container', ['generator', 'list_subclass', 'tuple_subclass', 'tuple'])
def test_phase13_T03_iterators_and_subclasses_preserve_consumption_trace(observer, phase13_v12, argument, container):
    def factory():
        graph, trace = phase13_graph(), []
        graph['errors'].append(deepcopy(graph['errors'][0]))
        original = graph[argument]
        if container == 'generator':
            def values():
                for index, value in enumerate(original):
                    trace.append(('yield', argument, index))
                    yield value
            graph[argument] = values()
        elif container in {'list_subclass', 'tuple_subclass'}:
            base = list if container == 'list_subclass' else tuple
            class HookContainer(base):
                def __iter__(self):
                    trace.append(('iter', argument))
                    return base.__iter__(self)
            graph[argument] = HookContainer(original)
        else:
            graph[argument] = tuple(original)
        return graph, trace
    phase13_equivalent(observer, phase13_v12, factory)


PHASE13_SELECTOR_FIELDS = [
    *[(where, field) for where in ('errors', 'paused') for field in
        ('cdp_scope_id', 'page_target_id', 'request_id', 'network_id')],
    *[('native', field) for field in ('cdp_scope_id', 'page_target_id', 'requestId', 'network_id')],
    *[('starts', field) for field in ('cdp_scope_id', 'page_target_id', 'request_id')]]


@pytest.mark.parametrize('where,field', PHASE13_SELECTOR_FIELDS)
@pytest.mark.parametrize('value', ['missing', None, ''])
def test_phase13_T04_missing_null_empty_get_semantics(observer, phase13_v12, where, field, value):
    graph = phase13_graph()
    if value == 'missing':
        graph[where][0].pop(field, None)
    else:
        graph[where][0][field] = value
    phase13_equivalent(observer, phase13_v12, phase13_native_factory(graph))


@pytest.mark.parametrize('where,field', PHASE13_SELECTOR_FIELDS)
@pytest.mark.parametrize('bad', ['bool', 'int', 'float', 'nan', 'bytes', 'list', 'dict', 'hook_str', 'inconsistent_hash_str'])
def test_phase13_T05_unknown_selector_values_whole_call_original_behavior(observer, phase13_v12, where, field, bad):
    def factory():
        graph, trace = phase13_graph(), []
        class HookString(str):
            def __eq__(self, other):
                trace.append(('eq', str.__str__(self)))
                raise RuntimeError('original equality hook')
            def __hash__(self):
                trace.append(('hash', str.__str__(self)))
                raise RuntimeError('original hash hook')
        class InconsistentHashString(str):
            def __eq__(self, other):
                trace.append(('str.eq', str.__str__(self)))
                return str.__eq__(self, other)
            def __hash__(self):
                trace.append(('str.hash', str.__str__(self)))
                return 0
        value = {'bool': True, 'int': 1, 'float': 1.0, 'nan': float('nan'), 'bytes': b'foreign',
            'list': [], 'dict': {}, 'hook_str': HookString('foreign'),
            'inconsistent_hash_str': InconsistentHashString(graph[where][0].get(field, 'foreign'))}[bad]
        graph[where][0][field] = value
        return graph, trace
    phase13_equivalent(observer, phase13_v12, factory)


@pytest.mark.parametrize('where', ['errors', 'paused', 'native', 'starts'])
def test_phase13_T05_T06_mapping_and_custom_collision_keys_do_not_add_hook_calls(observer, phase13_v12, where):
    def mapping_factory():
        graph, trace = phase13_graph(), []
        class HookMapping(dict):
            def get(self, key, default=None):
                trace.append(('mapping.get', key))
                return dict.get(self, key, default)
        graph[where][0] = HookMapping(graph[where][0])
        return graph, trace
    phase13_equivalent(observer, phase13_v12, mapping_factory)
    def key_factory():
        graph, trace = phase13_graph(), []
        class CollisionKey:
            def __hash__(self):
                trace.append('key.hash')
                return hash('cdp_scope_id')
            def __eq__(self, other):
                trace.append('key.eq')
                raise LookupError('original colliding key')
        row = {CollisionKey(): 'foreign'}
        graph[where][0] = row
        return graph, trace
    phase13_equivalent(observer, phase13_v12, key_factory)


@pytest.mark.parametrize('where', ['paused', 'native', 'starts'])
def test_phase13_T07_wrong_context_short_circuits_unhashable_id(observer, phase13_v12, where):
    graph = phase13_graph()
    row = graph[where][0]
    row.update({'cdp_scope_id': 'wrong-context', 'request_id': [], 'requestId': {}, 'network_id': []})
    phase13_equivalent(observer, phase13_v12, phase13_native_factory(graph))


@pytest.mark.parametrize('change', ['paused_network_only', 'paused_fetch_only', 'paused_both',
    'native_request_only', 'native_alias_only', 'native_nested_contradiction',
    'case', 'epoch', 'url', 'method', 'type', 'cdp', 'page',
    'equal_rows', 'same_object_rows', 'error_is_raw_and_repeated'])
def test_phase13_T08_T09_T10_T15_or_scope_and_physical_multiplicity(observer, phase13_v12, change):
    graph = phase13_graph()
    row = graph['paused'][0]
    if change == 'paused_network_only': row['request_id'] = 'foreign-fetch'
    if change == 'paused_fetch_only': row['network_id'] = 'foreign-network'
    if change == 'native_request_only': graph['native'][0]['network_id'] = 'foreign-network'
    if change == 'native_alias_only': graph['native'][0]['requestId'] = 'foreign-network'
    if change == 'native_nested_contradiction': graph['native'][0]['native_event']['requestId'] = 'foreign-network'
    field = {'case': 'case_id', 'epoch': 'case_epoch', 'url': 'url', 'method': 'method',
        'type': 'resource_type', 'cdp': 'cdp_scope_id', 'page': 'page_target_id'}.get(change)
    if field:
        late = deepcopy(row); late[field] = 'foreign'; graph['paused'].append(late)
    if change in {'equal_rows', 'same_object_rows'}:
        foreign = deepcopy(row); foreign.update(request_id='foreign-fetch', network_id='foreign-network')
        graph['paused'] = [row, foreign, deepcopy(row) if change == 'equal_rows' else row]
    if change == 'error_is_raw_and_repeated':
        error = graph['errors'][0]
        graph['paused'] = [error]
        graph['errors'] = [error, error]
    phase13_equivalent(observer, phase13_v12, phase13_native_factory(graph))


@pytest.mark.parametrize('target', ['paused', 'native', 'starts', 'errors', 'origins', 'artifact_ids'])
def test_phase13_T11_T14_history_write_alias_whole_call_preserves_live_mutation(observer, phase13_v12, target):
    graph = phase13_graph()
    first = graph['errors'][0]
    second = deepcopy(first)
    second.update(cdp_scope_id=None, page_target_id=None, network_id=None, request_id=None)
    graph['errors'].append(second)
    first['cancellation_reconciliation_history'] = first['artifact_ids'] if target == 'artifact_ids' else graph[target]
    phase13_equivalent(observer, phase13_v12, phase13_native_factory(graph))


def test_phase13_T12_origins_hook_mutates_later_row_without_extra_probes(observer, phase13_v12):
    def factory():
        graph, trace = phase13_graph(), []
        graph['errors'].append(deepcopy(graph['errors'][0]))
        late = deepcopy(graph['paused'][0]); late.update(request_id='foreign-fetch', network_id='foreign-network')
        graph['paused'].append(late)
        class HookOrigins(list):
            def __iter__(self):
                trace.append('origins.iter')
                if len(trace) == 2:
                    late['network_id'] = graph['errors'][0]['network_id']
                return list.__iter__(self)
        graph['origins'] = HookOrigins(graph['origins'])
        return graph, trace
    graph, _ = phase13_equivalent(observer, phase13_v12, factory)
    assert graph['errors'][0]['gate_failure'] is False and graph['errors'][1]['gate_failure'] is True
    assert len(graph['errors'][1]['authoritative_paused_records']) == 2


@pytest.mark.parametrize('hook', ['previous_proof_deepcopy', 'raw_payload_deepcopy', 'error_get'])
def test_phase13_T13_custom_hooks_keep_original_order_and_side_effects(observer, phase13_v12, hook):
    def factory():
        graph, trace = phase13_graph(), []
        graph['errors'].append(deepcopy(graph['errors'][0]))
        late = deepcopy(graph['paused'][0]); late.update(request_id='foreign-fetch', network_id='foreign-network')
        graph['paused'].append(late)
        class CopyHook:
            def __deepcopy__(self, memo):
                trace.append('deepcopy')
                late['network_id'] = graph['errors'][0]['network_id']
                return 'copied'
        if hook == 'previous_proof_deepcopy':
            graph['errors'][0].update(classification='confirmed_canceled_read', fetch_request_paused=CopyHook())
        elif hook == 'raw_payload_deepcopy':
            graph['paused'][0]['unknown_payload'] = CopyHook()
        else:
            class ErrorHook(dict):
                def get(self, key, default=None):
                    trace.append(('error.get', key))
                    if key == 'task_kind':
                        late['network_id'] = dict.get(self, 'network_id')
                    return dict.get(self, key, default)
            graph['errors'][1] = ErrorHook(graph['errors'][1])
        return graph, trace
    phase13_equivalent(observer, phase13_v12, factory)


@pytest.mark.parametrize('late_kind', ['same_fetch_other_network', 'same_network_other_fetch',
    'contradictory_native', 'native_outer_request', 'native_outer_alias', 'duplicate_native',
    'duplicate_start', 'cross_case_paused', 'cross_case_native', 'cross_case_start'])
def test_phase13_T16_T17_late_conflict_revokes_and_truncation_cannot_resurrect(observer, phase13_v12, late_kind):
    def run(module):
        graph = phase13_graph()
        states = [phase13_execute(module, graph, [])]
        where = 'paused' if late_kind in {'same_fetch_other_network', 'same_network_other_fetch', 'cross_case_paused'} else (
            'starts' if late_kind in {'duplicate_start', 'cross_case_start'} else 'native')
        late = deepcopy(graph[where][0])
        if late_kind == 'same_fetch_other_network': late['network_id'] = 'foreign-network'
        if late_kind == 'same_network_other_fetch': late['request_id'] = 'foreign-fetch'
        if late_kind == 'contradictory_native': late['canceled'] = late['native_event']['canceled'] = False
        if late_kind == 'native_outer_request': late['network_id'] = 'foreign-network'
        if late_kind == 'native_outer_alias': late['requestId'] = 'foreign-network'
        if late_kind.startswith('cross_case'): late['case_id'] = 'foreign-case'
        graph[where].append(late)
        states.append(phase13_execute(module, graph, []))
        assert graph['errors'][0]['gate_failure'] is True
        graph[where].pop()
        states.append(phase13_execute(module, graph, []))
        assert graph['errors'][0]['gate_failure'] is True
        return states
    assert run(observer) == run(phase13_v12)


@pytest.mark.parametrize('change', ['valid', 'missing', 'duplicate', 'late_cross_case'])
def test_phase13_T18_early_null_scope_keeps_raw_facts_and_original_start_proof(observer, phase13_v12, change):
    def run(module):
        origins, paused, error, native, start = early_start_fixture()
        graph = {'origins': origins, 'errors': [error], 'paused': [paused], 'native': [native], 'starts': [start]}
        if change == 'missing': graph['starts'] = []
        if change == 'duplicate': graph['starts'].append(deepcopy(start))
        states = [phase13_execute(module, graph, [])]
        if change == 'late_cross_case':
            late = deepcopy(start); late['case_id'] = 'foreign'
            graph['starts'].append(late)
            states.append(phase13_execute(module, graph, []))
        assert native['case_id'] is None and native['case_epoch'] is None
        return states
    assert run(observer) == run(phase13_v12)


@pytest.mark.parametrize('unsupported', [None, 3, [], 'foreign'])
def test_phase13_T19_later_unsupported_error_keeps_prior_output_and_original_exception(observer, phase13_v12, unsupported):
    graph = phase13_graph()
    graph['errors'].append(unsupported)
    phase13_equivalent(observer, phase13_v12, phase13_native_factory(graph))


def test_phase13_archived_change_scope_and_current_selector_purity(observer, phase13_v12):
    old_tree = ast.parse(Path(phase13_v12.__file__).read_text('utf8'))
    # This historical change-scope assertion applies to v12 -> v13, not to
    # later bug fixes. Current cancellation behavior is covered below.
    archived_tree = ast.parse((TOOLING / 'fixtures/cancellation_v13.py').read_text('utf8'))
    helper_names = {'_cancel_relation_index', '_cancel_relation_candidates', '_cancel_native_graph'}
    def preserved(nodes):
        return [node for node in nodes if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in helper_names | {'reconcile_canceled_read_errors'})
            and not (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                and target.id == 'OBSERVER_VERSION' for target in node.targets))]
    assert ast.dump(ast.Module(body=preserved(archived_tree.body), type_ignores=[])) == ast.dump(
        ast.Module(body=preserved(old_tree.body), type_ignores=[]))
    tree = ast.parse(Path(observer.__file__).read_text('utf8'))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in helper_names:
            assert not any(isinstance(child, (ast.Await, ast.AsyncFunctionDef, ast.Global, ast.Nonlocal))
                for child in ast.walk(node))


def test_phase13_T22_T23_readonly_metadata_alias_is_supported_and_each_call_fresh(observer, phase13_v12, monkeypatch):
    graph = phase13_graph()
    shared = graph['errors'][0]['artifact_ids']
    for where in ('paused', 'native', 'starts'):
        graph[where][0]['artifact_ids'] = shared
    factory = phase13_native_factory(graph)
    sample, _ = factory()
    assert sample['errors'][0]['artifact_ids'] is sample['paused'][0]['artifact_ids']
    builds = []
    original = getattr(observer, '_cancel_relation_index', None)
    if original is not None:
        def counted(*args):
            result = original(*args)
            builds.append(result is not None)
            return result
        monkeypatch.setattr(observer, '_cancel_relation_index', counted)
    phase13_equivalent(observer, phase13_v12, factory)
    phase13_equivalent(observer, phase13_v12, factory)
    if original is not None:
        assert builds == [True, True]


def test_phase13_T21_exact_eight_error_native_complexity_red(observer, phase13_v12, tmp_path):
    def run(module):
        origins, errors, paused, native, _, _ = native_cancel_workload(8, 200)
        filename = module.reconcile_canceled_read_errors.__code__.co_filename
        calls = [0]
        def count(frame, event, argument):
            if event == 'call' and frame.f_code.co_filename == filename and frame.f_code.co_name == 'same_context':
                calls[0] += 1
        previous = sys.getprofile()
        try:
            sys.setprofile(count)
            module.reconcile_canceled_read_errors(errors, paused, native, origins, request_starts=[])
        finally:
            sys.setprofile(previous)
        assert sys.getprofile() is previous
        assert all(error['classification'] == 'confirmed_canceled_read' for error in errors)
        return calls[0], phase13_typed_graph(errors)
    old_count, old_result = run(phase13_v12)
    count, result = run(observer)
    assert old_count == 8 * (208 + 208) == 3328
    assert result == old_result
    budget = 2 * (208 + 208 + 8)
    save_json(tmp_path/'eight-error-native-metrics.json', {'old_count': old_count,
        'candidate_count': count, 'linear_budget': budget, 'complete_result_equal': result == old_result,
        'old_source_sha256': sha_file(phase13_v12.__file__), 'candidate_source_sha256': sha_file(observer.__file__)})
    assert count <= budget, f'original comparison count {count} exceeds conservative linear budget {budget}'


def test_phase13_oracle_loader_never_writes_bytecode_even_when_global_flag_false(tmp_path):
    source = TOOLING/'fixtures/cancellation_v12.py'
    copied = tmp_path/'immutable_source_copy.py'
    copied.write_bytes(source.read_bytes())
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = False
        module = phase13_load_source(copied, 'phase13_no_bytecode_copy_control')
        assert sys.dont_write_bytecode is False
        assert module.OBSERVER_VERSION == '2026-10-14.12'
        assert list(tmp_path.rglob('*.pyc')) == []
    finally:
        sys.dont_write_bytecode = previous
    assert sys.dont_write_bytecode is previous


def test_phase13_T02_R2_metaclass_equality_hook_is_not_probed(observer, phase13_v12):
    def factory():
        graph, trace = phase13_graph(), []
        class HookMeta(type):
            def __eq__(cls, other):
                trace.append('metaclass.eq')
                return False
            __hash__ = type.__hash__
        class UnknownPayload(metaclass=HookMeta):
            pass
        foreign = deepcopy(graph['paused'][0])
        foreign.update(request_id='foreign-fetch', network_id='foreign-network', payload=UnknownPayload())
        graph['paused'].append(foreign)
        return graph, trace
    _, trace = phase13_equivalent(observer, phase13_v12, factory)
    assert trace == []


def test_phase13_24_native_results_complete_json_and_aliases_equal_v12(observer, phase13_v12):
    origins, errors, paused, native, starts, _ = native_cancel_workload()
    graph = {'origins': origins, 'errors': errors, 'paused': paused, 'native': native, 'starts': starts}
    phase13_equivalent(observer, phase13_v12, phase13_native_factory(graph))


@pytest.mark.parametrize('empty', [False, True])
def test_phase13_T01_no_eligible_never_builds_index_or_touches_inputs(observer, phase13_v12, monkeypatch, empty):
    def never_build(*args):
        raise AssertionError('no-eligible call must not build or probe an index')
    if hasattr(observer, '_cancel_relation_index'):
        monkeypatch.setattr(observer, '_cancel_relation_index', never_build)
    def factory():
        trace = []
        class NeverIterate:
            def __iter__(self):
                trace.append('unexpected iteration')
                raise AssertionError('original untouched input')
        return {'errors': [] if empty else [{'classification': 'unknown'}], 'origins': NeverIterate(),
            'paused': NeverIterate(), 'native': NeverIterate(), 'starts': NeverIterate()}, trace
    _, trace = phase13_equivalent(observer, phase13_v12, factory)
    assert trace == []


@pytest.mark.parametrize('relation', ['paused_network', 'paused_fetch', 'native_request', 'native_alias', 'starts', 'cdp', 'page'])
@pytest.mark.parametrize('row_missing', [False, True])
def test_phase13_T04_null_query_matches_missing_or_null_live_row(observer, phase13_v12, relation, row_missing):
    graph = phase13_graph()
    error = graph['errors'][0]
    if relation in {'cdp', 'page'}:
        field = 'cdp_scope_id' if relation == 'cdp' else 'page_target_id'
        error[field] = None
        rows = [graph[name][0] for name in ('paused', 'native', 'starts')]
    else:
        where, field, query = {'paused_network': ('paused', 'network_id', 'network_id'),
            'paused_fetch': ('paused', 'request_id', 'request_id'),
            'native_request': ('native', 'requestId', 'network_id'),
            'native_alias': ('native', 'network_id', 'network_id'),
            'starts': ('starts', 'request_id', 'network_id')}[relation]
        error[query] = None
        rows = [graph[where][0]]
        if where == 'paused':
            rows[0]['request_id' if field == 'network_id' else 'network_id'] = 'foreign'
        if where == 'native':
            rows[0]['requestId' if field == 'network_id' else 'network_id'] = 'foreign'
    for row in rows:
        if row_missing: row.pop(field, None)
        else: row[field] = None
    phase13_equivalent(observer, phase13_v12, phase13_native_factory(graph))


def test_phase13_T24_reconciliation_outside_three_selectors_is_identical(observer, phase13_v12):
    old = ast.parse(inspect.getsource(phase13_v12.reconcile_canceled_read_errors))
    candidate = ast.parse(inspect.getsource(observer.reconcile_canceled_read_errors))
    class OriginalSelectors(ast.NodeTransformer):
        def visit_Assign(self, node):
            if any(isinstance(name, ast.Name) and name.id == 'index_checked' for target in node.targets for name in ast.walk(target)):
                return None
            return self.generic_visit(node)
        def visit_If(self, node):
            if isinstance(node.test, ast.UnaryOp) and isinstance(node.test.operand, ast.Name) and node.test.operand.id == 'index_checked':
                return None
            return self.generic_visit(node)
        def visit_Call(self, node):
            if isinstance(node.func, ast.Name) and node.func.id == '_cancel_relation_candidates':
                return node.args[2]
            return self.generic_visit(node)
    assert ast.dump(OriginalSelectors().visit(candidate)) == ast.dump(old)


@pytest.mark.parametrize('change', ['native', 'unknown_payload', 'cycle', 'history_raw_alias', 'duplicate_error'])
def test_phase13_R2_R3_supported_domain_is_explicit_not_just_equal_output(observer, change):
    graph = phase13_graph()
    if change == 'unknown_payload': graph['paused'][0]['unknown'] = object()
    if change == 'cycle':
        cycle = []; cycle.append(cycle); graph['paused'][0]['unknown'] = cycle
    if change == 'history_raw_alias': graph['errors'][0]['cancellation_reconciliation_history'] = graph['paused']
    if change == 'duplicate_error': graph['errors'].append(graph['errors'][0])
    result = observer._cancel_relation_index(graph['errors'], graph['paused'], graph['native'], graph['origins'], graph['starts'])
    assert (result is not None) is (change == 'native')
