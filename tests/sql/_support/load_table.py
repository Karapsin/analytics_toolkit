from __future__ import annotations

import importlib
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pandas as pd
import pytest

from tests.sql._support.fakes import FakeDbapiConnection

CURRENT_DT = date.today().strftime("%Y%m%d")

TEST_CH_TABLE = f"test_table_{CURRENT_DT}"

TEST_CH_SHARD_TABLE = f"test_table_{CURRENT_DT}_shard"

TEST_CH_STAGE_TABLE = f"test_table_{CURRENT_DT}__stage__abcd1234"

TEST_CH_SHARD_RELATION = f"test_table_{CURRENT_DT}_shard"

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
        self.queries: list[str] = []
        self.created_tables: set[str] = set()
        self.close_calls = 0

    def command(self, sql: str) -> None:
        self.commands.append(sql)
        self._track_table_ddl(sql)

    def query(self, sql: str) -> object:
        self.queries.append(sql)
        if sql.startswith("SELECT getMacro("):
            return type("FakeResult", (), {"result_rows": [("core",)]})()
        if "clusterAllReplicas" in sql and "system, one" in sql:
            return type("FakeResult", (), {"result_rows": [(1,)]})()
        if "FROM system.clusters" in sql:
            return type("FakeResult", (), {"result_rows": [(1,)]})()
        if "clusterAllReplicas" in sql and "system, tables" in sql:
            return type(
                "FakeResult",
                (),
                {"result_rows": [(self._cluster_table_count(sql),)]},
            )()
        if "clusterAllReplicas" in sql and "system, columns" in sql:
            return type(
                "FakeResult",
                (),
                {"result_rows": [(sql.count("name = ") or 1,)]},
            )()
        if sql.startswith("EXISTS TABLE "):
            table_name = sql[len("EXISTS TABLE ") :].strip()
            return type(
                "FakeResult",
                (),
                {"result_rows": [(int(table_name in self.created_tables),)]},
            )()
        return type("FakeResult", (), {"result_rows": [(1,)]})()

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
        if sql.startswith("CREATE TABLE IF NOT EXISTS "):
            table_name = sql[len("CREATE TABLE IF NOT EXISTS ") :].split(maxsplit=1)[0]
            self.created_tables.add(table_name)
            return
        if sql.startswith("CREATE TABLE "):
            table_name = sql[len("CREATE TABLE ") :].split(maxsplit=1)[0]
            self.created_tables.add(table_name)
            return
        if sql.startswith("DROP TABLE IF EXISTS "):
            table_name = sql[len("DROP TABLE IF EXISTS ") :].split(maxsplit=1)[0]
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
