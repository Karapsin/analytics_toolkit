from __future__ import annotations

import datetime as dt
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import get_backend_adapter

schema_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.schema")

stage_module = importlib.import_module("analytics_toolkit.sql.dml.load.stage")

gp_adapter_module = importlib.import_module("analytics_toolkit.sql.backends.gp.adapter")

transfer_stage_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.stage")

transfer_finalize_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.flow.finalize"
)

runtime_models = importlib.import_module("analytics_toolkit.sql.dml.transfer.runtime.models")

retry_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.runtime.retry")

table_basic_module = importlib.import_module("analytics_toolkit.sql.dml.table._basic_ops")

identifiers_module = importlib.import_module("analytics_toolkit.sql.core.identifiers")


class FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.result_rows = rows


class FakeClickHouseClient:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def query(self, sql: str) -> FakeResult:
        self.queries.append(sql)
        if sql == "DESCRIBE TABLE (select id, amount from source)":
            return FakeResult(
                [
                    ("id", "UInt64"),
                    ("amount", "Nullable(Decimal(18, 4))"),
                ]
            )
        if sql == "DESCRIBE TABLE target":
            return FakeResult(
                [
                    ("id", "Nullable(Int64)"),
                    ("amount", "Nullable(Decimal(18, 4))"),
                ]
            )
        return FakeResult([])


class FakeDbapiCursor:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.closed = False
        self.description = [
            ("id", 23, None, None, None, None),
            ("amount", 1700, None, None, 12, 2),
        ]

    def execute(self, sql: str) -> None:
        self.executed.append(sql)

    def close(self) -> None:
        self.closed = True


class FakeDbapiConnection:
    def __init__(self) -> None:
        self.cursor_obj = FakeDbapiCursor()

    def cursor(self) -> FakeDbapiCursor:
        return self.cursor_obj


__all__ = [
    "Any",
    "FakeClickHouseClient",
    "FakeDbapiConnection",
    "FakeDbapiCursor",
    "FakeResult",
    "Path",
    "SimpleNamespace",
    "dt",
    "get_backend_adapter",
    "gp_adapter_module",
    "identifiers_module",
    "importlib",
    "pd",
    "pytest",
    "retry_module",
    "runtime_models",
    "schema_module",
    "stage_module",
    "sys",
    "table_basic_module",
    "transfer_finalize_module",
    "transfer_stage_module",
]
