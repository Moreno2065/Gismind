"""Offline containment/protocol checks; these are not live continuation E2E."""
from __future__ import annotations

import importlib
import importlib.util
import json
import sqlite3

import pytest


def test_independent_continuation_entrypoint_exists():
    assert importlib.util.find_spec('tests.live.resume_prompt_clarifications') is not None, 'independent clarification continuation script is missing'


@pytest.fixture
def harness():
    spec = importlib.util.find_spec('tests.live.resume_prompt_clarifications')
    assert spec is not None, 'independent clarification continuation script is missing'
    return importlib.import_module('tests.live.resume_prompt_clarifications')


def test_new_output_must_not_overlap_source_or_exist(harness, tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    for destination in [source, source / 'child', tmp_path]:
        with pytest.raises((ValueError, FileExistsError)):
            harness.validate_output(source, destination)
    target = tmp_path / 'continuation'
    assert harness.validate_output(source, target) == target.resolve()
    target.mkdir()
    with pytest.raises(FileExistsError):
        harness.validate_output(source, target)


def test_clone_preserves_wal_snapshot_and_source_bytes(harness, tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.mkdir()
    connection = sqlite3.connect(source / 'runtime.db')
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('CREATE TABLE evidence (value TEXT)')
    connection.execute("INSERT INTO evidence VALUES ('committed in WAL')")
    connection.commit()
    cas = source / 'data/artifacts/sha256/ab'
    cas.mkdir(parents=True)
    (cas / 'payload').write_bytes(b'original artifact')
    before = harness.evidence_manifest(source)
    assert 'runtime.db-wal' in before and 'runtime.db-shm' in before
    copied = harness.clone_evidence(source, target)
    assert copied == before == harness.evidence_manifest(source)
    with sqlite3.connect(target / 'runtime.db') as clone:
        assert clone.execute('SELECT value FROM evidence').fetchone()[0] == 'committed in WAL'
        clone.execute("INSERT INTO evidence VALUES ('continuation only')")
    assert harness.evidence_manifest(source) == before
    assert not (source / 'runtime.db').samefile(target / 'runtime.db')
    with pytest.raises(FileExistsError):
        harness.clone_evidence(source, target)
    connection.close()


def test_changed_source_cannot_pass_immutable_evidence_check(harness, tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'runtime.db').write_bytes(b'old')
    manifest = harness.evidence_manifest(source)
    (source / 'runtime.db').write_bytes(b'changed')
    with pytest.raises(AssertionError, match='source evidence changed'):
        harness.assert_source_unchanged(source, manifest)


def test_cohort_is_selected_from_actual_frozen_results(harness, tmp_path):
    from tests.live import prompt_e2e_2000 as oracle

    source = tmp_path / 'source'
    source.mkdir()
    rename = [case for case in oracle.load_cases() if case.family == 'rename'][:2]
    records = [dict(type='manifest', corpus_sha256=oracle.CORPUS_SHA256)]
    for index, case in enumerate(rename):
        records.append(dict(type='result', case_id=case.case_id, family='rename', kind='success',
            prompt=case.prompt, run_id=f'run-{index}', status='awaiting_input' if index == 0 else 'completed', ok=index == 1))
    (source / 'results.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in records), encoding='utf-8')
    (source / 'summary.json').write_text(json.dumps(dict(evidence_valid=True, completed=2, passed=1, failed=1)), encoding='utf-8')
    cohort, metadata = harness.load_cohort(source)
    assert [case.case_id for case, result in cohort] == [rename[0].case_id]
    assert metadata['available_clarifications'] == 1
    assert metadata['source_hashes']['results.jsonl']


def clarification_schema():
    return {'type': 'object', 'additionalProperties': False, 'required': ['clarification'],
        'properties': {'clarification': {'type': 'string', 'minLength': 1, 'maxLength': 2000}}}


@pytest.mark.parametrize('change', ['open', 'missing_required', 'wrong_type', 'unknown_property'])
def test_strict_patch_schema_is_verified_without_guessing(harness, change):
    schema = clarification_schema()
    if change == 'open':
        schema['additionalProperties'] = True
    elif change == 'missing_required':
        schema['required'] = []
    elif change == 'wrong_type':
        schema['properties']['clarification']['type'] = 'object'
    else:
        schema['properties']['business_value'] = {'type': 'string'}
    with pytest.raises(AssertionError, match='clarification patch schema'):
        harness.validate_patch_schema(schema)


def test_real_resume_api_conflicts_and_idempotency_without_planner_execution(harness, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.runtime import ArtifactStore, RuntimeDB, RuntimeStore, RuntimeSupervisor

    runtime = RuntimeStore(RuntimeDB(tmp_path / 'runtime.db'))
    runtime.initialize()
    runtime.create_session('session')
    runtime.create_run('run', 'session')
    runtime.add_input_revision('run', 1, {'prompt': '把名称改名', 'input_artifact_ids': []})
    artifacts = ArtifactStore(tmp_path / 'data', runtime)
    supervisor = RuntimeSupervisor(runtime, artifacts, autostart=False)
    claim = supervisor.queue.claim_planning_run('offline-protocol', run_id='run')
    runtime.reject_planning('run', planner_claim=claim, code='CLARIFICATION_REQUIRED',
        diagnostics={'message': '新字段名是什么？', 'patch_schema': clarification_schema()}, awaiting_input=True)
    client = TestClient(create_app(runtime_store=runtime, artifact_store=artifacts, runtime_supervisor=supervisor))
    try:
        before = client.get('/api/runs/run').json()
        evidence = harness.resume_with_protocol_checks(client, 'run', 'command-test')
        after = client.get('/api/runs/run').json()
        assert evidence['command_id'] == 'command-test'
        assert evidence['expected_run_version'] == before['run_version']
        assert evidence['idempotent_replay_verified'] is True
        assert after['active_input_revision']['revision'] == 2
        assert after['run_version'] == before['run_version'] + 1
        assert runtime._read(lambda c: c.execute('SELECT COUNT(*) FROM planner_attempts').fetchone()[0]) == 0
        saved = runtime._read(lambda c: c.execute('SELECT input_json FROM run_input_revisions WHERE revision=2').fetchone()[0])
        assert harness.CLARIFICATION in saved
    finally:
        client.close()
        supervisor.stop(timeout=1)


def test_fresh_attempt_filter_never_counts_original_planner_response(harness):
    attempts = [{'attempt_id': 'original', 'input_revision': 1}, {'attempt_id': 'resumed', 'input_revision': 2}]
    assert harness.continuation_attempts(attempts, 2) == [attempts[1]]


def test_provider_usage_requires_actual_positive_and_complete_evidence(harness):
    with pytest.raises(AssertionError, match='usage'):
        harness.validate_provider_usage([{'response_sha256': 'a' * 64, 'usage': None}])
    with pytest.raises(AssertionError, match='usage'):
        harness.validate_provider_usage([{'response_sha256': 'a' * 64, 'usage': {'prompt_tokens': 0, 'completion_tokens': 0}}])
    harness.validate_provider_usage([{'response_sha256': 'a' * 64, 'usage': {'prompt_tokens': 40, 'completion_tokens': 8}}])


def test_only_original_goal_and_scripted_reply_may_reach_continuation_request(harness):
    assert hasattr(harness, 'validate_continuation_request'), 'continuation request grounding check is missing'
    payload = {'user_goal': '原始请求', 'input_revision': 2, 'revision_patch': {'clarification': harness.CLARIFICATION}}
    harness.validate_continuation_request([{'role': 'user', 'content': json.dumps(payload)}], '原始请求', 2)
    payload['revision_patch'] = {}
    with pytest.raises(AssertionError, match='clarification'):
        harness.validate_continuation_request([{'role': 'user', 'content': json.dumps(payload)}], '原始请求', 2)


def test_script_requires_opt_in_before_creating_output(harness, tmp_path):
    with pytest.raises(SystemExit):
        harness.main(['--source', str(tmp_path / 'source'), '--output', str(tmp_path / 'target')])
    assert not (tmp_path / 'target').exists()


def test_executable_manifest_pins_continuation_runner_and_terminal_reader(harness):
    assert hasattr(harness, 'executable_manifest'), 'all continuation evidence readers must be fingerprinted'
    manifest = harness.executable_manifest()
    assert set(manifest) == {'continuation', 'runner_oracle', 'terminal_reader'}
    assert all(len(value) == 64 for value in manifest.values())


def test_changed_executable_records_drift_instead_of_raising_without_summary(harness, tmp_path):
    assert hasattr(harness, 'check_executable_sources'), 'test-side source drift check is missing'
    from tests.live.prompt_e2e_2000 import Journal

    journal = Journal(tmp_path / 'events.jsonl', 'offline')
    expected = harness.executable_manifest()
    assert harness.check_executable_sources(journal, expected)
    expected['terminal_reader'] = '0' * 64
    assert not harness.check_executable_sources(journal, expected)
    assert journal.records[-1]['type'] == 'runtime_drift'


def test_continuation_policy_records_budget_and_disjoint_selected_cases(harness):
    assert hasattr(harness, 'continuation_policy'), 'continuation policy manifest is missing'
    from types import SimpleNamespace

    args = SimpleNamespace(max_provider_requests=12, max_reserved_tokens=100000,
        max_cost_usd=3, usd_per_million=10, qps=.5, timeout=90, offset=2, limit=37)
    result = harness.continuation_policy(args, ['case-3', 'case-4'])
    assert result['selected_case_ids'] == ['case-3', 'case-4']
    assert result['offset'] == 2
    assert result['max_provider_requests'] == 12 and result['max_cost_usd'] == 3
    assert result['usd_per_million'] == 10 and result['timeout'] == 90


def test_source_root_link_is_rejected_before_resolve(harness, tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'runtime.db').write_bytes(b'source')
    alias = tmp_path / 'alias'
    try:
        alias.symlink_to(source, target_is_directory=True)
    except OSError:
        pytest.skip('host does not grant directory symlink creation')
    with pytest.raises(ValueError, match='link|junction'):
        harness.clone_evidence(alias, tmp_path / 'target')
