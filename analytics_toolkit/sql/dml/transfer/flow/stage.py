from __future__ import annotations

import pandas as pd

from ....backends import get_backend_adapter
from ....ddl.schema import validate_table_schema_columns
from ...load.stage import create_stage_table
from ...table._basic_ops import table_exists
from ...table.table_validation import (
    validate_key_columns_in_columns,
    validate_upsert_partition_column_in_columns,
)
from ...table.write_modes import _ensure_stage_target_table
from ..runtime.models import (
    RowBatch,
    TransferConnectionRefs,
    TransferOptions,
    TransferStageState,
)
from ..schema import refine_stage_column_types_from_rows


def create_stage_state(
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
) -> TransferStageState:
    target_exists = table_exists(
        options.to_db_backend,
        connection_refs.target["connection"],
        options.target_table,
        connection_key=options.to_db_key,
    )
    return TransferStageState(
        target_exists=target_exists,
        target_existed_at_start=target_exists,
    )


def ensure_transfer_target_table(
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    stage_state: TransferStageState,
    source_columns: list[str],
) -> None:
    if stage_state.target_exists:
        return
    if not get_backend_adapter(
        options.to_db_backend,
    ).can_create_transfer_target_before_batches():
        return

    create_columns = source_columns or list(stage_state.stage_column_types or {})
    if not create_columns:
        raise ValueError(
            "Cannot create target table before transfer batches because the "
            "source query schema has no columns."
        )

    _ensure_stage_target_table(
        backend=options.to_db_backend,
        connection=connection_refs.target["connection"],
        target_table=options.target_table,
        sample_batch=pd.DataFrame(columns=create_columns),
        target_column_types=stage_state.stage_column_types,
        gp_distributed_by_key=options.gp_distributed_by_key,
        gp_partitions=options.gp_partitions,
        partition_by=options.partition_by,
        order_by=options.order_by,
        ch_engine=options.ch_engine,
        ch_cluster=options.ch_cluster,
        ch_sharding_key=options.ch_sharding_key,
        query_label=options.query_label,
        connection_key=options.to_db_key,
        ch_only_shard=options.ch_only_shard,
        ch_creation_policy=options.regular_ch_policy,
    )
    if stage_state.target_existed_at_start is None:
        stage_state.target_existed_at_start = False
    stage_state.target_exists = True
    stage_state.target_created_by_operation = stage_state.target_existed_at_start is False


def initialize_stage_for_first_batch(
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    stage_state: TransferStageState,
    batch: RowBatch,
) -> None:
    if options.table_schema is not None:
        stage_state.stage_column_types = validate_table_schema_columns(
            options.table_schema,
            batch.columns,
        )
    else:
        stage_state.stage_column_types = refine_stage_column_types_from_rows(
            options.to_db_backend,
            stage_state.stage_column_types,
            batch.columns,
            batch.rows,
        )
    sample_batch = batch.to_dataframe(
        include_rows=stage_state.stage_column_types is None,
    )
    stage_state.first_non_empty_batch = sample_batch
    validate_key_columns_in_columns(
        options.key_columns,
        batch.columns,
    )
    validate_upsert_partition_column_in_columns(
        options.upsert_partition_column,
        batch.columns,
    )
    validate_key_columns_in_columns(
        options.gp_distributed_by_key,
        batch.columns,
    )
    get_backend_adapter(options.to_db_backend).validate_ch_columns_in_columns(
        options.partition_by,
        batch.columns,
        "partition_by",
        data_name="staged data",
    )
    get_backend_adapter(options.to_db_backend).validate_ch_columns_in_columns(
        options.order_by,
        batch.columns,
        "order_by",
        data_name="staged data",
    )
    stage_state.stage_table = create_stage_table(
        connection_type=options.to_db_backend,
        connection=connection_refs.target["connection"],
        target_table=options.target_table,
        batch=sample_batch,
        column_types=stage_state.stage_column_types,
        gp_distributed_by_key=options.gp_distributed_by_key,
        connection_key=options.to_db_key,
        query_label=options.query_label,
        transfer_staging_schema=options.transfer_staging_schema,
        transfer_staging_username=options.transfer_staging_username,
    )
    stage_state.stage_table_created = True
    stage_state.stage_column_types = get_backend_adapter(
        options.to_db_backend,
    ).resolve_transfer_stage_column_types(
        connection_refs.target["connection"],
        stage_state.stage_table,
        connection_key=options.to_db_key,
        current_column_types=stage_state.stage_column_types,
    )
