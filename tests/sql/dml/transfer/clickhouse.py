from __future__ import annotations

from tests.sql._support.assertions import strip_query_label as _strip_query_label

# ruff: noqa: I001

import importlib
from copy import deepcopy
from datetime import date
from typing import Any

import pytest
from analytics_toolkit.sql.backends.ch.metadata import extract_clickhouse_distributed_shard_table
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import engine_name, engine_sql
from sqlglot import exp

from tests.conftest import DEFAULT_SQL_CONNECTIONS

from tests.sql._support.transfer_clickhouse_protocol import (
    TransferClickHouseClient,
    column_types,
    command_trees,
    install_legacy_transfer_connections,
)
from tests.sql._support.transfer_driver_boundary import install_fake_gp_cursor_boundary


transfer_api_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.api")
transfer_attempt_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.attempt")
transfer_finalize_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.flow.finalize"
)


TARGET_TABLE = "test_transfer_target"
TARGET_SHARD_TABLE = "test_transfer_target_shard"


class FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.result_rows = rows
        self.row_count = len(rows)
        self.column_names = (
            tuple(f"column_{index}" for index in range(len(rows[0]))) if rows else ()
        )
        self.result_columns = (
            tuple([row[index] for row in rows] for index in range(len(rows[0]))) if rows else ()
        )


class FakeSourceCursor:
    def __init__(
        self,
        rows: list[tuple[Any, ...]],
        description: list[tuple[Any, ...]] | None = None,
    ) -> None:
        self._rows = rows
        self.description = description or [
            ("month_date", 1082, None, None, None, None),
            ("users", 20, None, None, None, None),
        ]
        self.executed_queries: list[str] = []
        self.close_calls = 0

    def execute(self, query: str) -> None:
        self.executed_queries.append(query)
        if "COUNT(*) FROM" in query:
            self._rows = [(len(self._rows),)]

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        batch = self._rows[:size]
        self._rows = self._rows[size:]
        return batch

    def close(self) -> None:
        self.close_calls += 1


class FakeSourceConnection:
    def __init__(
        self,
        rows: list[tuple[Any, ...]],
        description: list[tuple[Any, ...]] | None = None,
    ) -> None:
        self._rows = rows
        self._description = description
        self.cursors: list[FakeSourceCursor] = []
        self.close_calls = 0

    def cursor(self) -> FakeSourceCursor:
        cursor = FakeSourceCursor(self._rows.copy(), self._description)
        self.cursors.append(cursor)
        return cursor

    def close(self) -> None:
        self.close_calls += 1


@pytest.fixture(autouse=True)
def _offline_gp_driver(monkeypatch, write_sql_connections):
    install_fake_gp_cursor_boundary(monkeypatch, FakeSourceCursor)
    connections = deepcopy(DEFAULT_SQL_CONNECTIONS)
    connections["ch"]["database"] = "analytics"
    write_sql_connections(connections)


FakeClickHouseClient = TransferClickHouseClient


@pytest.mark.parametrize("users", [10, None])
def test_transfer_table_clickhouse_target_creates_distributed_table_on_cluster(
    monkeypatch,
    users,
) -> None:
    source = FakeSourceConnection(rows=[(date(2024, 2, 1), users)])
    target = FakeClickHouseClient()

    install_legacy_transfer_connections(monkeypatch, source, target)

    transferred_rows = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="ch",
        from_sql="select month_date, users from source_table",
        to_table=TARGET_TABLE,
        write_mode="replace",
        retry_cnt=1,
        timeout_increment=0,
        ch_ddl_ready_timeout_seconds=0.01,
        full_retry_cnt=1,
        full_timeout_increment=0,
        partition_by=["month_date"],
        order_by=["month_date"],
        ch_sharding_key="cityHash64(month_date)",
    )

    assert transferred_rows == 1
    stage_table = target.inserts[0]["table"]
    stage_identifier = stage_table.split(".")[-1]
    assert stage_table.startswith("analytics_toolkit_transfer.")
    assert stage_identifier[16:18] == "__"
    assert stage_identifier.endswith("__w00000")
    assert len(stage_identifier.encode()) <= 63
    staged_row = target.inserts[0]["data"][0]
    assert staged_row == (date(2024, 2, 1), users)
    assert target.inserts[0]["column_names"] == ["month_date", "users"]
    # GP metadata leaves nullability unknown. The certified native integer
    # representation retains that domain even when this selection has no NULL.
    assert target.inserts[0]["column_type_names"] == ["Date", "Nullable(Int64)"]
    assert "df" not in target.inserts[0]

    creates = command_trees(target, "CREATE", TARGET_TABLE)
    cluster_distributed_creates = [tree for tree in creates if tree.find(exp.OnCluster)]
    assert len(cluster_distributed_creates) == 1
    facade = cluster_distributed_creates[0]
    assert facade.args.get("exists") is False
    assert engine_name(facade) == "Distributed"
    assert column_types(facade) == {"month_date": "Date", "users": "Nullable(Int64)"}
    route = extract_clickhouse_distributed_shard_table(engine_sql(facade), "analytics")
    assert route.cluster == "{cluster}"
    assert route.table == TARGET_SHARD_TABLE
    assert "cityHash64(month_date)" in engine_sql(facade).replace('"', "").replace("`", "")
    local_shard_creates = [
        tree
        for tree in command_trees(target, "CREATE", TARGET_SHARD_TABLE)
        if tree.find(exp.OnCluster) is None
    ]
    assert len(local_shard_creates) == 1
    assert local_shard_creates[0].find(exp.UuidProperty) is not None
    live_inserts = command_trees(target, "INSERT", TARGET_TABLE)
    assert len(live_inserts) == 1
    assert [item.name for item in live_inserts[0].this.expressions] == ["month_date", "users"]
    assert live_inserts[0].expression.expressions[0].unalias().args["to"].sql() == "DATE"
    assert live_inserts[0].expression.find(exp.Table).name == stage_identifier
    assert live_inserts[0].expression.find(exp.Table).db == "analytics_toolkit_transfer"

    assert f"DROP TABLE IF EXISTS {TARGET_TABLE} ON CLUSTER '{{cluster}}'" not in map(
        _strip_query_label, target.commands
    )
    assert f"DROP TABLE IF EXISTS {TARGET_SHARD_TABLE} ON CLUSTER '{{cluster}}'" not in map(
        _strip_query_label, target.commands
    )


def test_transfer_table_preserves_greenplum_bytea_as_clickhouse_string(monkeypatch) -> None:
    source = FakeSourceConnection(
        rows=[(1, memoryview(b"\x00\xff\x80"), "Кириллица")],
        description=[
            ("id", 20, None, None, None, None),
            ("cheque_pk", 17, None, None, None, None),
            ("name", 25, None, None, None, None),
        ],
    )
    target = FakeClickHouseClient()

    install_legacy_transfer_connections(monkeypatch, source, target)

    transferred_rows = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="ch",
        from_sql="select id, cheque_pk, name from source_table",
        to_table=TARGET_TABLE,
        write_mode="replace",
        retry_cnt=1,
        timeout_increment=0,
        ch_ddl_ready_timeout_seconds=0.01,
        full_retry_cnt=1,
        full_timeout_increment=0,
        table_schema={"id": "Int64", "cheque_pk": "String", "name": "String"},
        order_by=["id"],
        ch_only_shard=True,
    )

    assert transferred_rows == 1
    assert target.inserts[0]["data"] == [(1, b"\x00\xff\x80", "Кириллица".encode())]
    assert bytes(source._rows[0][1]) == b"\x00\xff\x80"
    assert source._rows[0][2] == "Кириллица"
    assert target.inserts[0]["column_type_names"] == ["Int64", "String", "String"]
    target_creates = command_trees(target, "CREATE", TARGET_TABLE)
    assert target_creates
    for tree in target_creates:
        assert column_types(tree) == {"id": "Int64", "cheque_pk": "String", "name": "String"}
        assert [column.name for column in tree.find(exp.Order).find_all(exp.Column)] == ["id"]


def test_transfer_table_clickhouse_only_shard_creates_local_target(
    monkeypatch,
) -> None:
    source = FakeSourceConnection(rows=[(date(2024, 2, 1), 10)])
    target = FakeClickHouseClient()

    install_legacy_transfer_connections(monkeypatch, source, target)

    transferred_rows = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="ch",
        from_sql="select month_date, users from source_table",
        to_table=TARGET_TABLE,
        write_mode="replace",
        retry_cnt=1,
        timeout_increment=0,
        ch_ddl_ready_timeout_seconds=0.01,
        full_retry_cnt=1,
        full_timeout_increment=0,
        ch_only_shard=True,
        partition_by=["month_date"],
        order_by=["month_date"],
    )

    assert transferred_rows == 1
    assert f"DROP TABLE IF EXISTS {TARGET_TABLE}" not in map(
        _strip_query_label,
        target.commands,
    )
    target_creates = command_trees(target, "CREATE", TARGET_TABLE)
    assert any(tree.find(exp.OnCluster) is not None for tree in target_creates)
    assert all(
        "ENGINE = Distributed(" not in command
        for command in map(_strip_query_label, target.commands)
    )
    assert all(TARGET_SHARD_TABLE not in command for command in target.commands)
    assert len(target_creates) == 2
    assert engine_name(target_creates[0]) == "ReplicatedMergeTree"
    assert [
        column.name
        for column in target_creates[0].find(exp.PartitionedByProperty).find_all(exp.Column)
    ] == ["month_date"]
    assert [column.name for column in target_creates[0].find(exp.Order).find_all(exp.Column)] == [
        "month_date"
    ]
    assert any("clusterAllReplicas" in query for query in target.queries)
    live_inserts = command_trees(target, "INSERT", TARGET_TABLE)
    assert len(live_inserts) == 1
    assert [item.name for item in live_inserts[0].this.expressions] == ["month_date", "users"]


def test_transfer_table_clickhouse_empty_missing_target_creates_empty_target(
    monkeypatch,
) -> None:
    source = FakeSourceConnection(rows=[])
    target = FakeClickHouseClient()

    install_legacy_transfer_connections(monkeypatch, source, target)

    transferred_rows = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="ch",
        from_sql="select month_date, users from source_table",
        to_table=TARGET_TABLE,
        retry_cnt=1,
        timeout_increment=0,
        ch_ddl_ready_timeout_seconds=0.01,
        full_retry_cnt=3,
        full_timeout_increment=0,
        partition_by=["month_date"],
        order_by=["month_date"],
    )

    assert transferred_rows == 0
    assert any(
        command.startswith(f"CREATE TABLE IF NOT EXISTS {TARGET_TABLE}")
        for command in map(_strip_query_label, target.commands)
    )
    assert target.inserts == []
    assert f"analytics.{TARGET_TABLE}" in target.state.host_tables[target.host]
    assert source.close_calls == 1
    assert target.close_calls >= 1
