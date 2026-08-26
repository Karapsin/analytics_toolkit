from __future__ import annotations

import importlib
from datetime import date, datetime, timezone
from threading import Event, Lock
from time import sleep
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit.sql.connection.errors import (
    InvalidSqlInputError,
    UnsupportedConnectionTypeError,
)

from tests.sql._support.fakes import FakeDbapiConnection, FakeDbapiCursor

sql_module = importlib.import_module("analytics_toolkit.sql")

dml_module = importlib.import_module("analytics_toolkit.sql.dml")

dml_table_module = importlib.import_module("analytics_toolkit.sql.dml.table")

table_ops_module = importlib.import_module("analytics_toolkit.sql.dml.table.partitions")

gp_maintenance_module = importlib.import_module(
    "analytics_toolkit.sql.backends.gp.partition_maintenance"
)


def _stub_leaf_partition_discovery(
    monkeypatch: pytest.MonkeyPatch,
    partition_names: list[str],
) -> None:
    rows = [
        dict(zip(("schema_name", "relation_name"), name.split(".", 1))) for name in partition_names
    ]
    read_sql_module = importlib.import_module("analytics_toolkit.sql.dml.io.read_sql")
    monkeypatch.setattr(
        read_sql_module,
        "read_sql",
        lambda *_args, **_kwargs: pd.DataFrame(rows),
    )


class _FailingOnceGpCursor(FakeDbapiCursor):
    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        super().execute(sql, params)
        if self.connection.fail_first_execute:
            self.connection.fail_first_execute = False
            raise RuntimeError("temporary failure")


class _FailingOnceGpConnection(FakeDbapiConnection):
    def __init__(self, fail_first_execute: bool = True) -> None:
        super().__init__()
        self.fail_first_execute = fail_first_execute

    def cursor(self) -> _FailingOnceGpCursor:
        return _FailingOnceGpCursor(self)


__all__ = [
    "Any",
    "Event",
    "FakeDbapiConnection",
    "FakeDbapiCursor",
    "InvalidSqlInputError",
    "Lock",
    "SimpleNamespace",
    "UnsupportedConnectionTypeError",
    "_FailingOnceGpConnection",
    "_FailingOnceGpCursor",
    "_stub_leaf_partition_discovery",
    "date",
    "datetime",
    "dml_module",
    "dml_table_module",
    "gp_maintenance_module",
    "importlib",
    "pd",
    "pytest",
    "sleep",
    "sql_module",
    "table_ops_module",
    "timezone",
]
