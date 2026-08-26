from __future__ import annotations

import builtins
import importlib
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.connection.errors import SqlConfigError

trino_insert = importlib.import_module("analytics_toolkit.sql.backends.trino.insert")

ch_source_count = importlib.import_module("analytics_toolkit.sql.backends.ch.source_count")

ch_source_schema = importlib.import_module("analytics_toolkit.sql.backends.ch.source_schema")

connection_config = importlib.import_module("analytics_toolkit.sql.connection.config")

source_schema = importlib.import_module("analytics_toolkit.sql.backends.source_schema")

gp_config = importlib.import_module("analytics_toolkit.sql.backends.gp.config")


class FakeCursor:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, list[Any]]] = []
        self.closed = False

    def execute(self, sql: str, params: list[Any]) -> None:
        self.calls.append((sql, params))
        if self.error is not None:
            raise self.error

    def close(self) -> None:
        self.closed = True


class FakeTrinoAdapter:
    backend = "trino"

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...], int, str | None]] = []

    def build_dataframe_batch_insert_sql(
        self,
        table_name: str,
        columns: Any,
        *,
        row_count: int,
        query_label: str | None,
    ) -> str:
        self.calls.append((table_name, tuple(columns), row_count, query_label))
        return f"INSERT INTO {table_name} VALUES " + ", ".join(["(?, ?)"] * row_count)


class FakeClickHouseSourceAdapter:
    sqlglot_dialect = "clickhouse"

    @staticmethod
    def strip_query_semicolon(sql: str) -> str:
        return sql.strip().rstrip(";")

    @staticmethod
    def build_source_count_sql(source_sql: str, *, query_label: str | None) -> str:
        return f"COUNT {source_sql} {query_label or ''}".strip()


__all__ = [
    "Any",
    "FakeClickHouseSourceAdapter",
    "FakeCursor",
    "FakeTrinoAdapter",
    "SimpleNamespace",
    "SourceColumn",
    "SqlConfigError",
    "builtins",
    "ch_source_count",
    "ch_source_schema",
    "connection_config",
    "date",
    "datetime",
    "gp_config",
    "importlib",
    "pd",
    "pytest",
    "source_schema",
    "timezone",
    "trino_insert",
]
