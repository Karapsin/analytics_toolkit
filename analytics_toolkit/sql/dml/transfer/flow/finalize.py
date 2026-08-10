from __future__ import annotations

# ruff: noqa: EM101, TRY003
import warnings
from dataclasses import replace as replace_dataclass
from typing import Any

from analytics_toolkit.general import time_print
from analytics_toolkit.sql.dml.table._basic_ops import count_table_rows
from analytics_toolkit.sql.dml.transfer.flow.row_counts import (
    _count_loaded_stage_rows,
    cleanup_materialized_sources,
)

from ....backends import get_backend_adapter
from ....connection.get_sql_connection import get_sql_connection
from ...load.stage import cleanup_stage_table_with_retry, create_stage_table
from ...table.maintenance import analyze_table
from ...table.table_validation import (
    validate_stage_target_key_overlap,
    validate_stage_uniqueness,
)
from ...table.write_modes import (
    finalize_stage_table,
)
from ..runtime.connection_pool import BoundedConnectionManager
from ..runtime.models import TransferConnectionRefs, TransferOptions, TransferStageState
from ..runtime.retry import (
    replace_connection,
    rollback_quietly,
    run_with_fresh_connection,
    run_with_retry,
)
from ..schema import get_existing_target_insert_types
from .parquet_stage import cleanup_parquet_stage_location
from .stage_validation import validate_transfer_stage_identity


class FreshTargetFinalizationRowCountMismatchError(ValueError):
    pass


def finalize_loaded_stage(  # noqa: PLR0913
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    stage_state: TransferStageState,
    total_rows: int,
    *,
    target_connection_runner: Any | None = None,
    target_host_connection_runner: Any | None = None,
) -> None:
    if total_rows == 0:
        finalize_empty_transfer(options, connection_refs, stage_state)
        return

    if stage_state.first_non_empty_batch is None:
        raise RuntimeError("Expected a non-empty batch when rows were transferred.")
    if stage_state.stage_table is None:
        raise RuntimeError("Expected stage table to be initialized.")
    stage_table = stage_state.stage_table

    if options.transfer_id is not None:
        if stage_state.internal_columns is None:
            raise RuntimeError("Transfer internal columns were not resolved.")
        stage_tables = (
            stage_state.stage_tables
            if options.write_mode == "upsert" and stage_state.stage_tables
            else [stage_state.stage_table]
        )
        expected_slice_counts = (
            {item.index: item.streamed_rows for item in stage_state.slice_counts}
            if stage_state.slice_counts
            else {0: total_rows}
        )
        _run_target_operation(
            options,
            "validate_stage_identity",
            lambda target_ref: validate_transfer_stage_identity(
                options=options,
                connection=target_ref["connection"],
                stage_tables=stage_tables,
                internal_columns=stage_state.internal_columns,
                expected_slice_counts=expected_slice_counts,
            ),
            target_connection_runner=target_connection_runner,
        )

    _run_target_operation(
        options,
        "validate_stage",
        lambda target_ref: validate_stage_uniqueness(
            connection_type=options.to_db_backend,
            connection=target_ref["connection"],
            stage_table=stage_state.stage_table,
            key_columns=options.key_columns,
            stage_tables=(stage_state.stage_tables if options.write_mode == "upsert" else None),
        ),
        target_connection_runner=target_connection_runner,
    )
    if options.write_mode != "upsert":
        _run_target_operation(
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
            target_connection_runner=target_connection_runner,
        )
    source_stage_column_types = (
        stage_state.stage_column_types
        if options.transfer_id is None
        else (
            {
                column: stage_state.stage_column_types[column]
                for column in stage_state.source_columns
                if column in stage_state.stage_column_types
            }
            if stage_state.stage_column_types is not None
            else None
        )
    )
    if source_stage_column_types is None:
        stage_state.insert_column_types = None
        target_column_types = None
    elif stage_state.insert_column_types is not None:
        target_column_types = None
    elif stage_state.target_exists and (
        not options.replace_target_table or options.write_mode == "upsert"
    ):
        stage_state.insert_column_types = _run_target_operation(
            options,
            "target_metadata",
            lambda target_ref: get_existing_target_insert_types(
                options.to_db_backend,
                target_ref["connection"],
                options.target_table,
                source_stage_column_types,
                connection_key=options.to_db_key,
            ),
            target_connection_runner=target_connection_runner,
        )
        target_column_types = None
    else:
        stage_state.insert_column_types = source_stage_column_types
        target_column_types = source_stage_column_types

    _ensure_final_upsert_stage_table(
        options,
        stage_state,
        target_connection_runner=target_connection_runner,
    )
    fresh_clickhouse_target = _creates_fresh_clickhouse_target(options, stage_state)

    def finalize_operation(attempt: int) -> None:
        target_precleared = attempt > 1 and fresh_clickhouse_target
        if not target_precleared:
            target_precleared = _preclear_clickhouse_replace_target(
                options,
                stage_state,
                target_connection_runner=target_connection_runner,
                target_host_connection_runner=target_host_connection_runner,
            )
        try:
            _finalize_target_once(
                options,
                stage_state,
                stage_table,
                target_column_types,
                target_precleared=target_precleared,
                fresh_clickhouse_target=fresh_clickhouse_target,
                target_connection_runner=target_connection_runner,
            )
            if fresh_clickhouse_target:
                _validate_fresh_target_row_count(
                    options,
                    stage_state,
                    total_rows,
                    target_connection_runner=target_connection_runner,
                )
            _analyze_final_target(options, target_connection_runner=target_connection_runner)
        except Exception:
            if fresh_clickhouse_target:
                _drop_incomplete_fresh_target(
                    options,
                    target_connection_runner=target_connection_runner,
                )
            raise

    if fresh_clickhouse_target:
        run_with_retry(
            operation_name=f"finalizing fresh ClickHouse target {options.target_table}",
            retry_cnt=options.retry_cnt,
            timeout_increment=options.timeout_increment,
            operation=finalize_operation,
        )
    else:
        finalize_operation(1)


def _creates_fresh_clickhouse_target(
    options: TransferOptions,
    stage_state: TransferStageState,
) -> bool:
    adapter = get_backend_adapter(options.to_db_backend)
    supports_distributed_targets = getattr(
        adapter,
        "supports_distributed_table_targets",
        lambda: False,
    )
    return supports_distributed_targets() and (
        options.write_mode == "replace" or not stage_state.target_exists
    )


def _finalize_target_once(  # noqa: PLR0913
    options: TransferOptions,
    stage_state: TransferStageState,
    stage_table: str,
    target_column_types: dict[str, str] | None,
    *,
    target_precleared: bool,
    fresh_clickhouse_target: bool,
    target_connection_runner: Any | None,
) -> None:
    creation_policy = options.regular_ch_policy
    if fresh_clickhouse_target and creation_policy is not None:
        creation_policy = replace_dataclass(
            creation_policy,
            ddl_ready_timeout_increment_seconds=float(options.timeout_increment),
        )
    if fresh_clickhouse_target and stage_state.target_existed_at_start is False:
        stage_state.target_created_by_operation = True
    _run_target_operation(
        options,
        "finalize_target",
        lambda target_ref: finalize_stage_table(
            options.to_db_backend,
            target_ref["connection"],
            stage_table=stage_table,
            target_table=options.target_table,
            replace_target_table=options.replace_target_table and not target_precleared,
            target_exists=stage_state.target_exists and not target_precleared,
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
            ch_creation_policy=creation_policy,
        ),
        target_connection_runner=target_connection_runner,
    )


def _validate_fresh_target_row_count(
    options: TransferOptions,
    stage_state: TransferStageState,
    total_rows: int,
    *,
    target_connection_runner: Any | None,
) -> None:
    stage_rows = _count_loaded_stage_rows(
        options,
        stage_state,
        total_rows,
        open_connection=get_sql_connection,
        target_connection_runner=target_connection_runner,
    )
    target_rows = int(
        _run_target_operation(
            options,
            "validate_final_target_row_count",
            lambda target_ref: count_table_rows(
                options.to_db_backend,
                target_ref["connection"],
                options.target_table,
                query_label=options.query_label,
            ),
            target_connection_runner=target_connection_runner,
        )
    )
    time_print(
        f"Validated fresh target row count: stage {stage_rows:,}; target {target_rows:,}",
        connection=options.to_db_key,
        backend=options.to_db_backend,
        phase="validate_target_row_count",
    )
    if target_rows != stage_rows:
        message = (
            f"Fresh target/stage row-count mismatch for {options.target_table}: "
            f"target has {target_rows:,} row(s), stage has {stage_rows:,} row(s)."
        )
        raise FreshTargetFinalizationRowCountMismatchError(message)


def _analyze_final_target(
    options: TransferOptions,
    *,
    target_connection_runner: Any | None,
) -> None:
    _run_target_operation(
        options,
        "analyze_target",
        lambda target_ref: analyze_table(
            connection_type=options.to_db_backend,
            connection=target_ref["connection"],
            table_name=options.target_table,
            query_label=options.query_label,
        ),
        target_connection_runner=target_connection_runner,
    )


def _drop_incomplete_fresh_target(
    options: TransferOptions,
    *,
    target_connection_runner: Any | None,
) -> None:
    _run_target_operation(
        options,
        "cleanup_incomplete_target",
        lambda target_ref: cleanup_stage_table_with_retry(
            options.to_db_backend,
            options.to_db_key,
            target_ref,
            options.target_table,
            retry_fn=run_with_retry,
            retry_cnt=1,
            timeout_increment=0,
            rollback_fn=rollback_quietly,
            replace_connection_fn=target_ref.get(
                "bounded_replace_connection",
                replace_connection,
            ),
            query_label=options.query_label,
            ch_creation_policy=options.regular_ch_policy,
            operation_label="incomplete fresh target table",
        ),
        target_connection_runner=target_connection_runner,
    )


def _preclear_clickhouse_replace_target(
    options: TransferOptions,
    stage_state: TransferStageState,
    *,
    target_connection_runner: Any | None,
    target_host_connection_runner: Any | None,
) -> bool:
    adapter = get_backend_adapter(options.to_db_backend)
    if not (
        options.write_mode == "replace"
        and options.replace_target_table
        and stage_state.target_exists
        and adapter.needs_bounded_replace_preclear(options.ch_only_shard)
    ):
        return False
    owned_manager: BoundedConnectionManager | None = None
    if target_connection_runner is None and target_host_connection_runner is None:
        manager = BoundedConnectionManager(
            options.to_db_key,
            options.transfer_concurrency.effective_write,
            role="target finalization pool",
            open_connection=get_sql_connection,
        )
        owned_manager = manager
        target_connection_runner = manager.run

        def run_on_target_host(host: str, operation: Any) -> Any:
            return manager.run_with_connection(
                "per-host target finalization",
                lambda: adapter.open_transfer_host_connection(options.to_db_key, host),
                operation,
            )

        target_host_connection_runner = run_on_target_host
    if target_connection_runner is None or target_host_connection_runner is None:
        raise RuntimeError("Target finalization runners must be supplied together.")
    error: BaseException | None = None
    try:
        return bool(
            adapter.preclear_distributed_replace_target(
                options.target_table,
                options.ch_cluster,
                query_label=options.query_label,
                retry_per_host_drops=options.ch_retry_per_host_drops,
                only_shard=options.ch_only_shard,
                connection_runner=lambda role, operation: target_connection_runner(
                    role,
                    lambda target_ref: operation(target_ref["connection"]),
                ),
                host_connection_runner=target_host_connection_runner,
            )
        )
    except BaseException as exc:
        error = exc
        raise
    finally:
        if owned_manager is not None:
            owned_manager.close_preserving(error)


def cleanup_transfer_attempt_stages(  # noqa: PLR0913
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    stage_state: TransferStageState,
    read_retry_cnt: int,
    transfer_error: Exception | None,
    cleanup_stage_fn: Any,
) -> Exception | None:
    cleanup_error: Exception | None = None
    try:
        cleanup_stage_fn(
            options=options,
            connection_refs=connection_refs,
            stage_state=stage_state,
            read_retry_cnt=read_retry_cnt,
            drop_created_target=transfer_error is not None,
        )
    except Exception as exc:  # noqa: BLE001
        cleanup_error = exc
    try:
        cleanup_materialized_sources(options, connection_refs.source, stage_state)
    except Exception as exc:  # noqa: BLE001
        cleanup_error = cleanup_error or exc
    return cleanup_error


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
    *,
    target_connection_runner: Any | None = None,
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

    def register_candidate(table_name: str) -> None:
        if table_name not in stage_state.stage_table_candidates:
            stage_state.stage_table_candidates.append(table_name)
        stage_state.final_upsert_stage_table = table_name

    stage_state.final_upsert_stage_table = _run_target_operation(
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
            random_suffix=(
                f"{options.transfer_id}__upsert" if options.transfer_id is not None else None
            ),
            destination_hash=options.destination_hash,
            ddl_properties=options.staging_ddl_properties,
            ch_creation_policy=options.staging_ch_policy,
            on_stage_candidate=register_candidate,
        ),
        target_connection_runner=target_connection_runner,
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


def cleanup_stage(  # noqa: C901, PLR0912, PLR0913
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    stage_state: TransferStageState,
    read_retry_cnt: int,
    *,
    drop_created_target: bool = False,
    target_connection_runner: Any | None = None,
    safe_exception_logging: bool = False,
) -> None:
    stage_cleanup_error: Exception | None = None
    remote_cleanup_error: Exception | None = None
    target_cleanup_error: Exception | None = None

    if stage_state.stage_table_created:
        for stage_table in _stage_tables_to_cleanup(stage_state):
            try:
                _run_target_operation(
                    options,
                    "cleanup_stage",
                    lambda target_ref, stage_table=stage_table: cleanup_stage_table_with_retry(
                        options.to_db_backend,
                        options.to_db_key,
                        target_ref,
                        stage_table,
                        retry_fn=lambda **kwargs: run_with_retry(
                            **kwargs,
                            safe_exception_logging=safe_exception_logging,
                        ),
                        retry_cnt=read_retry_cnt,
                        timeout_increment=options.timeout_increment,
                        rollback_fn=rollback_quietly,
                        replace_connection_fn=target_ref.get(
                            "bounded_replace_connection",
                            replace_connection,
                        ),
                        query_label=options.query_label,
                        ch_creation_policy=options.staging_ch_policy,
                    ),
                    target_connection_runner=target_connection_runner,
                )
            except Exception as exc:  # noqa: PERF203
                stage_cleanup_error = stage_cleanup_error or exc

    if stage_state.stage_external_location is not None:
        try:
            if options.parquet_storage_options is None:
                cleanup_parquet_stage_location(stage_state.stage_external_location)
            else:
                cleanup_parquet_stage_location(
                    stage_state.stage_external_location,
                    storage_options=options.parquet_storage_options,
                )
        except Exception as exc:
            remote_cleanup_error = exc

    if drop_created_target and _should_drop_created_target(stage_state):
        try:
            _run_target_operation(
                options,
                "cleanup_target",
                lambda target_ref: cleanup_stage_table_with_retry(
                    options.to_db_backend,
                    options.to_db_key,
                    target_ref,
                    options.target_table,
                    retry_fn=lambda **kwargs: run_with_retry(
                        **kwargs,
                        safe_exception_logging=safe_exception_logging,
                    ),
                    retry_cnt=read_retry_cnt,
                    timeout_increment=options.timeout_increment,
                    rollback_fn=rollback_quietly,
                    replace_connection_fn=target_ref.get(
                        "bounded_replace_connection",
                        replace_connection,
                    ),
                    query_label=options.query_label,
                    ch_creation_policy=options.regular_ch_policy,
                    operation_label="created target table",
                ),
                target_connection_runner=target_connection_runner,
            )
        except Exception as exc:
            target_cleanup_error = exc

    if stage_cleanup_error is not None:
        if remote_cleanup_error is not None:
            from analytics_toolkit.general import time_print

            time_print(
                "Remote Parquet stage cleanup failed while handling stage table "
                "cleanup error: "
                f"{_cleanup_error_label(remote_cleanup_error, safe=safe_exception_logging)}"
            )
        if target_cleanup_error is not None:
            from analytics_toolkit.general import time_print

            time_print(
                "Target cleanup failed while handling stage table cleanup error: "
                f"{_cleanup_error_label(target_cleanup_error, safe=safe_exception_logging)}"
            )
        raise stage_cleanup_error.with_traceback(stage_cleanup_error.__traceback__)
    if remote_cleanup_error is not None:
        if target_cleanup_error is not None:
            from analytics_toolkit.general import time_print

            time_print(
                "Target cleanup failed while handling remote Parquet cleanup error: "
                f"{_cleanup_error_label(target_cleanup_error, safe=safe_exception_logging)}"
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
    stage_tables.extend(stage_state.stage_table_candidates)
    if stage_state.final_upsert_stage_table is not None:
        stage_tables.append(stage_state.final_upsert_stage_table)
    return list(dict.fromkeys(stage_tables))


def _should_drop_created_target(stage_state: TransferStageState) -> bool:
    return stage_state.target_created_by_operation and stage_state.target_existed_at_start is False


def _cleanup_error_label(error: Exception, *, safe: bool) -> str:
    return type(error).__name__ if safe else repr(error)


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


def _run_target_operation(
    options: TransferOptions,
    role: str,
    operation: Any,
    *,
    target_connection_runner: Any | None,
) -> Any:
    if target_connection_runner is not None:
        return target_connection_runner(role, operation)
    return _run_with_fresh_target_connection(options, role, operation)
