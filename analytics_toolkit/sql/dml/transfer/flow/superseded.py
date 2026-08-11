from __future__ import annotations

# ruff: noqa: BLE001, I001, PLR0913, TC001, TID252

import re
from dataclasses import dataclass
from typing import Any

from ....backends import get_backend_adapter
from ....execution.query_timing import run_timed_query
from ...load.stage import cleanup_stage_table
from ..runtime.models import TransferOptions
from .stage_identity import TransferInternalColumns


_TRANSFER_STAGE_SUFFIX_PATTERN = re.compile(
    r"(?P<transfer_id>[0-9a-f]{32})__"
    r"(?:source|s[0-9]{5}|w[0-9]{5}|upsert)"
    r"(?:__c_[0-9a-f]{8}|[0-9a-f]{5})?$"
)


@dataclass(frozen=True)
class _SupersededCleanupContext:
    options: TransferOptions
    connection: Any
    adapter: Any
    backend: str
    connection_key: str
    staging_schema: str
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
    """Clean transfer stages identified by the reserved collision-safe name."""
    del internal_columns
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
    transfer_id_match = _TRANSFER_STAGE_SUFFIX_PATTERN.search(table_name)
    if transfer_id_match is None:
        return None
    name_transfer_id = transfer_id_match.group("transfer_id")
    if not context.include_current_transfer_id and name_transfer_id == options.transfer_id:
        return None
    qualified = str(
        context.adapter.qualify_transfer_stage_table_name(
            context.connection_key,
            context.staging_schema,
            table_name,
        )
    )
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
