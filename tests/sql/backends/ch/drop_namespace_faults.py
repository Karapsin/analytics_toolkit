"""Drop certificates require the selected backend and an exact static namespace."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from analytics_toolkit import sql
from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.drop_layout import (
    DropIntent,
    certify_existing_drop,
    certify_explicit_drop,
    require_drop_configuration,
)
from analytics_toolkit.sql.backends.ch.reconfigure_support import (
    resolve_optional_cluster,
    resolve_required_cluster,
)
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_drop_scope import install_drop_scope

_TARGET = "analytics.events"


def _settings():
    # The autouse test fixture changes cwd to its own disposable project.
    return json.loads(Path(".connections").read_text(encoding="utf-8"))


def _select_settings(write_sql_connections, settings):
    set_connections_path(write_sql_connections(settings))


@pytest.fixture
def certified_context(monkeypatch, write_sql_connections):
    state = install_drop_scope(monkeypatch, write_sql_connections)
    connection = sql.get_sql_connection("ch")
    before = state.snapshot()
    try:
        certificate = certify_existing_drop(connection, _TARGET, DropIntent("ch", "storage"))
        assert {resource.role for resource in certificate.resources} == {"facade", "physical"}
        yield state, connection, certificate
    finally:
        connection.close()
        assert state.snapshot() == before
        assert state.commands == []
        assert all(commands == [] for commands in state.host_commands.values())
        assert state.opened
        assert all(client.closed and client.close_calls == 1 for client in state.opened)


def test_drop_namespace_faults_reject_non_clickhouse_alias_before_metadata(
    certified_context, write_sql_connections
):
    state, connection, _certificate = certified_context
    settings = _settings()
    settings["gp"] = {
        "type": "gp",
        "host": "gp.example",
        "port": 5432,
        "user": "user",
        "password": "password",
        "database": "db",
    }
    _select_settings(write_sql_connections, settings)
    before_queries = tuple(state.queries)

    with pytest.raises(InvalidSqlInputError, match="require a ClickHouse connection"):
        certify_explicit_drop(connection, _TARGET, None, connection_key="gp")

    assert tuple(state.queries) == before_queries


def test_drop_namespace_faults_reject_configuration_drift_after_genuine_certification(
    certified_context, write_sql_connections
):
    state, _connection, certificate = certified_context
    settings = _settings()
    settings["ch"]["host"] = "replacement-coordinator"
    _select_settings(write_sql_connections, settings)
    before_queries = tuple(state.queries)

    with pytest.raises(InvalidSqlInputError, match="configuration changed after drop ownership"):
        require_drop_configuration(certificate)

    assert tuple(state.queries) == before_queries


@pytest.mark.parametrize("table", ["catalog.analytics.events", "numbers(3)"])
def test_drop_namespace_faults_reject_nonstatic_or_three_part_identifiers(certified_context, table):
    state, connection, _certificate = certified_context
    before_queries = tuple(state.queries)

    with pytest.raises(InvalidSqlInputError, match="one static database/table identifier"):
        certify_explicit_drop(connection, table, None)

    assert tuple(state.queries) == before_queries


def test_drop_namespace_faults_resolve_unqualified_table_from_actual_current_database(
    certified_context, write_sql_connections
):
    state, connection, _certificate = certified_context
    settings = _settings()
    settings["ch"]["database"] = None
    _select_settings(write_sql_connections, settings)
    before_queries = len(state.queries)

    certificate = certify_explicit_drop(connection, "events", None)

    assert len(certificate.resources) == 1
    table = parse_one(certificate.resources[0].table, read=ClickHouseDialect, into=exp.Table)
    assert (table.db, table.name) == ("analytics", "events")
    assert any(
        strip_query_label(statement) == "SELECT currentDatabase()"
        for statement in state.queries[before_queries:]
    )
    assert certificate.resources[0].table_identities == (
        (1, state.host_tables[state.coordinator][_TARGET].uuid),
    )


@pytest.mark.parametrize(
    "rows",
    [[], [()], [(None,)], [("",)], [("analytics", "extra")], [("analytics",), ("analytics",)]],
    ids=["missing", "empty-row", "null", "empty-name", "extra-field", "duplicate-rows"],
)
def test_drop_namespace_faults_reject_unverifiable_current_database_metadata(
    certified_context, write_sql_connections, rows
):
    state, connection, _certificate = certified_context
    settings = _settings()
    settings["ch"]["database"] = None
    _select_settings(write_sql_connections, settings)
    observed = []

    def corrupt_current_database(_connection, statement, result):
        if strip_query_label(statement) == "SELECT currentDatabase()":
            observed.append(statement)
            result.result_rows = deepcopy(rows)

    state.after_query = corrupt_current_database

    with pytest.raises(InvalidSqlInputError, match="verify the current database"):
        certify_explicit_drop(connection, "events", None)

    assert len(observed) == 1


@pytest.mark.parametrize("kind", ["named", "explicit"])
@pytest.mark.parametrize("if_exists", [True, False])
def test_drop_namespace_faults_absent_resources_respect_if_exists(
    certified_context, kind, if_exists
):
    _state, connection, _certificate = certified_context

    def certify_missing():
        if kind == "named":
            return certify_existing_drop(
                connection, "analytics.absent", DropIntent("ch", None, if_exists=if_exists)
            )
        return certify_explicit_drop(connection, "analytics.absent", None, if_exists=if_exists)

    if if_exists:
        assert certify_missing().resources == ()
    else:
        with pytest.raises(InvalidSqlInputError, match="does not exist in the selected scope"):
            certify_missing()


@pytest.mark.parametrize(
    ("cluster", "diagnostic"),
    [
        (None, "ch_cluster must be a non-empty string"),
        ("", "ch_cluster must not be empty"),
        (" \t ", "ch_cluster must not be empty"),
        (7, "ch_cluster must be a non-empty string"),
    ],
)
def test_drop_namespace_faults_required_cluster_rejects_missing_names_before_metadata(
    certified_context, cluster, diagnostic
):
    state, connection, _certificate = certified_context
    before_queries = tuple(state.queries)

    with pytest.raises(InvalidSqlInputError, match=diagnostic):
        resolve_required_cluster(connection, cluster)

    assert tuple(state.queries) == before_queries


def test_drop_namespace_faults_optional_local_scope_needs_no_metadata(certified_context):
    state, connection, _certificate = certified_context
    before_queries = tuple(state.queries)

    assert resolve_optional_cluster(connection, None) is None

    assert tuple(state.queries) == before_queries


@pytest.mark.parametrize(
    ("cluster", "expected"),
    [("StorageMixed", "StorageMixed"), ("'StorageMixed'", "StorageMixed"), ("' \t '", " \t ")],
)
def test_drop_namespace_faults_static_cluster_names_preserve_case_without_probe(
    certified_context, cluster, expected
):
    state, connection, _certificate = certified_context
    before_queries = tuple(state.queries)

    assert resolve_required_cluster(connection, cluster) == expected
    assert resolve_optional_cluster(connection, cluster) == expected

    assert tuple(state.queries) == before_queries


@pytest.mark.parametrize("resolve", [resolve_required_cluster, resolve_optional_cluster])
def test_drop_namespace_faults_quoted_empty_cluster_cannot_become_a_local_scope(
    certified_context, resolve
):
    state, connection, _certificate = certified_context
    before_queries = tuple(state.queries)

    with pytest.raises(
        InvalidSqlInputError, match="ch_cluster must not resolve to an empty cluster name"
    ):
        resolve(connection, "''")

    assert tuple(state.queries) == before_queries


def test_drop_namespace_faults_required_macro_uses_actual_server_metadata(certified_context):
    state, connection, _certificate = certified_context
    before_queries = len(state.queries)

    assert resolve_required_cluster(connection, "{cluster}") == "storage"

    assert [strip_query_label(statement) for statement in state.queries[before_queries:]] == [
        "SELECT getMacro('cluster')"
    ]


@pytest.mark.parametrize(
    "rows",
    [[], [()], [(None,)], [(" \t ",)]],
    ids=["missing", "empty-row", "null", "empty-name"],
)
def test_drop_namespace_faults_required_macro_rejects_unusable_server_metadata(
    certified_context, rows
):
    state, connection, _certificate = certified_context
    observed = []

    def corrupt_macro(_connection, statement, result):
        if strip_query_label(statement) == "SELECT getMacro('cluster')":
            observed.append(statement)
            result.result_rows = deepcopy(rows)

    state.after_query = corrupt_macro

    with pytest.raises(ValueError, match="Could not resolve ClickHouse cluster macro"):
        resolve_required_cluster(connection, "{cluster}")

    assert len(observed) == 1


def test_drop_namespace_faults_required_macro_rejects_actual_metadata_permission_error(
    certified_context,
):
    state, connection, _certificate = certified_context
    observed = []

    def deny_macro(_connection, statement):
        if strip_query_label(statement) == "SELECT getMacro('cluster')":
            observed.append(statement)
            message = "The selected user cannot read cluster macros."
            raise PermissionError(message)

    state.before_query = deny_macro

    with pytest.raises(ValueError, match="Could not resolve ClickHouse cluster macro"):
        resolve_required_cluster(connection, "{cluster}")

    assert len(observed) == 1
