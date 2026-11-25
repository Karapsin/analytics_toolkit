"""Engine-valid storage clauses through public creation and legacy renderers."""

from __future__ import annotations

import importlib
from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.backends.ch.create_table_as import build_ch_create_table_as_sqls
from analytics_toolkit.sql.backends.ch.creation_policy import (
    ClickHouseCreationPolicy,
    build_policy_create_as_sqls,
)
from analytics_toolkit.sql.backends.ch.ddl import (
    _build_ch_create_table_sqls,
    build_ch_distributed_create_table_sqls,
    build_ch_local_create_table_sql,
)
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.execution.plans import SqlPlan
from sqlglot import exp, parse, parse_one

from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.assertions import strip_query_label
from tests.sql._support.policy_ctas import Client

_TARGET = "analytics.events"
_QUERY = "SELECT CAST(1 AS Int64) AS id"
_BUILDERS = ("local", "distributed", "legacy_ctas", "legacy_dispatch", "policy_ctas")


@pytest.fixture
def storage_client(monkeypatch, write_sql_connections):
    def configure(engine, *, exists=False, offline=False):
        connections = deepcopy(DEFAULT_SQL_CONNECTIONS)
        scope = {"create_distributed_pair": False, "shard": {"engine": engine, "on_cluster": None}}
        connections["ch"]["ddl_defaults"] = {"regular": scope, "staging": deepcopy(scope)}
        set_connections_path(write_sql_connections(connections))
        client = Client(exists=exists)
        opened = []

        def connect(key):
            if offline:
                pytest.fail("Offline creation opened a database connection")
            opened.append(key)
            return client

        for name in (
            "analytics_toolkit.sql.ddl.api",
            "analytics_toolkit.sql.dml.table.create_table_from_sql",
            "analytics_toolkit.sql.backends.ch.create_table_as",
        ):
            monkeypatch.setattr(importlib.import_module(name), "get_sql_connection", connect)
        return client, opened

    return configure


def _schema_source(source):
    if source == "schema":
        return {"table_schema": {"id": "Int64"}}
    if source == "dataframe":
        return {"df": pd.DataFrame({"id": [1]})}
    return {"sql": _QUERY}


def _public_create(source, mode, **options):
    return sql.create_table(
        "ch",
        _TARGET,
        **_schema_source(source),
        **({mode: True} if mode != "execute" else {}),
        ch_ddl_wait_policy="wait_none",
        retry_cnt=1,
        timeout_increment=0,
        **options,
    )


def _created_tables(statements):
    return [
        tree
        for statement in statements
        if isinstance(
            tree := parse_one(strip_query_label(statement), read=ClickHouseDialect), exp.Create
        )
    ]


def _assert_storage_clauses(statements, engine, *, explicit=False):
    creates = _created_tables(statements)
    assert creates
    physical = []
    for create in creates:
        properties = create.args["properties"].expressions
        native_engine = create.find(exp.EngineProperty).this.name
        order = next((item for item in properties if isinstance(item, exp.Order)), None)
        partition = next(
            (item for item in properties if isinstance(item, exp.PartitionedByProperty)), None
        )
        if native_engine == "Distributed":
            assert order is None
            assert partition is None
            continue
        physical.append(create)
        assert native_engine == engine
        if engine == "Memory":
            assert order is None
            assert partition is None
        else:
            assert order is not None
            expected = "ORDER BY id" if explicit else "ORDER BY tuple()"
            assert (
                order.sql(dialect=ClickHouseDialect).replace("`", "").replace('"', "") == expected
            )
            assert (partition is not None) is explicit
    assert physical


@pytest.mark.parametrize("engine", ["Memory", "MergeTree"])
@pytest.mark.parametrize("source", ["schema", "dataframe"])
@pytest.mark.parametrize("mode", ["execute", "return_sql", "only_generate_sql"])
def test_public_creation_default_clauses_match_selected_engine(
    storage_client, engine, source, mode
):
    client, opened = storage_client(engine, offline=mode != "execute")
    result = _public_create(source, mode)
    if mode == "execute":
        assert result is None
        statements = client.commands
        assert opened == ["ch"]
        assert client.close_calls == 1
    elif mode == "return_sql":
        assert isinstance(result, SqlPlan)
        statements = result.sqls
    else:
        assert isinstance(result, str)
        statements = [
            tree.sql(dialect=ClickHouseDialect) for tree in parse(result, read=ClickHouseDialect)
        ]
    if mode != "execute":
        assert opened == []
        assert client.commands == []
    _assert_storage_clauses(statements, engine)


@pytest.mark.parametrize("engine", ["Memory", "MergeTree"])
def test_public_query_creation_renders_engine_valid_clauses(storage_client, engine):
    client, opened = storage_client(engine)
    assert _public_create("query", "execute") is None
    _assert_storage_clauses(client.commands, engine)
    assert opened
    assert set(opened) == {"ch"}
    assert client.close_calls == len(opened)
    assert len(client.commands) == len(_created_tables(client.commands))


@pytest.mark.parametrize("source", ["schema", "dataframe", "query"])
@pytest.mark.parametrize("mode", ["execute", "return_sql", "only_generate_sql"])
@pytest.mark.parametrize("clause", ["order_by", "partition_by"])
def test_public_memory_keys_reject_before_replacement(storage_client, source, mode, clause):
    metadata_only_query = source == "query" and mode == "only_generate_sql"
    client, opened = storage_client(
        "Memory", exists=True, offline=mode != "execute" and not metadata_only_query
    )
    client.metadata._seed()
    before = dict(client.metadata.tables)
    original_ddl = client.ddl

    with pytest.raises(ValueError, match="Memory"):
        _public_create(source, mode, drop_if_exists=True, **{clause: ["id"]})

    assert client.commands == []
    assert client.exists is True
    assert client.ddl == original_ddl
    assert client.metadata.tables == before
    assert all(client.metadata.tables[key] is value for key, value in before.items())
    assert client.close_calls == len(opened)


def _legacy_sqls(builder, engine, **keys):
    options = {"table_name": _TARGET, "joined_columns": "id Int64", "ch_engine": engine, **keys}
    if builder == "local":
        return [build_ch_local_create_table_sql(**options)]
    if builder == "distributed":
        return build_ch_distributed_create_table_sqls(**options, ch_cluster="core")
    if builder == "legacy_ctas":
        return build_ch_create_table_as_sqls(**options, query=_QUERY, ch_only_shard=True)
    if builder == "legacy_dispatch":
        return _build_ch_create_table_sqls(
            **{
                **options,
                "partition_by": keys.get("partition_by"),
                "order_by": keys.get("order_by"),
            },
            ch_cluster="core",
            ch_sharding_key="rand()",
            ch_distributed_table=False,
            ch_only_shard=True,
            ch_replace_table=False,
        )
    policy = ClickHouseCreationPolicy(
        create_distributed_pair=False,
        shard_engine=engine,
        shard_on_cluster=None,
        distributed_engine_template=(
            "Distributed({cluster}, {database}, {shard_table}, {sharding_key})"
        ),
        distributed_cluster=None,
        distributed_on_cluster=None,
        sharding_key="rand()",
        ddl_ready_timeout_seconds=1,
        ddl_wait_policy="wait_none",
    )
    statements, needs_insert = build_policy_create_as_sqls(
        table_name=_TARGET,
        source_sql=_QUERY,
        partition_by=keys.get("partition_by"),
        order_by=keys.get("order_by"),
        policy=policy,
        ch_only_shard=False,
        if_not_exists=False,
    )
    assert needs_insert is False
    return statements


@pytest.mark.parametrize("builder", _BUILDERS)
@pytest.mark.parametrize("engine", ["Memory", "MergeTree"])
def test_legacy_builders_keep_engine_defaults(builder, engine):
    _assert_storage_clauses(_legacy_sqls(builder, engine), engine)


@pytest.mark.parametrize("builder", _BUILDERS)
def test_legacy_mergetree_explicit_keys_are_preserved(builder):
    _assert_storage_clauses(
        _legacy_sqls(builder, "MergeTree", partition_by=["id"], order_by=["id"]),
        "MergeTree",
        explicit=True,
    )


@pytest.mark.parametrize("builder", _BUILDERS)
@pytest.mark.parametrize("clause", ["order_by", "partition_by"])
def test_legacy_memory_keys_reject_without_silent_removal(builder, clause):
    with pytest.raises(ValueError, match="Memory"):
        _legacy_sqls(builder, "Memory", **{clause: ["id"]})


def test_policy_memory_create_as_retains_source_order_and_literal_bytes():
    query = "SELECT 'MergeTree ORDER BY tuple()' AS id ORDER BY id"
    policy = ClickHouseCreationPolicy(
        False, "Memory", None, None, None, None, None, 1, ddl_wait_policy="wait_none"
    )
    statements, needs_insert = build_policy_create_as_sqls(
        table_name=_TARGET,
        source_sql=query,
        partition_by=None,
        order_by=None,
        policy=policy,
        ch_only_shard=False,
        if_not_exists=False,
    )
    _assert_storage_clauses(statements, "Memory")
    assert needs_insert is False
    assert statements[0].endswith("AS " + query)
    select = _created_tables(statements)[0].expression
    assert isinstance(select, exp.Select)
    assert select.args["order"] is not None
    assert select.expressions[0].this.this == "MergeTree ORDER BY tuple()"


def test_engine_argument_text_does_not_select_mergetree_storage_defaults():
    engine = "File(CSV, 'MergeTree ORDER BY literal.csv')"
    statement = build_ch_local_create_table_sql(_TARGET, "id Int64", ch_engine=engine)
    assert f"ENGINE = {engine}" in statement
    properties = _created_tables([statement])[0].args["properties"].expressions
    assert not any(isinstance(item, exp.Order) for item in properties)


def test_other_engine_explicit_keys_are_preserved_for_engine_validation():
    engine = "KeeperMap('/unit-test') PRIMARY KEY id"
    statement = build_ch_local_create_table_sql(_TARGET, "id Int64", ch_engine=engine)
    assert f"ENGINE = {engine}" in statement
    properties = _created_tables([statement])[0].args["properties"].expressions
    assert any(isinstance(item, exp.PrimaryKey) for item in properties)
    assert not any(isinstance(item, exp.Order) for item in properties)


@pytest.mark.parametrize("engine", ["Memory PRIMARY KEY id", "Memory SAMPLE BY id"])
def test_memory_keys_in_engine_expression_reject_before_create(engine):
    with pytest.raises(ValueError, match="Memory"):
        build_ch_local_create_table_sql(_TARGET, "id Int64", ch_engine=engine)


def test_malformed_engine_expression_cannot_drop_existing_target(storage_client):
    client, _opened = storage_client("Memory", exists=True)
    with pytest.raises(ValueError, match="engine expression"):
        _public_create("schema", "execute", ch_engine="Memory(", drop_if_exists=True)
    assert client.commands == []
    assert client.exists is True


@pytest.mark.parametrize("source", ["schema", "dataframe", "query"])
def test_unparsed_engine_suffix_cannot_drop_existing_target(storage_client, source):
    client, opened = storage_client("Memory", exists=True)
    client.metadata._seed()
    before = dict(client.metadata.tables)
    original_ddl = client.ddl

    with pytest.raises(ValueError, match="Cannot parse the ClickHouse engine expression") as caught:
        _public_create(
            source,
            "execute",
            ch_engine="Memory unsupported_storage_clause",
            drop_if_exists=True,
        )

    assert getattr(caught.value, "analytics_toolkit_sql_retry_safe", None) is False
    assert "unsupported_storage_clause" not in str(caught.value)
    assert client.commands == []
    assert client.exists is True
    assert client.ddl == original_ddl
    assert client.metadata.tables == before
    assert all(client.metadata.tables[key] is value for key, value in before.items())
    assert set(opened) <= {"ch"}
    assert client.close_calls == len(opened)


def test_memory_replace_request_creates_absent_target_without_cutover(storage_client):
    client, opened = storage_client("Memory", exists=False)

    assert _public_create("schema", "execute", ch_replace_table=True) is None

    _assert_storage_clauses(client.commands, "Memory")
    assert len(client.commands) == 1
    create = _created_tables(client.commands)[0]
    assert create.args["replace"] is True
    assert client.exists is True
    state = client._state(exp.to_table(_TARGET))
    assert state is not None
    assert state.find(exp.EngineProperty).this.name == "Memory"
    assert [
        (column.name, column.args["kind"].sql(dialect=ClickHouseDialect))
        for column in state.this.expressions
    ] == [("id", "Int64")]
    assert opened
    assert set(opened) == {"ch"}
    assert client.close_calls == len(opened)
