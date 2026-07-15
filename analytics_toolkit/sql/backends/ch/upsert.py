from __future__ import annotations

from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.backends.base import _apply_query_label
from analytics_toolkit.sql.backends.upsert import incoming_stage_source_sql
from analytics_toolkit.sql.backends.utils import sql_literal

if TYPE_CHECKING:
    from collections.abc import Sequence


def build_upsert_stage_sqls(  # noqa: PLR0913 - mirrors the backend adapter protocol.
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
        msg = (
            "upsert_partition_column and final_stage_table are required for "
            "ClickHouse write_mode='upsert'."
        )
        raise ValueError(msg)
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


def build_preserved_target_rows_insert_sql(  # noqa: PLR0913 - mirrors the backend adapter protocol.
    adapter: Any,
    target_table: str,
    stage_table: str,
    *,
    final_stage_table: str,
    columns: Sequence[str],
    key_columns: Sequence[str],
    partition_column: str,
    incoming_stage_tables: Sequence[str] | None = None,
    query_label: str | None = None,
) -> str:
    """Build ClickHouse-compatible preservation SQL without correlated EXISTS."""
    target_columns = adapter.column_list_sql(columns)
    selected_columns = ", ".join(
        f"target_dst.{adapter.quote_identifier(column)}" for column in columns
    )
    incoming_source = incoming_stage_source_sql(
        adapter,
        stage_table,
        incoming_stage_tables=incoming_stage_tables,
        columns=columns,
    )
    incoming_partitions = incoming_stage_source_sql(
        adapter,
        stage_table,
        incoming_stage_tables=incoming_stage_tables,
        columns=[partition_column],
    )
    partition_sql = adapter.quote_identifier(partition_column)
    partition_predicate = adapter.null_safe_key_equality(
        "target_dst",
        "affected_partition",
        partition_column,
    )
    key_predicates = " AND ".join(
        adapter.null_safe_key_equality("target_dst", "stage_src", column_name)
        for column_name in key_columns
    )
    return _apply_query_label(
        f"INSERT INTO {final_stage_table} ({target_columns})\n"  # noqa: S608 - identifiers are adapter-quoted.
        f"SELECT {selected_columns}\n"
        f"FROM {target_table} AS target_dst\n"
        "INNER JOIN (\n"
        f"  SELECT DISTINCT {partition_sql} FROM {incoming_partitions}\n"
        f") AS affected_partition ON {partition_predicate}\n"
        f"LEFT ANTI JOIN {incoming_source} AS stage_src ON {key_predicates}",
        query_label,
    )


def build_upsert_stage_placeholder_sqls(  # noqa: PLR0913 - mirrors the backend adapter protocol.
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
        msg = (
            "upsert_partition_column and final_stage_table are required for "
            "ClickHouse write_mode='upsert'."
        )
        raise ValueError(msg)
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


def build_drop_upsert_partition_sqls(  # noqa: PLR0913 - mirrors the backend adapter protocol.
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
    from .adapter import ch_cluster_clause  # noqa: PLC0415 - avoid adapter cycle.
    from .lifecycle import ch_distributed_table_pair  # noqa: PLC0415 - avoid adapter cycle.

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
