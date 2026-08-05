from __future__ import annotations

# ruff: noqa: BLE001, I001, PLR0913, S608, TC001, TID252

import re
from dataclasses import dataclass
from typing import Any

from ....backends import get_backend_adapter
from ....dml.io.read_sql import _read_backend
from ....execution.query_timing import run_timed_query
from ...load.stage import cleanup_stage_table
from ..runtime.models import TransferOptions
from .stage_identity import TransferInternalColumns


_TRANSFER_ID_PATTERN = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class _SupersededCleanupContext:
    options: TransferOptions
    connection: Any
    adapter: Any
    backend: str
    connection_key: str
    staging_schema: str
    internal_columns: TransferInternalColumns
    include_current_transfer_id: bool


def cleanup_superseded_transfer_stages(
    *,
    options: TransferOptions,
    connection: Any,
    backend: str,
    connection_key: str,
    staging_schema: str | None,
    internal_columns: TransferInternalColumns,
    include_current_transfer_id: bool = False,
) -> list[str]:
    """Clean attempt-owned stages by row identity or collision-safe stage name."""
    if staging_schema is None or options.destination_hash is None:
        return []
    adapter = get_backend_adapter(backend)
    try:
        table_names = run_timed_query(
            backend,
            lambda: adapter.query_transfer_stage_table_names(
                connection,
                connection_key=connection_key,
                transfer_staging_schema=staging_schema,
                table_pattern=f"{options.destination_hash}__%",
            ),
            action_name="superseded-stage inspection",
            phase="inspect_superseded_stages",
        )
    except Exception:
        return []

    context = _SupersededCleanupContext(
        options=options,
        connection=connection,
        adapter=adapter,
        backend=backend,
        connection_key=connection_key,
        staging_schema=staging_schema,
        internal_columns=internal_columns,
        include_current_transfer_id=include_current_transfer_id,
    )
    return [
        qualified
        for table_name in table_names
        if (qualified := _drop_superseded_stage(context, table_name)) is not None
    ]


def _drop_superseded_stage(
    context: _SupersededCleanupContext,
    table_name: str,
) -> str | None:
    options = context.options
    if not table_name.startswith(f"{options.destination_hash}__"):
        return None
    transfer_id_match = _TRANSFER_ID_PATTERN.search(table_name)
    if transfer_id_match is None:
        return None
    qualified = str(
        context.adapter.qualify_transfer_stage_table_name(
            context.connection_key,
            context.staging_schema,
            table_name,
        )
    )
    try:
        rows = _read_stage_identities(
            context.backend,
            context.connection,
            qualified,
            context.internal_columns,
        )
    except Exception:
        return None
    if not _is_superseded_stage_identity(
        rows,
        transfer_id_match.group(0),
        options,
        include_current_transfer_id=context.include_current_transfer_id,
    ):
        return None
    cleanup_stage_table(
        context.backend,
        context.connection,
        qualified,
        query_label=options.query_label,
        ch_creation_policy=(
            options.staging_ch_policy
            if context.backend == options.to_db_backend
            and context.connection_key == options.to_db_key
            else None
        ),
    )
    return qualified


def _is_superseded_stage_identity(
    rows: list[tuple[Any, Any]],
    name_transfer_id: str,
    options: TransferOptions,
    *,
    include_current_transfer_id: bool,
) -> bool:
    if not rows:
        return (
            include_current_transfer_id
            or options.transfer_id is None
            or name_transfer_id != options.transfer_id
        )
    if len(rows) != 1:
        return False
    transfer_id, destination = rows[0]
    return destination == options.canonical_destination_identity and (
        include_current_transfer_id or transfer_id != options.transfer_id
    )


def _read_stage_identities(
    backend: str,
    connection: Any,
    stage_table: str,
    internal_columns: TransferInternalColumns,
) -> list[tuple[Any, Any]]:
    adapter = get_backend_adapter(backend)
    transfer_column = adapter.quote_identifier(internal_columns.transfer_id)
    destination_column = adapter.quote_identifier(internal_columns.destination_table)
    result = _read_backend(
        backend,
        connection,
        (
            f"SELECT {transfer_column}, {destination_column} FROM {stage_table} "
            f"GROUP BY {transfer_column}, {destination_column}"
        ),
        print_queries=False,
        output_type="dict",
        action_name="superseded-stage inspection",
        phase="inspect_superseded_stages",
    )
    return list(zip(result.columns[0], result.columns[1])) if result.row_count else []
