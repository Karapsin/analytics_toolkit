from typing import Any

from .extract_ddl import extract_ddl
from .identifiers import column_list_sql, quote_identifier
from .models import CreateSqlTableOptions


def __getattr__(name: str) -> Any:
    if name == "create_table":
        from .api import create_table  # noqa: PLC0415

        return create_table
    if name == "create_sql_table":
        from .compat import create_sql_table  # noqa: PLC0415

        return create_sql_table
    raise AttributeError(name)


__all__ = [
    "column_list_sql",
    "create_table",
    "create_sql_table",
    "CreateSqlTableOptions",
    "extract_ddl",
    "quote_identifier",
]
