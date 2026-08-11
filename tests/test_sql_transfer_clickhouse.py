from __future__ import annotations

# ruff: noqa: C901, I001

import importlib
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows
        self.description = [
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
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows
        self.cursors: list[FakeSourceCursor] = []
        self.close_calls = 0

    def cursor(self) -> FakeSourceCursor:
        cursor = FakeSourceCursor(self._rows.copy())
        self.cursors.append(cursor)
        return cursor

    def close(self) -> None:
        self.close_calls += 1


class FakeClickHouseClient:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.command_settings: list[dict[str, object] | None] = []
        self.queries: list[str] = []
        self.inserts: list[dict[str, object]] = []
        self.created_tables: set[str] = set()
        self.close_calls = 0

    def command(
        self,
        sql: str,
        settings: dict[str, object] | None = None,
    ) -> None:
        self.commands.append(sql)
        self.command_settings.append(settings)
        self._track_table_ddl(sql)

    def query(self, sql: str, column_oriented: bool = False) -> FakeResult:
        del column_oriented
        self.queries.append(sql)
        sql = _strip_query_label(sql)
        if "__analytics_toolkit_" in sql and "GROUP BY" in sql:
            row = next(
                (insert["data"][0] for insert in self.inserts if insert.get("data")),
                None,
            )
            if row is None:
                return FakeResult([])
            if "__analytics_toolkit_row_ordinal" in sql and "MIN(" in sql:
                rows = [item for insert in self.inserts for item in insert.get("data", [])]
                return FakeResult([(int(row[-2]), 1, len(rows), len(rows), len(rows))])
            return FakeResult([(row[-4], row[-3])])
        if sql.startswith("SELECT count() FROM "):
            table_name = sql[len("SELECT count() FROM ") :].strip()
            return FakeResult([(self._inserted_rows(table_name),)])
        if sql.startswith("SELECT getMacro("):
            return FakeResult([("core",)])
        if "clusterAllReplicas" in sql and "system, one" in sql:
            return FakeResult([(1,)])
        if "FROM system.clusters" in sql:
            return FakeResult([(1,)])
        if "clusterAllReplicas" in sql and "system, tables" in sql:
            return FakeResult([(self._cluster_table_count(sql),)])
        if "clusterAllReplicas" in sql and "system, columns" in sql:
            return FakeResult([(sql.count("name = ") or 1,)])
        if "clusterAllReplicas" in sql:
            return FakeResult([(len(self.created_tables),)])
        if sql.startswith("EXISTS TABLE "):
            table_name = sql[len("EXISTS TABLE ") :].strip()
            return FakeResult([(int(table_name in self.created_tables),)])
        return FakeResult([])

    def insert_df(
        self,
        table: str,
        df: pd.DataFrame,
        column_names: list[str],
    ) -> None:
        self.inserts.append(
            {
                "table": table,
                "df": df.copy(),
                "column_names": list(column_names),
            }
        )

    def insert(
        self,
        table: str,
        data: list[tuple[Any, ...]],
        column_names: list[str],
        column_type_names: list[str] | None = None,
    ) -> None:
        self.inserts.append(
            {
                "table": table,
                "data": list(data),
                "column_names": list(column_names),
                "column_type_names": (
                    list(column_type_names) if column_type_names is not None else None
                ),
            }
        )

    def close(self) -> None:
        self.close_calls += 1

    def _track_table_ddl(self, sql: str) -> None:
        body = _strip_query_label(sql)
        if body.startswith("CREATE TABLE IF NOT EXISTS "):
            table_name = body[len("CREATE TABLE IF NOT EXISTS ") :].split()[0]
            self.created_tables.add(table_name)
            return
        if body.startswith("CREATE TABLE "):
            table_name = body[len("CREATE TABLE ") :].split()[0]
            self.created_tables.add(table_name)
            return
        if body.startswith("DROP TABLE IF EXISTS "):
            table_name = body[len("DROP TABLE IF EXISTS ") :].split()[0]
            self.created_tables.discard(table_name)

    def _cluster_table_count(self, sql: str) -> int:
        marker = "AND name = '"
        if marker not in sql:
            return len(self.created_tables)
        relation_name = sql.split(marker, 1)[1].split("'", 1)[0]
        return sum(
            1
            for table_name in self.created_tables
            if table_name.rsplit(".", 1)[-1] == relation_name
        )

    def _inserted_rows(self, table_name: str) -> int:
        total = 0
        for insert in self.inserts:
            if insert["table"] != table_name:
                continue
            if "data" in insert:
                total += len(insert["data"])
            elif "df" in insert:
                total += len(insert["df"])
        if total == 0:
            for command in reversed(self.commands):
                body = _strip_query_label(command)
                if body.startswith(f"INSERT INTO {table_name} ") and " FROM " in body:
                    source_table = body.rsplit(" FROM ", 1)[1].split()[0]
                    return self._inserted_rows(source_table)
        return total


def _strip_query_label(sql: str) -> str:
    stripped = sql.lstrip()
    if stripped.startswith("/* analytics_toolkit query_label=") and "*/" in stripped:
        return stripped.split("*/", 1)[1].lstrip()
    return stripped


def test_transfer_table_clickhouse_target_creates_distributed_table_on_cluster(
    monkeypatch,
) -> None:
    source = FakeSourceConnection(rows=[(date(2024, 2, 1), 10)])
    target = FakeClickHouseClient()

    def fake_get_sql_connection(connection_key: str) -> object:
        if connection_key == "gp":
            return source
        if connection_key == "ch":
            return target
        raise AssertionError(f"Unexpected connection key: {connection_key}")

    monkeypatch.setattr(
        transfer_attempt_module,
        "get_sql_connection",
        fake_get_sql_connection,
    )
    monkeypatch.setattr(
        transfer_finalize_module,
        "get_sql_connection",
        fake_get_sql_connection,
    )

    transferred_rows = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="ch",
        from_sql="select month_date, users from source_table",
        to_table=TARGET_TABLE,
        write_mode="replace",
        retry_cnt=1,
        timeout_increment=0,
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
    assert staged_row == (date(2024, 2, 1), 10)
    assert target.inserts[0]["column_names"] == ["month_date", "users"]
    assert target.inserts[0]["column_type_names"] == ["Date", "Int64"]
    assert "df" not in target.inserts[0]

    cluster_distributed_creates = [
        command
        for command in map(_strip_query_label, target.commands)
        if command.startswith(f"CREATE TABLE IF NOT EXISTS {TARGET_TABLE}\n")
        and "ON CLUSTER '{cluster}'" in command
    ]
    assert len(cluster_distributed_creates) == 1
    assert "ENGINE = Distributed(" in cluster_distributed_creates[0]
    assert "`month_date` Date" in cluster_distributed_creates[0]
    assert "`users` Int64" in cluster_distributed_creates[0]
    assert "    '{cluster}'," in cluster_distributed_creates[0]
    assert f"    '{TARGET_SHARD_TABLE}'," in cluster_distributed_creates[0]
    local_shard_creates = [
        command
        for command in map(_strip_query_label, target.commands)
        if command.startswith(f"CREATE TABLE IF NOT EXISTS {TARGET_SHARD_TABLE}\n")
        and "ON CLUSTER" not in command
    ]
    assert len(local_shard_creates) == 1
    assert "UUID '" in local_shard_creates[0]
    assert any(
        "INSERT INTO test_transfer_target (`month_date`, `users`) SELECT CAST(`month_date` AS Date)"
        in command
        and "FROM analytics_toolkit_transfer." in command
        and stage_identifier in command
        for command in map(_strip_query_label, target.commands)
    )

    assert f"DROP TABLE IF EXISTS {TARGET_TABLE} ON CLUSTER '{{cluster}}'" in map(
        _strip_query_label, target.commands
    )
    assert f"DROP TABLE IF EXISTS {TARGET_SHARD_TABLE} ON CLUSTER '{{cluster}}'" in map(
        _strip_query_label, target.commands
    )


def test_transfer_table_clickhouse_only_shard_creates_local_target(
    monkeypatch,
) -> None:
    source = FakeSourceConnection(rows=[(date(2024, 2, 1), 10)])
    target = FakeClickHouseClient()

    def fake_get_sql_connection(connection_key: str) -> object:
        if connection_key == "gp":
            return source
        if connection_key == "ch":
            return target
        raise AssertionError(f"Unexpected connection key: {connection_key}")

    monkeypatch.setattr(
        transfer_attempt_module,
        "get_sql_connection",
        fake_get_sql_connection,
    )
    monkeypatch.setattr(
        transfer_finalize_module,
        "get_sql_connection",
        fake_get_sql_connection,
    )

    transferred_rows = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="ch",
        from_sql="select month_date, users from source_table",
        to_table=TARGET_TABLE,
        write_mode="replace",
        retry_cnt=1,
        timeout_increment=0,
        full_retry_cnt=1,
        full_timeout_increment=0,
        ch_only_shard=True,
        partition_by=["month_date"],
        order_by=["month_date"],
    )

    assert transferred_rows == 1
    assert f"DROP TABLE IF EXISTS {TARGET_TABLE}" in map(
        _strip_query_label,
        target.commands,
    )
    assert any(
        command.startswith(f"CREATE TABLE IF NOT EXISTS {TARGET_TABLE}")
        and "ON CLUSTER '{cluster}'" in command
        for command in map(_strip_query_label, target.commands)
    )
    assert all(
        "ENGINE = Distributed(" not in command
        for command in map(_strip_query_label, target.commands)
    )
    assert all(TARGET_SHARD_TABLE not in command for command in target.commands)
    target_creates = [
        command
        for command in map(_strip_query_label, target.commands)
        if command.startswith(f"CREATE TABLE IF NOT EXISTS {TARGET_TABLE}")
    ]
    assert len(target_creates) == 2
    assert "ENGINE = ReplicatedMergeTree" in target_creates[0]
    assert "PARTITION BY `month_date`" in target_creates[0]
    assert "ORDER BY `month_date`" in target_creates[0]
    assert any("clusterAllReplicas" in query for query in target.queries)
    assert any(
        command.startswith(f"INSERT INTO {TARGET_TABLE} (`month_date`, `users`) ")
        for command in map(_strip_query_label, target.commands)
    )


def test_transfer_table_clickhouse_empty_missing_target_warns_and_skips_creation(
    monkeypatch,
) -> None:
    source = FakeSourceConnection(rows=[])
    target = FakeClickHouseClient()

    def fake_get_sql_connection(connection_key: str) -> object:
        if connection_key == "gp":
            return source
        if connection_key == "ch":
            return target
        raise AssertionError(f"Unexpected connection key: {connection_key}")

    monkeypatch.setattr(
        transfer_attempt_module,
        "get_sql_connection",
        fake_get_sql_connection,
    )
    monkeypatch.setattr(
        transfer_finalize_module,
        "get_sql_connection",
        fake_get_sql_connection,
    )

    with pytest.warns(
        UserWarning,
        match=(
            "Transfer source returned zero rows and target table "
            f"{TARGET_TABLE} does not exist; no target table was created."
        ),
    ):
        transferred_rows = transfer_api_module.transfer_table(
            from_db="gp",
            to_db="ch",
            from_sql="select month_date, users from source_table",
            to_table=TARGET_TABLE,
            retry_cnt=1,
            timeout_increment=0,
            full_retry_cnt=3,
            full_timeout_increment=0,
            partition_by=["month_date"],
            order_by=["month_date"],
        )

    assert transferred_rows == 0
    assert target.commands == []
    assert target.inserts == []
    assert target.created_tables == set()
    assert source.close_calls == 1
    assert target.close_calls >= 1
