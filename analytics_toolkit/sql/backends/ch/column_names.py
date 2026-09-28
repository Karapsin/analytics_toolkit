"""SELECT-only column discovery for both ClickHouse transports."""

from __future__ import annotations

import json
from typing import Any


def get_table_column_names(
    adapter: Any, connection: Any, table_name: str, *, connection_key: str
) -> tuple[str, ...]:
    from ...core.identifiers import TableIdentifier

    del connection_key
    table = TableIdentifier.parse(table_name, adapter.backend).render_quoted(adapter.backend)
    query = f"SELECT * FROM {table} LIMIT 1"
    settings = {
        "asterisk_include_materialized_columns": 1,
        "asterisk_include_alias_columns": 1,
    }
    if getattr(connection, "is_native_transport", False):
        result = connection.query(query, settings=settings)
        names = result.column_names
    else:
        # An empty HTTP Native-format response has no header. JSON retains meta.
        result = json.loads(connection.raw_query(query, settings=settings, fmt="JSON"))
        names = [column["name"] for column in result["meta"]]
    if not names:
        raise ValueError("Column probe returned no result metadata.")
    return tuple(str(name) for name in names)
