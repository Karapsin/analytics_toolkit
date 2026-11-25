"""Validate reported required-column constraints before destination mutation."""

from __future__ import annotations

from numbers import Integral
from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.backends.execution_outcome import close_cursor
from analytics_toolkit.sql.execution.labels import apply_query_label

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class InsertConstraintError(ValueError):
    """A frozen input violates constraints; replay cannot repair its values."""

    analytics_toolkit_sql_retry_safe = False


def validate_not_null_input(
    columns: Sequence[str],
    required_defaults: Mapping[str, bool],
    *,
    null_columns: Sequence[str] = (),
    row_count: int | None = None,
) -> None:
    """Explicit NULL never invokes a default; omitted fields may invoke one."""
    invalid = [name for name in null_columns if name in required_defaults]
    if invalid:
        message = "Incoming NULL value(s) violate authoritative NOT NULL column(s): " + ", ".join(
            invalid
        )
        raise InsertConstraintError(message)
    if row_count == 0:
        return
    incoming = set(columns)
    missing = [
        name
        for name, has_default in required_defaults.items()
        if name not in incoming and not has_default
    ]
    if missing:
        message = "Incoming data omits required NOT NULL column(s): " + ", ".join(missing)
        raise InsertConstraintError(message)


def read_staged_null_counts(
    adapter: Any,
    connection: Any,
    stage_table: str,
    projection: Mapping[str, tuple[str, str | None]],
    *,
    query_label: str | None = None,
) -> tuple[int, tuple[str, ...]]:
    """Inspect actual SQL-cast values in an owned stage, without returning rows."""
    from analytics_toolkit.sql.core.identifiers import (  # noqa: PLC0415 -- backend registry cycle.
        TableIdentifier,
    )

    selected = [
        adapter.cast_select_expression(source, native)
        if native is not None
        else adapter.quote_identifier(source)
        for source, native in projection.values()
    ]
    counts = ["COUNT(*)"] + [
        f"COUNT(*) - COUNT({adapter.quote_identifier(source)})"
        for source, _native in projection.values()
    ]
    table = TableIdentifier.parse(stage_table, adapter.backend).render_quoted(adapter.backend)
    query = apply_query_label(
        f"SELECT {', '.join(counts)} FROM "  # noqa: S608 -- structured table and adapter-quoted columns/types.
        f"(SELECT {', '.join(selected) if selected else '1'} FROM {table}) "
        f"AS {adapter.quote_identifier('__atk_nn_input')}",
        query_label,
    )
    cursor = connection.cursor()
    try:
        cursor.execute(query)
        row = cursor.fetchone()
    finally:
        close_cursor(cursor, backend=adapter.backend)
    if (
        row is None
        or len(row) != len(counts)
        or any(
            not isinstance(count, Integral) or isinstance(count, bool) or count < 0 for count in row
        )
        or any(count > row[0] for count in row[1:])
    ):
        message = "Cannot certify staged NOT NULL constraints from query result."
        raise InsertConstraintError(message)
    return int(row[0]), tuple(name for name, count in zip(projection, row[1:]) if count)
