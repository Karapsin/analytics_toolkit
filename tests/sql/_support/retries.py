from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pandas as pd
import pytest
from analytics_toolkit.sql.connection.errors import SqlConfigError

execute_sql_module = importlib.import_module("analytics_toolkit.sql.dml.io.execute_sql")

execute_read_module = importlib.import_module("analytics_toolkit.sql.dml.io.execute_read")

read_sql_module = importlib.import_module("analytics_toolkit.sql.dml.io.read_sql")

load_df_module = importlib.import_module("analytics_toolkit.sql.dml.load.load_df")

retry_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.runtime.retry")

operation_runner_module = importlib.import_module(
    "analytics_toolkit.sql.execution.operation_runner"
)


class FakeConnection:
    def __init__(self, name: str) -> None:
        self.name = name
        self.close_calls = 0
        self.rollback_calls = 0

    def close(self) -> None:
        self.close_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1


class DatabaseError(Exception):
    pass


class FakeUndefinedTableError(Exception):
    pgcode = "42P01"


class FakeUndefinedObjectError(Exception):
    pgcode = "42704"


class AmbiguousColumn(Exception):
    pgcode = "42702"


class FakeTrinoSyntaxError(Exception):
    error_name = "SYNTAX_ERROR"


class FakeTrinoTypeMismatchError(Exception):
    error_name = "TYPE_MISMATCH"


class InsufficientPrivilege(Exception):
    pgcode = "42501"


class GroupingError(Exception):
    pgcode = "42803"


class FeatureNotSupported(Exception):
    pgcode = "0A000"


class CloseFailureConnection(FakeConnection):
    def close(self) -> None:
        self.close_calls += 1
        message = f"cannot close {self.name}"
        raise RuntimeError(message)


class RollbackFailureConnection(FakeConnection):
    def rollback(self) -> None:
        self.rollback_calls += 1
        message = f"cannot roll back {self.name}"
        raise RuntimeError(message)


__all__ = [
    "AmbiguousColumn",
    "CloseFailureConnection",
    "DatabaseError",
    "FakeConnection",
    "FakeTrinoSyntaxError",
    "FakeTrinoTypeMismatchError",
    "FakeUndefinedObjectError",
    "FakeUndefinedTableError",
    "FeatureNotSupported",
    "GroupingError",
    "InsufficientPrivilege",
    "Path",
    "RollbackFailureConnection",
    "SqlConfigError",
    "execute_read_module",
    "execute_sql_module",
    "importlib",
    "load_df_module",
    "operation_runner_module",
    "pd",
    "pytest",
    "read_sql_module",
    "retry_module",
    "sys",
]
