"""Wrapped layout discovery must retain native column-identity ownership checks."""

from __future__ import annotations

from copy import deepcopy

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.layout import (
    _inspect_existing_layout_local,
    inspect_existing_layout,
)
from analytics_toolkit.sql.backends.ch.lifecycle import build_drop_ch_creation_policy_table_sqls
from analytics_toolkit.sql.backends.ch.policy_drop import execute_policy_target_drop
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.backends.ch.routing import local_sql
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_drop_scope import install_drop_scope

_TARGET = "analytics.events"
_PHYSICAL = "analytics.events_shard"


@pytest.fixture(params=["type", "default"])
def mismatched_pair(monkeypatch, write_sql_connections, request):
    state = install_drop_scope(monkeypatch, write_sql_connections)
    if request.param == "type":
        columns = "(id String)"
        rows = [(f"foreign-{index}",) for index in range(100)]
    else:
        columns = "(id UInt64 DEFAULT 7)"
        rows = [(index,) for index in range(100)]
    physical = parse_create_table(
        f"CREATE TABLE {_PHYSICAL} {columns} ENGINE=MergeTree ORDER BY id", _PHYSICAL
    )
    state.host_tables[state.remote].pop(_PHYSICAL)
    state._create(state.remote, physical, rows)
    before = state.snapshot()
    connection = sql.get_sql_connection("ch")
    try:
        yield state, connection, before
    finally:
        connection.close()
        assert state.opened
        assert all(client.closed and client.close_calls == 1 for client in state.opened)


def _resources(statements):
    resources = []
    for statement in statements:
        parsed = parse_one(strip_query_label(statement), read=ClickHouseDialect)
        assert isinstance(parsed, exp.Drop)
        scope = parsed.find(exp.OnCluster)
        resources.extend(
            (f"{table.db}.{table.name}", scope.this.name if scope is not None else None)
            for table in parsed.args.get("tables") or [parsed.this]
        )
    return resources


def test_outage_layout_rejects_companion_ownership_when_column_identity_differs(mismatched_pair):
    state, connection, before = mismatched_pair
    with local_sql(connection):
        local_layout = _inspect_existing_layout_local(connection, _TARGET)
    wrapped_layout = inspect_existing_layout(connection, _TARGET)

    assert local_layout.create_distributed_pair is True
    assert local_layout.owns_physical is False
    assert local_layout.physical_table == wrapped_layout.physical_table
    assert local_layout.physical_ddl == wrapped_layout.physical_ddl
    assert state.snapshot() == before
    assert state.commands == []
    assert wrapped_layout.owns_physical is False


def test_policy_cleanup_preserves_schema_mismatched_same_named_physical_storage(mismatched_pair):
    state, connection, before = mismatched_pair
    layout = inspect_existing_layout(connection, _TARGET)
    statements = build_drop_ch_creation_policy_table_sqls(_TARGET, layout)

    execute_policy_target_drop(get_backend_adapter("ch"), connection, statements, target_key="ch")

    expected = deepcopy(before)
    expected[state.coordinator].pop(_TARGET)
    assert state.snapshot() == expected, repr(
        {"commands": state.commands, "after": state.snapshot()}
    )
    assert state.snapshot()[state.remote][_PHYSICAL] == before[state.remote][_PHYSICAL]
    assert len(state.rows(state.remote, _PHYSICAL)) == 100
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert _resources(statements) == [(_TARGET, None)]
    assert _resources(state.commands) == [(_TARGET, None)]
    assert any(settings == {"skip_unavailable_shards": 0} for settings in state.query_settings)
