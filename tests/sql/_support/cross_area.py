from __future__ import annotations

import importlib
import inspect
from typing import Any

import pandas as pd
import pytest

from tests.sql._support.fakes import (
    FakeClickHouseClient,
    FakeClickHouseResult,
    FakeDbapiConnection,
)

pytestmark = pytest.mark.filterwarnings("ignore:ch_cluster is deprecated:DeprecationWarning")

capabilities_module = importlib.import_module("analytics_toolkit.sql.core.capabilities")

identifiers_module = importlib.import_module("analytics_toolkit.sql.core.identifiers")

config_module = importlib.import_module("analytics_toolkit.sql.connection.config")

load_df_module = importlib.import_module("analytics_toolkit.sql.dml.load.load_df")

read_sql_module = importlib.import_module("analytics_toolkit.sql.dml.io.read_sql")

execute_sql_module = importlib.import_module("analytics_toolkit.sql.dml.io.execute_sql")

execute_read_module = importlib.import_module("analytics_toolkit.sql.dml.io.execute_read")

transfer_api_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.api")

models_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.runtime.models")

backend_registry_module = importlib.import_module("analytics_toolkit.sql.backends")

ddl_create_table_module = importlib.import_module("analytics_toolkit.sql.ddl.api")

ch_ctas_module = importlib.import_module("analytics_toolkit.sql.backends.ch.create_table_as")

operation_runner_module = importlib.import_module(
    "analytics_toolkit.sql.execution.operation_runner"
)

query_timing_module = importlib.import_module("analytics_toolkit.sql.execution.query_timing")

plans_module = importlib.import_module("analytics_toolkit.sql.execution.plans")

labels_module = importlib.import_module("analytics_toolkit.sql.execution.labels")

table_info_module = importlib.import_module("analytics_toolkit.sql.metadata.table_info")

sql_module = importlib.import_module("analytics_toolkit.sql")

cli_module = importlib.import_module("analytics_toolkit.cli")


class RoutingCursor:
    def __init__(self, connection: RoutingDbapiConnection) -> None:
        self.connection = connection
        self.rows: list[tuple[Any, ...]] = []
        self.close_calls = 0

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.connection.executed.append(sql)
        self.connection.executed_params.append(params)
        self.rows = self.connection.resolve(sql, params)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.rows)

    def close(self) -> None:
        self.close_calls += 1


class RoutingDbapiConnection:
    def __init__(
        self,
        resolver,
    ) -> None:
        self.resolver = resolver
        self.executed: list[str] = []
        self.executed_params: list[tuple[Any, ...] | None] = []
        self.close_calls = 0

    def cursor(self) -> RoutingCursor:
        return RoutingCursor(self)

    def resolve(
        self,
        sql: str,
        params: tuple[Any, ...] | None,
    ) -> list[tuple[Any, ...]]:
        return self.resolver(sql, params)

    def close(self) -> None:
        self.close_calls += 1


class InspectableClickHouseClient:
    def __init__(self, exists: bool = True) -> None:
        self.exists = exists
        self.queries: list[str] = []
        self.close_calls = 0

    def query(self, sql: str) -> FakeClickHouseResult:
        self.queries.append(sql)
        if sql.startswith("EXISTS TABLE "):
            return FakeClickHouseResult([(1 if self.exists else 0,)])
        if sql.startswith("DESCRIBE TABLE "):
            return FakeClickHouseResult(
                [
                    ("id", "UInt64"),
                    ("name", "String"),
                ]
            )
        if sql.startswith("SELECT count()"):
            return FakeClickHouseResult([(17,)])
        return FakeClickHouseResult([])

    def close(self) -> None:
        self.close_calls += 1


__all__ = [
    "Any",
    "FakeClickHouseClient",
    "FakeClickHouseResult",
    "FakeDbapiConnection",
    "InspectableClickHouseClient",
    "RoutingCursor",
    "RoutingDbapiConnection",
    "backend_registry_module",
    "capabilities_module",
    "ch_ctas_module",
    "cli_module",
    "config_module",
    "ddl_create_table_module",
    "execute_read_module",
    "execute_sql_module",
    "identifiers_module",
    "importlib",
    "inspect",
    "labels_module",
    "load_df_module",
    "models_module",
    "operation_runner_module",
    "pd",
    "plans_module",
    "pytest",
    "pytestmark",
    "query_timing_module",
    "read_sql_module",
    "sql_module",
    "table_info_module",
    "transfer_api_module",
]
