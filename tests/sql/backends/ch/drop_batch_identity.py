"""Batch identity comes from complete native certificates, not rendered SQL."""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.drop_layout import (
    certify_explicit_drop,
    verified_drop_resource_identity,
)
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_drop_scope import install_drop_scope


def _table(name):
    return exp.Table(
        this=exp.to_identifier(name, quoted=True),
        db=exp.to_identifier("analytics", quoted=True),
    ).sql(dialect=ClickHouseDialect)


def _create(state, name, *, host=None):
    table = _table(name)
    definition = parse_create_table(
        f"CREATE TABLE {table} (id UInt64) ENGINE=MergeTree ORDER BY id", table
    )
    state._create(state.coordinator if host is None else host, definition, [(7,), (8,)])
    return table


def _assert_read_only(state, before):
    assert state.snapshot() == before
    assert state.commands == []
    assert all(commands == [] for commands in state.host_commands.values())
    assert state.opened
    assert all(connection.closed and connection.close_calls == 1 for connection in state.opened)


def test_local_and_proven_singleton_coordinator_certificates_identify_the_same_resource(
    monkeypatch, write_sql_connections
):
    state = install_drop_scope(monkeypatch, write_sql_connections)
    table = _create(state, "local_identity")
    before = state.snapshot()

    with closing(sql.get_sql_connection("ch")) as connection:
        local = certify_explicit_drop(connection, table, None, if_exists=False).resources[0]
        named = certify_explicit_drop(
            connection, table, "coordinator_only", if_exists=False
        ).resources[0]

    assert local.scope is None
    assert named.scope is not None
    assert named.scope.coordinator == 1
    assert len(named.scope.endpoints) == 1
    assert local.local_identity == named.scope.identities[1]
    assert local.table_identities == named.table_identities
    assert verified_drop_resource_identity(local) == verified_drop_resource_identity(named)
    _assert_read_only(state, before)


def test_public_local_only_drop_accepts_a_verified_single_coordinator_route(
    monkeypatch, write_sql_connections
):
    state = install_drop_scope(
        monkeypatch,
        write_sql_connections,
        cluster_routing={"cluster": "coordinator_only"},
    )
    table = _create(state, "coordinator_local")
    before = state.snapshot()
    expected = deepcopy(before)
    expected[state.coordinator].pop("analytics.coordinator_local")

    result = sql.drop_tables("ch", table, ch_cluster=None, if_exists=True, ch_wait_for_absence=True)

    assert result is None
    assert state.snapshot() == expected
    assert len(state.commands) == 1
    dropped = parse_one(strip_query_label(state.commands[0]), read=ClickHouseDialect)
    assert isinstance(dropped, exp.Drop)
    assert dropped.find(exp.OnCluster) is None
    assert state.host_commands[state.remote] == []
    assert state.rows(state.coordinator, "analytics.events_shard") == state.foreign_rows
    assert state.opened
    assert all(connection.closed and connection.close_calls == 1 for connection in state.opened)


def test_equivalent_full_two_host_scopes_share_native_identity_without_cluster_spelling(
    monkeypatch, write_sql_connections
):
    state = install_drop_scope(monkeypatch, write_sql_connections)
    state.clusters["core_alias"] = state.clusters["core"]
    table = _create(state, "complete_identity")
    _create(state, "complete_identity", host=state.remote)
    before = state.snapshot()

    with closing(sql.get_sql_connection("ch")) as connection:
        original = certify_explicit_drop(connection, table, "core", if_exists=False).resources[0]
        alias = certify_explicit_drop(connection, table, "core_alias", if_exists=False).resources[0]

    assert original.cluster != alias.cluster
    assert original.scope is not None
    assert alias.scope is not None
    assert len(original.scope.endpoints) == len(original.table_identities) == 2
    assert original.scope.identities == alias.scope.identities
    assert original.table_identities == alias.table_identities
    assert verified_drop_resource_identity(original) == verified_drop_resource_identity(alias)
    _assert_read_only(state, before)


@pytest.mark.parametrize(
    "names", [("ExactCase", "exactcase"), ("space", " space ")], ids=["case", "whitespace"]
)
def test_public_batch_preserves_distinct_exact_identifier_components(
    monkeypatch, write_sql_connections, names
):
    state = install_drop_scope(monkeypatch, write_sql_connections)
    tables = [_create(state, name) for name in names]
    before = state.snapshot()
    expected = deepcopy(before)
    for name in names:
        expected[state.coordinator].pop("analytics." + name)

    result = sql.drop_tables("ch", tables, ch_cluster=None, if_exists=True)

    assert result is None
    assert state.snapshot() == expected
    assert state.rows(state.coordinator, "analytics.events_shard") == state.foreign_rows
    assert len(state.commands) == 2
    dropped = []
    for statement in state.commands:
        drop = parse_one(strip_query_label(statement), read=ClickHouseDialect)
        assert isinstance(drop, exp.Drop)
        assert drop.find(exp.OnCluster) is None
        dropped.extend(table.name for table in drop.args.get("tables") or [drop.this])
    assert dropped == list(names)
    assert all(connection.closed and connection.close_calls == 1 for connection in state.opened)


def test_an_absent_endpoint_remains_part_of_the_full_scope_certificate(
    monkeypatch, write_sql_connections
):
    state = install_drop_scope(monkeypatch, write_sql_connections)
    table = _create(state, "partial_identity", host=state.remote)
    before = state.snapshot()

    with closing(sql.get_sql_connection("ch")) as connection:
        remote = certify_explicit_drop(connection, table, "storage").resources[0]
        full = certify_explicit_drop(connection, table, "core").resources[0]

    assert remote.scope is not None
    assert full.scope is not None
    assert len(remote.scope.endpoints) == 1
    assert len(full.scope.endpoints) == 2
    assert remote.table_identities[0][0] == 1
    assert full.table_identities[0][0] == 2
    assert remote.table_identities[0][1] == full.table_identities[0][1]
    assert verified_drop_resource_identity(remote) != verified_drop_resource_identity(full)
    _assert_read_only(state, before)


def _is_local_uuid_read(statement, name):
    tree = parse_one(strip_query_label(statement), read=ClickHouseDialect)
    if not isinstance(tree, exp.Select) or [field.name for field in tree.expressions] != ["uuid"]:
        return False
    source = tree.args.get("from_")
    if source is None or source.this.db != "system" or source.this.name != "tables":
        return False
    return any(
        isinstance(condition.this, exp.Column)
        and condition.this.name == "name"
        and isinstance(condition.expression, exp.Literal)
        and condition.expression.this == name
        for condition in tree.find_all(exp.EQ)
    )


@pytest.mark.parametrize("change", ["uuid", "ddl"])
def test_changed_capture_between_aliases_rejects_the_entire_batch_before_ddl(
    monkeypatch, write_sql_connections, change
):
    state = install_drop_scope(monkeypatch, write_sql_connections)
    table = _create(state, "changed_identity")
    before = state.snapshot()
    identity_reads = []
    changed = []

    def alter_before_second_capture(_connection, statement):
        if not _is_local_uuid_read(statement, "changed_identity"):
            return
        identity_reads.append(statement)
        if len(identity_reads) != 2:
            return
        current = state.host_tables[state.coordinator]["analytics.changed_identity"]
        if change == "uuid":
            state.host_tables[state.coordinator].pop("analytics.changed_identity")
            state._create(state.coordinator, current.create.copy(), deepcopy(current.rows))
        else:
            current.create = parse_create_table(
                f"CREATE TABLE {table} (id UInt64) ENGINE=MergeTree ORDER BY tuple()", table
            )
        changed.append(state.snapshot())

    state.before_query = alter_before_second_capture

    with pytest.raises(InvalidSqlInputError, match="changed after drop ownership was verified"):
        sql.drop_tables(
            "ch", ["analytics.changed_identity", table], ch_cluster=None, if_exists=True
        )

    assert len(changed) == 1
    after = state.snapshot()
    assert after == changed[0]
    previous = before[state.coordinator]["analytics.changed_identity"]
    current = after[state.coordinator]["analytics.changed_identity"]
    assert current[change] != previous[change]
    other = "ddl" if change == "uuid" else "uuid"
    assert current[other] == previous[other]
    assert current["rows"] == previous["rows"] == [(7,), (8,)]
    assert after[state.remote] == before[state.remote]
    assert state.rows(state.coordinator, "analytics.events_shard") == state.foreign_rows
    assert state.commands == []
    assert all(commands == [] for commands in state.host_commands.values())
    assert state.opened
    assert all(connection.closed and connection.close_calls == 1 for connection in state.opened)
