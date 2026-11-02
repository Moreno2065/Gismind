"""Connection ownership checks using disposable SQLite files, without providers."""
from contextlib import closing
import sqlite3

import pytest

from tests.browser import runtime_browser_budget as budget_module
from tests.browser import runtime_live_browser as browser_module


class TrackingConnection(sqlite3.Connection):
    failure_statement = None
    fail_rollback = False
    close_calls = 0

    def execute(self, statement, parameters=()):
        if statement == self.failure_statement:
            raise sqlite3.OperationalError('injected setup failure')
        return super().execute(statement, parameters)

    def rollback(self):
        if self.fail_rollback:
            raise sqlite3.OperationalError('injected rollback failure')
        return super().rollback()

    def close(self):
        self.close_calls += 1
        return super().close()


@pytest.fixture
def track_connections(monkeypatch):
    original_connect = sqlite3.connect
    connections = []

    def install(path, *, failure_statement=None, fail_rollback=False):
        allowed_targets = {str(path), path.as_uri() + '?mode=ro'}

        def connect(database, *args, **kwargs):
            assert str(database) in allowed_targets, 'only this test fixture may be opened'
            connection = original_connect(database, *args, factory=TrackingConnection, **kwargs)
            connection.failure_statement = failure_statement
            connection.fail_rollback = fail_rollback
            connections.append(connection)
            return connection

        monkeypatch.setattr(sqlite3, 'connect', connect)
        return connections

    yield install
    for connection in connections:
        # Even a failed assertion must release its disposable database handle.
        sqlite3.Connection.close(connection)


def assert_closed(connections):
    assert connections, 'the helper must have opened a real SQLite connection'
    for connection in connections:
        assert connection.close_calls == 1
        with pytest.raises(sqlite3.ProgrammingError, match='closed database'):
            sqlite3.Connection.execute(connection, 'SELECT 1')


@pytest.mark.parametrize('statement', ['PRAGMA busy_timeout=30000', 'PRAGMA synchronous=FULL'])
def test_budget_connection_closes_when_setup_fails(tmp_path, track_connections, statement):
    path = tmp_path / 'setup.sqlite'
    connections = track_connections(path, failure_statement=statement)
    with pytest.raises(sqlite3.OperationalError, match='injected setup failure'):
        with budget_module._ledger_connection(path):
            pytest.fail('failed setup must not yield a connection')
    assert_closed(connections)


def test_budget_connection_closes_when_rollback_fails(tmp_path, track_connections):
    path = tmp_path / 'rollback.sqlite'
    connections = track_connections(path, fail_rollback=True)
    with pytest.raises(sqlite3.OperationalError, match='injected rollback failure'):
        with budget_module._ledger_connection(path) as connection:
            connection.execute('BEGIN')
    assert_closed(connections)


@pytest.mark.parametrize('body_error', [False, True])
def test_budget_connection_closes_and_preserves_transaction_outcome(tmp_path, track_connections, body_error):
    path = tmp_path / 'transaction.sqlite'
    connections = track_connections(path)

    def use_connection():
        with budget_module._ledger_connection(path) as connection:
            connection.execute('CREATE TABLE evidence (value INTEGER NOT NULL)')
            connection.execute('BEGIN')
            connection.execute('INSERT INTO evidence VALUES (7)')
            if body_error:
                raise LookupError('body failed')
            connection.commit()

    if body_error:
        with pytest.raises(LookupError, match='body failed'):
            use_connection()
    else:
        use_connection()
    assert_closed(connections)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute('SELECT value FROM evidence').fetchall() == ([] if body_error else [(7,)])
    assert_closed(connections)


@pytest.mark.parametrize('query_error', [False, True])
def test_sql_rows_closes_connection_after_query(tmp_path, track_connections, query_error):
    path = tmp_path / 'reader.sqlite'
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute('CREATE TABLE evidence (value INTEGER NOT NULL)')
        connection.execute('INSERT INTO evidence VALUES (7)')
    connections = track_connections(path)
    if query_error:
        with pytest.raises(sqlite3.OperationalError, match='no such table'):
            browser_module.sql_rows(path, 'SELECT value FROM missing_evidence')
    else:
        assert browser_module.sql_rows(path, 'SELECT value FROM evidence WHERE value=?', (7,)) == [{'value': 7}]
    assert_closed(connections)
