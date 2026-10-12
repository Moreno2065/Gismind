"""Opt-in real clarification continuation on fresh copies of frozen evidence.

This is an additional scripted user turn, never a rerating of first-turn E2E.
The original SQLite/WAL/SHM/CAS files are only read as bytes, never opened by
SQLite. Live admission and cost ceilings must be supplied by the operator.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import time

from . import prompt_e2e_2000 as oracle
from . import adjudicate_prompt_e2e_2000 as artifact_reader
from .adjudicate_prompt_e2e_2000 import read_terminal_artifacts

VERSION = 'scripted-clarification-continuation-v1'
SUITE = 'real_api_clarification_continuation'
CLARIFICATION = '源字段是“名称”，新字段名就叫“新名”这两个字；其他字段和几何不变。'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def executable_manifest():
    return {'continuation': sha(__file__), 'runner_oracle': sha(oracle.__file__),
            'terminal_reader': sha(artifact_reader.__file__)}


def check_executable_sources(journal, expected):
    try:
        current = executable_manifest()
    except OSError as exc:
        current = {'error': type(exc).__name__}
    if current == expected:
        return True
    journal.append(dict(type='runtime_drift', reason='continuation/oracle/reader source changed',
        expected=expected, observed=current))
    return False


def continuation_policy(args, selected_case_ids):
    names = ('max_provider_requests', 'max_reserved_tokens', 'max_cost_usd', 'usd_per_million', 'qps', 'timeout', 'offset', 'limit')
    return {**{name: getattr(args, name) for name in names}, 'selected_case_ids': list(selected_case_ids)}


def validate_output(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output == source or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError('continuation output must be outside and disjoint from frozen source')
    if output.exists():
        raise FileExistsError('continuation output already exists; choose a new directory')
    return output


def evidence_manifest(root):
    """Reject links/junctions and hash every retained file, including WAL/SHM."""
    root = Path(root)
    if root.is_symlink() or root.is_junction():
        raise ValueError('evidence root cannot be a link or junction')
    root = root.resolve(strict=True)
    result = {}

    def visit(folder):
        for child in sorted(folder.iterdir()):
            if child.is_symlink() or child.is_junction() or not child.resolve().is_relative_to(root):
                raise ValueError('evidence path escapes source or is a link/junction')
            if child.is_dir():
                visit(child)
            elif child.is_file():
                result[child.relative_to(root).as_posix()] = sha(child)
            else:
                raise ValueError('evidence contains a nonregular file')

    visit(root)
    return result


def assert_source_unchanged(source, expected):
    assert evidence_manifest(source) == expected, 'source evidence changed during continuation'


def clone_evidence(source, target):
    before = evidence_manifest(source)
    source = Path(source).resolve(strict=True)
    target = validate_output(source, target)
    assert 'runtime.db' in before, 'source runtime database is missing'
    target.mkdir(parents=True, exist_ok=False)
    for relative in before:
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, destination)
        assert not (source / relative).samefile(destination), 'copy must not share an original inode'
    assert evidence_manifest(target) == before, 'evidence copy checksum differs'
    assert_source_unchanged(source, before)
    return before


def load_cohort(source):
    source = Path(source).resolve(strict=True)
    paths = [source / 'results.jsonl', source / 'summary.json']
    hashes = {path.name: sha(path) for path in paths}
    records = [json.loads(line) for line in paths[0].read_text(encoding='utf-8').splitlines()]
    summary = json.loads(paths[1].read_text(encoding='utf-8'))
    assert records and records[0].get('corpus_sha256') == oracle.CORPUS_SHA256, 'source corpus differs'
    assert summary.get('evidence_valid') is True, 'source evidence is invalid'
    assert not any(row.get('type') == 'runtime_drift' for row in records), 'source runtime drift'
    results = [row for row in records if row.get('type') == 'result']
    assert len(results) == len({row['case_id'] for row in results}), 'multiple source results per case are ambiguous'
    assert summary['completed'] == len(results), 'source summary count differs'
    cases = {case.case_id: case for case in oracle.load_cases()}
    cohort = []
    for row in results:
        if row.get('family') != 'rename' or row.get('status') != 'awaiting_input':
            continue
        case = cases[row['case_id']]
        assert case.family == 'rename' and case.kind == row['kind'] == 'success', 'source case identity differs'
        assert case.prompt == row['prompt'] and row['ok'] is False, 'source prompt/verdict differs'
        cohort.append((case, row))
    assert hashes == {path.name: sha(path) for path in paths}, 'source journal changed while reading'
    return sorted(cohort, key=lambda item: item[0].case_id), dict(
        available_clarifications=len(cohort), source_hashes=hashes, original_summary=summary,
        original_manifest=records[0], source_directory=str(source))


def validate_patch_schema(schema):
    assert isinstance(schema, dict) and schema.get('type') == 'object' and schema.get('additionalProperties') is False, 'unexpected clarification patch schema'
    assert schema.get('required') == ['clarification'] and set(schema.get('properties', {})) == {'clarification'}, 'unexpected clarification patch schema'
    value = schema['properties']['clarification']
    assert value.get('type') == 'string' and value.get('minLength', 0) >= 1 and value.get('maxLength', 2001) <= 2000, 'unexpected clarification patch schema'
    assert value['minLength'] <= len(CLARIFICATION) <= value['maxLength'], 'reply does not satisfy clarification patch schema'


def resume_with_protocol_checks(client, run_id, command_id):
    route = f'/api/runs/{run_id}'
    before = client.get(route).json()
    assert before['status'] == 'awaiting_input' and not before.get('active_plan'), 'source run is not an unplanned clarification'
    validate_patch_schema(before.get('patch_schema'))
    version = before['run_version']
    payload = dict(command_id=command_id, expected_run_version=version, action='resume', patch={'clarification': CLARIFICATION})
    for suffix, changes, status, code in [
        ('empty', {'patch': {}}, 422, 'PATCH_SCHEMA_INVALID'),
        ('extra', {'patch': {'clarification': CLARIFICATION, 'unexpected': 'rejected'}}, 422, 'PATCH_SCHEMA_INVALID'),
        ('stale', {'expected_run_version': version - 1}, 409, 'RUN_VERSION_CONFLICT'),
    ]:
        rejected = client.post(route + '/resume', json={**payload, **changes, 'command_id': command_id + '-' + suffix})
        assert rejected.status_code == status and rejected.json()['error']['code'] == code, f'{suffix} resume was not rejected'
        unchanged = client.get(route).json()
        assert (unchanged['run_version'], unchanged['active_input_revision'], unchanged['last_seq']) == (version, before['active_input_revision'], before['last_seq']), 'rejected resume mutated run'
    response = client.post(route + '/resume', json=payload)
    assert response.status_code == 202 and response.json()['status'] == 'queued', 'valid clarification resume failed'
    replay = client.post(route + '/resume', json=payload)
    assert replay.status_code == 202 and replay.json() == response.json(), 'resume idempotency differs'
    conflict = client.post(route + '/resume', json={**payload, 'patch': {'clarification': CLARIFICATION + ' '}})
    assert conflict.status_code == 409 and conflict.json()['error']['code'] == 'COMMAND_ID_CONFLICT', 'command payload conflict was not rejected'
    after = client.get(route).json()
    revision = before['active_input_revision']['revision'] + 1
    assert after['active_input_revision']['revision'] == revision and after['run_version'] == version + 1, 'resume created extra input revisions'
    return dict(command_id=command_id, expected_run_version=version, input_revision=revision,
        patch=payload['patch'], patch_schema=before['patch_schema'], initial_last_seq=before['last_seq'],
        rejected_patch_checks=2, stale_version_rejected=True, command_conflict_rejected=True, idempotent_replay_verified=True)


def continuation_attempts(attempts, revision):
    return [attempt for attempt in attempts if attempt.get('input_revision') == revision]


def validate_provider_usage(calls):
    assert calls and all(call.get('response_sha256') and isinstance(call.get('usage'), dict) and all(
        type(call['usage'].get(key)) is int and call['usage'][key] > 0 for key in ('prompt_tokens', 'completion_tokens')) for call in calls), 'missing complete positive provider usage'


def validate_continuation_request(messages, original_goal, revision):
    payload = json.loads(next(message['content'] for message in messages if message['role'] == 'user'))
    assert payload['user_goal'] == original_goal and payload['input_revision'] == revision, 'continuation changed original goal/revision'
    assert payload.get('revision_patch') == {'clarification': CLARIFICATION}, 'explicit clarification was lost or augmented'


def run_continuation(case, original, source, output, journal, budget, timeout):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.runtime import ArtifactStore, RuntimeDB, RuntimeStore, RuntimeSupervisor
    from app.runtime.llm_planner import OpenAICompatiblePlanner

    class MeteredRealPlanner(OpenAICompatiblePlanner):
        def _call(self, messages):
            validate_continuation_request(messages, case.prompt, protocol['input_revision'])
            upper = len(oracle.canonical(messages).encode('utf-8')) + len(oracle.canonical(self._response_format(messages)).encode('utf-8')) + 8192
            budget.reserve(case.case_id, calls=2, tokens=upper + self.max_tokens)
            raw, elapsed = super()._call(messages)
            journal.append(dict(type='provider_response', case_id=case.case_id, continuation=True,
                model=self.model, provider=self.provider, latency_ms=elapsed,
                response_sha256=hashlib.sha256(oracle.canonical(raw).encode('utf-8')).hexdigest(), usage=raw.get('usage')))
            return raw, elapsed

    source_root = Path(source).resolve(strict=True)
    cases_root = source_root / 'cases'
    assert not cases_root.is_symlink() and not cases_root.is_junction(), 'cases root cannot be a link/junction'
    assert cases_root.resolve(strict=True).is_relative_to(source_root), 'cases path escapes frozen source'
    case_directory = cases_root / case.case_id
    assert not case_directory.is_symlink() and not case_directory.is_junction(), 'case root cannot be a link/junction'
    assert case_directory.resolve(strict=True).is_relative_to(cases_root.resolve()), 'case path escapes source cases'
    paths = list(case_directory.glob('attempt-*/runtime.db'))
    assert len(paths) == 1, 'source case must have exactly one retained attempt'
    assert paths[0].resolve(strict=True).is_relative_to(cases_root.resolve()), 'attempt path escapes source cases'
    source_case = paths[0].parent
    case_root = Path(output) / 'cases' / case.case_id / 'attempt-01'
    original_hashes = clone_evidence(source_case, case_root)
    journal.append(dict(type='evidence_copy', case_id=case.case_id, source=str(source_case),
        destination=str(case_root), source_files=original_hashes))
    runtime = RuntimeStore(RuntimeDB(case_root / 'runtime.db'))
    runtime.initialize()
    assert runtime._read(lambda c: c.execute('PRAGMA integrity_check').fetchone()[0]) == 'ok', 'copied SQLite integrity failed'
    artifacts = ArtifactStore(case_root / 'data', runtime)
    planner = MeteredRealPlanner.from_settings()
    assert planner.model == 'deepseek-flash', 'continuation requires the declared deepseek-flash model'
    planner.max_retries = 0
    supervisor = RuntimeSupervisor(runtime, artifacts, planner=planner, isolate=True, autostart=False, timeout_seconds=timeout)
    client = TestClient(create_app(runtime_store=runtime, artifact_store=artifacts, runtime_supervisor=supervisor))
    run_id, started = original['run_id'], time.monotonic()
    result = dict(type='result', suite=SUITE, case_id=case.case_id, family=case.family, kind=case.kind,
        run_id=run_id, ok=False, original_first_turn_pass=False, original_status=original['status'],
        scripted_user_clarification=True, clarification=CLARIFICATION, model=planner.model, provider=planner.provider,
        no_repeated_clarification=False, stages={})
    stage, snapshot = 'resume_protocol', {}
    try:
        baseline_workers = runtime._read(lambda c: {r[0] for r in c.execute('SELECT attempt_id FROM task_attempts')})
        command_id = 'clarification-' + hashlib.sha256((case.case_id + run_id + CLARIFICATION).encode('utf-8')).hexdigest()[:32]
        protocol = resume_with_protocol_checks(client, run_id, command_id)
        result['resume_protocol'] = protocol
        result['stages']['api'] = True
        journal.append(dict(type='resume_accepted', case_id=case.case_id, run_id=run_id, **protocol))
        stage = 'continuation_execution'
        deadline = started + timeout
        while time.monotonic() < deadline:
            snapshot = client.get(f'/api/runs/{run_id}').json()
            if snapshot.get('status') in ('completed', 'failed', 'cancelled', 'awaiting_input'):
                break
            supervisor.queue.recover_expired_leases(run_id=run_id)
            oracle.advance_run(supervisor, client, run_id, f'clarification-{case.case_id}')
            time.sleep(.01)
        else:
            client.post(f'/api/runs/{run_id}/cancel', json={'command_id': command_id + '-timeout'})
            raise TimeoutError('clarification continuation deadline exceeded')
        planners = runtime._read(lambda c: [dict(r) for r in c.execute('SELECT * FROM planner_attempts WHERE run_id=? ORDER BY created_at', (run_id,))])
        planners = continuation_attempts(planners, protocol['input_revision'])
        workers = runtime._read(lambda c: [dict(r) for r in c.execute('SELECT a.*,t.tool_name FROM task_attempts a JOIN tasks t ON t.task_id=a.task_id JOIN plans p ON p.plan_id=t.plan_id WHERE p.run_id=?', (run_id,))])
        workers = [worker for worker in workers if worker['attempt_id'] not in baseline_workers]
        calls = [row for row in journal.for_case(case.case_id) if row.get('type') == 'provider_response']
        result.update(status=snapshot['status'], usage=[row.get('usage') for row in calls],
            planner_attempts=[{key: row.get(key) for key in ('attempt_id', 'input_revision', 'phase', 'status', 'provider', 'model', 'response_hash', 'error_code')} for row in planners],
            worker_attempts=[{key: row.get(key) for key in ('attempt_id', 'tool_name', 'status', 'executor_identity', 'error_code')} for row in workers])
        result['stages'].update(planner=bool(calls), compiled=bool(snapshot.get('active_plan')), worker=bool(workers))
        stage = 'continuation_provenance'
        validate_provider_usage(calls)
        assert {row['response_hash'] for row in planners if row.get('response_hash')} == {row['response_sha256'] for row in calls}, 'new planner attempts differ from observed provider responses'
        oracle.assert_live_provenance(snapshot, planners, workers, 'success', calls, family=case.family)
        assert all(isinstance(json.loads(row['executor_identity']).get('create_time'), (float, int)) and json.loads(row['executor_identity'])['create_time'] > 0 for row in workers), 'worker creation time missing'
        stage = 'continuation_semantic'
        outputs = read_terminal_artifacts(case_root, run_id)
        for artifact in outputs:
            response = client.get(f"/api/artifacts/{artifact['artifact_id']}/content")
            assert response.status_code == 200 and response.content == artifact['payload'], 'terminal API content differs from retained artifact'
        result['stages']['artifact'] = bool(outputs)
        oracle.assert_semantics(case, outputs)
        result['stages']['semantic'] = True
        result.update(ok=True, no_repeated_clarification=True,
            outputs=[{key: artifact.get(key) for key in ('artifact_id', 'type', 'content_hash', 'crs')} for artifact in outputs])
    except Exception as exc:
        result.update(status=snapshot.get('status', 'not_started'), failure_stage=stage,
            failure_type=type(exc).__name__, failure_detail=str(exc).replace(planner.api_key, '[REDACTED]')[:1000] if planner.api_key else str(exc)[:1000])
    finally:
        client.close()
        supervisor.stop(timeout=5)
        try:
            assert_source_unchanged(source_case, original_hashes)
            result['source_evidence_unchanged'] = True
        except Exception as exc:
            result.update(ok=False, source_evidence_unchanged=False, failure_stage='source_integrity', failure_detail=str(exc))
        result['elapsed_ms'] = round((time.monotonic() - started) * 1000, 2)
        result['events'] = [{'seq': event['seq'], 'type': event['event_type']} for event in runtime.list_events(run_id)
            if event['seq'] > result.get('resume_protocol', {}).get('initial_last_seq', 0)]
        journal.append(result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--inventory', action='store_true')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--offset', type=int, default=0, help='Skip this many sorted source cases; each invocation still requires a fresh output')
    parser.add_argument('--qps', type=float, default=.5)
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--max-provider-requests', type=int, default=156)
    parser.add_argument('--max-reserved-tokens', type=int, default=12_000_000)
    parser.add_argument('--max-cost-usd', type=float)
    parser.add_argument('--usd-per-million', type=float, default=10)
    args = parser.parse_args(argv)
    if not args.inventory and not args.live:
        parser.error('use --inventory for read-only inspection or --live for paid continuation')
    cohort, source_info = load_cohort(args.source)
    if args.inventory:
        print(oracle.canonical(dict(suite='offline_clarification_inventory_not_e2e', **source_info,
            cases=[case.case_id for case, _ in cohort], scripted_user_clarification=CLARIFICATION)))
        return 0
    if not args.output or not cohort or not 0 <= args.offset < len(cohort) or (args.limit is not None and not 1 <= args.limit <= len(cohort) - args.offset):
        parser.error('fresh --output, a nonempty cohort and an in-range --limit are required')
    numbers = [args.qps, args.timeout, args.max_cost_usd, args.usd_per_million, args.max_provider_requests, args.max_reserved_tokens]
    if any(value is None or not math.isfinite(value) or value <= 0 for value in numbers):
        parser.error('positive explicit --max-cost-usd and positive budget/rate/timeout values are required')
    from app.config import Settings, get_settings
    from app.runtime.llm_planner import PROMPT_VERSION
    settings = get_settings()
    oracle.require_provider({key: getattr(settings, key) for key in ('LLM_API_KEY', 'LLM_BASE_URL', 'LLM_MODEL')})
    assert settings.LLM_MODEL == 'deepseek-flash', 'configured model must be deepseek-flash'
    output = validate_output(args.source, args.output)
    output.mkdir(parents=True, exist_ok=False)
    journal = oracle.Journal(output / 'results.jsonl', oracle.CORPUS_SHA256)
    executables = executable_manifest()
    script_hash, helper_hash = executables['continuation'], executables['runner_oracle']
    current = lambda: oracle.runtime_fingerprint(Path(__file__).resolve().parents[2] / 'app', Settings(), prompt_version=PROMPT_VERSION, case_timeout=args.timeout)
    baseline = current()
    selected = cohort[args.offset:args.offset + args.limit] if args.limit else cohort[args.offset:]
    journal.append(dict(type='continuation_manifest', suite=SUITE, continuation_version=VERSION,
        continuation_script_sha256=script_hash, reused_journal_runner_version=oracle.RUNNER_VERSION,
        reused_runner_sha256=helper_hash, oracle_version=oracle.ORACLE_VERSION, runtime=baseline,
        executable_sources=executables, policy=continuation_policy(args, [case.case_id for case, _ in selected]),
        **source_info, scripted_user_clarification=CLARIFICATION, counts_toward_original_first_turn=False))
    budget = oracle.CallBudget(journal, max_calls=args.max_provider_requests, max_tokens=args.max_reserved_tokens,
        max_cost=args.max_cost_usd, reserve_tokens=0, usd_per_million=args.usd_per_million, min_interval=1 / args.qps)
    for case, original in selected:
        if any(row.get('type') == 'budget_denied' for row in journal.records):
            break
        if not oracle.check_runtime_batch(journal, baseline, current):
            break
        if not check_executable_sources(journal, executables):
            break
        try:
            result = run_continuation(case, original, args.source, output, journal, budget, args.timeout)
        except Exception as exc:
            result = dict(type='result', suite=SUITE, case_id=case.case_id, family=case.family, kind=case.kind,
                ok=False, status='not_started', stages={}, source_evidence_unchanged=False,
                original_first_turn_pass=False, scripted_user_clarification=True,
                failure_stage='evidence_setup', failure_type=type(exc).__name__,
                failure_detail=str(exc).replace(settings.LLM_API_KEY, '[REDACTED]')[:1000])
            journal.append(result)
        print(oracle.canonical({key: result.get(key) for key in ('case_id', 'ok', 'status', 'failure_stage')}), flush=True)
        if not check_executable_sources(journal, executables) or not oracle.check_runtime_batch(journal, baseline, current) or result.get('source_evidence_unchanged') is not True:
            break
    source_same = source_info['source_hashes'] == {name: sha(args.source / name) for name in source_info['source_hashes']}
    source_files_same = True
    for copied in [row for row in journal.records if row.get('type') == 'evidence_copy']:
        try:
            assert_source_unchanged(copied['source'], copied['source_files'])
        except (AssertionError, OSError, ValueError):
            source_files_same = False
            journal.append(dict(type='runtime_drift', reason='original case evidence changed', case_id=copied['case_id']))
    executable_same = check_executable_sources(journal, executables)
    report = oracle.summary(journal, [case for case, _ in selected])
    report.update(suite=SUITE, continuation_version=VERSION, continuation_script_sha256=script_hash,
        runner_version=VERSION, runner_sha256=script_hash, reused_runner_version=oracle.RUNNER_VERSION,
        reused_runner_sha256=helper_hash, executable_sources_unchanged=executable_same,
        executable_sources=executables, policy=continuation_policy(args, [case.case_id for case, _ in selected]),
        available_clarifications=len(cohort), counts_toward_original_first_turn=False,
        scripted_user_clarification=CLARIFICATION, source_journal_unchanged=source_same, source_case_files_unchanged=source_files_same,
        evidence_valid=report['evidence_valid'] and source_same and source_files_same and executable_same and all(row.get('source_evidence_unchanged') for row in journal.completed.values()),
        continuation_passed=report['passed'], repeated_clarifications=sum(row.get('status') == 'awaiting_input' for row in journal.completed.values()),
        original_first_turn_summary=source_info['original_summary'], runtime_fingerprint=baseline['fingerprint'])
    report.pop('positive_e2e_passed', None)
    report.pop('positive_total', None)
    with (output / 'summary.json').open('x', encoding='utf-8') as stream:
        stream.write(oracle.canonical(report) + '\n')
    print(oracle.canonical(report))
    return 0 if report['evidence_valid'] and report['completed'] == len(selected) and not report['failed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
