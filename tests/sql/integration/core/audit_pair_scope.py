"""Real two-host pair-to-local ownership and destination scope checks."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING

import analytics_toolkit.general as general_module
import pytest
import sqlparse
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.outage_statements import exchange_or_rename
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp, parse_one

from tests.sql.integration._support.reconfiguration import (
    cleanup_after_acknowledgement,
    create_owned,
    create_storage,
    inventory,
    label,
    literal,
    quoted,
    resource_name,
    seed_storage,
    stored_rows,
)

if TYPE_CHECKING:
    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]

_CORE = "integration_cluster"
_ARCHIVE = "integration_archive_cluster"
_FULL = "integration_two_shards"
_DATABASE = "integration"


@dataclass(frozen=True)
class _SourcePair:
    relation: str
    shard: str
    source_rows: list[tuple[int, str]]
    shadow_rows: list[tuple[int, str]]
    foreign_rows: list[tuple[int, str]]
    before: dict[str, list[tuple[str, ...]]]


@dataclass(frozen=True)
class _RoutedPair:
    relation: str
    before: dict[str, list[tuple[str, ...]]]


def _source_pair(
    registry: ResourceRegistry, purpose: str, *, occupied_destination: bool
) -> _SourcePair:
    relation = resource_name(purpose)
    shard = relation + "_shard"
    create_storage(registry, _DATABASE, shard, _ARCHIVE)
    create_storage(registry, _DATABASE, shard, _CORE)
    table, facade_scope = quoted(_DATABASE, relation), quoted(_CORE)
    engine = f"Distributed({literal(_ARCHIVE)}, {literal(_DATABASE)}, {literal(shard)}, rand())"
    create_owned(
        registry,
        f"CREATE TABLE {table} ON CLUSTER {facade_scope} "
        f"(id UInt64, payload String) ENGINE={engine}",
        f"DROP TABLE IF EXISTS {table} ON CLUSTER {facade_scope} SYNC",
    )
    source_rows = seed_storage(_ARCHIVE, _DATABASE, shard, rows=3)
    shadow_rows = seed_storage(_CORE, _DATABASE, shard, rows=100, offset=2000)
    foreign_rows = []
    if occupied_destination:
        create_storage(registry, _DATABASE, relation, _ARCHIVE)
        foreign_rows = seed_storage(_ARCHIVE, _DATABASE, relation, rows=100, offset=1000)
    before = {cluster: inventory(cluster, [_DATABASE], relation) for cluster in (_CORE, _ARCHIVE)}
    assert stored_rows(_CORE, _DATABASE, relation) == source_rows
    return _SourcePair(relation, shard, source_rows, shadow_rows, foreign_rows, before)


def _desired_alias(
    integration_connections,
    write_sql_connections,
    facade_scope: str,
    *,
    physical_scope: str | None = _ARCHIVE,
    memory_staging: bool = False,
) -> str:
    # These are only the disposable stack's generated loopback connections.
    # Keep its selected transport/authentication and add isolated DDL defaults.
    connections = deepcopy(integration_connections)
    alias = resource_name("pair_scope_alias")
    connection = deepcopy(connections["ch"])
    connection["ddl_defaults"]["regular"] = {
        "create_distributed_pair": False,
        "shard": {"engine": "MergeTree", "on_cluster": physical_scope},
        "distributed": {
            "engine_template": "Distributed({cluster}, {database}, {shard_table}, {sharding_key})",
            "cluster": _ARCHIVE,
            "on_cluster": facade_scope,
            "sharding_key": "rand()",
        },
    }
    if memory_staging:
        connection["ddl_defaults"]["staging"] = {
            "create_distributed_pair": False,
            "shard": {"engine": "Memory", "on_cluster": None},
        }
    connections[alias] = connection
    general_module.set_connections_path(write_sql_connections(connections))
    return alias


def _convert_to_defaults(alias: str, relation: str):
    return sql.ch_reconfigure_table(
        alias,
        quoted(_DATABASE, relation),
        to_defaults=True,
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
        query_label=label("pair-to-local-defaults-scope"),
    )


def _assert_source_unchanged(source: _SourcePair) -> None:
    assert {
        cluster: inventory(cluster, [_DATABASE], source.relation) for cluster in (_CORE, _ARCHIVE)
    } == source.before
    assert stored_rows(_CORE, _DATABASE, source.relation) == source.source_rows
    assert stored_rows(_ARCHIVE, _DATABASE, source.shard) == source.source_rows
    assert stored_rows(_CORE, _DATABASE, source.shard) == source.shadow_rows
    assert stored_rows(_ARCHIVE, _DATABASE, source.relation) == source.foreign_rows


@pytest.mark.sql_scenario("audit.clickhouse.pair_to_local_wrong_facade_scope")
def test_pair_to_local_defaults_reject_a_scope_that_excludes_the_source_facade(
    resource_registry: ResourceRegistry, integration_connections, write_sql_connections
) -> None:
    source = _source_pair(resource_registry, "pair_wrong_scope", occupied_destination=True)
    alias = _desired_alias(integration_connections, write_sql_connections, _ARCHIVE)

    with pytest.raises(InvalidSqlInputError, match="does not contain the verified coordinator"):
        _convert_to_defaults(alias, source.relation)

    _assert_source_unchanged(source)


@pytest.mark.sql_scenario("audit.clickhouse.pair_to_local_occupied_destination")
def test_pair_to_local_defaults_reject_foreign_physical_destination_before_source_rename(
    resource_registry: ResourceRegistry, integration_connections, write_sql_connections
) -> None:
    source = _source_pair(resource_registry, "pair_occupied_destination", occupied_destination=True)
    alias = _desired_alias(integration_connections, write_sql_connections, _CORE)

    with pytest.raises(InvalidSqlInputError, match="Physical replacement destination contains"):
        _convert_to_defaults(alias, source.relation)

    _assert_source_unchanged(source)


@pytest.mark.sql_scenario("audit.clickhouse.pair_to_local_preserves_unrelated_storage")
def test_pair_to_local_defaults_preserve_the_unrelated_coordinator_suffix_table(
    resource_registry: ResourceRegistry, integration_connections, write_sql_connections
) -> None:
    source = _source_pair(resource_registry, "pair_keep_shadow", occupied_destination=False)
    alias = _desired_alias(integration_connections, write_sql_connections, _CORE)
    assert {row[1] for row in source.before[_ARCHIVE]} == {source.shard}
    acknowledge_destination = cleanup_after_acknowledgement(
        resource_registry,
        f"DROP TABLE IF EXISTS {quoted(_DATABASE, source.relation)} "
        f"ON CLUSTER {quoted(_ARCHIVE)} SYNC",
    )

    result = _convert_to_defaults(alias, source.relation)

    assert isinstance(result, sql.SqlOperationResult)
    assert result.data["strategy"] == "pair_to_local"
    acknowledge_destination()
    assert result.data["row_count_validated"] is True
    assert result.data["cleanup_complete"] is True
    assert stored_rows(_ARCHIVE, _DATABASE, source.relation) == source.source_rows
    assert stored_rows(_CORE, _DATABASE, source.shard) == source.shadow_rows
    after_core = inventory(_CORE, [_DATABASE], source.relation)
    assert after_core == [row for row in source.before[_CORE] if row[1] == source.shard]
    after_archive = inventory(_ARCHIVE, [_DATABASE], source.relation)
    assert {row[1] for row in after_archive} == {source.relation}
    assert "MergeTree" in after_archive[0][3]


@pytest.mark.sql_scenario("audit.transfer.clickhouse.replacement_physical_scope")
def test_staged_transfer_rejects_a_different_physical_scope_before_replacement_ddl(
    resource_registry: ResourceRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source_pair(resource_registry, "transfer_pair_scope", occupied_destination=False)
    adapter = get_backend_adapter("ch")
    execute = adapter.execute_command
    statements = []

    def observe_command(connection, statement):
        statements.append(statement)
        return execute(connection, statement)

    monkeypatch.setattr(adapter, "execute_command", observe_command)

    with pytest.raises(InvalidSqlInputError, match="Physical placement differs"):
        sql.transfer(
            from_db="trino_values",
            to_db="ch",
            from_sql=(
                "SELECT CAST(201 AS BIGINT) AS id, CAST('incoming-201' AS VARCHAR) AS payload "
                "UNION ALL SELECT CAST(202 AS BIGINT), CAST('incoming-202' AS VARCHAR)"
            ),
            to_table=quoted(_DATABASE, source.relation),
            write_mode="replace",
            table_schema={"id": "UInt64", "payload": "String"},
            ignore_source_staging=True,
            batch_size=2,
            min_batch_size=1,
            max_batch_size=2,
            adaptive_batch_size=False,
            retry_cnt=1,
            full_retry_cnt=1,
            timeout_increment=0,
            full_timeout_increment=0,
            return_metadata=True,
            ch_distributed_table=True,
            ch_engine="MergeTree",
            ch_shard_on_cluster=_CORE,
            ch_distributed_cluster=_CORE,
            ch_distributed_on_cluster=_CORE,
            ch_sharding_key="rand()",
            order_by=["id"],
            query_label=label("staged-transfer-physical-scope"),
        )

    assert {
        cluster: inventory(cluster, [_DATABASE], source.relation) for cluster in (_CORE, _ARCHIVE)
    } == source.before
    assert stored_rows(_CORE, _DATABASE, source.relation) == source.source_rows
    assert stored_rows(_ARCHIVE, _DATABASE, source.shard) == source.source_rows
    assert stored_rows(_CORE, _DATABASE, source.shard) == source.shadow_rows
    assert all("__replace_guarded_" not in statement for statement in statements)
    for statement in statements:
        special = exchange_or_rename(statement)
        if special is not None:
            operation, _scopes = special
            assert all(
                table.name not in {source.relation, source.shard} for table in operation.expressions
            )


def _observe_target_creates(monkeypatch: pytest.MonkeyPatch) -> list[exp.Create]:
    adapter = get_backend_adapter("ch")
    execute = adapter.execute_command
    creates = []

    def observe_command(connection, statement):
        result = execute(connection, statement)
        if sqlparse.parse(statement)[0].get_type() == "CREATE":
            create = parse_one(statement, read=ClickHouseDialect)
            assert isinstance(create, exp.Create)
            if create.args.get("kind") == "TABLE":
                creates.append(create)
        return result

    monkeypatch.setattr(adapter, "execute_command", observe_command)
    return creates


def _assert_local_memory_stage(creates: list[exp.Create]) -> None:
    memory_stages = []
    unsupported = (
        exp.Order,
        exp.PartitionedByProperty,
        exp.PrimaryKey,
        exp.SampleProperty,
        exp.OnCluster,
    )
    for create in creates:
        properties = create.args.get("properties")
        assert properties is not None
        engine = next(
            prop for prop in properties.expressions if isinstance(prop, exp.EngineProperty)
        )
        if engine.this.name.casefold() == "memory":
            memory_stages.append(create)
            assert not any(isinstance(prop, unsupported) for prop in properties.expressions)
            assert create.this.find(exp.PrimaryKeyColumnConstraint) is None
    assert memory_stages


def _replace_pair_with_local(
    registry: ResourceRegistry,
    integration_connections,
    write_sql_connections,
    monkeypatch: pytest.MonkeyPatch,
    *,
    physical_scope: str | None,
) -> None:
    source = _source_pair(registry, "transfer_pair_convert", occupied_destination=False)
    target_alias = _desired_alias(
        integration_connections,
        write_sql_connections,
        _CORE,
        physical_scope=physical_scope,
        memory_staging=True,
    )
    destination_scope = _CORE if physical_scope is None else physical_scope
    acknowledge_destination = cleanup_after_acknowledgement(
        registry,
        f"DROP TABLE IF EXISTS {quoted(_DATABASE, source.relation)} "
        f"ON CLUSTER {quoted(destination_scope)} SYNC",
    )
    source_adapter = get_backend_adapter("trino")
    stream = source_adapter.iter_source_batches
    streamed_queries = []

    def observe_source(**kwargs):
        streamed_queries.append(kwargs["query"])
        yield from stream(**kwargs)

    monkeypatch.setattr(source_adapter, "iter_source_batches", observe_source)
    target_creates = _observe_target_creates(monkeypatch)
    result = sql.transfer(
        from_db="trino_values",
        to_db=target_alias,
        from_sql=(
            "SELECT CAST(201 AS BIGINT) AS id, CAST('incoming-201' AS VARCHAR) AS payload "
            "UNION ALL SELECT CAST(202 AS BIGINT), CAST('incoming-202' AS VARCHAR)"
        ),
        to_table=quoted(_DATABASE, source.relation),
        write_mode="replace",
        table_schema={"id": "UInt64", "payload": "String"},
        ignore_source_staging=True,
        batch_size=2,
        min_batch_size=1,
        max_batch_size=2,
        adaptive_batch_size=False,
        retry_cnt=1,
        full_retry_cnt=1,
        timeout_increment=0,
        full_timeout_increment=0,
        return_metadata=True,
        ch_distributed_table=False,
        ch_engine="MergeTree",
        ch_shard_on_cluster=physical_scope,
        ch_distributed_cluster=_ARCHIVE,
        ch_distributed_on_cluster=_CORE,
        ch_sharding_key="rand()",
        order_by=["id"],
        query_label=label("staged-transfer-pair-to-local"),
    )

    assert isinstance(result, sql.SqlOperationResult)
    acknowledge_destination()
    assert result.rows == 2
    assert result.data is None
    assert result.metadata.source_rows == result.metadata.staged_rows == 2
    assert result.metadata.final_target_rows == 2
    assert result.metadata.row_count_validated is True
    assert len(streamed_queries) == 1
    _assert_local_memory_stage(target_creates)
    assert stored_rows(destination_scope, _DATABASE, source.relation) == [
        (201, "incoming-201"),
        (202, "incoming-202"),
    ]
    assert stored_rows(_CORE, _DATABASE, source.shard) == source.shadow_rows
    after = {
        cluster: inventory(cluster, [_DATABASE], source.relation) for cluster in (_CORE, _ARCHIVE)
    }
    assert [row for row in after[_CORE] if row[1] == source.shard] == [
        row for row in source.before[_CORE] if row[1] == source.shard
    ]
    for cluster, rows in after.items():
        expected = {source.shard} if cluster == _CORE else set()
        if cluster == destination_scope:
            expected.add(source.relation)
        assert {row[1] for row in rows} == expected
    destination = next(row for row in after[destination_scope] if row[1] == source.relation)
    assert "MergeTree" in destination[3]
    assert "Distributed(" not in destination[3]


@pytest.mark.sql_scenario("audit.transfer.clickhouse.pair_to_remote_local")
def test_staged_transfer_finishes_pair_to_remote_local_replacement(
    resource_registry: ResourceRegistry,
    integration_connections,
    write_sql_connections,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _replace_pair_with_local(
        resource_registry,
        integration_connections,
        write_sql_connections,
        monkeypatch,
        physical_scope=_ARCHIVE,
    )


@pytest.mark.sql_scenario("audit.transfer.clickhouse.pair_to_coordinator_local")
def test_staged_transfer_finishes_pair_to_coordinator_local_replacement(
    resource_registry: ResourceRegistry,
    integration_connections,
    write_sql_connections,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _replace_pair_with_local(
        resource_registry,
        integration_connections,
        write_sql_connections,
        monkeypatch,
        physical_scope=None,
    )


def _routed_pair(registry: ResourceRegistry) -> _RoutedPair:
    relation = resource_name("routed_facade_scope")
    shard = relation + "_shard"
    physical = quoted(_DATABASE, shard)
    for cluster, count, offset in ((_ARCHIVE, 3, 11), (_CORE, 100, 1001)):
        create_owned(
            registry,
            f"CREATE TABLE {physical} ON CLUSTER {quoted(cluster)} "
            "(id UInt64) ENGINE=MergeTree ORDER BY id",
            f"DROP TABLE IF EXISTS {physical} ON CLUSTER {quoted(cluster)} SYNC",
        )
        sql.execute(
            "ch",
            f"INSERT INTO FUNCTION cluster({literal(cluster)}, {literal(_DATABASE)}, "
            f"{literal(shard)}, rand()) SELECT number + {offset} FROM numbers({count})",
            retry_cnt=1,
            query_label=label("routed-facade-owned-seed"),
        )
    table = quoted(_DATABASE, relation)
    create_owned(
        registry,
        f"CREATE TABLE {table} ON CLUSTER {quoted(_FULL)} (id UInt64) "
        f"ENGINE=Distributed({literal(_ARCHIVE)}, {literal(_DATABASE)}, {literal(shard)}, rand())",
        f"DROP TABLE IF EXISTS {table} ON CLUSTER {quoted(_FULL)} SYNC",
    )
    before = {cluster: inventory(cluster, [_DATABASE], relation) for cluster in (_CORE, _ARCHIVE)}
    return _RoutedPair(relation, before)


def _routed_alias(integration_connections, write_sql_connections, facade_scope: str | None) -> str:
    connections = deepcopy(integration_connections)
    alias = resource_name("routed_facade_alias")
    connection = deepcopy(connections["ch"])
    connection["cluster_routing"] = {"cluster": _FULL, "sharding_key": "rand()"}
    connection["ddl_defaults"] = {
        "regular": {
            "create_distributed_pair": True,
            "shard": {"engine": "MergeTree", "on_cluster": _ARCHIVE},
            "distributed": {
                "engine_template": (
                    "Distributed({cluster}, {database}, {shard_table}, {sharding_key})"
                ),
                "cluster": _ARCHIVE,
                "on_cluster": facade_scope,
                "sharding_key": "rand()",
            },
        },
        "staging": {
            "create_distributed_pair": False,
            "shard": {"engine": "MergeTree", "on_cluster": _FULL},
        },
    }
    connections[alias] = connection
    general_module.set_connections_path(write_sql_connections(connections))
    return alias


def _observe_target_commands(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    adapter = get_backend_adapter("ch")
    open_connection = adapter.open_connection
    statements = []

    def observe_open(*args, **kwargs):
        connection = open_connection(*args, **kwargs)
        command = connection.command

        def observe_command(statement, *command_args, **command_kwargs):
            statements.append(statement)
            return command(statement, *command_args, **command_kwargs)

        monkeypatch.setattr(connection, "command", observe_command)
        return connection

    monkeypatch.setattr(adapter, "open_connection", observe_open)
    return statements


def _routed_rows(cluster: str, relation: str) -> list[tuple[int]]:
    frame = sql.read(
        "ch",
        f"SELECT id FROM cluster({literal(cluster)}, {literal(_DATABASE)}, {literal(relation)}) "
        "ORDER BY id",
        retry_cnt=1,
        query_label=label("routed-facade-owned-rows"),
    )
    return list(frame.itertuples(index=False, name=None))


def _replace_routed_pair(alias: str, relation: str):
    return sql.transfer(
        from_db="trino_values",
        to_db=alias,
        from_sql=(
            "SELECT CAST(201 AS BIGINT) AS id, CAST('incoming-201' AS VARCHAR) AS payload "
            "UNION ALL SELECT CAST(202 AS BIGINT), CAST('incoming-202' AS VARCHAR)"
        ),
        to_table=quoted(_DATABASE, relation),
        write_mode="replace",
        table_schema={"id": "UInt64", "payload": "String"},
        ignore_source_staging=True,
        batch_size=2,
        min_batch_size=1,
        max_batch_size=2,
        adaptive_batch_size=False,
        retry_cnt=1,
        full_retry_cnt=1,
        timeout_increment=0,
        full_timeout_increment=0,
        return_metadata=True,
        order_by=["id"],
        query_label=label("staged-transfer-routed-facade-scope"),
    )


@pytest.mark.sql_scenario("audit.transfer.clickhouse.routed_facade_scope_preservation")
def test_staged_transfer_rejects_a_local_facade_over_existing_full_routed_scope(
    resource_registry: ResourceRegistry,
    integration_connections,
    write_sql_connections,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _routed_pair(resource_registry)
    alias = _routed_alias(integration_connections, write_sql_connections, None)
    statements = _observe_target_commands(monkeypatch)

    with pytest.raises(InvalidSqlInputError, match="Facade placement differs"):
        _replace_routed_pair(alias, source.relation)

    assert {
        cluster: inventory(cluster, [_DATABASE], source.relation) for cluster in (_CORE, _ARCHIVE)
    } == source.before
    for cluster in (_CORE, _ARCHIVE):
        assert _routed_rows(cluster, source.relation) == [(11,), (12,), (13,)]
    assert _routed_rows(_ARCHIVE, source.relation + "_shard") == [(11,), (12,), (13,)]
    assert _routed_rows(_CORE, source.relation + "_shard") == [(i,) for i in range(1001, 1101)]
    assert all("__replace_guarded_" not in statement for statement in statements)
    for statement in statements:
        special = exchange_or_rename(statement)
        if special is not None:
            operation, _scopes = special
            assert all(
                table.name not in {source.relation, source.relation + "_shard"}
                for table in operation.expressions
            )


def _assert_routed_insert_commands_cleaned(statements: list[str], relation: str) -> None:
    column_lists = []
    private_tables = set()
    for statement in statements:
        kind = sqlparse.parse(statement)[0].get_type()
        if kind == "CREATE":
            create = parse_one(statement, read=ClickHouseDialect)
            if isinstance(create, exp.Create) and create.args.get("kind") == "TABLE":
                table = create.this.this if isinstance(create.this, exp.Schema) else create.this
                if table.name not in {relation, relation + "_shard"}:
                    private_tables.add((table.db, table.name))
            continue
        if kind != "INSERT":
            continue
        insert = parse_one(statement, read=ClickHouseDialect)
        target = insert.this
        if (
            isinstance(target, exp.Schema)
            and isinstance(target.this, exp.Anonymous)
            and target.this.name.casefold() in {"cluster", "clusterallreplicas"}
        ):
            column_lists.append(tuple(column.name for column in target.expressions))
    assert ("id", "payload") in column_lists
    assert private_tables
    for database, private_relation in private_tables:
        for cluster in (_CORE, _ARCHIVE):
            assert inventory(cluster, [database], private_relation) == []


@pytest.mark.sql_scenario("audit.transfer.clickhouse.routed_insert_function_columns")
def test_staged_transfer_replaces_every_facade_and_preserves_routed_insert_columns(
    resource_registry: ResourceRegistry,
    integration_connections,
    write_sql_connections,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _routed_pair(resource_registry)
    alias = _routed_alias(integration_connections, write_sql_connections, _FULL)
    statements = _observe_target_commands(monkeypatch)
    source_adapter = get_backend_adapter("trino")
    stream = source_adapter.iter_source_batches
    streamed_queries = []

    def observe_source(**kwargs):
        streamed_queries.append(kwargs["query"])
        yield from stream(**kwargs)

    monkeypatch.setattr(source_adapter, "iter_source_batches", observe_source)
    result = _replace_routed_pair(alias, source.relation)

    assert result.rows == 2
    assert result.data is None
    assert result.metadata.source_rows == result.metadata.staged_rows == 2
    assert result.metadata.final_target_rows == 2
    assert result.metadata.row_count_validated is True
    assert len(streamed_queries) == 1
    for cluster in (_CORE, _ARCHIVE):
        assert stored_rows(cluster, _DATABASE, source.relation) == [
            (201, "incoming-201"),
            (202, "incoming-202"),
        ]
        after = inventory(cluster, [_DATABASE], source.relation)
        assert {row[1] for row in after} == {source.relation, source.relation + "_shard"}
        previous_ids = {row[1]: row[2] for row in source.before[cluster]}
        for row in after:
            if cluster == _CORE and row[1] != source.relation:
                continue
            assert row[2] != previous_ids[row[1]]
            create = parse_one(row[3], read=ClickHouseDialect)
            assert tuple(column.name for column in create.this.expressions) == ("id", "payload")
        if cluster == _CORE:
            assert [row for row in after if row[1] != source.relation] == [
                row for row in source.before[cluster] if row[1] != source.relation
            ]
    assert _routed_rows(_CORE, source.relation + "_shard") == [(i,) for i in range(1001, 1101)]
    assert stored_rows(_ARCHIVE, _DATABASE, source.relation + "_shard") == [
        (201, "incoming-201"),
        (202, "incoming-202"),
    ]
    _assert_routed_insert_commands_cleaned(statements, source.relation)
