from __future__ import annotations

from collections.abc import Sequence

from sqlglot import exp, parse_one

from ..connection.config import resolve_connection_backend
from ..core.capabilities import get_backend_capability


def column_list_sql(columns: Sequence[str], connection_type: str) -> str:
    backend = resolve_connection_backend(connection_type)
    return ", ".join(
        quote_identifier(column_name, backend) for column_name in columns
    )

def quote_identifier(identifier: str, connection_type: str) -> str:
    backend = resolve_connection_backend(connection_type)
    quote_char = get_backend_capability(backend).identifier_quote
    escaped = identifier.replace(quote_char, quote_char * 2)
    return f"{quote_char}{escaped}{quote_char}"

def _add_table_identifier_suffix(table_name: str, suffix: str, dialect: str) -> str:
    table = _parse_table_name(table_name, dialect)
    table_identifier = table.this
    suffixed_identifier = exp.to_identifier(
        f"{_identifier_name(table_identifier)}{suffix}",
        quoted=bool(table_identifier.args.get("quoted")),
    )
    suffixed_table = table.copy()
    suffixed_table.set("this", suffixed_identifier)
    return suffixed_table.sql(dialect=dialect)

def _parse_table_name(table_name: str, dialect: str) -> exp.Table:
    table = parse_one(table_name, read=dialect, into=exp.Table)
    if not isinstance(table, exp.Table) or not isinstance(table.this, exp.Identifier):
        raise ValueError(f"Invalid table name: {table_name}")
    return table

def _identifier_name(identifier: exp.Expression) -> str:
    if not isinstance(identifier, exp.Identifier):
        raise ValueError(f"Invalid table identifier: {identifier}")
    return str(identifier.this)
