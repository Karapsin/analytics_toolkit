from __future__ import annotations

import importlib
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit.sql.connection.errors import (
    SqlOperationContext,
    SqlOperationError,
    UnsupportedConnectionTypeError,
)

maintenance = importlib.import_module("analytics_toolkit.sql.dml.table.maintenance")

table_validation = importlib.import_module("analytics_toolkit.sql.dml.table.table_validation")

errors = importlib.import_module("analytics_toolkit.sql.connection.errors")

backend_utils = importlib.import_module("analytics_toolkit.sql.backends.utils")

backend_upsert = importlib.import_module("analytics_toolkit.sql.backends.upsert")

backend_dbapi = importlib.import_module("analytics_toolkit.sql.backends.dbapi")

backend_validation = importlib.import_module("analytics_toolkit.sql.backends.validation")


class LifecycleAdapter:
    def __init__(self, *, analyze: bool = True) -> None:
        self.analyze = analyze
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def should_analyze_table(self) -> bool:
        return self.analyze

    def analyze_table_sql(self, table_name: str, *, query_label: str | None) -> str:
        self._record("analyze_table_sql", table_name, query_label=query_label)
        return f"ANALYZE {table_name}"

    def analyze_table(
        self,
        connection: Any,
        table_name: str,
        *,
        query_label: str | None,
    ) -> None:
        self._record("analyze_table", connection, table_name, query_label=query_label)

    def vacuum_table(self, connection: Any, table_name: str, **kwargs: Any) -> None:
        self._record("vacuum_table", connection, table_name, **kwargs)

    def rollback_quietly(self, connection: Any) -> None:
        self._record("rollback_quietly", connection)

    def drop_table(self, connection: Any, table_name: str, **kwargs: Any) -> None:
        self._record("drop_table", connection, table_name, **kwargs)

    def wait_for_table_absence(
        self,
        connection: Any,
        table_name: str,
        **kwargs: Any,
    ) -> None:
        self._record("wait_for_table_absence", connection, table_name, **kwargs)

    def drop_table_with_options(self, connection: Any, table_name: str, **kwargs: Any) -> None:
        self._record("drop_table_with_options", connection, table_name, **kwargs)

    def build_clear_target_sqls(self, table_name: str, **kwargs: Any) -> list[str]:
        self._record("build_clear_target_sqls", table_name, **kwargs)
        return ["TRUNCATE shard", "TRUNCATE distributed"]

    def execute_commands(self, connection: Any, sqls: list[str]) -> None:
        self._record("execute_commands", connection, sqls)


class ValidationAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def build_stage_duplicate_keys_sql_for_tables(
        self,
        stage_tables: Any,
        key_columns: Any,
    ) -> str:
        self.calls.append(("build", (stage_tables, key_columns)))
        return "duplicate query"

    def query_has_rows(self, connection: Any, sql: str) -> bool:
        self.calls.append(("query", (connection, sql)))
        return True

    def stage_has_duplicate_keys(self, *args: Any) -> bool:
        self.calls.append(("duplicate", args))
        return False

    def stage_keys_overlap_target(self, *args: Any) -> bool:
        self.calls.append(("overlap", args))
        return True

    def null_safe_key_equality(self, *args: Any) -> str:
        self.calls.append(("equality", args))
        return "left.id IS NOT DISTINCT FROM right.id"


class RowCountResult:
    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


class InsertDbApiAdapter(backend_dbapi.DbApiBackendAdapter):
    def build_insert_from_query_sql(
        self,
        target_table: str,
        source_sql: str,
        column_types: Any,
    ) -> str:
        del column_types
        return f"INSERT INTO {target_table} {source_sql}"


class InsertCursor:
    rowcount = 4

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.closed = False

    def execute(self, _sql: str) -> None:
        if self.error is not None:
            raise self.error

    def close(self) -> None:
        self.closed = True


__all__ = [
    "Any",
    "InsertCursor",
    "InsertDbApiAdapter",
    "LifecycleAdapter",
    "RowCountResult",
    "SimpleNamespace",
    "SqlOperationContext",
    "SqlOperationError",
    "UnsupportedConnectionTypeError",
    "ValidationAdapter",
    "backend_dbapi",
    "backend_upsert",
    "backend_utils",
    "backend_validation",
    "date",
    "datetime",
    "errors",
    "importlib",
    "maintenance",
    "pytest",
    "table_validation",
    "timezone",
]
