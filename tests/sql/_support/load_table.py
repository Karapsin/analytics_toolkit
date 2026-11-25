from __future__ import annotations

import importlib
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.identifiers import quote_identifier
from analytics_toolkit.sql.core.identifiers import TableIdentifier
from sqlglot import exp, parse_one

from tests.sql._support.fakes import FakeDbapiConnection

CURRENT_DT = datetime.now(tz=timezone.utc).astimezone().strftime("%Y%m%d")

TEST_CH_TABLE = f"test_table_{CURRENT_DT}"

TEST_CH_SHARD_TABLE = f"test_table_{CURRENT_DT}_shard"

TEST_CH_STAGE_TABLE = f"test_table_{CURRENT_DT}__stage__abcd1234"

TEST_CH_SHARD_RELATION = f"test_table_{CURRENT_DT}_shard"


def _table_key(parts: Any) -> str:
    return ".".join(part if part.isidentifier() else quote_identifier(part) for part in parts)


def _raw_table_key(name: str) -> str:
    return _table_key(TableIdentifier.parse(name, "ch").parts)


create_sql_table_module = importlib.import_module("analytics_toolkit.sql.ddl.api")

ch_wait_module = importlib.import_module("analytics_toolkit.sql.backends.ch.wait")

load_sql_table_module = importlib.import_module("analytics_toolkit.sql.dml.load.load_sql_table")

gp_insert_module = importlib.import_module("analytics_toolkit.sql.backends.gp.insert")

trino_insert_module = importlib.import_module("analytics_toolkit.sql.backends.trino.insert")

load_df_module = importlib.import_module("analytics_toolkit.sql.dml.load.load_df")

parquet_stage_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.flow.parquet_stage"
)

table_ops_module = importlib.import_module("analytics_toolkit.sql.dml.table.write_modes")


def _write_trino_connections(
    write_sql_connections: Any,
    *,
    s3_transfer_staging_location: str | None,
    s3_transfer_staging_schema: str | None = "hive.pa_core_stage",
    ddl_defaults: dict[str, object] | None = None,
) -> None:
    config: dict[str, object] = {
        "type": "trino",
        "host": "trino.example",
        "port": 8080,
        "user": "target_user",
        "password": "password",
        "catalog": "iceberg",
        "schema": "sandbox",
        "transfer_staging_schema": "object_storage.pa_core_stage",
        "upsert_partition_drop_sql_template": (
            "ALTER TABLE {table} DROP PARTITION ({partition_column} = {partition_value})"
        ),
    }
    if s3_transfer_staging_location is not None:
        config["s3_transfer_staging_location"] = s3_transfer_staging_location
    if s3_transfer_staging_location is not None and s3_transfer_staging_schema is not None:
        config["s3_transfer_staging_schema"] = s3_transfer_staging_schema
    if ddl_defaults is not None:
        config["ddl_defaults"] = ddl_defaults
    write_sql_connections({"trino_stage": config})


class FakeClickHouseClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.commands: list[str] = []
        self.command_settings: list[dict[str, Any]] = []
        self.queries: list[str] = []
        self.created_tables: set[str] = set()
        self.close_calls = 0
        self.table_ddls: dict[str, str] = {}

    def command(self, sql: str, settings: dict[str, Any] | None = None) -> None:
        self.commands.append(sql)
        self.command_settings.append(dict(settings or {}))
        self._track_table_ddl(sql)

    def query(self, sql: str) -> object:
        self.queries.append(sql)
        if sql.startswith("SELECT create_table_query FROM clusterAllReplicas("):
            rows = [
                (
                    f"CREATE TABLE default.{TEST_CH_SHARD_TABLE} (id UInt64, score Int64) "
                    "ENGINE=MergeTree ORDER BY id",
                )
            ]
        elif sql.startswith("SHOW CREATE TABLE "):
            rows = [(self._show_create_ddl(sql),)]
        elif (metadata_rows := self._metadata_rows(sql)) is not None:
            rows = metadata_rows
        elif "clusterAllReplicas" in sql and "system, tables" in sql:
            rows = [(self._cluster_table_count(sql),)]
        elif "clusterAllReplicas" in sql and "system, columns" in sql:
            rows = [(sql.count("name = ") or 1,)]
        elif sql.startswith("EXISTS TABLE "):
            table_name = _raw_table_key(sql[len("EXISTS TABLE ") :].strip())
            rows = [(int(table_name in self.created_tables),)]
        else:
            rows = [(1,)]
        return type("FakeResult", (), {"result_rows": rows})()

    def _show_create_ddl(self, sql: str) -> str:
        table = sql.removeprefix("SHOW CREATE TABLE ").replace("`", "")
        ddl = self.table_ddls.get(table)
        if ddl is not None:
            return ddl
        columns = "(id UInt64, score Int64)"
        if table.endswith("_shard"):
            return f"CREATE TABLE {table} {columns} ENGINE = MergeTree ORDER BY id"
        relation = table.rsplit(".", 1)[-1]
        return (
            f"CREATE TABLE {table} {columns} ENGINE = "
            f"Distributed('core', 'default', '{relation}_shard', rand())"
        )

    @staticmethod
    def _metadata_rows(sql: str) -> list[tuple[object, ...]] | None:
        if sql == "SELECT currentDatabase()":
            return [("default",)]
        if "system, databases" in sql or "system.databases" in sql:
            return [("Ordinary",)]
        if sql.startswith("SELECT getMacro("):
            return [("core",)]
        if "clusterAllReplicas" in sql and "system, one" in sql:
            return [(1,)]
        if "FROM system.clusters" in sql:
            return [(1,)]
        return None

    def insert_df(
        self,
        table: str,
        df: pd.DataFrame,
        column_names: list[str],
    ) -> None:
        self.calls.append(
            {
                "table": table,
                "df": df.copy(),
                "column_names": list(column_names),
            }
        )

    def insert(
        self,
        table: str,
        data: list[tuple[object, ...]],
        column_names: list[str],
        column_type_names: list[str] | None = None,
    ) -> None:
        self.calls.append(
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
        if not sql.lstrip().upper().startswith(("CREATE", "DROP")):
            return
        statement = parse_one(sql, read=ClickHouseDialect)
        if (
            not isinstance(statement, (exp.Create, exp.Drop))
            or statement.args.get("kind") != "TABLE"
        ):
            return
        if isinstance(statement, exp.Drop):
            tables = list(statement.find_all(exp.Table))
            assert tables
            for table in tables:
                self.created_tables.discard(_table_key(part.name for part in table.parts))
            return
        table = statement.this
        if isinstance(table, exp.Schema):
            table = table.this
        assert isinstance(table, exp.Table)
        table_name = _table_key(part.name for part in table.parts)
        self.created_tables.add(table_name)

    def _cluster_table_count(self, sql: str) -> int:
        marker = "AND name = '"
        if marker not in sql:
            return len(self.created_tables)
        relation_name = sql.split(marker, 1)[1].split("'", 1)[0]
        return sum(
            1
            for table_name in self.created_tables
            if TableIdentifier.parse(table_name, "ch").relation == relation_name
        )


__all__ = [
    "CURRENT_DT",
    "TEST_CH_SHARD_RELATION",
    "TEST_CH_SHARD_TABLE",
    "TEST_CH_STAGE_TABLE",
    "TEST_CH_TABLE",
    "UUID",
    "Any",
    "Decimal",
    "FakeClickHouseClient",
    "FakeDbapiConnection",
    "Path",
    "SimpleNamespace",
    "_write_trino_connections",
    "ch_wait_module",
    "create_sql_table_module",
    "date",
    "gp_insert_module",
    "importlib",
    "load_df_module",
    "load_sql_table_module",
    "parquet_stage_module",
    "pd",
    "pytest",
    "sys",
    "table_ops_module",
    "trino_insert_module",
]
