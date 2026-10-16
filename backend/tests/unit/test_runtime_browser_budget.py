"""Offline accounting safeguards; these tests never contact a model provider."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import multiprocessing
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest


MODULE = 'tests.browser.runtime_browser_budget'
MESSAGES = [{'role': 'user', 'content': '分析南京医院并导出结果'}]
FORMAT = {'type': 'json_object'}
HASH = hashlib.sha256(b'real response boundary').hexdigest()


def budget_module():
    assert importlib.util.find_spec(MODULE) is not None, 'durable browser budget module is missing'
    return importlib.import_module(MODULE)


def make_budget(path, **kwargs):
    defaults = dict(max_usd=10, input_usd_per_million=.3,
                    output_usd_per_million=1.2, cache_hit_usd_per_million=.006,
                    max_planner_calls=6000)
    defaults.update(kwargs)
    return budget_module().DurableCostBudget(path, **defaults)


def reserve(budget, cohort='first'):
    return budget.reserve(MESSAGES, max_tokens=4096, response_format=FORMAT, cohort=cohort)


def valid_usage(**kwargs):
    result = dict(prompt_tokens=1000, completion_tokens=100,
                  prompt_cache_hit_tokens=800, prompt_cache_miss_tokens=200,
                  total_tokens=1100, planner_http_calls=1,
                  planner_compatibility_retry=False)
    result.update(kwargs)
    return result


def test_reservation_persists_before_any_response_and_counts_exact_format(tmp_path):
    path = tmp_path / 'global.sqlite'
    budget = make_budget(path)
    entry = reserve(budget)
    raw_bytes = len(json.dumps(MESSAGES, ensure_ascii=False, sort_keys=True).encode())
    format_bytes = len(json.dumps(FORMAT, ensure_ascii=False, sort_keys=True).encode())
    expected = 2 * ((raw_bytes + format_bytes + 8192) * .3 + 4096 * 1.2) / 1_000_000
    assert entry['reserved_usd'] == pytest.approx(expected, abs=1e-9)
    reopened = make_budget(path)
    assert reopened.summary()['total_usd'] == entry['reserved_usd']
    assert reopened.summary()['planner_calls_reserved'] == 1
    assert reopened.summary()['pending_calls'] == 1


def test_valid_single_call_usage_settles_actual_cache_cost_and_is_idempotent(tmp_path):
    budget = make_budget(tmp_path / 'global.sqlite')
    entry = reserve(budget)
    settled = budget.settle(entry['reservation_id'], usage=valid_usage(), response_hash=HASH, latency_ms=12)
    expected = (800 * .006 + 200 * .3 + 100 * 1.2) / 1_000_000
    assert settled['status'] == 'settled'
    assert settled['charged_usd'] == pytest.approx(expected, abs=1e-9)
    assert budget.summary()['total_usd'] == settled['charged_usd']
    assert budget.summary()['reserved_usd'] == 0
    assert budget.summary()['settled_usd'] == settled['charged_usd']
    again = budget.settle(entry['reservation_id'], usage=valid_usage(), response_hash=HASH, latency_ms=12)
    assert again == settled
    assert len(budget.responses('first')) == 1
    with pytest.raises(ValueError, match='already finalized'):
        budget.settle(entry['reservation_id'], usage=valid_usage(completion_tokens=99, total_tokens=1099), response_hash=HASH, latency_ms=12)


@pytest.mark.parametrize('usage', [
    {}, valid_usage(planner_http_calls=2), valid_usage(planner_http_calls=True),
    valid_usage(planner_compatibility_retry=True), valid_usage(prompt_tokens=-1),
    valid_usage(prompt_tokens=True), valid_usage(prompt_tokens=1.1),
    valid_usage(prompt_tokens=float('nan')), valid_usage(completion_tokens=float('inf')),
    valid_usage(prompt_cache_hit_tokens=1001), valid_usage(prompt_cache_miss_tokens=201),
    valid_usage(prompt_cache_hit_tokens=-1), valid_usage(total_tokens=999),
    valid_usage(prompt_tokens=10**100), valid_usage(completion_tokens=4097, total_tokens=5097),
    valid_usage(prompt_tokens_details={'cached_tokens': True}),
    valid_usage(prompt_tokens_details={'cached_tokens': 799}),
    valid_usage(prompt_cache_miss_tokens=None),
])
def test_missing_ambiguous_or_invalid_usage_retains_full_bound(tmp_path, usage):
    budget = make_budget(tmp_path / 'global.sqlite')
    entry = reserve(budget)
    result = budget.settle(entry['reservation_id'], usage=usage, response_hash=HASH, latency_ms=25)
    assert result['status'] == 'retained'
    assert result['charged_usd'] == entry['reserved_usd']
    assert budget.summary()['total_usd'] == entry['reserved_usd']


def test_uncached_usage_without_cache_fields_is_charged_full_input_rate(tmp_path):
    budget = make_budget(tmp_path / 'global.sqlite')
    entry = reserve(budget)
    usage = dict(prompt_tokens=1000, completion_tokens=100, planner_http_calls=1)
    result = budget.settle(entry['reservation_id'], usage=usage, response_hash=HASH, latency_ms=25)
    assert result['status'] == 'settled'
    assert result['charged_usd'] == pytest.approx(.00042)


def test_provider_error_and_process_restart_never_release_reservation(tmp_path):
    path = tmp_path / 'global.sqlite'
    budget = make_budget(path)
    entry = reserve(budget, cohort='failed-cohort')
    result = budget.retain(entry['reservation_id'], error_code='LLM_UNAVAILABLE')
    assert result['status'] == 'retained'
    reopened = make_budget(path)
    reserve(reopened, cohort='retry-cohort')
    assert reopened.summary()['planner_calls_reserved'] == 2
    assert reopened.summary()['total_usd'] == pytest.approx(2 * entry['reserved_usd'])
    assert reopened.summary()['retained_calls'] == 1


@pytest.mark.parametrize('changed', [
    {'max_usd': 9}, {'max_usd': 11}, {'input_usd_per_million': .29},
    {'output_usd_per_million': 1.1}, {'cache_hit_usd_per_million': .005},
    {'max_planner_calls': 6001},
])
def test_global_policy_cannot_be_replaced_after_restart(tmp_path, changed):
    path = tmp_path / 'global.sqlite'
    make_budget(path)
    with pytest.raises(ValueError):
        make_budget(path, **changed)


def test_global_cap_and_call_cap_reject_before_second_reservation(tmp_path):
    budget = make_budget(tmp_path / 'money.sqlite', max_usd=.02)
    reserve(budget)
    with pytest.raises(RuntimeError, match='cost cap'):
        reserve(make_budget(tmp_path / 'money.sqlite', max_usd=.02), cohort='other')
    assert budget.summary()['planner_calls_reserved'] == 1
    calls = make_budget(tmp_path / 'calls.sqlite', max_planner_calls=1)
    reserve(calls)
    with pytest.raises(RuntimeError, match='call cap'):
        reserve(calls)


def _process_reservation(path, gate, results):
    try:
        budget = make_budget(Path(path), max_usd=.05)
        gate.wait(15)
        reserve(budget, cohort=f'process-{multiprocessing.current_process().pid}')
        results.put('reserved')
    except RuntimeError as exc:
        results.put(str(exc))


def test_independent_processes_share_atomic_cap(tmp_path):
    path = tmp_path / 'shared.sqlite'
    budget = make_budget(path, max_usd=.05)
    context = multiprocessing.get_context('spawn')
    gate, results = context.Event(), context.Queue()
    workers = [context.Process(target=_process_reservation, args=(str(path), gate, results)) for _ in range(6)]
    for worker in workers:
        worker.start()
    gate.set()
    observed = [results.get(timeout=30) for _ in workers]
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0
    assert observed.count('reserved') == 3
    assert budget.summary()['total_usd'] <= .05
    assert budget.summary()['planner_calls_reserved'] == 3


def test_evidence_is_durable_redacted_and_cohort_scoped(tmp_path):
    path = tmp_path / 'global.sqlite'
    budget = make_budget(path)
    first = reserve(budget, cohort='A')
    second = reserve(budget, cohort='B')
    budget.settle(first['reservation_id'], usage=valid_usage(api_key='secret-token'), response_hash=HASH, latency_ms=25)
    budget.retain(second['reservation_id'], error_code='LLM_UNAVAILABLE')
    reopened = make_budget(path)
    rows = reopened.responses('A')
    assert len(rows) == 1
    assert rows[0]['response_hash'] == HASH and rows[0]['http_calls'] == 1
    assert rows[0]['reservation_id'] == first['reservation_id']
    with sqlite3.connect(path) as connection:
        dump = '\n'.join(connection.iterdump())
    assert 'secret-token' not in dump
    assert MESSAGES[0]['content'] not in dump
    assert 'reserved' in dump and 'settled' in dump


def test_source_manifest_detects_changed_deleted_or_relative_sources(tmp_path):
    server = importlib.import_module('tests.browser.runtime_chinese_browser_server')
    path = tmp_path / 'source.py'
    path.write_text('original', encoding='utf-8')
    manifest = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()}
    server.verify_source_manifest(manifest)
    path.write_text('changed', encoding='utf-8')
    with pytest.raises(RuntimeError, match='source drift'):
        server.verify_source_manifest(manifest)
    path.unlink()
    with pytest.raises(RuntimeError, match='source drift'):
        server.verify_source_manifest(manifest)
    with pytest.raises(ValueError, match='absolute'):
        server.verify_source_manifest({'source.py': '0' * 64})


def test_server_factory_requires_explicit_live_opt_in(monkeypatch):
    server = importlib.import_module('tests.browser.runtime_chinese_browser_server')
    monkeypatch.delenv('GISMIND_LIVE_LLM', raising=False)
    with pytest.raises(RuntimeError, match='opt-in'):
        server.create_app()


def test_invalid_usage_metadata_cannot_discard_observed_response(tmp_path):
    budget = make_budget(tmp_path / 'global.sqlite')
    entry = reserve(budget)
    result = budget.settle(entry['reservation_id'], usage=valid_usage(planner_response_format={}),
                           response_hash=HASH, latency_ms=10)
    assert result['response_hash'] == HASH
    assert budget.responses('first')[0]['response_hash'] == HASH


def test_source_drift_fails_before_reserving_or_contacting_provider(tmp_path):
    server = importlib.import_module('tests.browser.runtime_chinese_browser_server')
    source = tmp_path / 'source.py'
    source.write_text('before', encoding='utf-8')
    planner = server.DurableRecordingPlanner(api_key='unused-offline-check',
        base_url='https://api.deepseek.com', model='deepseek-flash', timeout_seconds=30,
        max_tokens=4096, max_retries=0)
    planner.configure_evidence(dict(output=str(tmp_path), budget_db=str(tmp_path / 'global.sqlite'),
        source_manifest={str(source): hashlib.sha256(source.read_bytes()).hexdigest()},
        max_cost_usd=10, input_usd_per_million=.3, output_usd_per_million=1.2,
        cache_hit_usd_per_million=.006, max_planner_calls=6000, timeout_seconds=240))
    assert planner.timeout_seconds == 30
    assert planner.total_timeout_seconds == 90
    source.write_text('after', encoding='utf-8')
    with pytest.raises(RuntimeError, match='source drift'):
        planner._call(MESSAGES)
    assert planner.budget.summary()['planner_calls_reserved'] == 0


def configured_recording_planner(tmp_path):
    """An offline accounting fixture; no model response is supplied."""
    server = importlib.import_module('tests.browser.runtime_chinese_browser_server')
    source = tmp_path / 'source.py'
    source.write_text('frozen', encoding='utf-8')
    planner = server.DurableRecordingPlanner(api_key='unused-offline-check',
        base_url='https://api.deepseek.com', model='deepseek-flash', timeout_seconds=30,
        max_tokens=4096, max_retries=0)
    planner.configure_evidence(dict(output=str(tmp_path), budget_db=str(tmp_path / 'global.sqlite'),
        source_manifest={str(source): hashlib.sha256(source.read_bytes()).hexdigest()},
        max_cost_usd=10, input_usd_per_million=.3, output_usd_per_million=1.2,
        cache_hit_usd_per_million=.006, max_planner_calls=6000, timeout_seconds=240))
    return planner


def test_live_transport_retry_stays_inside_prepaid_two_http_limit(tmp_path):
    planner = configured_recording_planner(tmp_path)
    assert planner.max_retries == 1
    assert planner.max_http_calls == 2
    assert planner.max_tokens == 4096
    assert planner.total_timeout_seconds == 90
    assert planner.budget.summary()['planner_calls_reserved'] == 0


@pytest.mark.parametrize('changed', [
    {'max_retries': 0}, {'max_retries': 2}, {'max_http_calls': 3}, {'max_tokens': 8192},
])
def test_live_limit_drift_fails_before_new_reservation(tmp_path, monkeypatch, changed):
    import httpx

    async def forbidden_post(*args, **kwargs):
        raise AssertionError('no HTTP is allowed in the limit-drift test')

    monkeypatch.setattr(httpx.AsyncClient, 'post', forbidden_post)
    planner = configured_recording_planner(tmp_path)
    for name, value in changed.items():
        setattr(planner, name, value)
    with pytest.raises(RuntimeError, match='live provider limits changed'):
        planner._call(MESSAGES)
    assert planner.budget.summary()['planner_calls_reserved'] == 0


def test_transport_failures_retry_only_with_durable_prepaid_bound(tmp_path, monkeypatch):
    """Inject transport faults, never a synthetic successful LLM response."""
    import httpx
    from app.runtime.llm_planner import PlannerError

    planner = configured_recording_planner(tmp_path)
    observed = []

    async def interrupted_post(client, *args, **kwargs):
        summary = planner.budget.summary()
        assert summary['planner_calls_reserved'] == 1
        assert summary['pending_calls'] == 1
        assert summary['total_usd'] > 0
        observed.append(summary['total_usd'])
        raise httpx.ReadError('injected offline transport failure')

    monkeypatch.setattr(httpx.AsyncClient, 'post', interrupted_post)
    with pytest.raises(PlannerError) as caught:
        planner._call(MESSAGES)
    assert caught.value.code == 'LLM_UNAVAILABLE'
    assert len(observed) == 2
    assert observed[0] == observed[1]
    totals = planner.budget.summary()
    assert totals['planner_calls_reserved'] == 1
    assert totals['pending_calls'] == 0
    assert totals['retained_calls'] == 1
    assert totals['total_usd'] == observed[0]
    assert totals['settled_usd'] == 0
    assert planner.budget.responses(planner.cohort) == []
    evidence = json.loads(planner.evidence_path.read_text(encoding='utf-8'))
    assert evidence['retained_calls'] == 1
    assert evidence['responses'] == []


APPROVAL_TEXT = '提高到 20 美元总上限'
APPROVAL_REFERENCE = 'request_user_input_async:offline-approved-question:0'


def upgrade_facts(budget, **changes):
    result = dict(authorization_id='offline-human-approval-20',
                  approval_reference=APPROVAL_REFERENCE, approval_text=APPROVAL_TEXT,
                  expected_policy=dict(budget.policy),
                  expected_reservations=budget.summary()['planner_calls_reserved'],
                  corpus_sha256=hashlib.sha256(b'frozen 2000 cases, 400 clarifications').hexdigest(),
                  source_sha256=hashlib.sha256(b'frozen reviewed source').hexdigest())
    result.update(changes)
    return result


def authorize_upgrade(path, facts):
    operation = getattr(budget_module(), 'authorize_budget_upgrade_to_twenty', None)
    assert callable(operation), 'explicit operator-authorized 10-to-20 upgrade is missing'
    return operation(path, **facts)


def ledger_rows(path):
    with sqlite3.connect(path) as connection:
        return {table: connection.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
                for table in ('reservations', 'accounting_events', 'sqlite_sequence')}


def policy_row(path):
    with sqlite3.connect(path) as connection:
        return connection.execute('SELECT policy_json FROM budget_policy WHERE id=1').fetchone()[0]


def receipt_rows(path):
    with sqlite3.connect(path) as connection:
        return connection.execute('SELECT * FROM budget_policy_authorizations').fetchall()


def finalized_history(path):
    budget = make_budget(path)
    settled, retained = reserve(budget), reserve(budget, cohort='unknown-usage')
    budget.settle(settled['reservation_id'], usage=valid_usage(), response_hash=HASH, latency_ms=12)
    budget.retain(retained['reservation_id'], error_code='LLM_UNAVAILABLE')
    return budget


def test_upgrade_is_explicit_preserves_every_historical_value_and_reopens(tmp_path):
    path = tmp_path / 'history.sqlite'
    budget = finalized_history(path)
    before, original_policy = ledger_rows(path), dict(budget.policy)
    facts = upgrade_facts(budget)
    receipt = authorize_upgrade(path, facts)
    assert ledger_rows(path) == before
    assert receipt['authorization_id'] == facts['authorization_id']
    assert receipt['approval_reference'] == APPROVAL_REFERENCE
    assert receipt['approval_text'] == APPROVAL_TEXT
    assert receipt['old_policy'] == original_policy
    assert receipt['reservations_at_upgrade'] == 2
    assert receipt['new_policy'] == {**original_policy, 'max_nanos': 20_000_000_000,
                                    'max_planner_calls': 4802}
    assert receipt['call_allowance'] == dict(corpus_cases=2000, clarification_cases=400,
                                           minimum_calls=2400, maximum_calls=4800)
    assert len(receipt_rows(path)) == 1
    reopened = make_budget(path, max_usd=20, max_planner_calls=4802)
    assert reopened.summary()['max_cost_usd'] == 20
    assert reopened.summary()['reserved_usd'] == budget.summary()['reserved_usd']
    assert reopened.summary()['retained_calls'] == 1
    with pytest.raises(ValueError):
        make_budget(path)
    with pytest.raises(ValueError):
        make_budget(path, max_usd=20, max_planner_calls=6000)


@pytest.mark.parametrize('cost,calls', [(20, 6000), (20, 8767), (10, 6001), (21, 6000)])
def test_plain_construction_cannot_authorize_new_twenty_dollar_ledger(tmp_path, cost, calls):
    path = tmp_path / 'unapproved.sqlite'
    with pytest.raises(ValueError):
        make_budget(path, max_usd=cost, max_planner_calls=calls)
    assert not path.exists()


@pytest.mark.parametrize('existing', ['empty-file', 'empty-policy-table'])
def test_twenty_constructor_cannot_initialize_existing_empty_database(tmp_path, existing):
    path = tmp_path / 'empty.sqlite'
    with sqlite3.connect(path) as connection:
        if existing == 'empty-policy-table':
            connection.execute('CREATE TABLE budget_policy (id INTEGER PRIMARY KEY,policy_json TEXT NOT NULL)')
        before = list(connection.iterdump())
    with pytest.raises((ValueError, sqlite3.OperationalError)):
        make_budget(path, max_usd=20)
    with sqlite3.connect(path) as connection:
        assert list(connection.iterdump()) == before


def test_duplicate_upgrade_is_read_only_even_after_new_pending_reservation(tmp_path):
    path = tmp_path / 'idempotent.sqlite'
    budget = finalized_history(path)
    facts = upgrade_facts(budget)
    first = authorize_upgrade(path, facts)
    reserve(budget, cohort='after-upgrade')
    before, rows = ledger_rows(path), receipt_rows(path)
    assert authorize_upgrade(path, facts) == first
    assert ledger_rows(path) == before
    assert receipt_rows(path) == rows


@pytest.mark.parametrize('changes', [
    {'approval_reference': 'a-different-approved-question'},
    {'corpus_sha256': '1' * 64}, {'source_sha256': '2' * 64},
    {'expected_reservations': 3},
])
def test_authorization_id_conflicts_fail_without_changes(tmp_path, changes):
    path = tmp_path / 'conflict.sqlite'
    budget = finalized_history(path)
    facts = upgrade_facts(budget)
    authorize_upgrade(path, facts)
    before, rows, policy = ledger_rows(path), receipt_rows(path), policy_row(path)
    with pytest.raises(ValueError, match='authorization'):
        authorize_upgrade(path, {**facts, **changes})
    assert (ledger_rows(path), receipt_rows(path), policy_row(path)) == (before, rows, policy)
    with pytest.raises(ValueError):
        authorize_upgrade(path, {**facts, 'authorization_id': 'second-approval'})
    assert receipt_rows(path) == rows


@pytest.mark.parametrize('changes', [
    {'authorization_id': ''}, {'authorization_id': 'a' * 257},
    {'approval_reference': ''}, {'approval_reference': '   '},
    {'approval_reference': 'question\nforged continuation'}, {'approval_reference': 'a' * 513},
    {'approval_text': '另加 20 美元'}, {'approval_text': '提高到 21 美元总上限'},
    {'corpus_sha256': 'not-a-digest'}, {'source_sha256': 'F' * 64},
    {'expected_reservations': True}, {'expected_reservations': 3},
])
def test_invalid_authorization_facts_fail_before_any_mutation(tmp_path, changes):
    path = tmp_path / 'invalid.sqlite'
    budget = finalized_history(path)
    before, policy = ledger_rows(path), policy_row(path)
    with pytest.raises(ValueError):
        authorize_upgrade(path, upgrade_facts(budget, **changes))
    assert ledger_rows(path) == before and policy_row(path) == policy
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='budget_policy_authorizations'").fetchone() is None


@pytest.mark.parametrize('change', [
    {'max_nanos': 9_000_000_000}, {'max_nanos': 21_000_000_000},
    {'max_planner_calls': 6001}, {'input_rate': '0.29'},
    {'output_rate': '1.1'}, {'cache_rate': '0.005'}, {'version': 2},
    {'version': True}, {'max_nanos': 10_000_000_000.0}, {'max_planner_calls': 6000.0},
    {'extra': 'unexpected'},
])
def test_upgrade_requires_exact_old_policy_compare_and_swap(tmp_path, change):
    path = tmp_path / 'cas.sqlite'
    budget = finalized_history(path)
    before, policy = ledger_rows(path), policy_row(path)
    facts = upgrade_facts(budget, expected_policy={**budget.policy, **change})
    with pytest.raises(ValueError):
        authorize_upgrade(path, facts)
    assert ledger_rows(path) == before and policy_row(path) == policy


def test_pending_calls_block_first_upgrade_without_releasing_the_bound(tmp_path):
    path = tmp_path / 'pending.sqlite'
    budget = make_budget(path)
    reserve(budget)
    before, policy = ledger_rows(path), policy_row(path)
    with pytest.raises(ValueError, match='pending'):
        authorize_upgrade(path, upgrade_facts(budget))
    assert ledger_rows(path) == before and policy_row(path) == policy


def test_upgrade_does_not_create_missing_ledger(tmp_path):
    reference = make_budget(tmp_path / 'reference.sqlite')
    path = tmp_path / 'missing.sqlite'
    with pytest.raises((ValueError, sqlite3.OperationalError)):
        authorize_upgrade(path, upgrade_facts(reference))
    assert not path.exists()


def test_audit_insert_exception_rolls_back_policy_and_new_audit_table(tmp_path, monkeypatch):
    path = tmp_path / 'rollback.sqlite'
    budget = finalized_history(path)
    before, policy = ledger_rows(path), policy_row(path)
    original_connect = sqlite3.connect

    def deny_audit_insert(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_authorizer(lambda action, first, second, database, trigger:
                                  sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and
                                  first == 'budget_policy_authorizations' else sqlite3.SQLITE_OK)
        return connection

    with monkeypatch.context() as local:
        local.setattr(budget_module().sqlite3, 'connect', deny_audit_insert)
        with pytest.raises(sqlite3.DatabaseError):
            authorize_upgrade(path, upgrade_facts(budget))
    assert ledger_rows(path) == before and policy_row(path) == policy
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='budget_policy_authorizations'").fetchone() is None


def _crash_upgrade_before_audit_insert(path, facts):
    import os

    class CrashConnection(sqlite3.Connection):
        def execute(self, statement, parameters=()):
            if statement.startswith('INSERT INTO budget_policy_authorizations'):
                os._exit(23)
            return super().execute(statement, parameters)

    module = budget_module()
    original_connect = module.sqlite3.connect
    module.sqlite3.connect = lambda *args, **kwargs: original_connect(*args, factory=CrashConnection, **kwargs)
    authorize_upgrade(Path(path), facts)


def test_process_crash_before_receipt_commit_preserves_original_ledger(tmp_path):
    path = tmp_path / 'crash.sqlite'
    budget = finalized_history(path)
    before, policy = ledger_rows(path), policy_row(path)
    context = multiprocessing.get_context('spawn')
    child = context.Process(target=_crash_upgrade_before_audit_insert,
                            args=(str(path), upgrade_facts(budget)))
    child.start()
    child.join(timeout=30)
    assert child.exitcode == 23
    assert ledger_rows(path) == before and policy_row(path) == policy
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='budget_policy_authorizations'").fetchone() is None


def test_concurrent_identical_upgrade_creates_exactly_one_receipt(tmp_path):
    path = tmp_path / 'concurrent-upgrade.sqlite'
    budget = finalized_history(path)
    facts, before = upgrade_facts(budget), ledger_rows(path)
    barrier = threading.Barrier(4)

    def authorized_operator():
        barrier.wait(timeout=10)
        return authorize_upgrade(path, facts)

    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda _: authorized_operator(), range(4)))
    assert receipts == [receipts[0]] * 4
    assert len(receipt_rows(path)) == 1
    assert ledger_rows(path) == before


def test_concurrent_different_ids_cannot_authorize_second_increase(tmp_path):
    path = tmp_path / 'different-ids.sqlite'
    budget = finalized_history(path)
    facts = upgrade_facts(budget)
    barrier = threading.Barrier(2)

    def authorized_operator(identifier):
        barrier.wait(timeout=10)
        try:
            authorize_upgrade(path, {**facts, 'authorization_id': identifier})
            return 'upgraded'
        except ValueError:
            return 'rejected'

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(authorized_operator, ['approval-A', 'approval-B']))
    assert sorted(outcomes) == ['rejected', 'upgraded']
    assert len(receipt_rows(path)) == 1


def test_cached_instance_reads_durable_twenty_policy_and_prices(tmp_path):
    path = tmp_path / 'cached.sqlite'
    budget = finalized_history(path)
    authorize_upgrade(path, upgrade_facts(budget))
    # Seed a synthetic occupied-cost boundary, without any provider or LLM response.
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE reservations SET charged_nanos=9_999_999_999 WHERE status='retained'")
    budget.input_rate = budget.output_rate = budget.cache_rate = 0
    entry = reserve(budget)
    expected = 2 * ((entry['input_tokens_bound'] * .3) + 4096 * 1.2) / 1_000_000
    assert entry['reserved_usd'] == pytest.approx(expected, abs=1e-9)
    assert budget.summary()['total_usd'] > 10
    assert budget.summary()['total_usd'] <= 20


def seed_zero_cost_history(path, count):
    """Synthetic SQLite accounting rows for offline cumulative-call boundaries."""
    with sqlite3.connect(path) as connection:
        connection.executemany('INSERT INTO reservations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            [(f'offline-history-{index}', 'synthetic-history', '0' * 64, 1.0,
              0, 0, 0, 4096, 2, 'settled', '{"offline_history": true}', 1.0)
             for index in range(count)])


@pytest.mark.parametrize('historical_count', [0, 1200, 6000])
def test_upgrade_call_allowance_uses_exact_frozen_base_without_http_multiplier(tmp_path, historical_count):
    path = tmp_path / 'derived.sqlite'
    budget = make_budget(path)
    seed_zero_cost_history(path, historical_count)
    before = ledger_rows(path)
    receipt = authorize_upgrade(path, upgrade_facts(budget))
    assert receipt['new_policy']['max_planner_calls'] == historical_count + 4800
    assert ledger_rows(path) == before
    reopened = make_budget(path, max_usd=20, max_planner_calls=historical_count + 4800)
    assert reopened.summary()['planner_calls_reserved'] == historical_count


def test_concurrent_calls_use_persistent_call_cap_despite_mutated_cache(tmp_path):
    path = tmp_path / 'call-boundary.sqlite'
    budget = make_budget(path)
    authorize_upgrade(path, upgrade_facts(budget))
    seed_zero_cost_history(path, 4799)
    budget.policy.update(max_nanos=10**15, max_planner_calls=10**9)
    barrier = threading.Barrier(4)

    def caller(_):
        barrier.wait(timeout=10)
        try:
            reserve(budget)
            return 'reserved'
        except RuntimeError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(caller, range(4)))
    assert outcomes.count('reserved') == 1
    assert all(outcome == 'reserved' or 'call cap' in outcome for outcome in outcomes)
    assert budget.summary()['planner_calls_reserved'] == 4800


def test_settlement_uses_durable_rates_despite_mutated_instance_cache(tmp_path):
    path = tmp_path / 'settle-prices.sqlite'
    budget = finalized_history(path)
    authorize_upgrade(path, upgrade_facts(budget))
    entry = reserve(budget)
    budget.input_rate = budget.output_rate = budget.cache_rate = 0
    budget.policy.update(input_rate='0', output_rate='0', cache_rate='0')
    settled = budget.settle(entry['reservation_id'], usage=valid_usage(), response_hash=HASH, latency_ms=12)
    assert settled['charged_usd'] == pytest.approx(.0001848)


@pytest.mark.parametrize('change', [
    {'input_rate': '0.01'}, {'output_rate': '0.01'}, {'cache_rate': '0.001'},
    {'max_nanos': 10_000_000_000}, {'max_planner_calls': 6000},
])
def test_live_old_instance_rejects_unauthorized_persistent_policy_change(tmp_path, change):
    path = tmp_path / 'old-instance.sqlite'
    options = ({'max_usd': .02} if 'max_nanos' in change else
               {'max_planner_calls': 1} if 'max_planner_calls' in change else {})
    budget = make_budget(path, **options)
    entry = reserve(budget)
    with sqlite3.connect(path) as connection:
        connection.execute('UPDATE budget_policy SET policy_json=?',
                           (json.dumps({**budget.policy, **change}),))
    before = ledger_rows(path)
    with pytest.raises(ValueError):
        reserve(budget)
    with pytest.raises(ValueError):
        budget.settle(entry['reservation_id'], usage=valid_usage(), response_hash=HASH, latency_ms=12)
    assert ledger_rows(path) == before


@pytest.mark.parametrize('damage', ['over-call-cap', 'over-dollar-cap'])
def test_first_upgrade_rejects_overdrawn_original_policy(tmp_path, damage):
    path = tmp_path / 'overdrawn.sqlite'
    budget = make_budget(path)
    seed_zero_cost_history(path, 6001 if damage == 'over-call-cap' else 1)
    if damage == 'over-dollar-cap':
        with sqlite3.connect(path) as connection:
            connection.execute('UPDATE reservations SET charged_nanos=10000000001')
    before, policy = ledger_rows(path), policy_row(path)
    with pytest.raises(ValueError):
        authorize_upgrade(path, upgrade_facts(budget))
    assert ledger_rows(path) == before and policy_row(path) == policy


@pytest.mark.parametrize('damage', ['incomplete-reservations', 'missing-accounting-events'])
def test_authorization_rejects_nonledger_schema_without_repair(tmp_path, damage):
    reference = make_budget(tmp_path / 'reference.sqlite')
    with sqlite3.connect(reference.path) as connection:
        schemas = {name: connection.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()[0]
                   for name in ('budget_policy', 'reservations', 'accounting_events')}
    path = tmp_path / 'wrong-schema.sqlite'
    with sqlite3.connect(path) as connection:
        connection.execute(schemas['budget_policy'])
        connection.execute('INSERT INTO budget_policy VALUES (1,?)', (json.dumps(reference.policy),))
        if damage == 'incomplete-reservations':
            connection.execute('CREATE TABLE reservations (status TEXT)')
            connection.execute("INSERT INTO reservations VALUES ('settled')")
            connection.execute(schemas['accounting_events'])
        else:
            connection.execute(schemas['reservations'])
    if damage == 'missing-accounting-events':
        seed_zero_cost_history(path, 1)
    with sqlite3.connect(path) as connection:
        before = list(connection.iterdump())
    with pytest.raises(ValueError):
        authorize_upgrade(path, upgrade_facts(reference, expected_reservations=1))
    with sqlite3.connect(path) as connection:
        assert list(connection.iterdump()) == before


def test_reopen_and_reserve_reject_damaged_authorized_ledger_schema(tmp_path):
    path = tmp_path / 'damaged-authorized.sqlite'
    budget = finalized_history(path)
    authorize_upgrade(path, upgrade_facts(budget))
    with sqlite3.connect(path) as connection:
        connection.execute('DROP TABLE accounting_events')
        before = list(connection.iterdump())
    with pytest.raises(ValueError):
        make_budget(path, max_usd=20, max_planner_calls=4802)
    with pytest.raises(ValueError):
        reserve(budget)
    with sqlite3.connect(path) as connection:
        assert list(connection.iterdump()) == before


def test_concurrent_reservations_cannot_exceed_twenty_dollar_cap(tmp_path):
    path = tmp_path / 'twenty-boundary.sqlite'
    budget = finalized_history(path)
    authorize_upgrade(path, upgrade_facts(budget))
    entry = reserve(budget)
    budget.retain(entry['reservation_id'], error_code='OFFLINE_BOUNDARY')
    bound = round(entry['reserved_usd'] * 1_000_000_000)
    with sqlite3.connect(path) as connection:
        connection.execute('UPDATE reservations SET charged_nanos=0')
        connection.execute('UPDATE reservations SET charged_nanos=? WHERE reservation_id=?',
                           (20_000_000_000 - 2 * bound + 1, entry['reservation_id']))
    barrier = threading.Barrier(4)

    def caller(_):
        barrier.wait(timeout=10)
        try:
            reserve(budget)
            return 'reserved'
        except RuntimeError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(caller, range(4)))
    assert outcomes.count('reserved') == 1
    assert all(outcome == 'reserved' or 'cost cap' in outcome for outcome in outcomes)
    assert budget.summary()['total_usd'] <= 20


def test_new_reservation_racing_upgrade_is_serialized(tmp_path):
    path = tmp_path / 'upgrade-reserve-race.sqlite'
    budget = finalized_history(path)
    facts, barrier = upgrade_facts(budget), threading.Barrier(2)

    def operator():
        barrier.wait(timeout=10)
        try:
            authorize_upgrade(path, facts)
            return 'upgraded'
        except ValueError as exc:
            assert 'pending' in str(exc) or 'reservation' in str(exc)
            return 'blocked'

    def caller():
        barrier.wait(timeout=10)
        return reserve(budget)

    with ThreadPoolExecutor(max_workers=2) as pool:
        upgrade_result, reservation_result = pool.submit(operator), pool.submit(caller)
        outcome, entry = upgrade_result.result(), reservation_result.result()
    assert entry['reserved_usd'] > 0
    assert budget.summary()['planner_calls_reserved'] == 3
    assert budget.summary()['max_cost_usd'] == (20 if outcome == 'upgraded' else 10)


@pytest.mark.parametrize('tamper', ['delete-table', 'delete-row', 'receipt-json', 'receipt-hash', 'policy-price', 'policy-cap', 'policy-calls', 'downgrade'])
def test_missing_or_tampered_authorization_fails_closed_before_reserve(tmp_path, tamper):
    path = tmp_path / 'tampered.sqlite'
    budget = finalized_history(path)
    authorize_upgrade(path, upgrade_facts(budget))
    with sqlite3.connect(path) as connection:
        if tamper == 'delete-table':
            connection.execute('DROP TABLE budget_policy_authorizations')
        elif tamper == 'delete-row':
            connection.execute('DELETE FROM budget_policy_authorizations')
        elif tamper == 'receipt-json':
            connection.execute("UPDATE budget_policy_authorizations SET receipt_json='{}'")
        elif tamper == 'receipt-hash':
            connection.execute('UPDATE budget_policy_authorizations SET receipt_sha256=?', ('0' * 64,))
        else:
            policy = json.loads(policy_row(path))
            changes = {'policy-price': {'input_rate': '0.01'}, 'policy-cap': {'max_nanos': 21_000_000_000},
                       'policy-calls': {'max_planner_calls': 10000}, 'downgrade': {'max_nanos': 10_000_000_000}}
            connection.execute('UPDATE budget_policy SET policy_json=?',
                               (json.dumps({**policy, **changes[tamper]}),))
    before = ledger_rows(path)
    with pytest.raises(ValueError):
        reserve(budget)
    with pytest.raises(ValueError):
        make_budget(path, max_usd=20, max_planner_calls=4802)
    assert ledger_rows(path) == before


def test_hand_edited_twenty_policy_without_receipt_is_rejected(tmp_path):
    path = tmp_path / 'hand-edited.sqlite'
    budget = make_budget(path)
    with sqlite3.connect(path) as connection:
        connection.execute('UPDATE budget_policy SET policy_json=?',
                           (json.dumps({**budget.policy, 'max_nanos': 20_000_000_000}),))
    with pytest.raises(ValueError):
        make_budget(path, max_usd=20)
    with pytest.raises(ValueError):
        reserve(budget)


@pytest.mark.parametrize('field', ['old_policy', 'new_policy', 'reservations_at_upgrade', 'call_allowance', 'created_at', 'expected_policy'])
def test_rehashed_inconsistent_receipt_still_fails_closed(tmp_path, field):
    path = tmp_path / 'inconsistent-receipt.sqlite'
    budget = finalized_history(path)
    facts = upgrade_facts(budget)
    authorize_upgrade(path, facts)
    with sqlite3.connect(path) as connection:
        receipt = json.loads(connection.execute('SELECT receipt_json FROM budget_policy_authorizations').fetchone()[0])
        if field in {'old_policy', 'new_policy', 'expected_policy'}:
            receipt[field]['cache_rate'] = '0.005'
        elif field == 'reservations_at_upgrade':
            receipt[field] += 1
        elif field == 'call_allowance':
            receipt[field]['maximum_calls'] *= 2
        else:
            receipt[field] = 'not-a-timestamp'
        document = json.dumps(receipt, ensure_ascii=False, sort_keys=True, allow_nan=False)
        digest = hashlib.sha256(document.encode('utf-8')).hexdigest()
        connection.execute('UPDATE budget_policy_authorizations SET receipt_json=?,receipt_sha256=?',
                           (document, digest))
    before = ledger_rows(path)
    with pytest.raises(ValueError):
        reserve(budget)
    with pytest.raises(ValueError):
        make_budget(path, max_usd=20, max_planner_calls=4802)
    with pytest.raises(ValueError):
        authorize_upgrade(path, facts)
    assert ledger_rows(path) == before


def test_summary_cli_remains_read_only_for_existing_and_missing_ledger(tmp_path, monkeypatch, capsys):
    module = budget_module()
    path = tmp_path / 'readonly.sqlite'
    budget = finalized_history(path)
    before = ledger_rows(path), policy_row(path)
    monkeypatch.setattr('sys.argv', ['runtime_browser_budget', '--db', str(path)])
    module.main()
    assert json.loads(capsys.readouterr().out)['max_cost_usd'] == 10
    assert (ledger_rows(path), policy_row(path)) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='budget_policy_authorizations'").fetchone() is None
    missing = tmp_path / 'does-not-exist.sqlite'
    monkeypatch.setattr('sys.argv', ['runtime_browser_budget', '--db', str(missing)])
    with pytest.raises(sqlite3.OperationalError):
        module.main()
    assert not missing.exists()
