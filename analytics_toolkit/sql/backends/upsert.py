from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

def build_upsert_partition_values_sql(
    adapter: Any,
    stage_table: str,
    *,
    partition_column: str,
    incoming_stage_tables: Sequence[str] | None = None,
) -> str:
    incoming_source = incoming_stage_source_sql(
        adapter,
        stage_table,
        incoming_stage_tables=incoming_stage_tables,
        columns=[partition_column],
    )
    quoted_partition = adapter.quote_identifier(partition_column)
    return f"SELECT DISTINCT {quoted_partition} FROM {incoming_source}"


def fetch_upsert_partition_values(
    adapter: Any,
    connection: Any,
    stage_table: str,
    *,
    partition_column: str,
    incoming_stage_tables: Sequence[str] | None = None,
) -> list[Any]:
    cursor = connection.cursor()
    try:
        cursor.execute(
            adapter.build_upsert_partition_values_sql(
                stage_table,
                partition_column=partition_column,
                incoming_stage_tables=incoming_stage_tables,
            )
        )
        return [row[0] for row in cursor.fetchall()]
    finally:
        cursor.close()


def build_partition_replacement_upsert_sqls(
    adapter: Any,
    target_table: str,
    stage_table: str,
    *,
    final_stage_table: str,
    columns: Sequence[str],
    key_columns: Sequence[str],
    partition_column: str,
    column_types: Mapping[str, str] | None,
    incoming_stage_tables: Sequence[str] | None = None,
    partition_values: Sequence[Any] | None = None,
    ch_cluster: str = "{cluster}",
    ch_only_shard: bool = False,
    query_label: str | None = None,
    trino_partition_drop_sql_template: str | None = None,
) -> list[str]:
    drop_sqls = adapter.build_drop_upsert_partition_sqls(
        target_table,
        partition_column=partition_column,
        partition_values=partition_values,
        query_label=query_label,
        trino_partition_drop_sql_template=trino_partition_drop_sql_template,
        ch_cluster=ch_cluster,
        ch_only_shard=ch_only_shard,
    )
    return [
        adapter.build_preserved_target_rows_insert_sql(
            target_table,
            stage_table,
            final_stage_table=final_stage_table,
            columns=columns,
            key_columns=key_columns,
            partition_column=partition_column,
            incoming_stage_tables=incoming_stage_tables,
            query_label=query_label,
        ),
        adapter.build_incoming_rows_insert_sql(
            final_stage_table,
            stage_table,
            columns=columns,
            column_types=column_types,
            incoming_stage_tables=incoming_stage_tables,
            query_label=query_label,
        ),
        *drop_sqls,
        adapter.build_insert_from_stage_sql(
            target_table,
            final_stage_table,
            columns=columns,
            column_types=column_types,
            query_label=query_label,
        ),
    ]


def build_preserved_target_rows_insert_sql(
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
    partition_predicate = adapter.null_safe_key_equality(
        "target_dst",
        "affected_partition",
        partition_column,
    )
    key_predicates = " AND ".join(
        adapter.null_safe_key_equality("target_dst", "stage_src", column_name)
        for column_name in key_columns
    )
    partition_sql = adapter.quote_identifier(partition_column)
    return _apply_query_label(
        f"INSERT INTO {final_stage_table} ({target_columns})\n"
        f"SELECT {selected_columns}\n"
        f"FROM {target_table} AS target_dst\n"
        "WHERE EXISTS (\n"
        f"  SELECT 1 FROM (\n"
        f"    SELECT DISTINCT {partition_sql} FROM {incoming_partitions}\n"
        f"  ) AS affected_partition\n"
        f"  WHERE {partition_predicate}\n"
        ")\n"
        "AND NOT EXISTS (\n"
        f"  SELECT 1 FROM {incoming_source} AS stage_src\n"
        f"  WHERE {key_predicates}\n"
        ")",
        query_label,
    )


def build_incoming_rows_insert_sql(
    adapter: Any,
    final_stage_table: str,
    stage_table: str,
    *,
    columns: Sequence[str],
    column_types: Mapping[str, str] | None,
    incoming_stage_tables: Sequence[str] | None = None,
    query_label: str | None = None,
) -> str:
    incoming_source = incoming_stage_source_sql(
        adapter,
        stage_table,
        incoming_stage_tables=incoming_stage_tables,
        columns=columns,
    )
    if column_types:
        sql = adapter._build_typed_insert_select_sql(
            final_stage_table,
            f"FROM {incoming_source}",
            adapter.column_types_for_columns(column_types, columns) or {},
        )
    else:
        target_columns = adapter.column_list_sql(columns)
        selected_columns = ", ".join(adapter.quote_identifier(column) for column in columns)
        sql = (
            f"INSERT INTO {final_stage_table} ({target_columns}) "
            f"SELECT {selected_columns} FROM {incoming_source}"
        )
    return _apply_query_label(sql, query_label)


def incoming_stage_source_sql(
    adapter: Any,
    stage_table: str,
    *,
    incoming_stage_tables: Sequence[str] | None = None,
    columns: Sequence[str] | None = None,
) -> str:
    stage_tables = list(incoming_stage_tables or [stage_table])
    if len(stage_tables) == 1:
        return stage_tables[0]
    if not columns:
        raise ValueError("columns are required for multi-stage upsert SQL.")
    selected_columns = ", ".join(adapter.quote_identifier(column) for column in columns)
    union_sql = "\nUNION ALL\n".join(
        f"SELECT {selected_columns} FROM {table_name}" for table_name in stage_tables
    )
    return f"(\n{union_sql}\n)"


def _apply_query_label(sql: str, query_label: str | None) -> str:
    from ..execution.labels import apply_query_label

    return apply_query_label(sql, query_label)
