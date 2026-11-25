"""Deadline and cancellation boundaries retain acknowledged drop ownership."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch import drop_execution
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.execution.cancellation import (
    AsyncSqlCancelled,
    SqlCancellationScope,
    activate_cancellation_scope,
)
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_drop_scope import install_drop_scope

_TARGET = "analytics.events"
_PHYSICAL = "analytics.events_shard"


def _case(monkeypatch, write_sql_connections):
    state = install_drop_scope(monkeypatch, write_sql_connections, driver="native")
    state._create(
        state.remote,
        parse_create_table(
            f"CREATE TABLE {_TARGET} (id UInt64) ENGINE=MergeTree ORDER BY id", _TARGET
        ),
        state.foreign_rows,
    )
    state.delay_cluster_drops.add(("storage", _PHYSICAL))
    expected = deepcopy(state.snapshot())
    expected[state.coordinator].pop(_TARGET)
    return state, expected


def _drop():
    return sql.drop_tables(
        "ch",
        _TARGET,
        ch_cluster="storage",
        ch_wait_for_absence=True,
        ch_wait_timeout_seconds=1,
        ch_wait_poll_interval_seconds=0.001,
        ch_retry_per_host_drops=False,
    )


def _physical_identity_query(statement):
    tree = parse_one(strip_query_label(statement), read=ClickHouseDialect)
    return (
        isinstance(tree, exp.Select)
        and [item.name for item in tree.expressions] == ["_shard_num", "uuid"]
        and any(
            isinstance(item.this, exp.Column)
            and item.this.name == "name"
            and isinstance(item.expression, exp.Literal)
            and item.expression.this == "events_shard"
            for item in tree.find_all(exp.EQ)
        )
    )


def _assert_retained(state, expected):
    assert state.snapshot() == expected
    assert len(state.commands) == 2
    assert state.delayed_cluster_drops == [("storage", _PHYSICAL)]
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert state.rows(state.remote, _TARGET) == state.foreign_rows
    assert len(state.foreign_rows) == 100
    assert state.opened
    assert all(connection.closed for connection in state.opened)
    assert all(connection.close_calls == 1 for connection in state.opened)


def test_deadline_crossing_between_checks_never_waits_with_a_negative_timeout(
    monkeypatch, write_sql_connections
):
    state, expected = _case(monkeypatch, write_sql_connections)
    readings = []
    ticks = iter((0.0, 0.0, 0.5, 1.25))

    def monotonic():
        value = next(ticks, 2.0)
        readings.append(value)
        return value

    monkeypatch.setattr(drop_execution, "time", SimpleNamespace(monotonic=monotonic))

    with pytest.raises(TimeoutError, match=r"remains visible.*after 1 second"):
        _drop()

    assert readings == [0.0, 0.0, 0.5, 1.25, 2.0]
    _assert_retained(state, expected)


def test_cancellation_after_native_absence_probe_stops_before_recovery(
    monkeypatch, write_sql_connections
):
    state, expected = _case(monkeypatch, write_sql_connections)
    scope = SqlCancellationScope()
    cancelled = []

    def cancel_after_probe(_connection, statement, result):
        if len(state.commands) == 2 and not cancelled and _physical_identity_query(statement):
            assert len(result.result_rows) == 1
            cancelled.append(len(state.opened_configs))
            scope.request_cancel()

    state.after_query = cancel_after_probe

    with activate_cancellation_scope(scope), pytest.raises(AsyncSqlCancelled):
        _drop()

    assert cancelled == [len(state.opened_configs)]
    assert scope.aliases == ("ch",)
    _assert_retained(state, expected)


@pytest.mark.parametrize(
    "identity", ["invalid-native-uuid", "00000000-0000-0000-0000-000000000000"]
)
def test_malformed_native_absence_identity_after_ack_is_never_certified_absent(
    monkeypatch, write_sql_connections, identity
):
    state, expected = _case(monkeypatch, write_sql_connections)
    corrupted = []

    def corrupt_absence_identity(_connection, statement, result):
        if len(state.commands) == 2 and not corrupted and _physical_identity_query(statement):
            assert len(result.result_rows) == 1
            result.result_rows = [(1, identity)]
            corrupted.append(len(state.opened_configs))

    state.after_query = corrupt_absence_identity

    with pytest.raises(InvalidSqlInputError, match="stable ClickHouse table UUID ownership"):
        _drop()

    assert corrupted == [len(state.opened_configs)]
    _assert_retained(state, expected)
