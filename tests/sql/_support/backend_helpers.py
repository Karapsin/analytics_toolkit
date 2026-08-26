from __future__ import annotations

import builtins
import importlib
import sys
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pandas as pd
import pytest
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp

ch_metadata = importlib.import_module("analytics_toolkit.sql.backends.ch.metadata")

ch_operations = importlib.import_module("analytics_toolkit.sql.backends.ch.operations")

ch_wait = importlib.import_module("analytics_toolkit.sql.backends.ch.wait")

gp_adapter_module = importlib.import_module("analytics_toolkit.sql.backends.gp.adapter")

gp_ddl = importlib.import_module("analytics_toolkit.sql.backends.gp.ddl")

gp_insert = importlib.import_module("analytics_toolkit.sql.backends.gp.insert")

gp_operations = importlib.import_module("analytics_toolkit.sql.backends.gp.operations")

trino_operations = importlib.import_module("analytics_toolkit.sql.backends.trino.operations")

trino_parquet = importlib.import_module("analytics_toolkit.sql.backends.trino.parquet_stage")


class QueryResult:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.result_rows = rows


class RecordingCursor:
    def __init__(
        self,
        rows: list[tuple[Any, ...]] | None = None,
        *,
        fail_on: str | None = None,
    ) -> None:
        self.rows = rows or []
        self.fail_on = fail_on
        self.executed: list[tuple[str, Any]] = []
        self.closed = False
        self.description = [("value",)]

    def __enter__(self) -> RecordingCursor:  # noqa: PYI034
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))
        if self.fail_on is not None and self.fail_on in sql:
            message = "query failed"
            raise RuntimeError(message)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def close(self) -> None:
        self.closed = True


class RecordingConnection:
    def __init__(self, cursor: RecordingCursor | None = None) -> None:
        self.cursor_instance = cursor or RecordingCursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> RecordingCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class RoutingClickHouseConnection:
    def __init__(self, route: Any) -> None:
        self.route = route
        self.queries: list[str] = []

    def query(self, sql: str) -> QueryResult:
        self.queries.append(sql)
        value = self.route(sql)
        if isinstance(value, Exception):
            raise value
        return QueryResult(value)


__all__ = [
    "UUID",
    "Any",
    "InvalidSqlInputError",
    "QueryResult",
    "RecordingConnection",
    "RecordingCursor",
    "RoutingClickHouseConnection",
    "SimpleNamespace",
    "builtins",
    "ch_metadata",
    "ch_operations",
    "ch_wait",
    "exp",
    "gp_adapter_module",
    "gp_ddl",
    "gp_insert",
    "gp_operations",
    "importlib",
    "pd",
    "pytest",
    "sys",
    "trino_operations",
    "trino_parquet",
]
