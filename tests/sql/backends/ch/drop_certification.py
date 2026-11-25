"""Native catalog and endpoint faults cannot authorize a destructive drop."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from analytics_toolkit import sql
from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.drop_execution import (
    DropExecutionOptions,
    execute_verified_drop,
)
from analytics_toolkit.sql.backends.ch.drop_layout import (
    DropIntent,
    certify_existing_drop,
    certify_explicit_drop,
)
from analytics_toolkit.sql.backends.ch.outage_context import internal_io
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.connection.errors import (
    ClickHouseClusterUnavailableError,
    InvalidSqlInputError,
)
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_drop_scope import install_drop_scope

_TARGET = "analytics.events"
_PHYSICAL = "analytics.events_shard"


@pytest.fixture
def drop_context(monkeypatch, write_sql_connections, request):
    state = install_drop_scope(monkeypatch, write_sql_connections)
    if getattr(request, "param", None) == "facades":
        state.clusters["frontends"] = (state.coordinator, state.remote)
        state._create(state.remote, state.host_tables[state.coordinator][_TARGET].create)
        path = Path(".connections")
        settings = json.loads(path.read_text(encoding="utf-8"))
        settings["ch"]["cluster_routing"] = {"cluster": "frontends"}
        path.write_text(json.dumps(settings), encoding="utf-8")
        set_connections_path(path)
    # Raw driver metadata remains observable without an earlier wrapper rejecting
    # the same malformed response before the certification boundary under test.
    with internal_io():
        connection = sql.get_sql_connection("ch")
    try:
        yield state, connection
    finally:
        connection.close()
        assert state.opened
        assert all(client.closed and client.close_calls == 1 for client in state.opened)


def _query(statement):
    return parse_one(strip_query_label(statement), read=ClickHouseDialect)


def _fields(query):
    return tuple(item.name.casefold() for item in query.expressions)


def _requested_table(query):
    values = {
        item.this.name: item.expression.this
        for item in query.find_all(exp.EQ)
        if isinstance(item.this, exp.Column) and isinstance(item.expression, exp.Literal)
    }
    return f"{values.get('database')}.{values.get('name')}"


def _catalog_fields(query, fields, table):
    return (
        isinstance(query, exp.Select)
        and _fields(query) == fields
        and _requested_table(query) == table
    )


def _replace_rows(state, predicate, replacement):
    observed = []

    def after_query(_client, statement, result):
        query = _query(statement)
        if predicate(query):
            observed.append(statement)
            result.result_rows[:] = replacement(deepcopy(result.result_rows))

    state.after_query = after_query
    return observed


def _execute(connection, certificate, *, if_exists=True):
    execute_verified_drop(
        get_backend_adapter("ch"),
        connection,
        certificate,
        DropExecutionOptions(if_exists=if_exists),
    )


def _drop(connection, *, if_exists=True):
    certificate = certify_existing_drop(
        connection, _TARGET, DropIntent("ch", "storage", if_exists=if_exists)
    )
    _execute(connection, certificate, if_exists=if_exists)


def _reject_without_mutation(state, operation, error=InvalidSqlInputError):
    before = state.snapshot()
    with pytest.raises(error):
        operation()
    assert state.snapshot() == before
    assert state.commands == []
    assert all(commands == [] for commands in state.host_commands.values())


@pytest.mark.parametrize("fault", ["width", "invalid-uuid", "unknown-position", "duplicate"])
def test_drop_certification_rejects_inconsistent_native_table_uuid_rows(drop_context, fault):
    state, connection = drop_context

    def replacement(rows):
        assert len(rows) == 1
        position, identity = rows[0]
        if fault == "width":
            return [(position, identity, "unexpected")]
        if fault == "invalid-uuid":
            return [(position, "not-a-uuid")]
        if fault == "unknown-position":
            return [(position + 1, identity)]
        return [rows[0], rows[0]]

    observed = _replace_rows(
        state,
        lambda query: _catalog_fields(query, ("_shard_num", "uuid"), _PHYSICAL),
        replacement,
    )
    _reject_without_mutation(state, lambda: _drop(connection))
    assert observed


@pytest.mark.parametrize("rows", [[], [()], [(None,)], [(123,)], [("",)], [("x",), ("y",)]])
def test_drop_certification_rejects_missing_or_malformed_local_definitions(drop_context, rows):
    state, connection = drop_context
    observed = _replace_rows(
        state,
        lambda query: _catalog_fields(query, ("create_table_query",), _TARGET),
        lambda _rows: rows,
    )
    _reject_without_mutation(state, lambda: _drop(connection))
    assert observed


@pytest.mark.parametrize("rows", [[], [("not-a-server-uuid", 9000)], [(uuid4(),)]])
def test_drop_certification_rejects_incomplete_coordinator_identity(drop_context, rows):
    state, connection = drop_context
    observed = _replace_rows(
        state,
        lambda query: (
            isinstance(query, exp.Select)
            and _fields(query) == ("serveruuid", "tcpport")
            and query.args.get("from_") is None
        ),
        lambda _rows: rows,
    )
    _reject_without_mutation(state, lambda: _drop(connection))
    assert observed


@pytest.mark.parametrize("change", ["coordinator", "remote-instance", "membership"])
def test_drop_certification_rechecks_genuine_endpoint_evidence_before_ddl(drop_context, change):
    state, connection = drop_context
    certificate = certify_existing_drop(connection, _TARGET, DropIntent("ch", "storage"))
    assert {resource.role for resource in certificate.resources} == {"facade", "physical"}
    if change == "membership":
        state.clusters["storage"] = (state.coordinator, state.remote)
    else:
        host = state.coordinator if change == "coordinator" else state.remote
        _identity, port = state.server_identities[host]
        state.server_identities[host] = (uuid4(), port)
    _reject_without_mutation(state, lambda: _execute(connection, certificate))


def test_drop_certification_does_not_swallow_native_catalog_permission_failure(drop_context):
    state, connection = drop_context
    attempts = []

    def before_query(_client, statement):
        query = _query(statement)
        if _catalog_fields(query, ("_shard_num", "uuid"), _PHYSICAL):
            attempts.append(statement)
            message = "Catalog permission denied for selected physical resource"
            raise PermissionError(message)

    state.before_query = before_query
    _reject_without_mutation(state, lambda: _drop(connection), PermissionError)
    assert attempts


def test_drop_certification_requires_uuid_and_ddl_occupancy_to_agree(drop_context):
    state, connection = drop_context
    state.clusters["storage"] = (state.coordinator, state.remote)
    observed = _replace_rows(
        state,
        lambda query: _catalog_fields(query, ("_shard_num", "uuid"), _PHYSICAL),
        lambda rows: rows[1:],
    )
    _reject_without_mutation(state, lambda: certify_explicit_drop(connection, _PHYSICAL, "storage"))
    assert observed


def test_drop_certification_rejects_conflicting_physical_definitions(drop_context):
    state, connection = drop_context
    state.clusters["storage"] = (state.coordinator, state.remote)
    state.host_tables[state.coordinator].pop(_PHYSICAL)
    changed = parse_create_table(
        f"CREATE TABLE {_PHYSICAL} (id String) ENGINE=MergeTree ORDER BY id", _PHYSICAL
    )
    state._create(state.coordinator, changed, [("foreign",)])
    _reject_without_mutation(state, lambda: certify_explicit_drop(connection, _PHYSICAL, "storage"))


def test_drop_certification_strict_raw_cluster_drop_requires_every_table(drop_context):
    state, connection = drop_context
    state.clusters["storage"] = (state.coordinator, state.remote)
    state.host_tables[state.coordinator].pop(_PHYSICAL)
    _reject_without_mutation(
        state,
        lambda: certify_explicit_drop(connection, _PHYSICAL, "storage", if_exists=False),
        ClickHouseClusterUnavailableError,
    )


@pytest.mark.parametrize("if_exists", [True, False])
def test_drop_certification_missing_referenced_storage_keeps_actual_absence_semantics(
    drop_context, if_exists
):
    state, connection = drop_context
    state.host_tables[state.remote].pop(_PHYSICAL)
    if not if_exists:
        _reject_without_mutation(state, lambda: _drop(connection, if_exists=False))
        return
    before = state.snapshot()
    certificate = certify_existing_drop(connection, _TARGET, DropIntent("ch", "storage"))
    assert [(resource.role, resource.cluster) for resource in certificate.resources] == [
        ("facade", None)
    ]
    _execute(connection, certificate)
    expected = deepcopy(before)
    expected[state.coordinator].pop(_TARGET)
    assert state.snapshot() == expected


def test_drop_certification_rejects_unverifiable_distributed_catalog_relationship(drop_context):
    state, connection = drop_context
    observed = _replace_rows(
        state,
        lambda query: _catalog_fields(query, ("create_table_query",), _TARGET),
        lambda _rows: [(f"CREATE TABLE {_TARGET} (id UInt64) ENGINE=Distributed('storage')",)],
    )
    _reject_without_mutation(state, lambda: _drop(connection))
    assert observed


@pytest.mark.parametrize("columns", ["id String", "id UInt64 DEFAULT 7"])
def test_drop_certification_requires_matching_declared_companion_columns(drop_context, columns):
    state, connection = drop_context
    state.host_tables[state.remote].pop(_PHYSICAL)
    physical = parse_create_table(
        f"CREATE TABLE {_PHYSICAL} ({columns}) ENGINE=MergeTree ORDER BY id", _PHYSICAL
    )
    rows = [(f"foreign-{index}",) if columns == "id String" else (index,) for index in range(100)]
    state._create(state.remote, physical, rows)
    _reject_without_mutation(state, lambda: _drop(connection))
    assert len(state.rows(state.remote, _PHYSICAL)) == 100


@pytest.mark.parametrize("drop_context", ["facades"], indirect=True)
def test_drop_certification_checks_each_facades_actual_native_routing(drop_context):
    state, connection = drop_context

    def replacement(rows):
        changed = []
        for position, cluster, name, address, port in rows:
            endpoint = (name, address, port)
            if position == 2 and cluster == "storage":
                endpoint = state.host_endpoints[state.coordinator]
            changed.append((position, cluster, *endpoint))
        return changed

    observed = _replace_rows(
        state,
        lambda query: (
            isinstance(query, exp.Select)
            and _fields(query) == ("_shard_num", "cluster", "host_name", "host_address", "port")
        ),
        replacement,
    )
    _reject_without_mutation(state, lambda: _drop(connection))
    assert observed
