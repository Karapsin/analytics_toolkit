# ruff: noqa: S608 -- SQL builders quote values; catalog identifiers are quoted by the caller.
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
        ",\n    engine,\n    engine_full" if include_distributed_metadata else ""
    )
    inner_query = f"""
    SELECT
        database AS db,
        database AS schema,
        name AS table_name,
        total_rows AS row_count,
        total_bytes AS table_size_bytes{distributed_metadata_columns}
    FROM system.tables
    WHERE 1 = 1{_base_filters("database", "name", schema, table_names)}
    """.strip()
    return normalized_metadata_query(inner_query, filters)


def build_gp_show_tables_query(
    schema: str | None,
    table_names: list[str] | None,
    conditions: str | None,
) -> str:
    filters = metadata_filters(schema, table_names, conditions)
    inner_query = f"""
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
    FROM (SELECT * FROM information_schema.tables
          WHERE 1 = 1{_base_filters("table_schema", "table_name", schema, table_names)}) AS t
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
    WHERE 1 = 1{_base_filters("table_schema", "table_name", schema, table_names)}
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


def completion_prefix_filter(column: str, prefix: str) -> str:
    escaped = prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return f"lower({column}) LIKE lower({sql_string_literal(escaped + '%')}) ESCAPE '!'"


def build_gp_completion_query(schema: str | None, prefix: str, partition_catalog: str) -> str:
    filters = [completion_prefix_filter("t.table_name", prefix)]
    if schema is not None:
        filters.append(f"t.table_schema = {sql_string_literal(schema)}")
    if partition_catalog == "legacy":
        filters.append("""NOT EXISTS (
 SELECT 1 FROM pg_catalog.pg_partitions p
 JOIN information_schema.tables parent
   ON parent.table_schema = p.schemaname AND parent.table_name = p.tablename
 WHERE p.partitionschemaname = t.table_schema AND p.partitiontablename = t.table_name
)""")
    elif partition_catalog == "declarative":
        filters.append("""NOT EXISTS (
 SELECT 1 FROM pg_catalog.pg_class child
 JOIN pg_catalog.pg_namespace ns ON ns.oid = child.relnamespace
 JOIN pg_catalog.pg_inherits inheritance ON inheritance.inhrelid = child.oid
 JOIN pg_catalog.pg_class parent ON parent.oid = inheritance.inhparent
 JOIN pg_catalog.pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
 JOIN information_schema.tables visible_parent
   ON visible_parent.table_schema = parent_ns.nspname AND visible_parent.table_name = parent.relname
 WHERE child.relispartition AND ns.nspname = t.table_schema AND child.relname = t.table_name
)""")
    return (
        "SELECT t.table_name FROM information_schema.tables t WHERE "
        + " AND ".join(filters)
        + " ORDER BY t.table_name"
    )


GP_PARTITION_CAPABILITIES_QUERY = """
SELECT CASE
 WHEN EXISTS (SELECT 1 FROM pg_catalog.pg_class c
              JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
              WHERE n.nspname='pg_catalog' AND c.relname='pg_partitions') THEN 'legacy'
 WHEN EXISTS (SELECT 1 FROM pg_catalog.pg_attribute
              WHERE attrelid='pg_catalog.pg_class'::regclass
                AND attname='relispartition') THEN 'declarative'
 ELSE 'none' END AS partition_catalog
""".strip()


def build_ch_completion_query(schema: str | None, prefix: str) -> str:
    filters = [completion_prefix_filter("name", prefix)]
    if schema is not None:
        filters.append(f"database = {sql_string_literal(schema)}")
    return "SELECT name FROM system.tables WHERE " + " AND ".join(filters) + " ORDER BY name"


def build_trino_completion_query(catalog: str, schema: str | None, prefix: str) -> str:
    filters = [completion_prefix_filter("table_name", prefix)]
    if schema is not None:
        filters.append(f"table_schema = {sql_string_literal(schema)}")
    return (
        f"SELECT table_name FROM {catalog}.information_schema.tables WHERE "
        + " AND ".join(filters)
        + " ORDER BY table_name"
    )


def _base_filters(
    schema_column: str, name_column: str, schema: str | None, names: list[str] | None
) -> str:
    filters = []
    if schema is not None:
        filters.append(f"{schema_column} = {sql_string_literal(schema)}")
    if names is not None:
        filters.append(table_names_filter(name_column, names))
    return format_filter_lines(filters)
