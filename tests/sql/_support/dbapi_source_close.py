"""Public transfer source faults at the real DBAPI cursor boundary."""

from __future__ import annotations

from analytics_toolkit.sql.backends import execution_outcome
from analytics_toolkit.sql.dml.transfer.io import source
from analytics_toolkit.sql.dml.transfer.runtime import retry

from tests.sql._support.adaptive_transfer import Connection, Scenario, attempt, install


class SourceFailureError(RuntimeError):
    analytics_toolkit_sql_retry_safe = False


SourceFailure = SourceFailureError


class SourceCursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = [("id", "bigint", None, None, None, None, None)]
        self.rows = [(7,), (8,)]
        self.close_calls = 0

    def execute(self, query):
        state = self.connection.state
        state.queries.append(query)
        if state.phase == "execute":
            raise state.failure

    def fetchmany(self, size):
        if self.connection.state.phase == "fetch":
            raise self.connection.state.failure
        selected, self.rows = self.rows[:size], self.rows[size:]
        return selected

    def close(self):
        self.close_calls += 1
        message = "private cursor-close diagnostic"
        raise OSError(message)


class SourceConnection(Connection):
    def __init__(self, state):
        self.state = state
        self.cursors = []
        self.close_calls = 0
        self.rollback_calls = 0

    def cursor(self):
        cursor = SourceCursor(self)
        self.cursors.append(cursor)
        return cursor

    def close(self):
        self.close_calls += 1

    def rollback(self):
        self.rollback_calls += 1


class SourceState:
    def __init__(self, phase):
        self.phase = phase
        self.failure = SourceFailure(f"primary {phase} failure")
        self.queries = []
        self.opened = []
        self.messages = []

    def open(self):
        connection = SourceConnection(self)
        self.opened.append(connection)
        return connection


def install_source_close(monkeypatch, phase):
    state = SourceState(phase)

    def read(scenario, key, backend, ref, query, *_args, **_kwargs):
        scenario.read_count += 1
        yield from source.iter_source_batches(key, backend, ref, query, 1, 1, 0)

    monkeypatch.setattr(Scenario, "read", read)
    scenario = install(
        monkeypatch,
        native_type="BIGINT",
        from_db_key="trino",
        from_db_backend="trino",
        to_db_key="gp",
        to_db_backend="gp",
        table_schema={"id": "BIGINT"},
        ignore_source_staging=True,
    )
    original = attempt.get_sql_connection

    def connect(alias):
        if alias == "trino":
            scenario.source_open_count += 1
            return state.open()
        return original(alias)

    monkeypatch.setattr(attempt, "get_sql_connection", connect)
    monkeypatch.setattr(retry, "get_sql_connection", connect)
    for module in (source, execution_outcome):
        monkeypatch.setattr(
            module, "time_print", lambda message, **_kwargs: state.messages.append(message)
        )
    return scenario, state
