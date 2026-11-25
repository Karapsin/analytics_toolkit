"""Native CTAS client, singleton scope and temporary connection setup."""

from __future__ import annotations

import importlib
import json
from contextlib import nullcontext
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit.general import set_connections_path

from tests.sql._support.assertions import strip_query_label as _strip_query_label
from tests.sql._support.ch_drop_scope import DropScopeState, _DropConnection

ch_ctas_module = importlib.import_module("analytics_toolkit.sql.backends.ch.create_table_as")


TARGET_TABLE = "default.events_result"
TARGET_SHARD_TABLE = "default.events_result_shard"
QUERY = """
SELECT
    dt,
    id,
    amount
FROM default.events_source
WHERE amount > 0
""".strip()
CTE_JOIN_QUERY = """
WITH trigger_map AS (
    SELECT 1 AS id
)
SELECT
    events.id,
    trigger_map.kind
FROM default.events_source AS events
LEFT JOIN trigger_map AS trigger_map
    ON events.id = trigger_map.id
""".strip()


class FakeClickHouseResult:
    def __init__(
        self,
        result_rows: list[tuple[Any, ...]],
        column_names: tuple[str, ...] = (),
        column_types: tuple[Any, ...] = (),
    ) -> None:
        self.result_rows = result_rows
        self.column_names = column_names
        self.column_types = column_types


class FakeDatabaseError(Exception):
    pass


class _SingletonDropScopeState(DropScopeState):
    def insert_rows(self, host, table, rows):
        value = self.host_tables[host][table]
        if value.route is not None:
            cluster, physical = value.route
            hosts = self.clusters[cluster]
            if len(hosts) == 1:
                return super().insert_rows(hosts[0], physical, rows)
        return super().insert_rows(host, table, rows)


class FakeClickHouseClient:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.command_settings: list[dict[str, object] | None] = []
        self.queries: list[str] = []
        self.created_tables: set[str] = set()
        self.close_calls = 0
        self.insert_error: Exception | None = None
        self.schema_query_error: Exception | None = None
        self.table_ddls: dict[str, str] = {}
        self.data_inserts: list[tuple[str, list[str], list[tuple[Any, ...]]]] = []
        self.ownership_connection: _DropConnection | None = None

    def command(
        self,
        sql: str,
        settings: dict[str, object] | None = None,
    ) -> None:
        self.commands.append(sql)
        self.command_settings.append(settings)
        self._track_table_ddl(sql)
        body = _strip_query_label(sql)
        if self.ownership_connection is not None and body.startswith(
            ("CREATE TABLE", "DROP TABLE")
        ):
            self.ownership_connection.command(sql, settings=settings)
        if self.insert_error is not None and sql.startswith("INSERT INTO"):
            raise self.insert_error

    def query(self, sql: str, **_kwargs: Any) -> FakeClickHouseResult:
        self.queries.append(sql)
        body = _strip_query_label(sql)
        if body.startswith("SELECT getMacro("):
            return FakeClickHouseResult([("core",)])
        if body.startswith("SELECT *\nFROM (\n"):
            if self.schema_query_error is not None:
                raise self.schema_query_error
            return FakeClickHouseResult(
                [],
                column_names=("dt", "id", "amount"),
                column_types=(
                    type("FakeType", (), {"name": "Date"})(),
                    type("FakeType", (), {"name": "UInt64"})(),
                    type("FakeType", (), {"name": "Decimal(18, 4)"})(),
                ),
            )
        if body.startswith("DESCRIBE TABLE ("):
            if self.schema_query_error is not None:
                raise self.schema_query_error
            return FakeClickHouseResult(
                [("dt", "Date"), ("id", "UInt64"), ("amount", "Decimal(18, 4)")]
            )
        if self.ownership_connection is not None:
            result = self.ownership_connection.query(sql, **_kwargs)
            return FakeClickHouseResult(
                result.result_rows,
                column_names=result.column_names,
                column_types=result.column_types,
            )
        return self._legacy_query(body)

    def _legacy_query(self, body: str) -> FakeClickHouseResult:
        if body.startswith("SHOW CREATE TABLE "):
            name = body.removeprefix("SHOW CREATE TABLE ").replace("`", "")
            return FakeClickHouseResult([(self.table_ddls[name],)])
        if body.startswith("SELECT create_table_query FROM clusterAllReplicas("):
            return FakeClickHouseResult([(self.table_ddls[TARGET_SHARD_TABLE],)])
        return self._metadata_result(body)

    def _metadata_result(self, sql: str) -> FakeClickHouseResult:
        if "clusterAllReplicas" in sql and "system, one" in sql:
            return FakeClickHouseResult([(1,)])
        if "FROM system.clusters" in sql:
            return FakeClickHouseResult([(1,)])
        if "clusterAllReplicas" in sql and "system, tables" in sql:
            return FakeClickHouseResult([(self._cluster_table_count(sql),)])
        if "clusterAllReplicas" in sql and "system, columns" in sql:
            return FakeClickHouseResult([(sql.count("name = ") or 1,)])
        if "clusterAllReplicas" in sql:
            return FakeClickHouseResult([(1,)])
        if sql.startswith("EXISTS TABLE "):
            table_name = sql.removeprefix("EXISTS TABLE ").strip().replace("`", "")
            return FakeClickHouseResult([(int(table_name in self.created_tables),)])
        message = f"Unexpected query: {sql}"
        raise AssertionError(message)

    def query_df_stream(self, query: str) -> Any:
        self.queries.append(query)
        frame = pd.DataFrame({"dt": [date(2026, 1, 1)], "id": [1], "amount": [Decimal("1.2500")]})
        return nullcontext(iter([frame]))

    def insert_df(self, table: str, df: pd.DataFrame, column_names: list[str]) -> None:
        self.data_inserts.append((table, column_names, list(df.itertuples(index=False, name=None))))
        if self.insert_error is not None:
            raise self.insert_error
        if self.ownership_connection is not None:
            self.ownership_connection.insert(table, self.data_inserts[-1][2], column_names)

    def insert(self, table: str, data: Any, column_names: list[str], **_kwargs: Any) -> None:
        self.data_inserts.append((table, column_names, [tuple(row) for row in data]))
        if self.insert_error is not None:
            raise self.insert_error
        if self.ownership_connection is not None:
            self.ownership_connection.insert(table, self.data_inserts[-1][2], column_names)

    def close(self) -> None:
        self.close_calls += 1
        if self.ownership_connection is not None:
            self.ownership_connection.close()

    def _track_table_ddl(self, sql: str) -> None:
        body = _strip_query_label(sql)
        for prefix in ("CREATE TABLE IF NOT EXISTS ", "CREATE OR REPLACE TABLE ", "CREATE TABLE "):
            if body.startswith(prefix):
                name = body[len(prefix) :].split()[0].replace("`", "")
                self.created_tables.add(name)
                self.table_ddls[name] = "CREATE TABLE " + body[len(prefix) :]
                return
        if body.startswith("DROP TABLE IF EXISTS "):
            name = body.removeprefix("DROP TABLE IF EXISTS ").split()[0].replace("`", "")
            self.created_tables.discard(name)
            self.table_ddls.pop(name, None)

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


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeClickHouseClient:
    path = Path(".connections")
    config = json.loads(path.read_text())
    config["ch"]["transfer_staging_schema"] = None
    path.write_text(json.dumps(config))
    set_connections_path(path)
    client = FakeClickHouseClient()
    monkeypatch.setattr(
        ch_ctas_module,
        "get_sql_connection",
        lambda connection_key: client,
    )
    return client
