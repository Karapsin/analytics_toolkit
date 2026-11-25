from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from analytics_toolkit import sql
from analytics_toolkit.sql.metadata import column_names

if TYPE_CHECKING:
    import pytest

table_info_module = importlib.import_module("analytics_toolkit.sql.metadata.table_info")
drop_tables_module = importlib.import_module("analytics_toolkit.sql.dml.table.drop_tables")
maintenance_module = importlib.import_module("analytics_toolkit.sql.dml.table.maintenance")


class FailingCloseConnection:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        message = "connection cleanup failed"
        raise ConnectionError(message)


def arrange_facade_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    primary_error: BaseException | None,
) -> tuple[FailingCloseConnection, list[str]]:
    connection = FailingCloseConnection()
    calls: list[str] = []
    modules = {
        "table_info": table_info_module,
        "column_names": column_names,
        "drop_tables": drop_tables_module,
        "gp_vacuum": maintenance_module,
    }
    methods = {
        "table_info": "get_table_column_types",
        "column_names": "get_table_column_names",
        "drop_tables": "drop_table_with_options",
        "gp_vacuum": "vacuum_table",
    }
    results = {"table_info": {"id": "BIGINT"}, "column_names": ("id",)}

    def perform(*_args: Any, **_kwargs: Any) -> Any:
        calls.append(operation)
        if primary_error is not None:
            raise primary_error
        return results.get(operation)

    module = modules[operation]
    monkeypatch.setattr(module, "get_sql_connection", lambda _key: connection)
    adapter = module.get_backend_adapter("gp")
    monkeypatch.setattr(adapter, methods[operation], perform)
    if operation == "table_info":
        monkeypatch.setattr(adapter, "table_exists", lambda *_a, **_k: True)
        monkeypatch.setattr(adapter, "count_table_rows", lambda *_a, **_k: 42)
    return connection, calls


def invoke_cleanup_facade(operation: str) -> Any:
    if operation == "table_info":
        return sql.table_info("gp", "public.events", include_row_count=True)
    if operation == "column_names":
        return column_names.table_column_names("gp", "public.events")
    if operation == "drop_tables":
        return sql.drop_tables("gp", "public.events", return_metadata=True)
    return sql.gp_vacuum("gp", "public.events", analyze=True, full=True, verbose=False)
