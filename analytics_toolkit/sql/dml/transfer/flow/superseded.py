from __future__ import annotations

# ruff: noqa: BLE001, I001, PLR0913, S112, S608, TC001, TID252

import re
from typing import Any

from ....backends import get_backend_adapter
from ....dml.io.read_sql import _read_backend
from ....execution.query_timing import run_timed_query
from ...load.stage import cleanup_stage_table
from ..runtime.models import TransferOptions
from .stage_identity import TransferInternalColumns


_TRANSFER_ID_PATTERN = re.compile(r"[0-9a-f]{32}")


def cleanup_superseded_transfer_stages(
    *,
    options: TransferOptions,
    connection: Any,
    backend: str,
    connection_key: str,
    staging_schema: str | None,
    internal_columns: TransferInternalColumns,
) -> list[str]:
    """Best-effort cleanup verified by exact row identity, never by hash alone."""
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

    dropped: list[str] = []
    for table_name in table_names:
        if not table_name.startswith(f"{options.destination_hash}__"):
            continue
        if _TRANSFER_ID_PATTERN.search(table_name) is None:
            continue
        qualified = adapter.qualify_transfer_stage_table_name(
            connection_key,
            staging_schema,
            table_name,
        )
        try:
            rows = _read_stage_identities(
                backend,
                connection,
                qualified,
                internal_columns,
            )
        except Exception:
            continue
        if len(rows) != 1:
            continue
        transfer_id, destination = rows[0]
        if (
            destination != options.canonical_destination_identity
            or transfer_id == options.transfer_id
        ):
            continue
        cleanup_stage_table(
            backend,
            connection,
            qualified,
            query_label=options.query_label,
        )
        dropped.append(qualified)
    return dropped


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
