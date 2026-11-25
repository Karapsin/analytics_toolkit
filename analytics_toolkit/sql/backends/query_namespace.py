"""Resolve a scripted creation target in its actual execution session."""

from __future__ import annotations

from typing import Any

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

from .execution_outcome import close_cursor


def bind_query_target_namespace(backend: str, connection: Any, table: str) -> str:
    from analytics_toolkit.sql.core.identifiers import TableIdentifier  # noqa: PLC0415 -- registry.

    identifier = TableIdentifier.parse(table, backend)
    required = 3 if backend == "trino" else 2
    if len(identifier.parts) >= required:
        return table
    if backend == "ch":
        from .ch.coordinator_database import resolve_current_database  # noqa: PLC0415

        namespace = (resolve_current_database(connection),)
    else:
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT current_catalog, current_schema"
                if backend == "trino"
                else "SELECT current_schema()"
            )
            rows = cursor.fetchall()
        finally:
            close_cursor(cursor, backend=backend)
        if len(rows) != 1 or len(rows[0]) != required - 1:
            message = "Cannot resolve the scripted target's current native namespace."
            raise InvalidSqlInputError(message)
        namespace = tuple(rows[0])
    missing = required - len(identifier.parts)
    defaults = namespace[:missing]
    if any(not isinstance(part, str) or not part for part in defaults):
        message = "The scripted target has no usable current native namespace."
        raise InvalidSqlInputError(message)
    return TableIdentifier(
        (*defaults, *identifier.parts), (True,) * missing + identifier.quoted
    ).render(backend)
