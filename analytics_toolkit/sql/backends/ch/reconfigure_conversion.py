from __future__ import annotations

# ruff: noqa: PLR0913, S608, TC002
from sqlglot import exp

from .reconfigure_ddl import (
    retarget_create,
    transform_distributed_create,
)
from .reconfigure_execution import cluster_clause
from .reconfigure_models import ChReconfiguration, ChReconfigureOptions
from .reconfigure_support import add_sqls, drop_table_sql, new_plan, suffixed_table


def plan_topology_conversion(
    *,
    options: ChReconfigureOptions,
    table: str,
    source_shard_table: str,
    desired_shard_table: str,
    table_create: exp.Create,
    desired_shard: exp.Create,
    desired_table: exp.Create,
    source_pair: bool,
    source_cluster: str | None,
    target_cluster: str | None,
    source_cluster_resolved: str | None,
    target_cluster_resolved: str | None,
    distributed_on_cluster: str | None,
    distributed_cluster: str | None,
    database_engine: str,
    before_ddl: dict[str, str],
    after_ddl: dict[str, str],
    token: str,
) -> ChReconfiguration:
    copy_wrapper = suffixed_table(table, f"__copy_{token}")
    facade_replacement = suffixed_table(table, f"__reconfigure_{token}")
    source_backup = suffixed_table(table, f"__backup_{token}")
    create_sqls: list[str] = []
    cleanup_sqls: list[str] = []
    cleanup_tables: list[tuple[str, str | None]] = []
    temporary_scopes: list[tuple[str, str | None]] = []

    if source_pair:
        replacement_physical = suffixed_table(table, f"__physical_{token}")
        replacement_create = retarget_create(
            desired_shard,
            replacement_physical,
            target_cluster,
        )
        copy_create = transform_distributed_create(
            table_create,
            table_name=copy_wrapper,
            execution_cluster=None,
            target_cluster=source_cluster or distributed_cluster or "{cluster}",
            shard_table=replacement_physical,
            ch_sharding_key=None,
        )
        facade_backup = suffixed_table(table, f"__facade_backup_{token}")
        cutover = [
            f"RENAME TABLE {table} TO {facade_backup}{cluster_clause(distributed_on_cluster)}",
            f"RENAME TABLE {replacement_physical} TO {desired_shard_table}"
            f"{cluster_clause(target_cluster)}",
        ]
        rollback = [
            f"RENAME TABLE {desired_shard_table} TO {replacement_physical}"
            f"{cluster_clause(target_cluster)}",
            f"RENAME TABLE {facade_backup} TO {table}{cluster_clause(distributed_on_cluster)}",
        ]
        create_sqls.extend(
            [
                replacement_create.sql(dialect="clickhouse", pretty=True),
                copy_create.sql(dialect="clickhouse", pretty=True),
            ]
        )
        cleanup_sqls.extend(
            [
                drop_table_sql(copy_wrapper, None),
                drop_table_sql(facade_backup, distributed_on_cluster),
                drop_table_sql(source_shard_table, source_cluster),
            ]
        )
        cleanup_tables.extend(
            [
                (copy_wrapper, None),
                (facade_backup, distributed_on_cluster),
                (source_shard_table, source_cluster),
            ]
        )
        temporary_scopes.extend([(replacement_physical, target_cluster), (copy_wrapper, None)])
        strategy = "pair_to_local"
        backup_tables = [facade_backup]
    else:
        replacement_create = retarget_create(
            desired_shard,
            desired_shard_table,
            target_cluster,
        )
        copy_create = retarget_create(desired_table, copy_wrapper, None)
        facade_create = retarget_create(
            desired_table,
            facade_replacement,
            distributed_on_cluster,
        )
        cutover = [
            f"RENAME TABLE {table} TO {source_backup}{cluster_clause(target_cluster)}",
            f"RENAME TABLE {facade_replacement} TO {table}{cluster_clause(distributed_on_cluster)}",
        ]
        rollback = [
            f"RENAME TABLE {table} TO {facade_replacement}{cluster_clause(distributed_on_cluster)}",
            f"RENAME TABLE {source_backup} TO {table}{cluster_clause(target_cluster)}",
        ]
        create_sqls.extend(
            [
                replacement_create.sql(dialect="clickhouse", pretty=True),
                copy_create.sql(dialect="clickhouse", pretty=True),
                facade_create.sql(dialect="clickhouse", pretty=True),
            ]
        )
        cleanup_sqls.extend(
            [drop_table_sql(copy_wrapper, None), drop_table_sql(source_backup, target_cluster)]
        )
        cleanup_tables.extend([(copy_wrapper, None), (source_backup, target_cluster)])
        temporary_scopes.extend(
            [
                (desired_shard_table, target_cluster),
                (copy_wrapper, None),
                (facade_replacement, distributed_on_cluster),
            ]
        )
        strategy = "local_to_pair"
        backup_tables = [source_backup]

    plan = new_plan(options, table, strategy)
    add_sqls(plan, create_sqls, options, "create_replacement", copy_wrapper)
    add_sqls(
        plan,
        [f"INSERT INTO {copy_wrapper} SELECT * FROM {table}"],
        options,
        "copy_data",
        copy_wrapper,
    )
    add_sqls(plan, cutover, options, "cutover", table)
    add_sqls(plan, cleanup_sqls, options, "cleanup", table)
    return ChReconfiguration(
        plan=plan,
        strategy=strategy,
        table=table,
        source_table=table,
        replacement_table=copy_wrapper,
        source_cluster=source_cluster,
        target_cluster=target_cluster,
        source_cluster_resolved=source_cluster_resolved,
        target_cluster_resolved=target_cluster_resolved,
        source_pair=source_pair,
        target_pair=not source_pair,
        distributed_on_cluster=distributed_on_cluster,
        distributed_cluster=distributed_cluster,
        database_engine=database_engine,
        before_ddl=before_ddl,
        after_ddl=after_ddl,
        temporary_tables=[name for name, _scope in temporary_scopes],
        temporary_table_scopes=temporary_scopes,
        backup_tables=backup_tables,
        cleanup_tables=cleanup_tables,
        rollback_sqls=rollback,
        cutover_sqls=cutover,
        final_count_cluster=target_cluster_resolved if source_pair else None,
    )


__all__ = ["plan_topology_conversion"]
