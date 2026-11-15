"""A staged replacement cannot exchange storage outside the existing target layout."""

from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.cluster_functions import cluster_arguments
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.outage_statements import exchange_or_rename
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp, parse_one

from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_transfer_replacement import ReplacementScopeState
from tests.sql._support.outage_gp import Connection, MemoryGreenplum

_SOURCE_ROWS = [(201,), (202,)]
_TARGET = "analytics.events"
_PHYSICAL = "analytics.events_shard"


def _install(monkeypatch, write_sql_connections, *, desired, pair=True, stage_engine="Memory"):
    connections = deepcopy(DEFAULT_SQL_CONNECTIONS)
    connections["ch"].update(
        driver="http",
        host="coordinator",
        database="analytics",
        transfer_staging_schema="analytics",
        ddl_defaults={
            "regular": {
                "create_distributed_pair": pair,
                "shard": {"engine": "MergeTree", "on_cluster": desired},
                "distributed": {
                    "engine_template": (
                        "Distributed({cluster}, {database}, {shard_table}, {sharding_key})"
                    ),
                    "cluster": desired if desired is not None else "storage",
                    "on_cluster": "coordinator_only",
                    "sharding_key": "rand()",
                },
            },
            "staging": {
                "create_distributed_pair": False,
                "shard": {"engine": stage_engine, "on_cluster": None},
            },
        },
    )
    set_connections_path(write_sql_connections(connections))
    source = MemoryGreenplum()
    source.tables["db.source"] = pd.DataFrame(_SOURCE_ROWS, columns=["id"])
    state = ReplacementScopeState()
    monkeypatch.setattr(
        get_backend_adapter("gp"),
        "open_connection",
        lambda *_args, **_kwargs: Connection(source),
    )
    monkeypatch.setattr(get_backend_adapter("ch"), "open_connection", state.open)
    return source, state


def _transfer(desired, *, write_mode="replace", facade_scope="coordinator_only", pair=True):
    return sql.transfer(
        from_db="gp",
        to_db="ch",
        from_sql="SELECT id FROM db.source",
        to_table=_TARGET,
        write_mode=write_mode,
        table_schema={"id": "UInt64"},
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
        ch_distributed_table=pair,
        ch_engine="MergeTree",
        ch_shard_on_cluster=desired,
        ch_distributed_cluster=desired,
        ch_distributed_on_cluster=facade_scope,
        ch_sharding_key="rand()",
        order_by=["id"],
    )


def _assert_no_replacement_ddl(state):
    for statement in state.commands:
        body = strip_query_label(statement)
        special = exchange_or_rename(body)
        if special is not None:
            operation, _scopes = special
            assert all(
                table.name not in {"events", "events_shard"} for table in operation.expressions
            )
        elif body.startswith("CREATE TABLE"):
            create = parse_one(body, read=ClickHouseDialect)
            assert isinstance(create, exp.Create)
            target = create.this.this if isinstance(create.this, exp.Schema) else create.this
            assert "__replace_guarded_" not in target.name


def _replacement_that_must_reject(state, before):
    result = _transfer("incoming")
    diagnostic = {
        "counts": (
            result.metadata.source_rows,
            result.metadata.staged_rows,
            result.metadata.final_target_rows,
        ),
        "validated": result.metadata.row_count_validated,
        "before": before,
        "after": state.snapshot(),
        "commands": state.commands,
    }
    assert state.snapshot() == before, repr(diagnostic)
    pytest.fail("Staged replacement accepted an unowned physical scope")


@pytest.mark.parametrize("shared_hostname", [False, True], ids=["different-host", "native-port"])
@pytest.mark.parametrize("stage_engine", ["Memory", "MergeTree"])
def test_public_transfer_replacement_rejects_unowned_physical_scope_before_cutover(
    monkeypatch, write_sql_connections, shared_hostname, stage_engine
):
    source, state = _install(
        monkeypatch, write_sql_connections, desired="incoming", stage_engine=stage_engine
    )
    if shared_hostname:
        state.host_endpoints[state.coordinator] = ("shared", "10.0.0.1", 9000)
        state.host_endpoints[state.remote] = ("shared", "10.0.0.1", 9001)
        identity, _port = state.server_identities[state.remote]
        state.server_identities[state.remote] = (identity, 9001)
    before = state.snapshot()

    with pytest.raises(InvalidSqlInputError, match="Physical placement differs"):
        _replacement_that_must_reject(state, before)

    assert state.snapshot() == before
    assert state.rows(state.remote, _PHYSICAL) == [(11,), (12,), (13,)]
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert state.rows(state.coordinator, _TARGET) == [(11,), (12,), (13,)]
    assert list(source.tables["db.source"].itertuples(index=False, name=None)) == _SOURCE_ROWS
    _assert_no_replacement_ddl(state)
    assert state.opened
    assert all(connection.closed for connection in state.opened)


def test_public_transfer_replacement_accepts_a_verified_native_endpoint_alias(
    monkeypatch, write_sql_connections
):
    source, state = _install(monkeypatch, write_sql_connections, desired="storage_alias")
    before = state.snapshot()

    result = _transfer("storage_alias")

    assert result.rows == 2
    assert result.data is None
    assert result.metadata.source_rows == result.metadata.staged_rows == 2
    assert result.metadata.final_target_rows == 2
    assert result.metadata.row_count_validated is True
    assert state.snapshot()[state.coordinator][_PHYSICAL] == before[state.coordinator][_PHYSICAL]
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert state.rows(state.remote, _PHYSICAL) == _SOURCE_ROWS
    assert state.rows(state.coordinator, _TARGET) == _SOURCE_ROWS
    assert list(source.tables["db.source"].itertuples(index=False, name=None)) == _SOURCE_ROWS
    assert all(set(tables) == set(before[host]) for host, tables in state.host_tables.items())
    assert state.inserts
    assert state.opened
    assert all(connection.closed for connection in state.opened)


@pytest.mark.parametrize("desired", ["storage", None], ids=["remote", "coordinator-local"])
def test_public_transfer_replacement_finishes_pair_to_remote_local_conversion(
    monkeypatch, write_sql_connections, desired
):
    source, state = _install(monkeypatch, write_sql_connections, desired=desired, pair=False)
    before = state.snapshot()

    result = _transfer(desired, pair=False)

    assert result.rows == 2
    assert result.data is None
    assert result.metadata.source_rows == result.metadata.staged_rows == 2
    assert result.metadata.final_target_rows == 2
    assert result.metadata.row_count_validated is True
    after = state.snapshot()
    destination = state.remote if desired == "storage" else state.coordinator
    assert after[state.coordinator][_PHYSICAL] == before[state.coordinator][_PHYSICAL]
    assert set(after[state.coordinator]) == (
        {_PHYSICAL} if destination == state.remote else {_PHYSICAL, _TARGET}
    )
    assert set(after[state.remote]) == ({_TARGET} if destination == state.remote else set())
    assert after[destination][_TARGET]["uuid"] != before[state.remote][_PHYSICAL]["uuid"]
    assert after[destination][_TARGET]["uuid"] != before[state.coordinator][_TARGET]["uuid"]
    assert after[destination][_TARGET]["route"] is None
    assert state.rows(destination, _TARGET) == _SOURCE_ROWS
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert list(source.tables["db.source"].itertuples(index=False, name=None)) == _SOURCE_ROWS
    assert [row for insertion in state.inserts for row in insertion["rows"]] == _SOURCE_ROWS
    assert state.opened
    assert all(connection.closed for connection in state.opened)


def test_public_transfer_replacement_retains_completed_conversion_on_missing_target_metadata(
    monkeypatch, write_sql_connections
):
    source, state = _install(monkeypatch, write_sql_connections, desired="storage", pair=False)
    before = state.snapshot()
    converted = []
    missing = _missing_scope_rows("storage", "tables", ("_shard_num", "create_table_query"))

    def lose_completed_target_metadata(tree, rows):
        if (
            _TARGET in state.host_tables[state.coordinator]
            or _PHYSICAL in state.host_tables[state.remote]
            or state.rows(state.remote, _TARGET) != [(11,), (12,), (13,)]
            or missing(tree, rows) is None
        ):
            return None
        filters = {
            predicate.this.name: predicate.expression.this
            for predicate in tree.find_all(exp.EQ)
            if isinstance(predicate.this, exp.Column)
            and isinstance(predicate.expression, exp.Literal)
        }
        if filters != {"database": "analytics", "name": "events"}:
            return None
        assert len(rows) == 1
        converted.append(state.snapshot())
        return []

    state.metadata_transform = lose_completed_target_metadata

    with pytest.raises(InvalidSqlInputError, match="complete existing physical table coverage"):
        _transfer("storage", pair=False)

    assert converted
    assert state.metadata_faults
    after = state.snapshot()
    assert after[state.coordinator] == {_PHYSICAL: before[state.coordinator][_PHYSICAL]}
    assert set(after[state.remote]) == {_TARGET}
    assert all(after[state.remote] == snapshot[state.remote] for snapshot in converted)
    assert after[state.remote][_TARGET]["route"] is None
    assert state.rows(state.remote, _TARGET) == [(11,), (12,), (13,)]
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert list(source.tables["db.source"].itertuples(index=False, name=None)) == _SOURCE_ROWS
    assert [row for insertion in state.inserts for row in insertion["rows"]] == _SOURCE_ROWS
    creates = [
        parse_one(strip_query_label(statement), read=ClickHouseDialect)
        for statement in state.commands
        if strip_query_label(statement).startswith("CREATE TABLE")
    ]
    assert creates
    created_tables = [
        create.this.this if isinstance(create.this, exp.Schema) else create.this
        for create in creates
    ]
    assert all("__replace_guarded_" not in table.name for table in created_tables)
    assert state.opened
    assert all(connection.closed for connection in state.opened)


def test_public_transfer_append_keeps_existing_storage_despite_desired_replacement_scope(
    monkeypatch, write_sql_connections
):
    source, state = _install(monkeypatch, write_sql_connections, desired="incoming")
    before = state.snapshot()

    result = _transfer("incoming", write_mode="append")

    assert result.rows == 2
    assert result.data is None
    assert result.metadata.source_rows == result.metadata.staged_rows == 2
    assert result.metadata.final_target_rows == 5
    after = state.snapshot()
    assert after[state.coordinator] == before[state.coordinator]
    physical_before = before[state.remote][_PHYSICAL]
    physical_after = after[state.remote][_PHYSICAL]
    assert physical_after["uuid"] == physical_before["uuid"]
    assert physical_after["ddl"] == physical_before["ddl"]
    assert physical_after["route"] == physical_before["route"]
    expected = [(11,), (12,), (13,), *_SOURCE_ROWS]
    assert state.rows(state.remote, _PHYSICAL) == expected
    assert state.rows(state.coordinator, _TARGET) == expected
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert list(source.tables["db.source"].itertuples(index=False, name=None)) == _SOURCE_ROWS
    assert all(set(tables) == set(before[host]) for host, tables in state.host_tables.items())
    _assert_no_replacement_ddl(state)
    assert state.opened
    assert all(connection.closed for connection in state.opened)


def test_public_transfer_memory_stage_uses_only_supported_storage_clauses(
    monkeypatch, write_sql_connections
):
    source, state = _install(monkeypatch, write_sql_connections, desired="incoming")
    before = state.snapshot()

    result = _transfer("incoming", write_mode="append")

    assert result.rows == 2
    assert result.data is None
    assert result.metadata.source_rows == result.metadata.staged_rows == 2
    assert result.metadata.final_target_rows == 5
    after = state.snapshot()
    assert after[state.coordinator] == before[state.coordinator]
    physical_before = before[state.remote][_PHYSICAL]
    physical_after = after[state.remote][_PHYSICAL]
    assert physical_after["uuid"] == physical_before["uuid"]
    assert physical_after["ddl"] == physical_before["ddl"]
    assert physical_after["route"] == physical_before["route"]
    expected = [(11,), (12,), (13,), *_SOURCE_ROWS]
    assert state.rows(state.remote, _PHYSICAL) == expected
    assert state.rows(state.coordinator, _TARGET) == expected
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    stages = [
        parse_one(strip_query_label(statement), read=ClickHouseDialect)
        for statement in state.commands
        if strip_query_label(statement).startswith("CREATE TABLE")
    ]
    assert stages
    unsupported = (exp.Order, exp.PartitionedByProperty, exp.PrimaryKey, exp.SampleProperty)
    for stage in stages:
        assert stage.find(exp.EngineProperty).this.name.casefold() == "memory"
        properties = stage.args.get("properties")
        assert properties is not None
        assert not any(isinstance(prop, unsupported) for prop in properties.expressions)
        assert stage.this.find(exp.PrimaryKeyColumnConstraint) is None
    assert list(source.tables["db.source"].itertuples(index=False, name=None)) == _SOURCE_ROWS
    assert all(set(tables) == set(before[host]) for host, tables in state.host_tables.items())
    _assert_no_replacement_ddl(state)
    assert state.opened
    assert all(connection.closed for connection in state.opened)


def _missing_scope_rows(cluster, relation, projection):
    def transform(tree, _rows):
        source = tree.args.get("from_")
        function = source.this.this if source is not None else None
        if not isinstance(function, exp.Anonymous):
            return None
        if function.name.casefold() != "clusterallreplicas":
            return None
        actual_cluster, database, actual_relation, _trailing = cluster_arguments(
            function, "analytics"
        )
        names = tuple(item.name.casefold() for item in tree.expressions)
        if (actual_cluster, database, actual_relation, names) == (
            cluster,
            "system",
            relation,
            projection,
        ):
            return []
        return None

    return transform


@pytest.mark.parametrize(
    ("cluster", "relation", "projection"),
    [
        ("storage", "one", ("_shard_num", "serveruuid", "tcpport")),
        ("storage_alias", "one", ("_shard_num", "serveruuid", "tcpport")),
        ("storage", "tables", ("_shard_num", "create_table_query")),
    ],
    ids=["source-host-coverage", "desired-host-coverage", "source-storage-definition"],
)
def test_public_transfer_replacement_requires_complete_physical_ownership_metadata(
    monkeypatch, write_sql_connections, cluster, relation, projection
):
    source, state = _install(monkeypatch, write_sql_connections, desired="storage_alias")
    before = state.snapshot()
    state.metadata_transform = _missing_scope_rows(cluster, relation, projection)

    with pytest.raises(InvalidSqlInputError, match=r"(?i)verify|coverage|placement|coordinator"):
        _transfer("storage_alias")

    assert state.metadata_faults
    assert state.snapshot() == before
    assert state.rows(state.remote, _PHYSICAL) == [(11,), (12,), (13,)]
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert state.rows(state.coordinator, _TARGET) == [(11,), (12,), (13,)]
    assert list(source.tables["db.source"].itertuples(index=False, name=None)) == _SOURCE_ROWS
    _assert_no_replacement_ddl(state)
    assert state.opened
    assert all(connection.closed for connection in state.opened)


def test_public_transfer_replacement_rejects_an_unowned_facade_cutover_scope(
    monkeypatch, write_sql_connections
):
    source, state = _install(monkeypatch, write_sql_connections, desired="storage_alias")
    foreign = parse_create_table(
        "CREATE TABLE analytics.events (id UInt64) ENGINE=MergeTree ORDER BY id",
        _TARGET,
    )
    state._create(state.remote, foreign, state.foreign_rows)
    before = state.snapshot()

    with pytest.raises(InvalidSqlInputError, match=r"(?i)facade.*coordinator|unowned"):
        _transfer("storage_alias", facade_scope="frontends")

    assert state.snapshot() == before
    assert state.rows(state.remote, _TARGET) == state.foreign_rows
    assert state.rows(state.remote, _PHYSICAL) == [(11,), (12,), (13,)]
    assert state.rows(state.coordinator, _TARGET) == [(11,), (12,), (13,)]
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert list(source.tables["db.source"].itertuples(index=False, name=None)) == _SOURCE_ROWS
    _assert_no_replacement_ddl(state)
    assert state.opened
    assert all(connection.closed for connection in state.opened)
