from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.backends.base import _apply_query_label
from analytics_toolkit.sql.backends.common_methods import get_table_column_names as read_names
from analytics_toolkit.sql.backends.execution_outcome import close_cursor
from analytics_toolkit.sql.ddl.properties import overlay_with_properties

from ..utils import sql_string_literal

if TYPE_CHECKING:
    from collections.abc import Mapping


def get_table_column_names(
    adapter: Any, connection: Any, table_name: str, *, connection_key: str
) -> tuple[str, ...]:
    from analytics_toolkit.sql.core.identifiers import (  # noqa: PLC0415 -- adapter registry cycle.
        TableIdentifier,
        split_gp_table_name,
    )

    parts = split_gp_table_name(table_name)
    table = TableIdentifier(parts, (True, True)).render_quoted("gp")
    return read_names(adapter, connection, table, connection_key=connection_key)


def get_writable_table_column_types(
    adapter: Any, connection: Any, table_name: str, *, connection_key: str
) -> dict[str, str]:
    """Read insertable columns, including defaults, on old and new GP catalogs."""
    from analytics_toolkit.sql.core.identifiers import (  # noqa: PLC0415 -- registry cycle.
        split_gp_table_name,
    )

    del adapter, connection_key
    schema, relation = split_gp_table_name(table_name)
    cursor = connection.cursor()
    try:
        # JSON field lookup is safe when older GP catalogs lack generated/identity fields.
        cursor.execute(
            "SELECT a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod) "
            "|| pg_catalog.repeat('[]', GREATEST(a.attndims - 1, 0)) "
            "FROM pg_catalog.pg_attribute AS a "
            "JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid "
            "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = %s "
            "AND a.attnum > 0 AND NOT a.attisdropped "
            "AND COALESCE(row_to_json(a)->>'attgenerated', '') = '' "
            "AND COALESCE(row_to_json(a)->>'attidentity', '') <> 'a' "
            "ORDER BY a.attnum",
            (schema, relation),
        )
        return {str(name): str(native_type) for name, native_type in cursor.fetchall()}
    finally:
        close_cursor(cursor, backend="gp")


def validate_existing_dataframe_insert_columns(
    adapter: Any,
    connection: Any,
    table_name: str,
    columns: Sequence[str],
    *,
    connection_key: str,
) -> None:
    """Reject explicit values for generated columns before a committed clear."""
    from analytics_toolkit.sql.core.identifiers import (  # noqa: PLC0415 -- registry cycle.
        split_gp_table_name,
    )

    del adapter, connection_key
    schema, relation = split_gp_table_name(table_name)
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT a.attname FROM pg_catalog.pg_attribute AS a "
            "JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid "
            "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = %s "
            "AND a.attnum > 0 AND NOT a.attisdropped "
            "AND (COALESCE(row_to_json(a)->>'attgenerated', '') <> '' "
            "OR COALESCE(row_to_json(a)->>'attidentity', '') = 'a') "
            "ORDER BY a.attnum",
            (schema, relation),
        )
        noninsertable = {str(row[0]) for row in cursor.fetchall()}
    finally:
        close_cursor(cursor, backend="gp")
    blocked = [column for column in columns if column in noninsertable]
    if blocked:
        message = "Incoming column(s) cannot be inserted explicitly: " + ", ".join(blocked)
        raise ValueError(message)


def build_execute_create_as_sqls(  # noqa: PLR0913
    adapter: Any,
    *,
    table_name: str,
    source_sql: str,
    gp_distributed_by_key: list[str] | None,
    gp_partitions: Any,
    partition_by: Any,
    order_by: Any,
    ddl_properties: Mapping[str, Any] | None,
    ch_creation_policy: Any,
    ch_only_shard: bool,
    if_not_exists: bool,
) -> tuple[list[str], bool]:
    del partition_by, order_by, ch_creation_policy, ch_only_shard
    if gp_partitions is not None:
        return [f"CREATE TABLE {table_name} (<schema inferred from final query>)"], True
    create = "CREATE TABLE IF NOT EXISTS" if if_not_exists else "CREATE TABLE"
    storage = (
        "WITH (appendonly=true, blocksize=32768, compresstype=zstd, "
        "compresslevel=4, orientation=column)"
    )
    distribution = (
        f"DISTRIBUTED BY ({adapter.column_list_sql(gp_distributed_by_key)})"
        if gp_distributed_by_key
        else "DISTRIBUTED RANDOMLY"
    )
    sql = f"{create} {table_name} {storage} AS {source_sql} {distribution}"
    return [overlay_with_properties(sql, ddl_properties or {})], False


def build_upsert_stage_sqls(  # noqa: PLR0913
    adapter: Any,
    target_table: str,
    stage_table: str,
    *,
    columns: Sequence[str],
    key_columns: Sequence[str],
    column_types: Mapping[str, str] | None,
    query_label: str | None,
    incoming_stage_tables: Sequence[str] | None,
) -> list[str]:
    sqls: list[str] = []
    for incoming_stage in incoming_stage_tables or [stage_table]:
        sqls.extend(
            [
                _apply_query_label(
                    adapter._build_delete_matching_stage_sql(  # noqa: SLF001
                        target_table,
                        incoming_stage,
                        key_columns,
                    ),
                    query_label,
                ),
                adapter.build_insert_from_stage_sql(
                    target_table,
                    incoming_stage,
                    columns=columns,
                    column_types=column_types,
                    query_label=query_label,
                ),
            ]
        )
    return sqls


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
    del adapter, config, ch_distributed_table_stats
    if trino_catalog is not None:
        from ...connection.errors import InvalidSqlInputError

        raise InvalidSqlInputError("trino_catalog is only supported for Trino connections.")
    from ..metadata import build_gp_show_tables_query

    return build_gp_show_tables_query(schema, table_names, conditions)


def extract_table_ddl(
    adapter: Any,
    connection_key: str,
    table_name: str,
    *,
    read_sql: Callable[[str, str], Any],
) -> str:
    del adapter
    from .ddl import extract_greenplum_table_ddl

    return extract_greenplum_table_ddl(
        connection_key,
        table_name,
        read_sql=read_sql,
    )


def validate_drop_partitions_options(
    adapter: Any,
    *,
    partition_column: str | None,
    gp_truncate: bool,
) -> None:
    del adapter, gp_truncate
    if partition_column is not None:
        from ...connection.errors import InvalidSqlInputError

        raise InvalidSqlInputError(
            "trino_partition_column is only supported for Trino partition deletes."
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
    del adapter, partition_column, ch_cluster
    action = "TRUNCATE" if gp_truncate else "DROP"
    return [
        f"ALTER TABLE {table} {action} PARTITION FOR ({sql_string_literal(key)})"
        for key in partition_keys
    ]


def build_create_partition_sql(
    adapter: Any,
    table: str,
    *,
    name: str,
    start: str | None = None,
    end: str | None = None,
    value: str | None = None,
) -> str:
    del adapter
    if value is not None:
        return f"ALTER TABLE {table} ADD PARTITION {name} VALUES ({sql_string_literal(value)})"
    if start is None or end is None:
        from ...connection.errors import InvalidSqlInputError

        raise InvalidSqlInputError("Range partitions require both start and end values.")
    return (
        f"ALTER TABLE {table} ADD PARTITION {name} "
        f"START ({sql_string_literal(start)}) INCLUSIVE "
        f"END ({sql_string_literal(end)}) EXCLUSIVE"
    )


def build_upsert_stage_sqls_with_options(  # noqa: PLR0913 -- backend adapter protocol.
    self: Any,
    target_table: str,
    stage_table: str,
    *,
    columns: Sequence[str],
    key_columns: Sequence[str],
    column_types: Mapping[str, str] | None = None,
    ch_cluster: str = "{cluster}",
    ch_only_shard: bool = False,
    query_label: str | None = None,
    upsert_partition_column: str | None = None,
    final_stage_table: str | None = None,
    incoming_stage_tables: Sequence[str] | None = None,
    partition_values: Sequence[Any] | None = None,
    trino_partition_drop_sql_template: str | None = None,
) -> list[str]:
    del (
        ch_cluster,
        ch_only_shard,
        final_stage_table,
        partition_values,
        trino_partition_drop_sql_template,
    )

    return build_upsert_stage_sqls(
        self,
        target_table,
        stage_table,
        columns=columns,
        key_columns=list(
            dict.fromkeys(
                ([upsert_partition_column] if upsert_partition_column else []) + list(key_columns)
            )
        ),
        column_types=column_types,
        query_label=query_label,
        incoming_stage_tables=incoming_stage_tables,
    )


def build_upsert_stage_placeholder_sqls(  # noqa: PLR0913 -- backend adapter protocol.
    self: Any,
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
    del (
        ch_cluster,
        ch_only_shard,
        final_stage_table,
        incoming_stage_tables,
        partition_values,
        trino_partition_drop_sql_template,
    )

    return [
        _apply_query_label(
            self._build_delete_matching_stage_sql(
                target_table,
                stage_table,
                list(
                    dict.fromkeys(
                        ([upsert_partition_column] if upsert_partition_column else [])
                        + list(key_columns)
                    )
                ),
            ),
            query_label,
        ),
        self.build_insert_from_stage_placeholder_sql(
            target_table,
            stage_table,
            query_label=query_label,
        ),
    ]
