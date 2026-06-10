from __future__ import annotations

import warnings
from typing import Any

from ...table._basic_ops import split_trino_table_name
from ...load.stage import build_stage_table_prefix
from ...table.maintenance import (
    analyze_table,
    clear_ch_distributed_table_data,
    drop_table_with_retry,
)
from ...table.write_modes import (
    clear_target_table,
    finalize_stage_table,
)
from ...table.table_validation import (
    validate_stage_target_key_overlap,
    validate_stage_uniqueness,
)
from ..runtime.models import TransferConnectionRefs, TransferOptions, TransferStageState
from ..runtime.retry import replace_connection, rollback_quietly, run_with_retry
from ..schema import get_existing_target_insert_types


def cleanup_transfer_staging_schema(
    options: TransferOptions,
    connection_ref: dict[str, Any],
    read_retry_cnt: int,
) -> None:
    if not options.clean_transfer_staging_schema:
        return

    if not options.transfer_staging_schema:
        _warn_transfer_staging_schema_cleanup_not_configured(options)
        return

    matching_tables = _find_matching_transfer_stage_tables(
        options,
        connection=connection_ref,
    )
    for table_name in matching_tables:
        drop_table_with_retry(
            options.to_db_backend,
            options.to_db_key,
            connection_ref,
            table_name,
            retry_fn=run_with_retry,
            retry_cnt=read_retry_cnt,
            timeout_increment=options.timeout_increment,
            rollback_fn=rollback_quietly,
            replace_connection_fn=replace_connection,
            query_label=options.query_label,
        )


def _warn_transfer_staging_schema_cleanup_not_configured(options: TransferOptions) -> None:
    if options._warned_transfer_staging_schema_cleanup:
        return

    warnings.warn(
        "clean_transfer_staging_schema is enabled, "
        "but transfer_staging_schema is not configured for the target connection",
    )
    object.__setattr__(
        options,
        "_warned_transfer_staging_schema_cleanup",
        True,
    )


def _find_matching_transfer_stage_tables(
    options: TransferOptions,
    connection: dict[str, Any],
) -> list[str]:
    transfer_schema = options.transfer_staging_schema
    if transfer_schema is None:
        return []

    prefix = _build_transfer_stage_prefix(options)
    like_prefix = f"{prefix}%"
    if options.to_db_backend == "gp":
        table_names = _query_gp_stage_tables(
            transfer_schema,
            like_prefix,
            connection=connection["connection"],
        )
    elif options.to_db_backend == "trino":
        table_names = _query_trino_stage_tables(
            transfer_schema,
            options.to_db_key,
            like_prefix,
            connection=connection["connection"],
        )
    elif options.to_db_backend == "ch":
        table_names = _query_ch_stage_tables(transfer_schema, connection["connection"])
    else:
        raise ValueError(
            f"Unsupported transfer backend for staging cleanup: {options.to_db_backend}"
        )

    prefix = build_stage_table_prefix(
        options.to_db_backend,
        options.target_table,
        options.transfer_staging_username,
    )
    return [
        _qualify_staging_table_name(options, table_name)
        for table_name in table_names
        if table_name.startswith(prefix)
    ]


def _query_gp_stage_tables(
    transfer_staging_schema: str,
    table_prefix: str,
    *,
    connection: Any,
) -> list[str]:
    cursor = _require_cursor(connection)
    try:
        cursor.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_name LIKE %s
            """.strip(),
            (transfer_staging_schema, table_prefix),
        )
        return [str(row[0]) for row in (cursor.fetchall() or [])]
    finally:
        cursor.close()


def _query_trino_stage_tables(
    transfer_staging_schema: str,
    connection_key: str,
    table_prefix: str,
    *,
    connection: Any,
) -> list[str]:
    catalog_name, schema_name, _ = split_trino_table_name(
        f"{transfer_staging_schema}.__analytics_toolkit_stage_marker__",
        connection_key=connection_key,
    )
    cursor = _require_cursor(connection)
    try:
        cursor.execute(
            f"""
            SELECT table_name
            FROM {catalog_name}.information_schema.tables
            WHERE table_schema = ?
              AND table_name LIKE ?
            """.strip(),
            (schema_name, table_prefix),
        )
        return [str(row[0]) for row in (cursor.fetchall() or [])]
    finally:
        cursor.close()


def _query_ch_stage_tables(
    transfer_staging_schema: str,
    *,
    connection: Any,
) -> list[str]:
    result = _require_query(connection).query(
        "SELECT name FROM system.tables WHERE database = "
        f"{_quote_sql_literal(transfer_staging_schema)}"
    )
    rows = getattr(result, "result_rows", None) or []
    return [str(row[0]) for row in rows if row]


def _qualify_staging_table_name(options: TransferOptions, table_name: str) -> str:
    if options.to_db_backend == "ch":
        return f"{options.transfer_staging_schema}.{table_name}"
    if options.to_db_backend == "trino":
        _, schema_name, _ = split_trino_table_name(
            f"{options.transfer_staging_schema}.__analytics_toolkit_stage_marker__",
            connection_key=options.to_db_key,
        )
        return f"{schema_name}.{table_name}"
    return f"{options.transfer_staging_schema}.{table_name}"


def _build_transfer_stage_prefix(options: TransferOptions) -> str:
    return build_stage_table_prefix(
        options.to_db_backend,
        options.target_table,
        options.transfer_staging_username,
    )


def _quote_sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _require_cursor(connection: object) -> Any:
    if not hasattr(connection, "cursor"):
        raise TypeError("Target connection must provide a cursor() method.")
    return connection.cursor()


def _require_query(connection: object):
    if not hasattr(connection, "query"):
        raise TypeError("Target connection must provide a query() method.")
    return connection


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

    validate_stage_uniqueness(
        connection_type=options.to_db_backend,
        connection=connection_refs.target["connection"],
        stage_table=stage_state.stage_table,
        key_columns=options.key_columns,
    )
    validate_stage_target_key_overlap(
        connection_type=options.to_db_backend,
        connection=connection_refs.target["connection"],
        stage_table=stage_state.stage_table,
        target_table=options.target_table,
        key_columns=options.key_columns,
        target_exists=stage_state.target_exists,
        replace_target_table=options.replace_target_table,
    )
    if stage_state.stage_column_types is None:
        stage_state.insert_column_types = None
        target_column_types = None
    elif stage_state.target_exists and not options.replace_target_table:
        stage_state.insert_column_types = get_existing_target_insert_types(
            options.to_db_backend,
            connection_refs.target["connection"],
            options.target_table,
            stage_state.stage_column_types,
            connection_key=options.to_db_key,
        )
        target_column_types = None
    else:
        stage_state.insert_column_types = stage_state.stage_column_types
        target_column_types = stage_state.stage_column_types

    finalize_stage_table(
        options.to_db_backend,
        connection_refs.target["connection"],
        stage_table=stage_state.stage_table,
        target_table=options.target_table,
        replace_target_table=options.replace_target_table,
        target_exists=stage_state.target_exists,
        sample_batch=stage_state.first_non_empty_batch,
        target_column_types=target_column_types,
        insert_column_types=stage_state.insert_column_types,
        write_mode=options.write_mode,
        gp_distributed_by_key=options.gp_distributed_by_key,
        partition_by=options.partition_by,
        order_by=options.order_by,
        ch_engine=options.ch_engine,
        ch_cluster=options.ch_cluster,
        ch_sharding_key=options.ch_sharding_key,
        ch_only_shard=options.ch_only_shard,
        query_label=options.query_label,
        connection_key=options.to_db_key,
        ch_retry_per_host_drops=options.ch_retry_per_host_drops,
    )
    analyze_table(
        connection_type=options.to_db_backend,
        connection=connection_refs.target["connection"],
        table_name=options.target_table,
        query_label=options.query_label,
    )


def finalize_empty_transfer(
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    stage_state: TransferStageState,
) -> None:
    if options.replace_target_table:
        if not stage_state.target_exists:
            raise ValueError("Cannot create target table from an empty result set.")
        if options.to_db_backend == "ch":
            if options.ch_only_shard:
                clear_target_table(
                    options.to_db_backend,
                    connection_refs.target["connection"],
                    options.target_table,
                    query_label=options.query_label,
                )
                return
            clear_ch_distributed_table_data(
                connection_refs.target["connection"],
                options.target_table,
                ch_cluster=options.ch_cluster,
                query_label=options.query_label,
            )
            return
        clear_target_table(
            options.to_db_backend,
            connection_refs.target["connection"],
            options.target_table,
            query_label=options.query_label,
        )
        return

    if not stage_state.target_exists:
        raise ValueError("Cannot create target table from an empty result set.")


def cleanup_stage(
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    stage_state: TransferStageState,
    read_retry_cnt: int,
) -> None:
    if not stage_state.stage_table_created:
        return

    drop_table_with_retry(
        options.to_db_backend,
        options.to_db_key,
        connection_refs.target,
        stage_state.stage_table,
        retry_fn=run_with_retry,
        retry_cnt=read_retry_cnt,
        timeout_increment=options.timeout_increment,
        rollback_fn=rollback_quietly,
        replace_connection_fn=replace_connection,
        query_label=options.query_label,
    )
