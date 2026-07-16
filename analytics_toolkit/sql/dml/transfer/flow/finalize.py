from __future__ import annotations

import warnings
from typing import Any

from analytics_toolkit.general import time_print
from ....backends import get_backend_adapter
from ...load.stage import create_stage_table
from ...load.stage import cleanup_stage_table_with_retry
from ....connection.get_sql_connection import get_sql_connection
from ...table.maintenance import analyze_table, drop_table_with_retry
from ...table.write_modes import (
    finalize_stage_table,
)
from ...table.table_validation import (
    validate_stage_target_key_overlap,
    validate_stage_uniqueness,
)
from .parquet_stage import cleanup_parquet_stage_location
from ..runtime.models import TransferConnectionRefs, TransferOptions, TransferStageState
from ..runtime.retry import (
    replace_connection,
    rollback_quietly,
    run_with_fresh_connection,
    run_with_retry,
)
from ..schema import get_existing_target_insert_types


def finalize_loaded_stage(
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    stage_state: TransferStageState,
    total_rows: int,
) -> None:
    if total_rows == 0:
        finalize_empty_transfer(options, connection_refs, stage_state)
        return

    if stage_state.first_non_empty_batch is None:
        raise RuntimeError("Expected a non-empty batch when rows were transferred.")
    if stage_state.stage_table is None:
        raise RuntimeError("Expected stage table to be initialized.")

    _run_with_fresh_target_connection(
        options,
        "validate_stage",
        lambda target_ref: validate_stage_uniqueness(
            connection_type=options.to_db_backend,
            connection=target_ref["connection"],
            stage_table=stage_state.stage_table,
            key_columns=options.key_columns,
            stage_tables=(
                stage_state.stage_tables if options.write_mode == "upsert" else None
            ),
        ),
    )
    if options.write_mode != "upsert":
        _run_with_fresh_target_connection(
            options,
            "validate_stage",
            lambda target_ref: validate_stage_target_key_overlap(
                connection_type=options.to_db_backend,
                connection=target_ref["connection"],
                stage_table=stage_state.stage_table,
                target_table=options.target_table,
                key_columns=options.key_columns,
                target_exists=stage_state.target_exists,
                replace_target_table=options.replace_target_table,
            ),
        )
    if stage_state.stage_column_types is None:
        stage_state.insert_column_types = None
        target_column_types = None
    elif stage_state.target_exists and (
        not options.replace_target_table or options.write_mode == "upsert"
    ):
        stage_state.insert_column_types = _run_with_fresh_target_connection(
            options,
            "target_metadata",
            lambda target_ref: get_existing_target_insert_types(
                options.to_db_backend,
                target_ref["connection"],
                options.target_table,
                stage_state.stage_column_types,
                connection_key=options.to_db_key,
            ),
        )
        target_column_types = None
    else:
        stage_state.insert_column_types = stage_state.stage_column_types
        target_column_types = stage_state.stage_column_types

    _ensure_final_upsert_stage_table(options, stage_state)

    _run_with_fresh_target_connection(
        options,
        "finalize_target",
        lambda target_ref: finalize_stage_table(
            options.to_db_backend,
            target_ref["connection"],
            stage_table=stage_state.stage_table,
            target_table=options.target_table,
            replace_target_table=options.replace_target_table,
            target_exists=stage_state.target_exists,
            sample_batch=stage_state.first_non_empty_batch,
            target_column_types=target_column_types,
            insert_column_types=stage_state.insert_column_types,
            write_mode=options.write_mode,
            key_columns=options.key_columns,
            gp_distributed_by_key=options.gp_distributed_by_key,
            gp_partitions=options.gp_partitions,
            partition_by=options.partition_by,
            order_by=options.order_by,
            ch_engine=options.ch_engine,
            ch_cluster=options.ch_cluster,
            ch_sharding_key=options.ch_sharding_key,
            ch_only_shard=options.ch_only_shard,
            query_label=options.query_label,
            connection_key=options.to_db_key,
            ch_retry_per_host_drops=options.ch_retry_per_host_drops,
            upsert_partition_column=options.upsert_partition_column,
            final_upsert_stage_table=stage_state.final_upsert_stage_table,
            incoming_stage_tables=(
                stage_state.stage_tables if options.write_mode == "upsert" else None
            ),
            trino_upsert_partition_drop_sql_template=(
                options.trino_upsert_partition_drop_sql_template
            ),
        ),
    )
    _run_with_fresh_target_connection(
        options,
        "analyze_target",
        lambda target_ref: analyze_table(
            connection_type=options.to_db_backend,
            connection=target_ref["connection"],
            table_name=options.target_table,
            query_label=options.query_label,
        ),
    )


def finalize_empty_transfer(
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    stage_state: TransferStageState,
) -> None:
    del connection_refs
    if not stage_state.target_exists:
        _warn_empty_transfer_missing_target(options)
        return


def _ensure_final_upsert_stage_table(
    options: TransferOptions,
    stage_state: TransferStageState,
) -> None:
    if options.write_mode != "upsert":
        return
    if not get_backend_adapter(
        options.to_db_backend,
    ).uses_partition_replacement_upsert():
        return
    if not stage_state.target_exists:
        return
    if stage_state.final_upsert_stage_table is not None:
        return
    if stage_state.first_non_empty_batch is None:
        raise RuntimeError("Expected a sample batch for final upsert stage creation.")

    create_schema = stage_state.insert_column_types or stage_state.stage_column_types
    stage_state.final_upsert_stage_table = _run_with_fresh_target_connection(
        options,
        "create_final_upsert_stage",
        lambda target_ref: create_stage_table(
            connection_type=options.to_db_backend,
            connection=target_ref["connection"],
            target_table=options.target_table,
            batch=stage_state.first_non_empty_batch,
            column_types=create_schema,
            gp_distributed_by_key=options.gp_distributed_by_key,
            connection_key=options.to_db_key,
            query_label=options.query_label,
            transfer_staging_schema=options.transfer_staging_schema,
            transfer_staging_username=options.transfer_staging_username,
        ),
    )


def _warn_empty_transfer_missing_target(options: TransferOptions) -> None:
    message = (
        "Transfer source returned zero rows and target table "
        f"{options.target_table} does not exist; no target table was created."
    )
    warnings.warn(message, stacklevel=3)
    time_print(
        message,
        level="warning",
        connection=options.to_db_key,
        backend=options.to_db_backend,
    )


def cleanup_stage(
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    stage_state: TransferStageState,
    read_retry_cnt: int,
    *,
    drop_created_target: bool = False,
) -> None:
    stage_cleanup_error: Exception | None = None
    remote_cleanup_error: Exception | None = None
    target_cleanup_error: Exception | None = None

    if stage_state.stage_table_created:
        try:
            for stage_table in _stage_tables_to_cleanup(stage_state):
                _run_with_fresh_target_connection(
                    options,
                    "cleanup_stage",
                    lambda target_ref, stage_table=stage_table: cleanup_stage_table_with_retry(
                        options.to_db_backend,
                        options.to_db_key,
                        target_ref,
                        stage_table,
                        retry_fn=run_with_retry,
                        retry_cnt=read_retry_cnt,
                        timeout_increment=options.timeout_increment,
                        rollback_fn=rollback_quietly,
                        replace_connection_fn=replace_connection,
                        query_label=options.query_label,
                    ),
                )
        except Exception as exc:
            stage_cleanup_error = exc

    if stage_state.stage_external_location is not None:
        try:
            cleanup_parquet_stage_location(stage_state.stage_external_location)
        except Exception as exc:
            remote_cleanup_error = exc

    if drop_created_target and _should_drop_created_target(stage_state):
        try:
            _run_with_fresh_target_connection(
                options,
                "cleanup_target",
                lambda target_ref: drop_table_with_retry(
                    options.to_db_backend,
                    options.to_db_key,
                    target_ref,
                    options.target_table,
                    retry_fn=run_with_retry,
                    retry_cnt=read_retry_cnt,
                    timeout_increment=options.timeout_increment,
                    rollback_fn=rollback_quietly,
                    replace_connection_fn=replace_connection,
                    query_label=options.query_label,
                    operation_label="created target table",
                ),
            )
        except Exception as exc:
            target_cleanup_error = exc

    if stage_cleanup_error is not None:
        if remote_cleanup_error is not None:
            from analytics_toolkit.general import time_print

            time_print(
                "Remote Parquet stage cleanup failed while handling stage table "
                f"cleanup error: {remote_cleanup_error!r}"
            )
        if target_cleanup_error is not None:
            from analytics_toolkit.general import time_print

            time_print(
                "Target cleanup failed while handling stage table cleanup error: "
                f"{target_cleanup_error!r}"
            )
        raise stage_cleanup_error.with_traceback(stage_cleanup_error.__traceback__)
    if remote_cleanup_error is not None:
        if target_cleanup_error is not None:
            from analytics_toolkit.general import time_print

            time_print(
                "Target cleanup failed while handling remote Parquet cleanup error: "
                f"{target_cleanup_error!r}"
            )
        raise remote_cleanup_error.with_traceback(remote_cleanup_error.__traceback__)
    if target_cleanup_error is not None:
        raise target_cleanup_error.with_traceback(target_cleanup_error.__traceback__)


def _stage_tables_to_cleanup(stage_state: TransferStageState) -> list[str]:
    stage_tables: list[str] = []
    if stage_state.stage_tables is not None:
        stage_tables.extend(stage_state.stage_tables)
    elif stage_state.stage_table is not None:
        stage_tables.append(stage_state.stage_table)
    if stage_state.final_upsert_stage_table is not None:
        stage_tables.append(stage_state.final_upsert_stage_table)
    return list(dict.fromkeys(stage_tables))


def _should_drop_created_target(stage_state: TransferStageState) -> bool:
    return (
        stage_state.target_created_by_operation
        and stage_state.target_existed_at_start is False
    )


def _run_with_fresh_target_connection(
    options: TransferOptions,
    role: str,
    operation: Any,
) -> Any:
    return run_with_fresh_connection(
        options.to_db_key,
        role,
        operation,
        open_connection=get_sql_connection,
    )
