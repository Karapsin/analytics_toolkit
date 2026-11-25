"""Keep object-storage data until its external-table DROP acknowledges success."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from analytics_toolkit.sql.dml.transfer.runtime.models import (
        TransferOptions,
        TransferStageState,
    )


def cleanup_transfer_parquet_files(
    options: TransferOptions,
    state: TransferStageState,
    stage_error: Exception | None,
    cleanup: Callable[..., None],
    log: Callable[..., None],
) -> Exception | None:
    location = state.stage_external_location
    if location is None or not state.stage_table_created:
        return None
    if stage_error is not None:
        stage_error.__dict__["analytics_toolkit_sql_stage_external_location"] = location
        log(
            f"Retaining Parquet files at {location} because a stage cleanup was not acknowledged.",
            level="warning",
            connection=options.to_db_key,
            backend=options.to_db_backend,
            phase="cleanup_stage",
        )
        return None
    try:
        if options.parquet_storage_options is None:
            cleanup(location)
        else:
            cleanup(location, storage_options=options.parquet_storage_options)
    except Exception as error:  # noqa: BLE001 -- finalization preserves error precedence.
        return error
    return None
