from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

create_module = importlib.import_module("analytics_toolkit.sql.dml.table.create_table_from_sql")

ch_fast_path_module = importlib.import_module(
    "analytics_toolkit.sql.backends.ch.create_table_from_sql"
)

sql_module = importlib.import_module("analytics_toolkit.sql")


class FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.result_rows = rows


class FakeDbapiCursor:
    def __init__(self, connection: FakeDbapiConnection) -> None:
        self.connection = connection
        self.description = connection.description
        self.rowcount = -1
        self.close_calls = 0

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.connection.executed.append(sql)
        self.connection.executed_params.append(params)
        if sql.startswith("INSERT INTO "):
            self.rowcount = self.connection.insert_rowcount

    def fetchone(self) -> tuple[Any, ...] | None:
        return None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []

    def close(self) -> None:
        self.close_calls += 1


class FakeDbapiConnection:
    def __init__(
        self,
        description: list[tuple[Any, ...]] | None = None,
        insert_rowcount: int = 0,
    ) -> None:
        self.description = description or []
        self.insert_rowcount = insert_rowcount
        self.executed: list[str] = []
        self.executed_params: list[tuple[Any, ...] | None] = []
        self.cursors: list[FakeDbapiCursor] = []
        self.commit_calls = 0
        self.rollback_calls = 0
        self.close_calls = 0

    def cursor(self) -> FakeDbapiCursor:
        cursor = FakeDbapiCursor(self)
        self.cursors.append(cursor)
        return cursor

    def commit(self) -> None:
        self.commit_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class CloseFailDbapiConnection(FakeDbapiConnection):
    def close(self) -> None:
        self.close_calls += 1
        raise RuntimeError("connection close failed")


class FakeClickHouseClient:
    def __init__(self, insert_rowcount: int = 0) -> None:
        self.insert_rowcount = insert_rowcount
        self.commands: list[str] = []
        self.command_settings: list[dict[str, object] | None] = []
        self.queries: list[str] = []
        self.created_tables: set[str] = set()
        self.close_calls = 0

    def command(
        self,
        sql: str,
        settings: dict[str, object] | None = None,
    ) -> dict[str, int] | None:
        self.commands.append(sql)
        self.command_settings.append(settings)
        self._track_table_ddl(sql)
        if sql.startswith("INSERT INTO "):
            return {"written_rows": self.insert_rowcount}
        return None

    def query(self, sql: str) -> FakeResult:
        self.queries.append(sql)
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

    def close(self) -> None:
        self.close_calls += 1

    def _track_table_ddl(self, sql: str) -> None:
        body = _strip_query_label(sql)
        if body.startswith("CREATE TABLE IF NOT EXISTS "):
            table_name = body[len("CREATE TABLE IF NOT EXISTS ") :].split()[0]
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


def _strip_query_label(sql: str) -> str:
    stripped = sql.lstrip()
    if stripped.startswith("/* analytics_toolkit query_label=") and "*/" in stripped:
        return stripped.split("*/", 1)[1].lstrip()
    return stripped


SOURCE_DESCRIPTION = [
    ("id", 23, None, None, None, None),
    ("amount", 1700, None, None, 12, 2),
]


def _candidate_create_options(**overrides: object) -> Any:
    values: dict[str, object] = {
        "source_key": "gp",
        "source_backend": "gp",
        "target_key": "gp",
        "target_backend": "gp",
        "target_table": "sandbox.target",
        "source_sql": "SELECT id FROM source",
    }
    values.update(overrides)
    return create_module.CreateTableFromSqlOptions(**values)


__all__ = [
    "SOURCE_DESCRIPTION",
    "Any",
    "CloseFailDbapiConnection",
    "FakeClickHouseClient",
    "FakeDbapiConnection",
    "FakeDbapiCursor",
    "FakeResult",
    "Path",
    "SimpleNamespace",
    "_candidate_create_options",
    "_strip_query_label",
    "ch_fast_path_module",
    "create_module",
    "importlib",
    "pytest",
    "sql_module",
    "sys",
]
