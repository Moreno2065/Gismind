import sqlite3

import pytest

from app.runtime import RuntimeDB
from app.runtime import db as db_module


@pytest.mark.parametrize("entry", ["connection", "transaction", "memory"])
@pytest.mark.parametrize("failure", [sqlite3.OperationalError, KeyboardInterrupt])
def test_connection_setup_failure_closes_the_acquired_database(tmp_path, monkeypatch, entry, failure):
    opened = []
    original_connect = sqlite3.connect

    class SetupFailureConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == "PRAGMA foreign_keys=ON":
                raise failure("injected connection setup failure")
            return super().execute(sql, *args, **kwargs)

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs, factory=SetupFailureConnection)
        opened.append(connection)
        return connection

    monkeypatch.setattr(db_module.sqlite3, "connect", connect)
    try:
        with pytest.raises(failure, match="injected connection setup failure"):
            if entry == "memory":
                RuntimeDB(":memory:")
            else:
                runtime_db = RuntimeDB(tmp_path / "runtime.db")
                with getattr(runtime_db, entry)():
                    pytest.fail("setup failure must not yield a connection")
        assert len(opened) == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            opened[0].execute("SELECT 1")
    finally:
        for connection in opened:
            connection.close()
