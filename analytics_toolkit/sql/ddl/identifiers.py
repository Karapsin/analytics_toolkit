from __future__ import annotations

from collections.abc import Sequence

from sqlglot import exp, parse_one

from ..backends import get_backend_adapter


def column_list_sql(columns: Sequence[str], connection_type: str) -> str:
    return ", ".join(
        quote_identifier(column_name, connection_type) for column_name in columns
    )

def quote_identifier(identifier: str, connection_type: str) -> str:
    return get_backend_adapter(connection_type).quote_identifier(identifier)

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
