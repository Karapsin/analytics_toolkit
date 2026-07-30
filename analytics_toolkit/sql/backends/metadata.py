from __future__ import annotations

from .utils import sql_string_literal


def build_clickhouse_show_tables_query(
    schema: str | None,
    table_names: list[str] | None,
    conditions: str | None,
    *,
    include_distributed_metadata: bool = False,
) -> str:
    filters = metadata_filters(schema, table_names, conditions)
    distributed_metadata_columns = (
        ",\n    engine,\n    engine_full"
        if include_distributed_metadata
        else ""
    )
    inner_query = f"""
    SELECT
        database AS db,
        database AS schema,
        name AS table_name,
        total_rows AS row_count,
        total_bytes AS table_size_bytes{distributed_metadata_columns}
    FROM system.tables
    """.strip()
    return normalized_metadata_query(inner_query, filters)


def build_gp_show_tables_query(
    schema: str | None,
    table_names: list[str] | None,
    conditions: str | None,
) -> str:
    filters = metadata_filters(schema, table_names, conditions)
    inner_query = """
    SELECT
        current_database() AS db,
        table_schema AS schema,
        table_name,
        CASE
            WHEN c.reltuples >= 0 THEN c.reltuples::bigint
            ELSE NULL
        END AS row_count,
        CASE
            WHEN c.relkind IN ('r', 'm', 'p') THEN pg_total_relation_size(c.oid)
            ELSE NULL
        END AS table_size_bytes
    FROM information_schema.tables AS t
    LEFT JOIN pg_catalog.pg_namespace AS n
      ON n.nspname = t.table_schema
    LEFT JOIN pg_catalog.pg_class AS c
      ON c.relnamespace = n.oid
      AND c.relname = t.table_name
    """.strip()
    return normalized_metadata_query(inner_query, filters)


def build_trino_show_tables_query(
    catalog: str,
    schema: str | None,
    table_names: list[str] | None,
    conditions: str | None,
) -> str:
    filters = metadata_filters(schema, table_names, conditions)
    inner_query = f"""
    SELECT
        table_catalog AS db,
        table_schema AS schema,
        table_name,
        CAST(NULL AS BIGINT) AS row_count,
        CAST(NULL AS BIGINT) AS table_size_bytes
    FROM {catalog}.information_schema.tables
    """.strip()
    return normalized_metadata_query(inner_query, filters)


def metadata_filters(
    schema: str | None,
    table_names: list[str] | None,
    conditions: str | None,
) -> list[str]:
    filters: list[str] = []
    if schema is not None:
        filters.append(f"schema = {sql_string_literal(schema)}")
    if table_names is not None:
        filters.append(table_names_filter("table_name", table_names))
    if conditions is not None:
        filters.append(f"({conditions})")
    return filters


def normalized_metadata_query(inner_query: str, filters: list[str]) -> str:
    return f"""
SELECT *
FROM (
{inner_query}
) AS table_metadata
WHERE 1 = 1{format_filter_lines(filters)}
ORDER BY schema, table_name
""".strip()


def format_filter_lines(filters: list[str]) -> str:
    return "".join(f"\n  AND {filter_sql}" for filter_sql in filters)


def table_names_filter(column: str, table_names: list[str]) -> str:
    if len(table_names) == 1:
        return f"{column} = {sql_string_literal(table_names[0])}"
    values = ", ".join(sql_string_literal(name) for name in table_names)
    return f"{column} IN ({values})"
