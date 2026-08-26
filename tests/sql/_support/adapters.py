from __future__ import annotations

import importlib
import inspect
import threading
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import (
    BACKEND_ADAPTERS,
    BACKEND_REGISTRY,
    get_backend,
    get_backend_adapter,
    get_backend_names,
)
from analytics_toolkit.sql.backends.base import BackendAdapter
from analytics_toolkit.sql.backends.gp.adapter import GP_IDENTIFIER_MAX_BYTES
from analytics_toolkit.sql.backends.models import (
    SourceColumn,
    StageFinalizationRequest,
    StageTargetTableRequest,
    TargetWriteModeRequest,
)
from analytics_toolkit.sql.backends.registry import (
    backend_capability_map,
    get_backend_capability,
    normalize_backend_name,
    require_backend_name,
    supported_backend_message,
)
from analytics_toolkit.sql.connection.errors import (
    InvalidSqlInputError,
    SqlConfigError,
    UnsupportedConnectionTypeError,
)

from tests.sql._support.fakes import FakeClickHouseResult, FakeDbapiConnection

sql_module = importlib.import_module("analytics_toolkit.sql")

read_sql_module = importlib.import_module("analytics_toolkit.sql.dml.io.read_sql")

execute_sql_module = importlib.import_module("analytics_toolkit.sql.dml.io.execute_sql")

execute_read_module = importlib.import_module("analytics_toolkit.sql.dml.io.execute_read")

load_sql_table_module = importlib.import_module("analytics_toolkit.sql.dml.load.load_sql_table")

table_ops_module = importlib.import_module("analytics_toolkit.sql.dml.table.api")

table_basic_ops_module = importlib.import_module("analytics_toolkit.sql.dml.table._basic_ops")

ch_lifecycle_module = importlib.import_module("analytics_toolkit.sql.backends.ch.lifecycle")

ch_backend_wait_module = importlib.import_module("analytics_toolkit.sql.backends.ch.wait")

backend_registry_module = importlib.import_module("analytics_toolkit.sql.backends.registry")

backend_validation_module = importlib.import_module("analytics_toolkit.sql.backends.validation")

backend_source_count_module = importlib.import_module("analytics_toolkit.sql.backends.source_count")

backend_common_methods_module = importlib.import_module(
    "analytics_toolkit.sql.backends.common_methods"
)

gp_stage_module = importlib.import_module("analytics_toolkit.sql.backends.gp.stage")

adapter_defaults_module = importlib.import_module("analytics_toolkit.sql.backends.adapter_defaults")

trino_adapter_module = importlib.import_module("analytics_toolkit.sql.backends.trino.adapter")

ch_adapter_module = importlib.import_module("analytics_toolkit.sql.backends.ch.adapter")

ch_lifecycle_backend_module = ch_lifecycle_module

ch_ddl_backend_module = importlib.import_module("analytics_toolkit.sql.backends.ch.ddl")

ch_insert_backend_module = importlib.import_module("analytics_toolkit.sql.backends.ch.insert")

ch_operations_backend_module = importlib.import_module(
    "analytics_toolkit.sql.backends.ch.operations"
)

ch_queries_backend_module = importlib.import_module("analytics_toolkit.sql.backends.ch.queries")

ch_target_create_backend_module = importlib.import_module(
    "analytics_toolkit.sql.backends.ch.target_create"
)

ch_upsert_backend_module = importlib.import_module("analytics_toolkit.sql.backends.ch.upsert")


class RecordingClickHouseClient:
    def __init__(self) -> None:
        self.commands: list[tuple[str, dict[str, object] | None]] = []
        self.queries: list[str] = []

    def command(
        self,
        sql: str,
        settings: dict[str, object] | None = None,
    ) -> dict[str, int] | None:
        self.commands.append((sql, settings))
        if sql.startswith("INSERT INTO "):
            return {"written_rows": 3}
        return None

    def query(self, sql: str) -> FakeClickHouseResult:
        self.queries.append(sql)
        if sql.startswith("EXISTS TABLE "):
            return FakeClickHouseResult([(1,)])
        if sql.startswith("SELECT count()"):
            return FakeClickHouseResult([(9,)])
        if sql.startswith("DESCRIBE TABLE "):
            return FakeClickHouseResult([("id", "Nullable(Int64)")])
        return FakeClickHouseResult([])


class _SourceCountCursor:
    def __init__(
        self,
        *,
        fetchone: object | None = None,
        fetchall: object | None = None,
        rows: list[tuple[int, ...]] | None = None,
    ) -> None:
        if fetchone is not None:
            self.fetchone = fetchone
        if fetchall is not None:
            self.fetchall = fetchall
        if rows is not None:
            self._rows = rows


class TrinoRecordingCursor:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.executed: list[str] = []
        self.description = [("answer",)]
        self.closed = False

    def execute(self, sql: str, _params: Any = None) -> None:
        self.executed.append(sql)
        if self.fail_on and self.fail_on in sql:
            message = "trino query failed"
            raise RuntimeError(message)

    def fetchall(self) -> list[tuple[int]]:
        return [(7,)]

    def close(self) -> None:
        self.closed = True


class MinimalContractAdapter(BackendAdapter):
    backend = "minimal"
    display_name = "Minimal"
    sqlglot_dialect = "postgres"
    identifier_quote = '"'
    supports_transactions = False
    supports_analyze = True
    supports_distributed_tables = False
    truncate_semantics = "truncate"
    drop_semantics = "drop"
    create_semantics = "create"
    type_family = "test"


__all__ = [
    "BACKEND_ADAPTERS",
    "BACKEND_REGISTRY",
    "GP_IDENTIFIER_MAX_BYTES",
    "UUID",
    "Any",
    "BackendAdapter",
    "Decimal",
    "FakeClickHouseResult",
    "FakeDbapiConnection",
    "InvalidSqlInputError",
    "MinimalContractAdapter",
    "RecordingClickHouseClient",
    "SimpleNamespace",
    "SourceColumn",
    "SqlConfigError",
    "StageFinalizationRequest",
    "StageTargetTableRequest",
    "TargetWriteModeRequest",
    "TrinoRecordingCursor",
    "UnsupportedConnectionTypeError",
    "_SourceCountCursor",
    "adapter_defaults_module",
    "backend_capability_map",
    "backend_common_methods_module",
    "backend_registry_module",
    "backend_source_count_module",
    "backend_validation_module",
    "ch_adapter_module",
    "ch_backend_wait_module",
    "ch_ddl_backend_module",
    "ch_insert_backend_module",
    "ch_lifecycle_backend_module",
    "ch_lifecycle_module",
    "ch_operations_backend_module",
    "ch_queries_backend_module",
    "ch_target_create_backend_module",
    "ch_upsert_backend_module",
    "date",
    "datetime",
    "execute_read_module",
    "execute_sql_module",
    "get_backend",
    "get_backend_adapter",
    "get_backend_capability",
    "get_backend_names",
    "gp_stage_module",
    "importlib",
    "inspect",
    "load_sql_table_module",
    "normalize_backend_name",
    "pd",
    "pytest",
    "read_sql_module",
    "require_backend_name",
    "sql_module",
    "supported_backend_message",
    "table_basic_ops_module",
    "table_ops_module",
    "threading",
    "trino_adapter_module",
]
