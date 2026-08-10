from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from analytics_toolkit.sql.backends.base import _apply_query_label

from ..models import TargetConnectionDefaults
from ..utils import sql_literal


def build_materialize_transfer_source_sql(
    adapter: Any,
    table_name: str,
    source_sql: str,
    *,
    query_label: str | None = None,
) -> str:
    return _apply_query_label(
        f"CREATE TABLE {table_name} AS {adapter.strip_query_semicolon(source_sql)}",
        query_label,
    )


def target_connection_defaults(adapter: Any, config: Any) -> TargetConnectionDefaults:
    del adapter
    return TargetConnectionDefaults(
        insert_chunk_size=config.insert_chunk_size,
        s3_transfer_staging_location=config.s3_transfer_staging_location,
        upsert_partition_drop_sql_template=config.upsert_partition_drop_sql_template,
    )


def resolve_transfer_staging_mode(
    adapter: Any,
    requested_mode: Any,
    *,
    s3_transfer_staging_schema: str | None,
    s3_transfer_staging_location: str | None,
) -> Any:
    del adapter
    if requested_mode is None:
        if s3_transfer_staging_schema is not None:
            return "parquet"
        return "values"
    if requested_mode not in {"parquet", "values"}:
        raise ValueError("trino_mode must be one of: 'parquet', 'values'.")
    if requested_mode == "parquet" and s3_transfer_staging_schema is None:
        raise ValueError("trino_mode='parquet' requires s3_transfer_staging_schema on to_db.")
    if requested_mode == "parquet" and s3_transfer_staging_location is None:
        raise ValueError("trino_mode='parquet' requires s3_transfer_staging_location on to_db.")
    return requested_mode


def resolve_transfer_stage_column_types(
    adapter: Any,
    connection: Any,
    stage_table: str,
    *,
    connection_key: str,
    current_column_types: dict[str, str] | None,
) -> dict[str, str] | None:
    del adapter
    if current_column_types is not None:
        return current_column_types

    from ...dml.table._basic_ops import get_trino_table_column_types

    return get_trino_table_column_types(
        connection,
        stage_table,
        connection_key=connection_key,
    )


def build_show_tables_query(
    adapter: Any,
    config: Any,
    schema: str | None,
    table_names: list[str] | None,
    conditions: str | None,
    *,
    trino_catalog: str | None = None,
    ch_distributed_table_stats: bool = False,
) -> str:
    del adapter, ch_distributed_table_stats
    catalog = trino_catalog or getattr(config, "catalog", None)
    if not catalog:
        raise ValueError(
            "show_tables for Trino requires "
            f"trino_catalog or .connections['{config.connection_key}'].catalog."
        )
    from ..metadata import build_trino_show_tables_query

    return build_trino_show_tables_query(
        catalog,
        schema,
        table_names,
        conditions,
    )


def extract_table_ddl(
    adapter: Any,
    connection_key: str,
    table_name: str,
    *,
    read_sql: Callable[[str, str], Any],
) -> str:
    del adapter
    from .adapter import split_trino_table_name

    catalog, schema_name, relation_name = split_trino_table_name(
        table_name,
        connection_key=connection_key,
    )
    result = read_sql(
        connection_key,
        f"SHOW CREATE TABLE {catalog}.{schema_name}.{relation_name}",
    )
    return _first_result_value(result, table_name)


def validate_drop_partitions_options(
    adapter: Any,
    *,
    partition_column: str | None,
    gp_truncate: bool,
) -> None:
    del adapter
    from ...connection.errors import InvalidSqlInputError

    if gp_truncate:
        raise InvalidSqlInputError("gp_truncate=True is only supported for Greenplum connections.")
    if partition_column is None:
        raise InvalidSqlInputError(
            "trino_partition_column is required for Trino partition deletes."
        )


def build_drop_partitions_sqls(
    adapter: Any,
    table: str,
    partition_keys: Sequence[str],
    *,
    partition_column: str | None = None,
    gp_truncate: bool = False,
    ch_cluster: str = "{cluster}",
) -> list[str]:
    del adapter, gp_truncate, ch_cluster
    if partition_column is None:
        from ...connection.errors import InvalidSqlInputError

        raise InvalidSqlInputError(
            "trino_partition_column is required for Trino partition deletes."
        )
    partition_values = ", ".join(f"DATE {sql_literal(key)}" for key in partition_keys)
    return [f"DELETE FROM {table}\nWHERE {partition_column} IN ({partition_values})"]


def query_transfer_stage_table_names(
    adapter: Any,
    connection: Any,
    *,
    connection_key: str,
    transfer_staging_schema: str,
    table_pattern: str,
) -> list[str]:
    del adapter
    from .adapter import split_trino_table_name

    catalog_name, schema_name, _ = split_trino_table_name(
        f"{transfer_staging_schema}.__analytics_toolkit_stage_marker__",
        connection_key=connection_key,
    )
    cursor = connection.cursor()
    try:
        cursor.execute(
            f"""
            SELECT table_name
            FROM {catalog_name}.information_schema.tables
            WHERE table_schema = ?
              AND table_name LIKE ?
            """.strip(),
            (schema_name, table_pattern),
        )
        return [str(row[0]) for row in (cursor.fetchall() or [])]
    finally:
        cursor.close()


def qualify_transfer_stage_table_name(
    adapter: Any,
    connection_key: str,
    transfer_staging_schema: str,
    table_name: str,
) -> str:
    del adapter
    from .adapter import split_trino_table_name

    catalog_name, schema_name, _ = split_trino_table_name(
        f"{transfer_staging_schema}.__analytics_toolkit_stage_marker__",
        connection_key=connection_key,
    )
    return f"{catalog_name}.{schema_name}.{table_name}"


def _first_result_value(result: Any, table_name: str) -> str:
    import pandas as pd

    if result.empty or len(result.columns) == 0:
        raise ValueError(f"No DDL returned for table {table_name}.")

    value = result.iat[0, 0]
    if pd.isna(value):
        raise ValueError(f"No DDL returned for table {table_name}.")
    return str(value)
