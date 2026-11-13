"""Retry target partition writes from a certified complete stage payload."""

from __future__ import annotations

from typing import Any

from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.dml.table.upsert_checkpoint import UpsertCheckpoint
from analytics_toolkit.sql.dml.table.write_modes import upsert_stage_table


def partition_upsert_enabled(options: Any, state: Any) -> bool:
    return bool(
        options.write_mode == "upsert"
        and state.target_exists
        and get_backend_adapter(options.to_db_backend).uses_partition_replacement_upsert()
    )


def finalize_partition_upsert(options: Any, state: Any, connection: Any) -> None:
    if state.upsert_checkpoint is None:
        state.upsert_checkpoint = UpsertCheckpoint()
    upsert_stage_table(
        options.to_db_backend,
        connection,
        options.target_table,
        state.stage_table,
        columns=list(state.insert_column_types or state.source_columns),
        key_columns=options.key_columns or [],
        column_types=state.insert_column_types,
        ch_cluster=options.ch_cluster,
        ch_only_shard=options.ch_only_shard,
        query_label=options.query_label,
        upsert_partition_column=options.upsert_partition_column,
        final_stage_table=state.final_upsert_stage_table,
        incoming_stage_tables=state.stage_tables,
        trino_partition_drop_sql_template=options.trino_upsert_partition_drop_sql_template,
        checkpoint=state.upsert_checkpoint,
        connection_key=options.to_db_key,
    )


def retry_certified_finalization(options: Any, state: Any, operation: Any, retry: Any) -> None:
    try:
        retry(
            operation_name=f"finalizing staged target {options.target_table}",
            retry_cnt=options.retry_cnt,
            timeout_increment=options.timeout_increment,
            operation=operation,
        )
    except Exception as exc:
        if state.upsert_checkpoint is not None and state.upsert_checkpoint.prepared:
            # Never restart preservation from a partially replaced target.
            exc.__dict__["analytics_toolkit_sql_retry_safe"] = False
        raise
