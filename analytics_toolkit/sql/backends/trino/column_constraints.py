"""Read Trino's reported required-column metadata without changing SQL types."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.backends.execution_outcome import close_cursor
from analytics_toolkit.sql.backends.insert_constraints import validate_not_null_input

if TYPE_CHECKING:
    from collections.abc import Sequence

_CONSTRAINT_FIELDS = 3


def get_required_column_defaults(
    adapter: Any, connection: Any, table_name: str, *, connection_key: str
) -> dict[str, bool]:
    """Snapshot only connector-reported required/default constraints."""
    from .adapter import split_trino_table_name  # noqa: PLC0415 -- adapter registry cycle.

    catalog, schema, relation = split_trino_table_name(table_name, connection_key=connection_key)
    cursor = connection.cursor()
    try:
        # Catalog is one quoted identifier; the other components remain parameters.
        cursor.execute(
            f"SELECT column_name, is_nullable, column_default "  # noqa: S608
            f"FROM {adapter.quote_identifier(catalog)}.information_schema.columns "
            "WHERE table_schema = ? AND table_name = ? AND is_nullable = 'NO' "
            "ORDER BY ordinal_position",
            (schema, relation),
        )
        rows = cursor.fetchall()
    finally:
        close_cursor(cursor, backend="trino")
    required_defaults = {}
    for row in rows:
        if len(row) != _CONSTRAINT_FIELDS or str(row[1]).upper() != "NO":
            message = "Cannot verify existing target NOT NULL constraints from metadata."
            raise ValueError(message)
        required_defaults[str(row[0])] = row[2] is not None
    return required_defaults


def validate_existing_dataframe_insert_columns(  # noqa: PLR0913 -- shared adapter hook signature.
    adapter: Any,
    connection: Any,
    table_name: str,
    columns: Sequence[str],
    *,
    connection_key: str,
    null_columns: Sequence[str] = (),
    row_count: int | None = None,
) -> None:
    required_defaults = get_required_column_defaults(
        adapter, connection, table_name, connection_key=connection_key
    )
    validate_not_null_input(
        columns, required_defaults, null_columns=null_columns, row_count=row_count
    )
