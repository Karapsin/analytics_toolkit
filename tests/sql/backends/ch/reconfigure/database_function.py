"""A managed facade may identify its storage database with an existing SQL alias."""

from __future__ import annotations

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.metadata import (
    extract_clickhouse_distributed_shard_table,
    normalize_clickhouse_distributed_database_arg,
)
from analytics_toolkit.sql.backends.ch.outage_client import OutageClient
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import engine_sql, parse_create_table
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp

from tests.sql._support.reconfigure_pair_scope import (
    PairScopeClient,
    configure_pair_scope,
    connect_pair_scope,
)


def _database_argument(raw, argument):
    for host in (raw.coordinator, raw.remote):
        facade = raw.host_tables[host][raw.source_table]
        facade.create = facade.create.copy()
        engine_property = facade.create.find(exp.EngineProperty)
        assert engine_property is not None
        engine = engine_property.this
        assert isinstance(engine, exp.Anonymous)
        assert engine.name.casefold() == "distributed"
        arguments = list(engine.expressions)
        arguments[1] = argument.copy()
        engine.set("expressions", arguments)


def _mutate_facade(*, dry_run):
    return sql.ch_reconfigure_table(
        "ch",
        "analytics.events",
        ch_sharding_key="id + 1",
        ch_distributed_on_cluster="core",
        retry_cnt=1,
        timeout_increment=0,
        dry_run=dry_run,
        return_metadata=True,
    )


@pytest.mark.parametrize("function", ["currentDatabase", "database"])
@pytest.mark.parametrize("dry_run", [True, False], ids=["plan", "execute"])
def test_database_function_facade_can_reconfigure_its_verified_managed_relationship(
    monkeypatch, write_sql_connections, function, dry_run
):
    configure_pair_scope(write_sql_connections, "core")
    raw = PairScopeClient(managed_remote_facade=True)
    _database_argument(raw, exp.Anonymous(this=function))
    original = raw.snapshot()
    physical = raw.host_tables[raw.remote]["analytics.events_shard"]
    physical_identity = (physical.uuid, physical.create.sql(dialect=ClickHouseDialect))
    connect_pair_scope(monkeypatch, raw)

    result = _mutate_facade(dry_run=dry_run)

    assert len(raw.wrappers) == 1
    assert isinstance(raw.wrappers[0], OutageClient)
    if dry_run:
        assert result.options["strategy"] == "wrapper_recreate"
        assert raw.commands == []
        assert raw.snapshot() == original
    else:
        assert result.data["strategy"] == "wrapper_recreate"
        assert result.data["cleanup_complete"] is True
        assert result.metadata.source_rows == result.metadata.final_target_rows == 3
        assert result.metadata.row_count_validated is True
        assert all(
            set(raw.host_tables[host]) == set(original[host])
            for host in (raw.coordinator, raw.remote)
        )
    retained = raw.host_tables[raw.remote]["analytics.events_shard"]
    assert retained is physical
    assert (retained.uuid, retained.create.sql(dialect=ClickHouseDialect)) == physical_identity
    assert raw.rows(raw.remote, "analytics.events_shard") == [(1,), (2,), (3,)]
    assert raw.rows(raw.coordinator, raw.source_table) == [(1,), (2,), (3,)]
    assert raw.rows(raw.remote, raw.source_table) == [(1,), (2,), (3,)]
    assert raw.closed


@pytest.mark.parametrize("database", ["currentDatabase()", "database()"])
def test_database_function_quoted_literal_is_an_independent_storage_namespace(
    monkeypatch, write_sql_connections, database
):
    configure_pair_scope(write_sql_connections, "core")
    raw = PairScopeClient(managed_remote_facade=True)
    physical = raw.host_tables[raw.remote]["analytics.events_shard"]
    foreign_create = physical.create.copy()
    foreign_create.this.this.set("db", exp.to_identifier(database, quoted=True))
    foreign_relation = foreign_create.this.this
    foreign_table = f"{database}.events_shard"
    foreign = type(physical)(
        create=foreign_create,
        engine=physical.engine,
        schema=dict(physical.schema),
        route=None,
        rows=[(91,), (92,)],
    )
    raw.host_tables[raw.remote][foreign_table] = foreign
    _database_argument(raw, exp.Literal.string(database))
    for host in (raw.coordinator, raw.remote):
        raw.host_tables[host][raw.source_table].route = ("storage", foreign_table)
    original = raw.snapshot()
    connect_pair_scope(monkeypatch, raw)

    with pytest.raises(InvalidSqlInputError, match="not a managed Distributed/_shard pair"):
        _mutate_facade(dry_run=True)

    assert isinstance(raw.wrappers[0], OutageClient)
    assert raw.commands == []
    assert raw.snapshot() == original
    assert foreign.uuid != physical.uuid
    assert foreign.create.this.this.db == database
    assert foreign.create.this.this.args["db"].args["quoted"] is True
    assert raw.rows(raw.remote, foreign_relation) == [(91,), (92,)]
    assert raw.rows(raw.remote, "analytics.events_shard") == [(1,), (2,), (3,)]
    assert raw.closed


@pytest.mark.parametrize("argument", ["1", "'another_database'"])
def test_database_function_with_arguments_cannot_certify_a_storage_namespace(argument):
    expression = f"database({argument})"
    ddl = (
        "CREATE TABLE analytics.events (id UInt64) "
        f"ENGINE=Distributed('storage', {expression}, 'events_shard', rand())"
    )
    parsed = parse_create_table(ddl, "analytics.events")

    assert normalize_clickhouse_distributed_database_arg(expression, "analytics") == expression
    assert extract_clickhouse_distributed_shard_table(engine_sql(parsed), "analytics") is None
