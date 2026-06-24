from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def build_stage_duplicate_keys_sql(
    adapter: Any,
    stage_table: str,
    key_columns: Sequence[str],
) -> str:
    key_sql = adapter.column_list_sql(key_columns)
    return (
        f"SELECT 1 FROM {stage_table} "
        f"GROUP BY {key_sql} "
        "HAVING COUNT(*) > 1 "
        "LIMIT 1"
    )


def build_stage_duplicate_keys_sql_for_tables(
    adapter: Any,
    stage_tables: Sequence[str],
    key_columns: Sequence[str],
) -> str:
    if len(stage_tables) == 1:
        return adapter.build_stage_duplicate_keys_sql(stage_tables[0], key_columns)
    key_sql = adapter.column_list_sql(key_columns)
    selected_keys = ", ".join(adapter.quote_identifier(column) for column in key_columns)
    union_sql = "\nUNION ALL\n".join(
        f"SELECT {selected_keys} FROM {stage_table}" for stage_table in stage_tables
    )
    return (
        "SELECT 1 FROM (\n"
        f"{union_sql}\n"
        f") AS stage_src GROUP BY {key_sql} "
        "HAVING COUNT(*) > 1 "
        "LIMIT 1"
    )


def build_stage_target_key_overlap_sql(
    adapter: Any,
    stage_table: str,
    target_table: str,
    key_columns: Sequence[str],
) -> str:
    join_condition = " AND ".join(
        adapter.null_safe_key_equality("stage_src", "target_dst", column_name)
        for column_name in key_columns
    )
    return (
        "SELECT 1 "
        f"FROM {stage_table} AS stage_src "
        f"INNER JOIN {target_table} AS target_dst ON {join_condition} "
        "LIMIT 1"
    )


def stage_has_duplicate_keys(
    adapter: Any,
    connection: Any,
    stage_table: str,
    key_columns: Sequence[str],
) -> bool:
    return adapter.query_has_rows(
        connection,
        adapter.build_stage_duplicate_keys_sql(stage_table, key_columns),
    )


def stage_keys_overlap_target(
    adapter: Any,
    connection: Any,
    stage_table: str,
    target_table: str,
    key_columns: Sequence[str],
) -> bool:
    return adapter.query_has_rows(
        connection,
        adapter.build_stage_target_key_overlap_sql(
            stage_table,
            target_table,
            key_columns,
        ),
    )


def query_has_rows(adapter: Any, connection: Any, sql: str) -> bool:
    cursor = connection.cursor()
    try:
        cursor.execute(sql)
        return cursor.fetchone() is not None
    finally:
        cursor.close()
