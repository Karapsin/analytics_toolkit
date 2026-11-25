"""Query replay certifies native destination storage before DDL and after CREATE ACK."""

from __future__ import annotations

import pytest
from analytics_toolkit.sql.backends.calendar_ticks import days_from_civil
from analytics_toolkit.sql.core.exact_values import ExactTemporal

from tests.sql.dml.table.create_from_sql.generic_adaptive import create
from tests.sql.dml.table.create_from_sql.generic_adaptive import install as _install_fixture

install = _install_fixture
del _install_fixture


def _target(monkeypatch, install, *, before="memory", after=None):
    wide = ExactTemporal(days_from_civil(10000, 1, 1) * 86400 * 10**9 + 1, 9, wall_time=True)
    state = install("trino", rows=((None,), (wide,)), native_type="TIMESTAMP(12)")
    state.metadata = []

    class Cursor:
        def execute(self, statement, params=None):
            state.metadata.append((statement, list(params or [])))
            if "system.metadata.catalogs" in statement:
                assert params == ["target_catalog"]
                self.rows = [((after if "create" in state.events else before) or before,)]
            elif "SHOW CREATE TABLE" in statement:
                self.rows = [
                    (
                        "CREATE TABLE target_catalog.sandbox.events (id TIMESTAMP(9)) "
                        "WITH (format_version = 3, format = 'PARQUET')",
                    )
                ]
            else:
                message = f"Unexpected storage metadata: {statement}"
                raise AssertionError(message)

        def fetchall(self):
            return self.rows

        def close(self):
            pass

    class Connection:
        catalog = "target_catalog"
        schema = "sandbox"

        def cursor(self):
            return Cursor()

        def close(self):
            pass

        def rollback(self):
            pass

    connection = Connection()
    monkeypatch.setattr(create, "get_sql_connection", lambda _key: connection)
    return state, wide


def _run():
    return create.create_table_from_sql(
        "trino",
        "target_catalog.sandbox.events",
        "SELECT id FROM sandbox.source",
        table_schema={"id": "TIMESTAMP(9)"},
        drop_target_if_exists=True,
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
    )


def test_public_query_creation_rejects_wide_iceberg_timestamp_before_drop_or_create(
    monkeypatch,
    install,
):
    state, _wide = _target(monkeypatch, install, before="iceberg")

    try:
        with pytest.raises(ValueError, match=r"(?i)timestamp|nanosecond|storage"):
            _run()
    finally:
        assert "drop" not in state.events
        assert "create" not in state.events
        assert state.inserts == []

    assert state.reads == 1
    assert state.metadata


def test_public_query_replay_rechecks_actual_storage_after_create_ack_before_inserting(
    monkeypatch,
    install,
):
    state, _wide = _target(monkeypatch, install, before="memory", after="iceberg")

    try:
        with pytest.raises(ValueError, match=r"(?i)timestamp|nanosecond|storage"):
            _run()
    finally:
        assert state.events.index("certified") < state.events.index("create")
        assert state.inserts == []

    assert any("SHOW CREATE TABLE" in statement for statement, _params in state.metadata)
    assert state.reads == 1


def test_public_query_creation_keeps_exact_wide_values_in_verified_memory_storage(
    monkeypatch,
    install,
):
    state, wide = _target(monkeypatch, install)

    assert _run().rows == 2

    assert state.inserts == [[(None,), (wide,)]]
    assert state.events.index("certified") < state.events.index("create")
    assert state.reads == 1
    assert state.metadata
