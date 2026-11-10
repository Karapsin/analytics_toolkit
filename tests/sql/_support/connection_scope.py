from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.backends.models import SourceColumn

from tests.sql._support.cross_area import ddl_create_table_module

if TYPE_CHECKING:
    import pytest

connection_module = importlib.import_module("analytics_toolkit.sql.connection.get_sql_connection")
basic_ops_module = importlib.import_module("analytics_toolkit.sql.dml.table._basic_ops")


class FailingCloseConnection:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        message = "private connection close details"
        raise ConnectionError(message)


def patch_create_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    error: Exception | None,
) -> tuple[FailingCloseConnection, list[str]]:
    connection = FailingCloseConnection()
    probes: list[str] = []

    def probe(_backend: str, _connection: Any, table: str, _alias: str) -> bool:
        probes.append(table)
        if error is not None:
            raise error
        return True

    monkeypatch.setattr(ddl_create_table_module, "get_sql_connection", lambda _key: connection)
    monkeypatch.setattr(basic_ops_module, "table_exists", probe)
    return connection, probes


def patch_schema_inspection(
    monkeypatch: pytest.MonkeyPatch,
    *,
    error: Exception | None,
) -> tuple[FailingCloseConnection, list[str], list[str]]:
    connection = FailingCloseConnection()
    opened: list[str] = []
    inspected: list[str] = []

    def open_connection(key: str) -> FailingCloseConnection:
        opened.append(key)
        return connection

    def inspect(_connection: Any, query: str) -> list[SourceColumn]:
        inspected.append(query)
        if error is not None:
            raise error
        return [SourceColumn("id", "INTEGER")]

    monkeypatch.setattr(ddl_create_table_module, "get_sql_connection", open_connection)
    monkeypatch.setattr(
        ddl_create_table_module.get_backend_adapter("gp"), "inspect_source_query_schema", inspect
    )
    return connection, opened, inspected
