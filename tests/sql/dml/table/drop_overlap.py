"""Overlapping fully authorized drop scopes retain idempotent table-list behavior."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_drop_scope import install_drop_scope

_TARGET = "analytics.events"
_PHYSICAL = "analytics.events_shard"
_SENTINEL = "analytics.foreign_sentinel"


def _complete_pair_and_sentinels(state):
    facade = state.host_tables[state.coordinator][_TARGET].create.copy()
    state._create(state.remote, facade)
    sentinel = parse_create_table(
        "CREATE TABLE analytics.foreign_sentinel (id UInt64) ENGINE=MergeTree ORDER BY id",
        _SENTINEL,
    )
    state._create(state.coordinator, sentinel.copy(), state.foreign_rows)
    state._create(state.remote, sentinel.copy(), [(99,)])


def _native_drop_counts(statements):
    resources = []
    for statement in statements:
        drop = parse_one(strip_query_label(statement), read=ClickHouseDialect)
        assert isinstance(drop, exp.Drop)
        resources.extend(
            f"{table.db}.{table.name}" for table in drop.args.get("tables") or [drop.this]
        )
    return Counter(resources)


@pytest.mark.parametrize(
    "tables",
    [[_TARGET, _PHYSICAL], [_PHYSICAL, _TARGET]],
    ids=["facade-first", "physical-first"],
)
def test_public_drop_executes_the_verified_union_of_overlapping_requested_scopes(
    monkeypatch, write_sql_connections, tables
):
    state = install_drop_scope(
        monkeypatch,
        write_sql_connections,
        cluster_routing={"cluster": "core", "sharding_key": "rand()"},
    )
    _complete_pair_and_sentinels(state)
    before = state.snapshot()
    expected = deepcopy(before)
    for host in (state.coordinator, state.remote):
        expected[host].pop(_TARGET)
        expected[host].pop(_PHYSICAL)
    assert state.rows(state.coordinator, _TARGET) == [(11,), (12,), (13,)]
    assert state.rows(state.remote, _TARGET) == [(11,), (12,), (13,)]
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows

    result = sql.drop_tables(
        "ch",
        tables,
        ch_cluster="storage",
        if_exists=True,
        ch_wait_for_absence=True,
        ch_wait_timeout_seconds=1,
        ch_wait_poll_interval_seconds=0.001,
        ch_retry_per_host_drops=True,
    )

    assert result is None
    assert state.snapshot() == expected
    selected_counts = Counter({_TARGET: 1, _PHYSICAL: 1})
    for host in (state.coordinator, state.remote):
        assert state.snapshot()[host][_SENTINEL] == before[host][_SENTINEL]
        assert _native_drop_counts(state.host_commands[host]) == selected_counts
    assert state.rows(state.coordinator, _SENTINEL) == state.foreign_rows
    assert state.rows(state.remote, _SENTINEL) == [(99,)]
    assert state.opened
    assert all(connection.closed and connection.close_calls == 1 for connection in state.opened)


@pytest.mark.parametrize("external_change", ["disappear", "recreate"])
def test_public_drop_overlap_never_adopts_an_unacknowledged_external_change(
    monkeypatch, write_sql_connections, external_change
):
    state = install_drop_scope(
        monkeypatch,
        write_sql_connections,
        driver="native",
        cluster_routing={"cluster": "core", "sharding_key": "rand()"},
    )
    _complete_pair_and_sentinels(state)
    before = state.snapshot()
    event = {}

    def concurrent_ddl(_connection, _statement):
        # The first full facade DROP and B's physical DROP have returned ACKs.
        # A's explicitly requested physical instance has not been submitted.
        if event or len(state.commands) != 2:
            return
        removed = state.host_tables[state.coordinator].pop(_PHYSICAL)
        if external_change == "recreate":
            state._create(state.coordinator, removed.create.copy(), list(removed.rows))
        event["snapshot"] = state.snapshot()

    state.before_query = concurrent_ddl

    with pytest.raises(InvalidSqlInputError, match="changed after drop ownership"):
        sql.drop_tables(
            "ch",
            [_TARGET, _PHYSICAL],
            ch_cluster="storage",
            if_exists=True,
            ch_wait_for_absence=True,
            ch_wait_timeout_seconds=1,
            ch_wait_poll_interval_seconds=0.001,
        )

    assert event
    assert state.snapshot() == event["snapshot"]
    assert _native_drop_counts(state.commands) == Counter({_TARGET: 1, _PHYSICAL: 1})
    assert _PHYSICAL not in state.host_tables[state.remote]
    assert all(_TARGET not in state.host_tables[host] for host in state.host_tables)
    if external_change == "recreate":
        old = before[state.coordinator][_PHYSICAL]
        replacement = state.snapshot()[state.coordinator][_PHYSICAL]
        assert replacement["uuid"] != old["uuid"]
        assert replacement["ddl"] == old["ddl"]
        assert replacement["rows"] == old["rows"]
    else:
        assert _PHYSICAL not in state.host_tables[state.coordinator]
    for host in state.host_tables:
        assert state.snapshot()[host][_SENTINEL] == before[host][_SENTINEL]
    assert all(connection.closed and connection.close_calls == 1 for connection in state.opened)
