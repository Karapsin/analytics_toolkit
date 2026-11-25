"""ClickHouse drop compatibility retains namespaces, independently placed facades and views."""

from __future__ import annotations

from copy import deepcopy

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch import lifecycle
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from sqlglot import parse_one

from tests.sql._support.ch_drop_compatibility import view_metadata
from tests.sql._support.ch_drop_scope import install_drop_scope


@pytest.mark.parametrize("override", ["custom_storage", "analytics.custom_storage"])
def test_drop_pair_explicit_physical_override_uses_the_effective_database(
    monkeypatch, write_sql_connections, override
):
    state = install_drop_scope(monkeypatch, write_sql_connections)
    old = state.host_tables[state.remote].pop("analytics.events_shard")
    state._create(
        state.remote,
        parse_create_table(
            "CREATE TABLE analytics.custom_storage (id UInt64) ENGINE=MergeTree ORDER BY id",
            "analytics.custom_storage",
        ),
        old.rows,
    )
    state.host_tables[state.coordinator].pop("analytics.events")
    state._create(
        state.coordinator,
        parse_create_table(
            "CREATE TABLE analytics.events (id UInt64) "
            "ENGINE=Distributed(storage, analytics, custom_storage, rand())",
            "analytics.events",
        ),
    )
    foreign = deepcopy(state.host_tables[state.coordinator]["analytics.events_shard"])
    connection = sql.get_sql_connection("ch")
    try:
        assert (
            lifecycle.drop_ch_distributed_table_pair(
                connection,
                "analytics.events",
                ch_cluster="storage",
                shard_table=override,
            )
            is None
        )
    finally:
        connection.close()

    assert "analytics.events" not in state.host_tables[state.coordinator]
    assert "analytics.custom_storage" not in state.host_tables[state.remote]
    assert state.host_tables[state.coordinator]["analytics.events_shard"].rows == foreign.rows


@pytest.mark.parametrize("if_exists", [False, True])
def test_public_routed_drop_finds_a_coordinator_only_facade(
    monkeypatch, write_sql_connections, if_exists
):
    state = install_drop_scope(
        monkeypatch,
        write_sql_connections,
        cluster_routing={"cluster": "storage"},
    )
    foreign = deepcopy(state.host_tables[state.coordinator]["analytics.events_shard"])

    assert (
        sql.drop_tables(
            "ch",
            "analytics.events",
            ch_cluster="storage",
            if_exists=if_exists,
        )
        is None
    )

    assert "analytics.events" not in state.host_tables[state.coordinator]
    assert "analytics.events_shard" not in state.host_tables[state.remote]
    assert state.host_tables[state.coordinator]["analytics.events_shard"].rows == foreign.rows


@pytest.mark.parametrize("entrypoint", ["public", "explicit"])
@pytest.mark.parametrize(
    "materialized",
    [False, True, "external"],
    ids=["ordinary-view", "materialized-view", "materialized-external"],
)
def test_named_view_drop_never_infers_or_removes_a_companion(
    monkeypatch, write_sql_connections, entrypoint, materialized
):
    state = install_drop_scope(monkeypatch, write_sql_connections)
    view_metadata(monkeypatch, state)
    target = "analytics.selected_view"
    state._create(
        state.coordinator,
        parse_create_table(
            f"CREATE TABLE {target} (id UInt64) ENGINE=Memory",
            target,
        ),
    )
    value = state.host_tables[state.coordinator][target]
    prefix = "MATERIALIZED VIEW" if materialized else "VIEW"
    if materialized == "external":
        state._create(
            state.coordinator,
            parse_create_table(
                "CREATE TABLE analytics.output_storage (id UInt64) ENGINE=Memory",
                "analytics.output_storage",
            ),
            [(index,) for index in range(100)],
        )
        columns_engine = " TO analytics.output_storage"
    else:
        columns_engine = " (id UInt64) ENGINE=Memory" if materialized else ""
    value.create = parse_one(
        f"CREATE {prefix} {target}{columns_engine} AS SELECT id FROM analytics.events_shard",
        read=ClickHouseDialect,
    )
    value.engine = "MaterializedView" if materialized else "View"
    before = state.snapshot()

    if entrypoint == "public":
        result = sql.drop_tables("ch", target, ch_cluster=None, ch_drop_shard=False)
    else:
        connection = sql.get_sql_connection("ch")
        try:
            result = lifecycle.drop_ch_table(connection, target, ch_cluster=None)
        finally:
            connection.close()

    expected = deepcopy(before)
    expected[state.coordinator].pop(target)
    assert result is None
    assert state.snapshot() == expected
