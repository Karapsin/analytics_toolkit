"""Certify complete owned SQL-stage payloads before live-target publication."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.insert_constraints import (
    InsertConstraintError,
    read_staged_null_counts,
    validate_not_null_input,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


def validate_staged_not_null_input(  # noqa: PLR0913 -- shared finalization constraint context.
    backend: str,
    connection: Any,
    target_table: str,
    stage_tables: Sequence[str],
    *,
    columns: Sequence[str] | None,
    column_types: Mapping[str, str] | None = None,
    connection_key: str | None = None,
    query_label: str | None = None,
) -> None:
    adapter = get_backend_adapter(backend)
    alias = connection_key or backend
    required = adapter.get_required_column_defaults(connection, target_table, connection_key=alias)
    if not required:
        return
    target_order = (
        list(adapter.get_table_column_types(connection, target_table, connection_key=alias))
        if columns is None
        else []
    )
    for stage_table in dict.fromkeys(stage_tables):
        if columns is None:
            source_order = list(
                adapter.get_table_column_types(connection, stage_table, connection_key=alias)
            )
            if len(source_order) > len(target_order):
                message = "Staged positional projection has more columns than the target."
                raise InsertConstraintError(message)
            incoming = target_order[: len(source_order)]
            sources = dict(zip(incoming, source_order))
        else:
            incoming = list(columns)
            sources = {name: name for name in incoming}
        checked = [name for name in required if name in sources]
        if not checked and all(
            name in sources or has_default for name, has_default in required.items()
        ):
            continue
        projection = {
            name: (sources[name], column_types[name] if column_types else None) for name in checked
        }
        row_count, null_columns = read_staged_null_counts(
            adapter, connection, stage_table, projection, query_label=query_label
        )
        validate_not_null_input(incoming, required, null_columns=null_columns, row_count=row_count)
