"""Offline re-adjudication of frozen live evidence; never opens a provider client.

Original journals/databases/artifacts are read-only. A fresh, separate output
directory records both original and rechecked verdicts, pinned to source hashes.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from . import prompt_e2e_2000 as oracle

ADJUDICATOR_VERSION = 'retained-terminal-artifacts-v2'


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _connect(case_root):
    connection = sqlite3.connect((Path(case_root) / 'runtime.db').resolve().as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def read_terminal_artifacts(case_root, run_id):
    """Only successful producers' unconsumed outputs within this exact run."""
    case_root = Path(case_root)
    with closing(_connect(case_root)) as connection:
        rows = connection.execute('''
            SELECT DISTINCT a.* FROM artifacts a
            JOIN task_outputs o ON o.artifact_id=a.artifact_id
            JOIN tasks t ON t.task_id=o.task_id
            JOIN plans p ON p.plan_id=t.plan_id
            WHERE p.run_id=? AND t.status='succeeded' AND a.producer_task_id=t.task_id
            AND NOT EXISTS (
                SELECT 1 FROM task_inputs i JOIN tasks consumer ON consumer.task_id=i.task_id
                WHERE i.artifact_id=o.artifact_id AND consumer.plan_id=p.plan_id)
            ORDER BY a.artifact_id
        ''', (run_id,)).fetchall()
    assert rows, 'missing terminal produced artifact'
    data_root = (case_root / 'data').resolve()
    outputs = []
    for row in rows:
        artifact = dict(row)
        assert artifact['validation_status'] == 'valid', 'terminal artifact was not validated'
        path = (data_root / artifact['storage_uri']).resolve()
        assert path.is_relative_to(data_root), 'artifact storage path escapes the case'
        payload = path.read_bytes()
        assert hashlib.sha256(payload).hexdigest() == artifact['content_hash'], 'retained artifact checksum mismatch'
        artifact['crs'] = json.loads(artifact['crs']) if artifact['crs'] else None
        outputs.append(dict(artifact, payload=payload))
    return outputs


def _recheck(case, original, case_root, provider_calls):
    with closing(_connect(case_root)) as connection:
        run = connection.execute('SELECT * FROM runs WHERE run_id=?', (original['run_id'],)).fetchone()
        assert run is not None, 'frozen run is missing'
        tasks = [dict(r) for r in connection.execute(
            'SELECT t.* FROM tasks t JOIN plans p ON p.plan_id=t.plan_id WHERE p.run_id=?', (original['run_id'],))]
        planners = [dict(r) for r in connection.execute('SELECT * FROM planner_attempts WHERE run_id=?', (original['run_id'],))]
        workers = [dict(r) for r in connection.execute('''
            SELECT a.*,t.tool_name FROM task_attempts a JOIN tasks t ON t.task_id=a.task_id
            JOIN plans p ON p.plan_id=t.plan_id WHERE p.run_id=?
        ''', (original['run_id'],))]
    snapshot = dict(status=run['status'], active_plan=bool(tasks), tasks=tasks)
    oracle.assert_live_provenance(snapshot, planners, workers, case.kind, provider_calls, family=case.family, historical=True)
    artifacts = read_terminal_artifacts(case_root, original['run_id'])
    oracle.assert_semantics(case, artifacts)
    return artifacts


def adjudicate(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output == source or output.is_relative_to(source):
        raise ValueError('adjudication output must be outside the frozen evidence directory')
    if output.exists():
        raise FileExistsError('adjudication output already exists; use a fresh directory')
    journal_path, summary_path = source / 'results.jsonl', source / 'summary.json'
    source_hashes = dict(results_sha256=_sha(journal_path), summary_sha256=_sha(summary_path))
    records = [json.loads(line) for line in journal_path.read_text(encoding='utf-8').splitlines()]
    original_summary = json.loads(summary_path.read_text(encoding='utf-8'))
    assert original_summary.get('evidence_valid') is True, 'original runtime evidence was invalidated'
    assert records and records[0].get('corpus_sha256') == oracle.CORPUS_SHA256, 'frozen corpus mismatch'
    assert not any(r.get('type') == 'runtime_drift' for r in records), 'source has runtime drift'
    originals = [r for r in records if r.get('type') == 'result']
    assert len(originals) == len({r['case_id'] for r in originals}), 'multiple attempts require separate adjudication cohorts'
    assert original_summary['completed'] == len(originals), 'source summary/journal count mismatch'
    assert original_summary['passed'] == sum(r['ok'] for r in originals), 'source summary verdict mismatch'
    assert original_summary['failed'] == sum(not r['ok'] for r in originals), 'source summary verdict mismatch'
    cases = {c.case_id: c for c in oracle.load_cases()}
    calls = {}
    for record in records:
        if record.get('type') == 'provider_response':
            calls.setdefault(record['case_id'], []).append(record)
    reviewed = []
    for original in originals:
        case = cases[original['case_id']]
        assert original['kind'] == case.kind and original['family'] == case.family, 'source case identity differs'
        result = dict(case_id=case.case_id, family=case.family, kind=case.kind,
            original_ok=original['ok'], original_failure_stage=original.get('failure_stage'),
            original_failure_detail=original.get('failure_detail'), run_id=original.get('run_id'),
            adjudicated_ok=original['ok'], rechecked=False)
        if case.kind == 'success':
            result['rechecked'] = True
            case_root = source / 'cases' / case.case_id / 'attempt-01'
            result['database_sha256'] = _sha(case_root / 'runtime.db')
            try:
                artifacts = _recheck(case, original, case_root, calls.get(case.case_id, []))
                result['adjudicated_ok'] = True
                result['terminal_artifacts'] = [{k: a.get(k) for k in ('artifact_id', 'type', 'crs', 'content_hash')} for a in artifacts]
                result['delivery_types'] = sorted({a['type'] for a in artifacts})
                result['rule'] = ('clip_source_schema_equivalence' if case.family == 'clip'
                    and original.get('failure_detail') == 'geometry detached from its source record properties'
                    else 'terminal_spatial_content')
            except (AssertionError, ValueError, KeyError, OSError, sqlite3.Error) as exc:
                result.update(adjudicated_ok=False, adjudication_error_type=type(exc).__name__, adjudication_error=str(exc)[:1000])
            assert result['database_sha256'] == _sha(case_root / 'runtime.db'), 'source database changed during adjudication'
        else:
            result['rule'] = 'original_safety_verdict_retained_not_rechecked'
        reviewed.append(result)
    assert source_hashes == dict(results_sha256=_sha(journal_path), summary_sha256=_sha(summary_path)), 'source evidence changed during adjudication'
    corrected = [r for r in reviewed if not r['original_ok'] and r['adjudicated_ok']]
    regressions = [r for r in reviewed if r['original_ok'] and not r['adjudicated_ok']]
    report = dict(adjudicator_version=ADJUDICATOR_VERSION, oracle_version=oracle.ORACLE_VERSION,
        scope='offline_semantic_adjudication_of_retained_live_artifacts', provider_calls_made=0,
        isolation_evidence='recorded worker PID and creation time; original live parent-PID check retained, not re-proven against offline process',
        source_directory=str(source), source_hashes=source_hashes, source_manifest=records[0],
        source_summary=original_summary, corpus_sha256=oracle.CORPUS_SHA256,
        oracle_source_sha256=oracle.runner_digest(), adjudicator_sha256=_sha(__file__),
        total=len(reviewed), rechecked_positive=sum(r['rechecked'] for r in reviewed),
        retained_safety_verdicts=sum(not r['rechecked'] for r in reviewed),
        original_passed=sum(r['original_ok'] for r in reviewed), original_failed=sum(not r['original_ok'] for r in reviewed),
        adjudicated_passed=sum(r['adjudicated_ok'] for r in reviewed), adjudicated_failed=sum(not r['adjudicated_ok'] for r in reviewed),
        corrected_false_negatives=len(corrected), regressions=len(regressions),
        corrected_by_family=dict(Counter(r['family'] for r in corrected)),
        failures_by_family=dict(Counter(r['family'] for r in reviewed if not r['adjudicated_ok'])),
        source_evidence_unchanged=True)
    output.mkdir(parents=True, exist_ok=False)
    with (output / 'adjudication.jsonl').open('x', encoding='utf-8') as stream:
        for result in reviewed:
            stream.write(oracle.canonical(result) + '\n')
    with (output / 'summary.json').open('x', encoding='utf-8') as stream:
        stream.write(oracle.canonical(report) + '\n')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    report = adjudicate(args.source, args.output)
    print(oracle.canonical(report))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
