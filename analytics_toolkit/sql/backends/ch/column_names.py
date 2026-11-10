"""SELECT-only column discovery for both ClickHouse transports."""

from __future__ import annotations

import json
from typing import Any

_DESCRIBE_TYPE_FIELDS = 2
_DESCRIBE_DEFAULT_FIELDS = 3
_NONINSERTABLE_KINDS = {"ALIAS", "MATERIALIZED", "EPHEMERAL"}


def get_writable_table_column_types(
    adapter: Any, connection: Any, table_name: str, *, connection_key: str
) -> dict[str, str]:
    from .routing import query_local  # noqa: PLC0415 -- backend registry cycle.

    del adapter, connection_key
    result = query_local(connection, f"DESCRIBE TABLE {table_name}")
    rows = getattr(result, "result_rows", None) or []
    return {
        str(row[0]): str(row[1])
        for row in rows
        if len(row) >= _DESCRIBE_TYPE_FIELDS
        and (len(row) < _DESCRIBE_DEFAULT_FIELDS or str(row[2]).upper() not in _NONINSERTABLE_KINDS)
    }


def get_table_column_names(
    adapter: Any, connection: Any, table_name: str, *, connection_key: str
) -> tuple[str, ...]:
    from analytics_toolkit.sql.core.identifiers import (  # noqa: PLC0415 -- adapter registry cycle.
        TableIdentifier,
    )

    del connection_key
    table = TableIdentifier.parse(table_name, adapter.backend).render_quoted(adapter.backend)
    query = f"SELECT * FROM {table} LIMIT 1"  # noqa: S608 -- parsed and quoted table identifier.
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
        message = "Column probe returned no result metadata."
        raise ValueError(message)
    return tuple(str(name) for name in names)
