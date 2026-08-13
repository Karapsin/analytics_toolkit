from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from analytics_toolkit.sql.backends.base import _apply_query_label

from ..utils import sql_string_literal


def build_upsert_stage_sqls(  # noqa: PLR0913
    adapter: Any,
    target_table: str,
    stage_table: str,
    *,
    columns: Sequence[str],
    key_columns: Sequence[str],
    column_types: dict[str, str] | None,
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

        raise InvalidSqlInputError(
            "trino_catalog is only supported for Trino connections."
        )
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
        return (
            f"ALTER TABLE {table} ADD PARTITION {name} "
            f"VALUES ({sql_string_literal(value)})"
        )
    if start is None or end is None:
        from ...connection.errors import InvalidSqlInputError

        raise InvalidSqlInputError(
            "Range partitions require both start and end values."
        )
    return (
        f"ALTER TABLE {table} ADD PARTITION {name} "
        f"START ({sql_string_literal(start)}) INCLUSIVE "
        f"END ({sql_string_literal(end)}) EXCLUSIVE"
    )
