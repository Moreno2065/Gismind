"""Durable global accounting for opt-in real browser/provider runs.

Reservations are committed before HTTP. SQLite serializes independent API
processes; restarts and failed cohorts never reset their shared policy or cost.
Only validated usage for one completely observed HTTP request releases a bound.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Any
import uuid


_NANO_USD = 1_000_000_000
_MAX_INTEGER = 2**63 - 1
_ORIGINAL_CAP_NANOS = 10 * _NANO_USD
_UPGRADED_CAP_NANOS = 20 * _NANO_USD
_APPROVAL_TEXT = '提高到 20 美元总上限'
_CALL_ALLOWANCE = {'corpus_cases': 2000, 'clarification_cases': 400,
                   'minimum_calls': 2400, 'maximum_calls': 4800}
_POLICY_KEYS = frozenset({'version', 'max_nanos', 'input_rate', 'output_rate',
                          'cache_rate', 'max_planner_calls'})
_AUTHORIZATION_KEYS = frozenset({'authorization_id', 'approval_reference', 'approval_text',
                                'expected_policy', 'expected_reservations',
                                'corpus_sha256', 'source_sha256'})
_LEDGER_SCHEMA = {
    'budget_policy': [('id', 'INTEGER', 0, 1), ('policy_json', 'TEXT', 1, 0)],
    'reservations': [('reservation_id', 'TEXT', 0, 1), ('cohort', 'TEXT', 1, 0),
        ('request_hash', 'TEXT', 1, 0), ('created_at', 'REAL', 1, 0),
        ('reserved_nanos', 'INTEGER', 1, 0), ('charged_nanos', 'INTEGER', 1, 0),
        ('input_tokens_bound', 'INTEGER', 1, 0), ('max_tokens', 'INTEGER', 1, 0),
        ('max_http_calls', 'INTEGER', 1, 0), ('status', 'TEXT', 1, 0),
        ('final_json', 'TEXT', 0, 0), ('finalized_at', 'REAL', 0, 0)],
    'accounting_events': [('sequence', 'INTEGER', 0, 1), ('reservation_id', 'TEXT', 1, 0),
        ('event', 'TEXT', 1, 0), ('created_at', 'REAL', 1, 0), ('details_json', 'TEXT', 1, 0)],
}
_AUTHORIZATION_SCHEMA = [('authorization_id', 'TEXT', 0, 1), ('receipt_json', 'TEXT', 1, 0),
                         ('receipt_sha256', 'TEXT', 1, 0)]
_USAGE_KEYS = frozenset({
    'prompt_tokens', 'completion_tokens', 'total_tokens',
    'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens', 'prompt_tokens_details',
    'completion_tokens_details', 'planner_http_calls', 'planner_compatibility_retry',
    'planner_response_format', 'planner_schema_strict',
})


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _positive_decimal(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f'{name} must be finite and positive') from exc
    if isinstance(value, bool) or not result.is_finite() or result <= 0:
        raise ValueError(f'{name} must be finite and positive')
    return result


def _integer(value: Any, *, positive: bool = False) -> bool:
    return type(value) is int and (1 if positive else 0) <= value <= _MAX_INTEGER


def _nanos(usd: Decimal) -> int:
    result = int((usd * _NANO_USD).to_integral_value(rounding=ROUND_CEILING))
    if not 0 <= result <= _MAX_INTEGER:
        raise ValueError('cost is outside the supported accounting range')
    return result


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _sha256(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in '0123456789abcdef' for char in value):
        raise ValueError(f'{name} must be a SHA256 hex digest')
    return value


def _operator_text(value: Any, name: str, limit: int) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > limit or
            any(ord(char) < 32 or ord(char) == 127 or char in '\u2028\u2029' for char in value)):
        raise ValueError(f'{name} must be nonblank bounded single-line text')
    return value


def _policy_fields(policy: Any) -> dict[str, Any]:
    if not isinstance(policy, dict) or policy.keys() != _POLICY_KEYS:
        raise ValueError('invalid global budget policy fields')
    if (type(policy['version']) is not int or policy['version'] != 1 or
            not _integer(policy['max_nanos'], positive=True) or
            not _integer(policy['max_planner_calls'], positive=True)):
        raise ValueError('invalid global budget policy integers or version')
    rates = {}
    for name in ('input_rate', 'output_rate', 'cache_rate'):
        if not isinstance(policy[name], str):
            raise ValueError('global budget rates must be canonical decimal strings')
        rates[name] = _positive_decimal(policy[name], name)
        if str(rates[name].normalize()) != policy[name]:
            raise ValueError('global budget rates must be canonical decimal strings')
    if rates['cache_rate'] > rates['input_rate']:
        raise ValueError('cache hit price cannot exceed the uncached input price')
    return dict(policy)


def _authorization_facts(*, authorization_id: str, approval_reference: str,
                         approval_text: str, expected_policy: dict[str, Any],
                         expected_reservations: int, corpus_sha256: str,
                         source_sha256: str) -> dict[str, Any]:
    policy = _policy_fields(expected_policy)
    if policy['max_nanos'] != _ORIGINAL_CAP_NANOS or policy['max_planner_calls'] != 6000:
        raise ValueError('authorization requires the original USD 10 / 6000-call policy')
    if approval_text != _APPROVAL_TEXT:
        raise ValueError('authorization must record the exact USD 20 total-cap approval text')
    if not _integer(expected_reservations) or expected_reservations > 6000:
        raise ValueError('expected_reservations must be an integer within the original 6000-call cap')
    return {
        'authorization_id': _operator_text(authorization_id, 'authorization_id', 256),
        'approval_reference': _operator_text(approval_reference, 'approval_reference', 512),
        'approval_text': approval_text, 'expected_policy': policy,
        'expected_reservations': expected_reservations,
        'corpus_sha256': _sha256(corpus_sha256, 'corpus_sha256'),
        'source_sha256': _sha256(source_sha256, 'source_sha256'),
    }


def _upgraded_policy(old_policy: dict[str, Any], historical_calls: int) -> dict[str, Any]:
    return {**old_policy, 'max_nanos': _UPGRADED_CAP_NANOS,
            'max_planner_calls': historical_calls + _CALL_ALLOWANCE['maximum_calls']}


@contextmanager
def _ledger_connection(path: Path, *, existing: bool = False):
    # mode=rw rejects a missing ledger; an authorization can never initialize one.
    target = path.as_uri() + '?mode=rw' if existing else str(path)
    connection = sqlite3.connect(target, uri=existing, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA busy_timeout=30000')
    connection.execute('PRAGMA synchronous=FULL')
    try:
        yield connection
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _stored_policy(connection: sqlite3.Connection) -> tuple[str, dict[str, Any]]:
    row = connection.execute('SELECT policy_json FROM budget_policy WHERE id=1').fetchone()
    if row is None:
        raise ValueError('global budget policy is missing')
    try:
        policy = _policy_fields(json.loads(row[0]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError('invalid global budget policy') from exc
    return row[0], policy


def _validate_table(connection: sqlite3.Connection, name: str, expected: list[tuple[str, str, int, int]]) -> None:
    table = connection.execute("SELECT type FROM sqlite_master WHERE name=?", (name,)).fetchone()
    actual = [(row['name'], row['type'].upper(), row['notnull'], row['pk'])
              for row in connection.execute(f'PRAGMA table_info({name})')]
    if table is None or table[0] != 'table' or actual != expected:
        raise ValueError(f'invalid global budget ledger schema for {name}')


def _validate_ledger_schema(connection: sqlite3.Connection) -> None:
    for name, expected in _LEDGER_SCHEMA.items():
        _validate_table(connection, name, expected)


def _authorization_receipt(connection: sqlite3.Connection, policy: dict[str, Any]) -> dict[str, Any] | None:
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='budget_policy_authorizations'").fetchone() is None:
        return None
    _validate_table(connection, 'budget_policy_authorizations', _AUTHORIZATION_SCHEMA)
    rows = connection.execute('SELECT authorization_id,receipt_json,receipt_sha256 FROM budget_policy_authorizations').fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError('global budget authorization must contain exactly one receipt')
    row = rows[0]
    try:
        receipt = json.loads(row['receipt_json'])
        keys = _AUTHORIZATION_KEYS | {'old_policy', 'new_policy', 'reservations_at_upgrade',
                                      'call_allowance', 'created_at', 'receipt_version'}
        if not isinstance(receipt, dict) or receipt.keys() != keys:
            raise ValueError('invalid authorization receipt fields')
        if _json(receipt) != row['receipt_json'] or _digest(row['receipt_json']) != row['receipt_sha256']:
            raise ValueError('authorization receipt integrity check failed')
        facts = _authorization_facts(**{name: receipt[name] for name in _AUTHORIZATION_KEYS})
        if type(receipt['receipt_version']) is not int or receipt['receipt_version'] != 1:
            raise ValueError('invalid authorization receipt version')
        if (not _integer(receipt['reservations_at_upgrade']) or
                receipt['reservations_at_upgrade'] != facts['expected_reservations'] or
                receipt['authorization_id'] != row['authorization_id']):
            raise ValueError('authorization receipt historical baseline differs')
        old = _policy_fields(receipt['old_policy'])
        new = _policy_fields(receipt['new_policy'])
        if old != facts['expected_policy'] or new != _upgraded_policy(old, receipt['reservations_at_upgrade']) or new != policy:
            raise ValueError('authorization receipt does not match the persistent policy')
        if (_json(receipt['call_allowance']) != _json(_CALL_ALLOWANCE) or
                type(receipt['created_at']) not in (int, float) or
                not math.isfinite(receipt['created_at']) or receipt['created_at'] <= 0):
            raise ValueError('invalid authorization receipt allowance or timestamp')
        return receipt
    except (TypeError, KeyError, json.JSONDecodeError, OverflowError) as exc:
        raise ValueError('invalid global budget authorization receipt') from exc


def _validated_policy(connection: sqlite3.Connection, *, expected_policy: dict[str, Any] | None = None) -> dict[str, Any]:
    _validate_ledger_schema(connection)
    _, policy = _stored_policy(connection)
    receipt = _authorization_receipt(connection, policy)
    original = policy['max_nanos'] <= _ORIGINAL_CAP_NANOS and policy['max_planner_calls'] <= 6000 and receipt is None
    upgraded = policy['max_nanos'] == _UPGRADED_CAP_NANOS and receipt is not None
    if not (original or upgraded):
        raise ValueError('global budget policy requires a valid explicit authorization receipt')
    if (expected_policy is not None and policy != expected_policy and
            (receipt is None or receipt['old_policy'] != expected_policy)):
        raise ValueError('global budget policy changed without a matching authorization')
    return policy


def authorize_budget_upgrade_to_twenty(path: Path | str, *, authorization_id: str,
                                      approval_reference: str, approval_text: str,
                                      expected_policy: dict[str, Any], expected_reservations: int,
                                      corpus_sha256: str, source_sha256: str) -> dict[str, Any]:
    """Record an operator-verified human approval for one USD 10 -> 20 upgrade.

    The trusted operator must verify the approval in the human conversation.
    Supplied references, quotes and checksums record facts and detect drift;
    they cannot independently authenticate a human or grant permission.
    The total-dollar cap and all historical charges remain cumulative. The
    fixed 2000-case corpus adds 2400 planning stages, each with at most initial
    plus one repair (4800 reservations). HTTP retries are prepaid inside each
    reservation; abnormal replays also consume this finite call allowance.
    """
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('budget database path must be absolute')
    facts = _authorization_facts(authorization_id=authorization_id,
        approval_reference=approval_reference, approval_text=approval_text,
        expected_policy=expected_policy, expected_reservations=expected_reservations,
        corpus_sha256=corpus_sha256, source_sha256=source_sha256)
    with _ledger_connection(path, existing=True) as connection:
        connection.execute('BEGIN IMMEDIATE')
        _validate_ledger_schema(connection)
        original_json, policy = _stored_policy(connection)
        receipt = _authorization_receipt(connection, policy)
        if receipt is not None:
            if {name: receipt[name] for name in _AUTHORIZATION_KEYS} == facts:
                return receipt  # Read-only retry, even if new reservations are pending.
            raise ValueError('global budget authorization already exists with different facts')
        if _validated_policy(connection) != facts['expected_policy']:
            raise ValueError('global budget authorization policy compare-and-swap failed')
        counts = connection.execute("SELECT COUNT(*),COALESCE(SUM(status='pending'),0),COALESCE(SUM(charged_nanos),0) FROM reservations").fetchone()
        if counts[1]:
            raise ValueError('pending reservations block a first budget authorization')
        if counts[0] != facts['expected_reservations']:
            raise ValueError('global budget authorization reservation baseline changed')
        if counts[2] > policy['max_nanos']:
            raise ValueError('global budget history exceeds the original authorized cost cap')
        new_policy = _upgraded_policy(policy, counts[0])
        receipt = {**facts, 'receipt_version': 1, 'old_policy': policy,
                   'new_policy': new_policy, 'reservations_at_upgrade': counts[0],
                   'call_allowance': dict(_CALL_ALLOWANCE), 'created_at': time.time()}
        document = _json(receipt)
        connection.execute('''CREATE TABLE IF NOT EXISTS budget_policy_authorizations (
            authorization_id TEXT PRIMARY KEY, receipt_json TEXT NOT NULL,
            receipt_sha256 TEXT NOT NULL)''')
        updated = connection.execute('UPDATE budget_policy SET policy_json=? WHERE id=1 AND policy_json=?',
                                     (_json(new_policy), original_json))
        if updated.rowcount != 1:
            raise ValueError('global budget authorization policy compare-and-swap failed')
        connection.execute('INSERT INTO budget_policy_authorizations VALUES (?,?,?)',
                           (facts['authorization_id'], document, _digest(document)))
        _validated_policy(connection)
        connection.commit()
        return receipt


def _safe_usage(usage: Any) -> dict[str, Any]:
    """Persist only usage facts, never arbitrary provider data or credentials."""
    if not isinstance(usage, dict):
        return {}
    result = {}
    for name in _USAGE_KEYS & usage.keys():
        value = usage[name]
        if name in {'prompt_tokens_details', 'completion_tokens_details'}:
            if isinstance(value, dict):
                result[name] = {key: number if _integer(number) else '<invalid>'
                                for key, number in value.items()
                                if key in {'cached_tokens', 'reasoning_tokens', 'audio_tokens',
                                           'accepted_prediction_tokens', 'rejected_prediction_tokens'}}
        elif _integer(value) or isinstance(value, bool):
            result[name] = value
        elif name == 'planner_response_format' and isinstance(value, str) and value in {'json_object', 'json_schema'}:
            result[name] = value
        else:
            result[name] = '<invalid>'
    return result


def _summary(connection: sqlite3.Connection, policy: dict[str, Any]) -> dict[str, Any]:
    rows = connection.execute(
        'SELECT status, COUNT(*) AS calls, COALESCE(SUM(charged_nanos),0) AS amount '
        'FROM reservations GROUP BY status'
    ).fetchall()
    counts = {row['status']: row['calls'] for row in rows}
    amounts = {row['status']: row['amount'] for row in rows}
    total = sum(amounts.values())
    cap = policy['max_nanos']
    return {
        'max_cost_usd': cap / _NANO_USD,
        'total_usd': total / _NANO_USD,
        'settled_usd': amounts.get('settled', 0) / _NANO_USD,
        'reserved_usd': (amounts.get('pending', 0) + amounts.get('retained', 0)) / _NANO_USD,
        'remaining_usd': max(0, cap - total) / _NANO_USD,
        'planner_calls_reserved': sum(counts.values()),
        'max_planner_calls': policy['max_planner_calls'],
        'pending_calls': counts.get('pending', 0),
        'retained_calls': counts.get('retained', 0),
        'settled_calls': counts.get('settled', 0),
    }


def budget_summary(path: Path | str) -> dict[str, Any]:
    """Read an existing ledger without creating a new budget or changing policy."""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('budget database path must be absolute')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=30)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        policy = _validated_policy(connection)
        return _summary(connection, policy)


class DurableCostBudget:
    def __init__(self, path: Path | str, *, max_usd: float = 10,
                 input_usd_per_million: float, output_usd_per_million: float,
                 cache_hit_usd_per_million: float, max_planner_calls: int = 6000):
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError('budget database path must be absolute')
        cap = _positive_decimal(max_usd, 'max_usd')
        if cap > 10 and cap != 20:
            raise ValueError('global cost cap must be at most USD 10 or explicitly authorized USD 20')
        if cap == 20 and not self.path.is_file():
            raise ValueError('USD 20 cannot initialize a ledger without explicit authorization')
        self.input_rate = _positive_decimal(input_usd_per_million, 'input rate')
        self.output_rate = _positive_decimal(output_usd_per_million, 'output rate')
        self.cache_rate = _positive_decimal(cache_hit_usd_per_million, 'cache hit rate')
        if self.cache_rate > self.input_rate:
            raise ValueError('cache hit price cannot exceed the uncached input price')
        if not _integer(max_planner_calls, positive=True) or (cap != 20 and max_planner_calls > 6000):
            raise ValueError('max_planner_calls above 6000 requires explicit USD 20 authorization')
        self.policy = {
            'version': 1, 'max_nanos': _nanos(cap),
            'input_rate': str(self.input_rate.normalize()),
            'output_rate': str(self.output_rate.normalize()),
            'cache_rate': str(self.cache_rate.normalize()),
            'max_planner_calls': max_planner_calls,
        }
        self._initial_policy_json = _json(self.policy)
        if cap == 20:
            # Reopening an authorized ledger must never initialize or repair it.
            with _ledger_connection(self.path, existing=True) as connection:
                connection.execute('BEGIN IMMEDIATE')
                if _validated_policy(connection) != self.policy:
                    raise ValueError('global budget policy is immutable across processes and cohorts')
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('CREATE TABLE IF NOT EXISTS budget_policy (id INTEGER PRIMARY KEY CHECK(id=1), policy_json TEXT NOT NULL)')
            connection.execute('''CREATE TABLE IF NOT EXISTS reservations (
                reservation_id TEXT PRIMARY KEY, cohort TEXT NOT NULL,
                request_hash TEXT NOT NULL, created_at REAL NOT NULL,
                reserved_nanos INTEGER NOT NULL CHECK(reserved_nanos>=0),
                charged_nanos INTEGER NOT NULL CHECK(charged_nanos>=0),
                input_tokens_bound INTEGER NOT NULL, max_tokens INTEGER NOT NULL,
                max_http_calls INTEGER NOT NULL, status TEXT NOT NULL,
                final_json TEXT, finalized_at REAL)''')
            connection.execute('''CREATE TABLE IF NOT EXISTS accounting_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, reservation_id TEXT NOT NULL,
                event TEXT NOT NULL, created_at REAL NOT NULL, details_json TEXT NOT NULL)''')
            stored = connection.execute('SELECT policy_json FROM budget_policy WHERE id=1').fetchone()
            if stored is None:
                connection.execute('INSERT INTO budget_policy VALUES (1,?)', (_json(self.policy),))
            elif _validated_policy(connection) != self.policy:
                raise ValueError('global budget policy is immutable across processes and cohorts')
            connection.commit()

    @contextmanager
    def _connection(self):
        with _ledger_connection(self.path) as connection:
            yield connection

    def reserve(self, messages: list[dict[str, str]], *, max_tokens: int,
                response_format: dict[str, Any], cohort: str,
                max_http_calls: int = 2) -> dict[str, Any]:
        if not _integer(max_tokens, positive=True) or max_tokens > 4096:
            raise ValueError('max_tokens must be an integer from 1 to 4096')
        if not _integer(max_http_calls, positive=True) or max_http_calls != 2:
            raise ValueError('exactly two compatibility HTTP bounds must be reserved')
        if not isinstance(cohort, str) or not cohort:
            raise ValueError('cohort is required')
        message_bytes = _json(messages).encode('utf-8')
        format_bytes = _json(response_format).encode('utf-8')
        input_bound = len(message_bytes) + len(format_bytes) + 8192
        identifier = uuid.uuid4().hex
        request_hash = hashlib.sha256(message_bytes + b'\0' + format_bytes).hexdigest()
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            policy = _validated_policy(connection, expected_policy=json.loads(self._initial_policy_json))
            bound = _nanos(Decimal(max_http_calls) * (
                input_bound * Decimal(policy['input_rate']) + max_tokens * Decimal(policy['output_rate'])
            ) / 1_000_000)
            result = {'reservation_id': identifier, 'cohort': cohort,
                      'request_hash': request_hash, 'reserved_usd': bound / _NANO_USD,
                      'input_tokens_bound': input_bound, 'max_tokens': max_tokens,
                      'max_http_calls': max_http_calls}
            totals = connection.execute('SELECT COUNT(*),COALESCE(SUM(charged_nanos),0) FROM reservations').fetchone()
            if totals[0] >= policy['max_planner_calls']:
                raise RuntimeError('global planner call cap reached before provider request')
            if totals[1] + bound > policy['max_nanos']:
                raise RuntimeError('global cost cap reached before provider request')
            now = time.time()
            connection.execute(
                'INSERT INTO reservations VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL)',
                (identifier, cohort, request_hash, now, bound, bound, input_bound,
                 max_tokens, max_http_calls, 'pending'),
            )
            connection.execute('INSERT INTO accounting_events (reservation_id,event,created_at,details_json) VALUES (?,?,?,?)',
                               (identifier, 'reserved', now, _json(result)))
            connection.commit()
        return result

    def _actual_cost(self, usage: dict[str, Any], row: sqlite3.Row,
                     policy: dict[str, Any]) -> int | None:
        if not _integer(usage.get('planner_http_calls'), positive=True) or usage['planner_http_calls'] != 1:
            return None
        if usage.get('planner_compatibility_retry', False) is not False:
            return None
        prompt, completion = usage.get('prompt_tokens'), usage.get('completion_tokens')
        if not _integer(prompt) or not _integer(completion):
            return None
        if prompt > row['input_tokens_bound'] or completion > row['max_tokens']:
            return None
        if 'total_tokens' in usage and (not _integer(usage['total_tokens']) or usage['total_tokens'] != prompt + completion):
            return None
        hit, miss = 0, prompt
        if 'prompt_cache_hit_tokens' in usage or 'prompt_cache_miss_tokens' in usage:
            hit, miss = usage.get('prompt_cache_hit_tokens'), usage.get('prompt_cache_miss_tokens')
            if not _integer(hit) or not _integer(miss) or hit + miss != prompt:
                return None
        details = usage.get('prompt_tokens_details')
        if details is not None:
            if not isinstance(details, dict):
                return None
            if 'cached_tokens' in details:
                cached = details['cached_tokens']
                if not _integer(cached) or cached > prompt:
                    return None
                if 'prompt_cache_hit_tokens' in usage and cached != hit:
                    return None
                hit, miss = cached, prompt - cached
        actual = _nanos((hit * Decimal(policy['cache_rate']) + miss * Decimal(policy['input_rate']) +
                         completion * Decimal(policy['output_rate'])) / 1_000_000)
        return actual if actual <= row['reserved_nanos'] else None

    def _finalize(self, reservation_id: str, *, usage: Any, response_hash: str | None,
                  latency_ms: int | None, error_code: str | None) -> dict[str, Any]:
        facts = {'usage': _safe_usage(usage), 'response_hash': response_hash,
                 'latency_ms': latency_ms, 'error_code': error_code}
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            policy = _validated_policy(connection, expected_policy=json.loads(self._initial_policy_json))
            row = connection.execute('SELECT * FROM reservations WHERE reservation_id=?', (reservation_id,)).fetchone()
            if row is None:
                raise ValueError('unknown reservation')
            if row['final_json'] is not None:
                existing = json.loads(row['final_json'])
                if any(existing.get(key) != value for key, value in facts.items()):
                    raise ValueError('reservation already finalized with different evidence')
                return existing
            actual = self._actual_cost(usage, row, policy) if isinstance(usage, dict) and response_hash and error_code is None else None
            status = 'settled' if actual is not None else 'retained'
            charged = actual if actual is not None else row['reserved_nanos']
            result = {**facts, 'reservation_id': reservation_id, 'cohort': row['cohort'],
                      'request_hash': row['request_hash'], 'status': status,
                      'reserved_usd': row['reserved_nanos'] / _NANO_USD,
                      'charged_usd': charged / _NANO_USD,
                      'http_calls': facts['usage'].get('planner_http_calls'),
                      'max_http_calls': row['max_http_calls']}
            now, document = time.time(), _json(result)
            connection.execute('UPDATE reservations SET status=?,charged_nanos=?,final_json=?,finalized_at=? WHERE reservation_id=?',
                               (status, charged, document, now, reservation_id))
            connection.execute('INSERT INTO accounting_events (reservation_id,event,created_at,details_json) VALUES (?,?,?,?)',
                               (reservation_id, status, now, document))
            connection.commit()
        return result

    def settle(self, reservation_id: str, *, usage: dict[str, Any],
               response_hash: str, latency_ms: int) -> dict[str, Any]:
        if not isinstance(response_hash, str) or len(response_hash) != 64 or any(char not in '0123456789abcdef' for char in response_hash):
            raise ValueError('response_hash must be a SHA256 hex digest')
        if not _integer(latency_ms):
            raise ValueError('latency_ms must be a nonnegative integer')
        return self._finalize(reservation_id, usage=usage, response_hash=response_hash,
                              latency_ms=latency_ms, error_code=None)

    def retain(self, reservation_id: str, *, error_code: str) -> dict[str, Any]:
        # Codes only: exception messages may contain arbitrary provider data.
        if not isinstance(error_code, str) or not error_code or any(char not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789' for char in error_code):
            error_code = 'PROVIDER_CALL_FAILED'
        return self._finalize(reservation_id, usage={}, response_hash=None,
                              latency_ms=None, error_code=error_code)

    def summary(self) -> dict[str, Any]:
        return budget_summary(self.path)

    def responses(self, cohort: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return [json.loads(row[0]) for row in connection.execute(
                'SELECT final_json FROM reservations WHERE cohort=? AND final_json IS NOT NULL ORDER BY created_at,reservation_id',
                (cohort,),
            ) if json.loads(row[0]).get('response_hash')]


def main() -> None:
    parser = argparse.ArgumentParser(description='Read the shared real-provider budget without network access.')
    parser.add_argument('--db', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(budget_summary(args.db), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
