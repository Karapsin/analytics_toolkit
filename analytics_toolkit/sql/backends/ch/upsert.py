from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlglot import exp, parse_one

from analytics_toolkit.sql.backends.base import _apply_query_label
from analytics_toolkit.sql.backends.upsert import incoming_stage_source_sql
from analytics_toolkit.sql.backends.utils import sql_literal

from .dialect import ClickHouseDialect, sql_string_literal
from .routing import local_sql
from .upsert_context import verified_local_upsert
from .upsert_storage import verified_upsert_storage

if TYPE_CHECKING:
    from collections.abc import Sequence


def resolve_upsert_execution_options(
    adapter: Any, connection: Any, table_name: str, **options: Any
) -> dict[str, Any]:
    from .layout import inspect_existing_layout  # noqa: PLC0415 -- adapter/layout import cycle.

    del adapter
    layout = inspect_existing_layout(connection, table_name)
    return {
        **options,
        "ch_cluster": layout.shard_on_cluster,
        "ch_only_shard": not layout.create_distributed_pair,
    }


def resolve_upsert_execution_sqls(
    adapter: Any, connection: Any, table_name: str, sqls: list[str]
) -> list[str]:
    from .layout import inspect_existing_layout  # noqa: PLC0415 -- backend registry cycle.

    del adapter
    layout = inspect_existing_layout(connection, table_name)
    result = []
    for sql in sqls:
        tree = parse_one(sql, read=ClickHouseDialect)
        if isinstance(tree, exp.Alter):
            tree.set("this", exp.to_table(layout.physical_table, dialect=ClickHouseDialect))
            tree.set(
                "cluster",
                exp.OnCluster(this=exp.Literal.string(layout.shard_on_cluster))
                if layout.shard_on_cluster is not None
                else None,
            )
            result.append(tree.sql(dialect=ClickHouseDialect))
        else:
            result.append(sql)
    return result


def execute_upsert_command(adapter: Any, connection: Any, table_name: str, sql: str) -> Any:
    from .layout import inspect_existing_layout  # noqa: PLC0415 -- backend registry cycle.
    from .reconfigure_ddl import (  # noqa: PLC0415 -- DDL parsing imports backend registry helpers.
        _distributed_sharding_key,
        parse_create_table,
    )

    layout = inspect_existing_layout(connection, table_name)
    sharding_key = (
        _distributed_sharding_key(parse_create_table(layout.ddl, layout.logical_table))
        if layout.create_distributed_pair
        else None
    )
    with verified_upsert_storage(connection, layout, sharding_key=sharding_key):
        return _execute_upsert_on_storage(adapter, connection, layout, sql)


def _execute_upsert_on_storage(adapter: Any, connection: Any, layout: Any, sql: str) -> Any:
    tree = parse_one(sql, read=ClickHouseDialect)
    if not isinstance(tree, exp.Alter):
        return adapter.execute_command(connection, sql)
    tree.set("this", exp.to_table(layout.physical_table, dialect=ClickHouseDialect))
    tree.set(
        "cluster",
        exp.OnCluster(this=exp.Literal.string(layout.shard_on_cluster))
        if layout.shard_on_cluster is not None
        else None,
    )
    resolved_sql = tree.sql(dialect=ClickHouseDialect)
    if (
        layout.shard_on_cluster is None
        and tree.args.get("actions")
        and all(isinstance(action, exp.DropPartition) for action in tree.args["actions"])
    ):
        with verified_local_upsert(connection, layout.physical_table), local_sql(connection):
            return adapter.execute_command(connection, resolved_sql)
    return adapter.execute_command(connection, resolved_sql)


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
        for column_name in dict.fromkeys([partition_column, *key_columns])
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
    literals = (
        [
            sql_string_literal(value) if isinstance(value, str) else sql_literal(value)
            for value in partition_values
        ]
        if partition_values is not None
        else ["<affected partition value>"]
    )
    return [
        _apply_query_label(
            f"ALTER TABLE {drop_table}{ch_cluster_clause(cluster_clause)} DROP PARTITION {literal}",
            query_label,
        )
        for literal in literals
    ]
