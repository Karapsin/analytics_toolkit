"""Resolve planned SQL and Parquet stage identities through runtime naming."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from analytics_toolkit.sql.dml.transfer.runtime.models import TransferOptions


def planned_stage_table_name(
    options: TransferOptions,
    *,
    worker_index: int | None,
    build_stage_table_name: Callable[..., str],
) -> str:
    runtime_id = options.transfer_id or "<runtime-transfer-id>"
    suffix = (
        f"{runtime_id}__w00000" if worker_index is None else f"{runtime_id}__w{worker_index:05d}"
    )
    schema = (
        options.s3_transfer_staging_schema
        if options.trino_mode == "parquet"
        else options.transfer_staging_schema
    )
    try:
        return build_stage_table_name(
            options.to_db_backend,
            options.target_table,
            transfer_staging_schema=schema,
            transfer_staging_username=options.transfer_staging_username,
            random_suffix=suffix,
            destination_hash=options.destination_hash,
        )
    except Exception:  # noqa: BLE001 -- offline placeholder remains usable when unresolved.
        return f"{options.target_table}__stage__{suffix}"
