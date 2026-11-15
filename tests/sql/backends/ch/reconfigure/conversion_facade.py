"""A conversion cannot acquire remote logical tables through its facade scope."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.backends.ch.cluster_functions import cluster_arguments
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp, parse_one

from tests.sql._support.reconfigure import reconfigure_api
from tests.sql._support.reconfigure_conversion_scope import (
    ConversionScopeClient,
    configure_conversion,
    convert,
)

_ENDPOINT_COLUMNS = ("_shard_num", "serveruuid", "tcpport")
_DEFINITION_COLUMNS = ("_shard_num", "create_table_query")


def _definition_target(select):
    where = select.args.get("where")
    if where is None:
        return None
    names = [
        item.expression.this
        for item in where.find_all(exp.EQ)
        if isinstance(item.this, exp.Column)
        and item.this.name == "name"
        and isinstance(item.expression, exp.Literal)
        and item.expression.is_string
    ]
    return names[0] if len(names) == 1 else None


def _metadata_queries(client):
    queries = []
    for statement, settings in zip(client.queries, client.query_settings):
        if "clusterallreplicas" not in statement.casefold():
            continue
        select = parse_one(statement, read=ClickHouseDialect)
        if not isinstance(select, exp.Select):
            continue
        columns = tuple(item.name.casefold() for item in select.expressions)
        for table in select.find_all(exp.Table):
            function = table.this
            if (
                not isinstance(function, exp.Anonymous)
                or function.name.casefold() != "clusterallreplicas"
            ):
                continue
            cluster, database, relation, _trailing = cluster_arguments(function, None)
            if database == "system":
                queries.append((cluster, relation, columns, _definition_target(select), settings))
    return queries


def _local_identity_queries(client):
    queries = []
    for statement, settings in zip(client.queries, client.query_settings):
        if "serveruuid" not in statement.casefold():
            continue
        select = parse_one(statement, read=ClickHouseDialect)
        if (
            isinstance(select, exp.Select)
            and select.args.get("from_") is None
            and tuple(item.name.casefold() for item in select.expressions)
            == ("serveruuid", "tcpport")
        ):
            queries.append(settings)
    return queries


def _guarded_query_settings(client):
    queries = []
    for statement, settings in zip(client.queries, client.query_settings):
        if not any(name in statement.casefold() for name in ("serveruuid", "_shard_num")):
            continue
        select = parse_one(statement, read=ClickHouseDialect)
        if not isinstance(select, exp.Select):
            continue
        columns = tuple(item.name.casefold() for item in select.expressions)
        if "serveruuid" in columns or (columns and columns[0] == "_shard_num"):
            queries.append(settings)
    return queries


def _prepare(monkeypatch, write_sql_connections, client=None):
    configure_conversion(write_sql_connections)
    client = client or ConversionScopeClient()
    monkeypatch.setattr(reconfigure_api, "get_sql_connection", lambda _key: client)
    return client


@pytest.mark.parametrize("dry_run", [True, False], ids=["plan", "execute"])
def test_local_to_pair_rejects_unowned_facade_destination_before_ddl(
    monkeypatch, write_sql_connections, dry_run
):
    client = _prepare(monkeypatch, write_sql_connections)
    initial_rows = deepcopy(client.host_rows)

    def refuse_ddl(statement, **_kwargs):
        pytest.fail(f"A conflicting facade destination reached DDL: {statement}")

    monkeypatch.setattr(client, "command", refuse_ddl)
    with pytest.raises(InvalidSqlInputError, match="unowned logical table"):
        convert("core", dry_run=dry_run)

    assert client.commands == []
    assert client.host_rows == initial_rows
    assert client.closed


def test_local_to_pair_rejects_same_hostname_with_an_unowned_native_instance(
    monkeypatch, write_sql_connections
):
    client = _prepare(monkeypatch, write_sql_connections)
    client.host_endpoints[client.coordinator] = ("shared", "10.0.0.1", 9000)
    client.host_endpoints[client.remote] = ("shared", "10.0.0.1", 9001)
    identity, _port = client.server_identities[client.remote]
    client.server_identities[client.remote] = (identity, 9001)
    initial_rows = deepcopy(client.host_rows)

    with pytest.raises(InvalidSqlInputError, match="unowned logical table"):
        convert("core", dry_run=True)

    assert client.commands == []
    assert client.host_rows == initial_rows


def test_local_to_pair_allows_secure_facade_endpoint_with_a_plain_native_listener(
    monkeypatch, write_sql_connections
):
    client = _prepare(monkeypatch, write_sql_connections)
    host, address, _port = client.host_endpoints[client.coordinator]
    client.host_endpoints[client.coordinator] = (host, address, 9440)
    original = client.host_tables[client.remote][client.source_table]
    original_uuid = original.uuid
    original_ddl = original.create.sql(dialect=ClickHouseDialect)

    result = convert("coordinator_only", return_metadata=True)

    assert client.rows(client.remote, client.source_table) == client.remote_original
    assert client.host_tables[client.remote][client.source_table] is original
    assert original.uuid == original_uuid
    assert original.create.sql(dialect=ClickHouseDialect) == original_ddl
    assert sorted(client.rows(client.coordinator, client.source_table)) == [(1,), (2,), (3,)]
    assert result.metadata.final_target_rows == 3
    assert client.host_endpoints[client.coordinator][2] == 9440
    assert client.server_identities[client.coordinator][1] == 9000
    proofs = [
        (cluster, relation, columns, target, settings)
        for cluster, relation, columns, target, settings in _metadata_queries(client)
        if columns in {_ENDPOINT_COLUMNS, _DEFINITION_COLUMNS}
    ]
    assert [
        (relation, columns, target)
        for cluster, relation, columns, target, _settings in proofs
        if cluster == "coordinator_only"
    ] == [("one", _ENDPOINT_COLUMNS, None), ("tables", _DEFINITION_COLUMNS, "events")]
    assert {
        (relation, columns)
        for cluster, relation, columns, _target, _settings in proofs
        if cluster == "core"
    } >= {("one", _ENDPOINT_COLUMNS), ("tables", _DEFINITION_COLUMNS)}
    assert all(
        target == "events_shard"
        for cluster, relation, _columns, target, _settings in proofs
        if cluster == "core" and relation == "tables"
    )
    guarded = _guarded_query_settings(client)
    assert guarded
    assert all(settings == {"skip_unavailable_shards": 0} for settings in guarded)
    local_identities = _local_identity_queries(client)
    assert local_identities
    assert all(settings == {"skip_unavailable_shards": 0} for settings in local_identities)


def test_local_to_pair_cluster_facade_can_create_only_unoccupied_remote_destinations(
    monkeypatch, write_sql_connections
):
    client = _prepare(monkeypatch, write_sql_connections)
    client.host_tables[client.remote].pop(client.source_table)

    result = convert("core", return_metadata=True)

    for host in (client.coordinator, client.remote):
        assert sorted(client.rows(host, client.source_table)) == [(1,), (2,), (3,)]
    assert result.metadata.source_rows == result.metadata.final_target_rows == 3


def test_local_to_pair_local_facade_requires_only_physical_endpoint_identity(
    monkeypatch, write_sql_connections
):
    client = _prepare(monkeypatch, write_sql_connections)
    original = client.host_tables[client.remote][client.source_table]
    original_uuid = original.uuid
    original_ddl = original.create.sql(dialect=ClickHouseDialect)

    result = convert(None, return_metadata=True)

    assert client.rows(client.remote, client.source_table) == client.remote_original
    assert client.host_tables[client.remote][client.source_table] is original
    assert original.uuid == original_uuid
    assert original.create.sql(dialect=ClickHouseDialect) == original_ddl
    assert sorted(client.rows(client.coordinator, client.source_table)) == [(1,), (2,), (3,)]
    assert result.metadata.final_target_rows == 3
    metadata = _metadata_queries(client)
    assert metadata
    assert all(cluster == "core" for cluster, *_query in metadata)
    definitions = [
        (target, settings)
        for _cluster, relation, columns, target, settings in metadata
        if relation == "tables" and columns == _DEFINITION_COLUMNS
    ]
    endpoints = [
        settings
        for _cluster, relation, columns, _target, settings in metadata
        if relation == "one" and columns == _ENDPOINT_COLUMNS
    ]
    assert definitions
    assert endpoints
    assert all(target == "events_shard" for target, _settings in definitions)
    assert all(settings == {"skip_unavailable_shards": 0} for _target, settings in definitions)
    assert all(settings == {"skip_unavailable_shards": 0} for settings in endpoints)
    guarded = _guarded_query_settings(client)
    assert guarded
    assert all(settings == {"skip_unavailable_shards": 0} for settings in guarded)
    local_identities = _local_identity_queries(client)
    assert local_identities
    assert all(settings == {"skip_unavailable_shards": 0} for settings in local_identities)


@pytest.mark.parametrize("facade_scope", [None, "coordinator_only"])
def test_local_to_pair_remote_only_storage_preserves_the_remote_original_identity(
    monkeypatch, write_sql_connections, facade_scope
):
    client = _prepare(monkeypatch, write_sql_connections)
    client.clusters["core"] = (client.remote,)
    original = client.host_tables[client.remote][client.source_table]
    original_uuid = original.uuid
    original_ddl = original.create.sql(dialect="clickhouse")

    result = convert(facade_scope, return_metadata=True)

    retained = client.host_tables[client.remote][client.source_table]
    assert retained is original
    assert retained.uuid == original_uuid
    assert retained.create.sql(dialect="clickhouse") == original_ddl
    assert client.rows(client.remote, client.source_table) == client.remote_original
    assert sorted(client.rows(client.remote, "analytics.events_shard")) == [(1,), (2,), (3,)]
    assert "analytics.events_shard" not in client.host_tables[client.coordinator]
    assert sorted(client.rows(client.coordinator, client.source_table)) == [(1,), (2,), (3,)]
    assert result.metadata.source_rows == result.metadata.final_target_rows == 3


@pytest.mark.parametrize(
    "fault",
    ["missing-coordinator", "ambiguous-locality", "duplicate-server-identity", "partial-probe"],
)
def test_local_to_pair_rejects_unverified_facade_scope_before_ddl(
    monkeypatch, write_sql_connections, fault
):
    client = _prepare(monkeypatch, write_sql_connections)
    scope = "core"
    if fault == "missing-coordinator":
        scope = "remote_only"
        client.clusters[scope] = (client.remote,)
    elif fault == "ambiguous-locality":
        client.local_candidates.add(client.remote)
    elif fault == "duplicate-server-identity":
        client.server_identities[client.remote] = client.server_identities[client.coordinator]
    else:
        query = client.query

        def partial_probe(statement, settings=None):
            result = query(statement, settings=settings)
            if "serverUUID()" in statement and "clusterAllReplicas(" in statement:
                return SimpleNamespace(result_rows=result.result_rows[:1])
            return result

        monkeypatch.setattr(client, "query", partial_probe)
    initial_rows = deepcopy(client.host_rows)

    with pytest.raises(InvalidSqlInputError, match="verified coordinator endpoint"):
        convert(scope, dry_run=True)

    assert client.commands == []
    assert client.host_rows == initial_rows
