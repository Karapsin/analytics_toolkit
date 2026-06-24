from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..base import _apply_query_label
from ..utils import sql_literal


def build_upsert_stage_sqls(
    adapter: Any,
    target_table: str,
    stage_table: str,
    *,
    columns: Sequence[str],
    key_columns: Sequence[str],
    column_types: dict[str, str] | None = None,
    ch_cluster: str = "{cluster}",
    ch_only_shard: bool = False,
    query_label: str | None = None,
    upsert_partition_column: str | None = None,
    final_stage_table: str | None = None,
    incoming_stage_tables: Sequence[str] | None = None,
    partition_values: Sequence[Any] | None = None,
    trino_partition_drop_sql_template: str | None = None,
) -> list[str]:
    del trino_partition_drop_sql_template
    if upsert_partition_column is None or final_stage_table is None:
        raise ValueError(
            "upsert_partition_column and final_stage_table are required for "
            "ClickHouse write_mode='upsert'."
        )
    return adapter.build_partition_replacement_upsert_sqls(
        target_table,
        stage_table,
        final_stage_table=final_stage_table,
        columns=columns,
        key_columns=key_columns,
        partition_column=upsert_partition_column,
        column_types=column_types,
        incoming_stage_tables=incoming_stage_tables,
        partition_values=partition_values,
        ch_cluster=ch_cluster,
        ch_only_shard=ch_only_shard,
        query_label=query_label,
    )


def build_upsert_stage_placeholder_sqls(
    adapter: Any,
    target_table: str,
    stage_table: str,
    *,
    key_columns: Sequence[str],
    ch_cluster: str = "{cluster}",
    ch_only_shard: bool = False,
    query_label: str | None = None,
    upsert_partition_column: str | None = None,
    final_stage_table: str | None = None,
    incoming_stage_tables: Sequence[str] | None = None,
    partition_values: Sequence[Any] | None = None,
    trino_partition_drop_sql_template: str | None = None,
) -> list[str]:
    del trino_partition_drop_sql_template
    if upsert_partition_column is None or final_stage_table is None:
        raise ValueError(
            "upsert_partition_column and final_stage_table are required for "
            "ClickHouse write_mode='upsert'."
        )
    return [
        adapter.build_preserved_target_rows_insert_sql(
            target_table,
            stage_table,
            final_stage_table=final_stage_table,
            columns=["<source query columns>"],
            key_columns=key_columns,
            partition_column=upsert_partition_column,
            incoming_stage_tables=incoming_stage_tables,
            query_label=query_label,
        ),
        adapter.build_incoming_rows_insert_sql(
            final_stage_table,
            stage_table,
            columns=["<source query columns>"],
            column_types=None,
            incoming_stage_tables=incoming_stage_tables,
            query_label=query_label,
        ),
        *adapter.build_drop_upsert_partition_sqls(
            target_table,
            partition_column=upsert_partition_column,
            partition_values=partition_values,
            ch_cluster=ch_cluster,
            ch_only_shard=ch_only_shard,
            query_label=query_label,
        ),
        adapter.build_insert_from_stage_placeholder_sql(
            target_table,
            final_stage_table,
            query_label=query_label,
        ),
    ]


def build_drop_upsert_partition_sqls(
    adapter: Any,
    target_table: str,
    *,
    partition_column: str,
    partition_values: Sequence[Any] | None,
    query_label: str | None = None,
    trino_partition_drop_sql_template: str | None = None,
    ch_cluster: str = "{cluster}",
    ch_only_shard: bool = False,
) -> list[str]:
    del adapter, partition_column, trino_partition_drop_sql_template
    from .adapter import ch_cluster_clause
    from .lifecycle import ch_distributed_table_pair

    drop_table = (
        target_table if ch_only_shard else ch_distributed_table_pair(target_table).shard_table
    )
    cluster_clause = None if ch_only_shard else ch_cluster
    values = list(partition_values) if partition_values is not None else [
        "<affected partition value>"
    ]
    return [
        _apply_query_label(
            f"ALTER TABLE {drop_table}{ch_cluster_clause(cluster_clause)} "
            f"DROP PARTITION "
            f"{value if isinstance(value, str) and value.startswith('<') else sql_literal(value)}",
            query_label,
        )
        for value in values
    ]
