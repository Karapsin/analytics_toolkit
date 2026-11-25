"""Off-side native tables cannot join the live source's replication group."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_replica_lifecycle import install_replica_lifecycle

_SOURCE = "analytics.events_shard"


def _reseed(state: Any, engine: str) -> None:
    for host in (state.coordinator, state.remote):
        state.host_tables[host].pop(_SOURCE)
        state._create(
            host,
            parse_create_table(
                f"CREATE TABLE {_SOURCE} (id UInt64) ENGINE={engine} ORDER BY id", _SOURCE
            ),
            [(11,), (12,), (13,)],
        )
    state.created.clear()


def _source_is_unchanged(state: Any, original: Any) -> None:
    state.drain_replication()
    current = state.snapshot()
    for host, tables in original.items():
        for name, value in tables.items():
            assert current[host][name] == value, (host, name, current[host][name], value)


def _candidate_creates(state: Any) -> list[Any]:
    creates = []
    for command in state.commands:
        tree = parse_one(strip_query_label(command), read=ClickHouseDialect)
        if isinstance(tree, exp.Create):
            target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
            if "__replace_guarded_" in target.name or "__reconfigure_" in target.name:
                creates.append(tree)
    return creates


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_staged_replace_rejects_shared_live_replication_path_before_create(
    monkeypatch: Any, write_sql_connections: Any, driver: str
) -> None:
    state = install_replica_lifecycle(monkeypatch, write_sql_connections, driver=driver)
    _reseed(state, "ReplicatedMergeTree('/shared/events', '{replica}')")
    original = state.snapshot()
    error = None
    try:
        sql.load_df(
            "ch",
            "analytics.events",
            pd.DataFrame({"id": pd.Series([201, 202], dtype="uint64")}),
            table_schema={"id": "UInt64"},
            write_mode="replace",
            ch_engine="ReplicatedMergeTree('/shared/events', 'replacement_{replica}')",
            ch_distributed_on_cluster="storage",
            order_by="id",
            retry_cnt=1,
            timeout_increment=0,
        )
    except Exception as caught:  # noqa: BLE001 - inspect source safety even after later count failure.
        error = caught
    _source_is_unchanged(state, original)
    assert isinstance(error, InvalidSqlInputError), repr(error)
    assert "replication" in str(error).casefold() or "isolat" in str(error).casefold()
    assert not _candidate_creates(state)


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("qualified", [False, True])
def test_public_staged_replace_accepts_verified_distinct_fixed_replication_path(
    monkeypatch: Any, write_sql_connections: Any, driver: str, qualified: bool
) -> None:
    state = install_replica_lifecycle(monkeypatch, write_sql_connections, driver=driver)
    _reseed(state, "ReplicatedMergeTree('/shared/events', '{replica}')")
    original = state.snapshot()
    result = sql.load_df(
        "ch",
        "analytics.events" if qualified else "events",
        pd.DataFrame({"id": pd.Series([201, 202], dtype="uint64")}),
        table_schema={"id": "UInt64"},
        write_mode="replace",
        ch_engine="ReplicatedMergeTree('/isolated/replacement', 'replacement_{replica}')",
        ch_distributed_on_cluster="storage",
        order_by="id",
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
    )
    assert result.rows == 2
    assert result.metadata.source_rows == result.metadata.staged_rows == 2
    visible = sql.read("ch", "SELECT id FROM analytics.events ORDER BY id", retry_cnt=1)
    assert visible.id.tolist() == [201, 202]
    for host in (state.coordinator, state.remote):
        actual = state.host_tables[host][_SOURCE]
        assert actual.rows == [(201,), (202,)]
        assert actual.uuid != original[host][_SOURCE]["uuid"]
        assert state.replication_identity(host, _SOURCE)[1] == "/isolated/replacement"
    assert (
        state.host_tables[state.coordinator]["analytics.unrelated"].rows
        == original[state.coordinator]["analytics.unrelated"]["rows"]
    )


def _relocation_state(monkeypatch: Any, write_sql_connections: Any, driver: str) -> Any:
    state = install_replica_lifecycle(monkeypatch, write_sql_connections, driver=driver)
    state.clusters["storage"] = (state.coordinator,)
    state.groups["storage"] = ((1, 1, state.coordinator),)
    state.clusters["archive"] = (state.remote,)
    state.groups["archive"] = ((1, 1, state.remote),)
    state.host_tables[state.remote].pop(_SOURCE)
    state.host_tables[state.remote].pop(state.source_table)
    return state


def _relocate(engine: str) -> Any:
    return sql.ch_reconfigure_table(
        "ch",
        "analytics.events",
        ch_engine=engine,
        ch_shard_on_cluster="archive",
        ch_distributed_cluster="archive",
        ch_distributed_on_cluster="coordinator_only",
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
    )


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_cross_cluster_reconfigure_rejects_same_expanded_table_replication_path(
    monkeypatch: Any, write_sql_connections: Any, driver: str
) -> None:
    state = _relocation_state(monkeypatch, write_sql_connections, driver)
    original = state.snapshot()
    error = None
    try:
        _relocate("ReplicatedMergeTree('/tables/{table}/{shard}', 'new_{replica}')")
    except Exception as caught:  # noqa: BLE001 - flush eventual shared writes before asserting safety.
        error = caught
    _source_is_unchanged(state, original)
    assert isinstance(error, InvalidSqlInputError), repr(error)
    assert "replication" in str(error).casefold() or "isolat" in str(error).casefold()
    assert not state.commands


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize(
    "path", ["/isolated/destination", "/isolated/{uuid}"], ids=["fixed-distinct", "fresh-uuid"]
)
def test_public_cross_cluster_reconfigure_uses_independent_resolved_replication_path(
    monkeypatch: Any, write_sql_connections: Any, driver: str, path: str
) -> None:
    state = _relocation_state(monkeypatch, write_sql_connections, driver)
    original = state.snapshot()
    result = _relocate(f"ReplicatedMergeTree('{path}', 'new_{{replica}}')")
    state.drain_replication()
    assert result.metadata.row_count_validated is True
    assert result.data["cleanup_complete"] is True
    actual = state.host_tables[state.remote][_SOURCE]
    assert actual.rows == [(11,), (12,), (13,)]
    assert actual.uuid != original[state.coordinator][_SOURCE]["uuid"]
    resolved = state.replication_identity(state.remote, _SOURCE)[1]
    if "{uuid}" in path:
        assert resolved == "/isolated/" + str(actual.uuid)
        create = next(
            parse_one(strip_query_label(command), read=ClickHouseDialect)
            for command in state.commands
            if "CREATE TABLE" in command
            and "events_shard" in command
            and "ReplicatedMergeTree" in command
        )
        assert create.find(exp.UuidProperty).this.name == str(actual.uuid)
    else:
        assert resolved == path
    assert (
        state.host_tables[state.coordinator]["analytics.unrelated"].rows
        == original[state.coordinator]["analytics.unrelated"]["rows"]
    )
