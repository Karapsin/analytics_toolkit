from __future__ import annotations

# ruff: noqa: BLE001, EM101, PLR0913, PLR0915, S608, TRY003, TRY301
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from analytics_toolkit.general import time_print
from analytics_toolkit.sql.backends.models import StageFinalizationRequest, StageTargetTableRequest
from analytics_toolkit.sql.connection.errors import AmbiguousSqlReplaceError

from .ddl import _sql_string_literal, split_ch_table_name_for_distributed_engine
from .reconfigure_ddl import distributed_table_parts
from .reconfigure_execution import cluster_clause, execute_reconfiguration_sqls
from .reconfigure_support import (
    cutover_sqls,
    database_engine,
    drop_table_sql,
    suffixed_table,
    supports_exchange,
)
from .routing import local_sql, query_local

if TYPE_CHECKING:
    from collections.abc import Sequence


def finalize_stage_table(adapter: Any, request: StageFinalizationRequest) -> None:
    if request.write_mode == "upsert":
        _finalize_upsert(adapter, request)
        return
    if request.replace_target_table and request.target_exists:
        _replace_from_stage(adapter, request)
        return

    adapter.ensure_distributed_target_pair(
        request.connection,
        request.target_table,
        request.sample_batch,
        target_exists=request.target_exists,
        target_column_types=request.target_column_types,
        insert_column_types=request.insert_column_types,
        gp_distributed_by_key=request.gp_distributed_by_key,
        partition_by=request.partition_by,
        order_by=request.order_by,
        ch_engine=request.ch_engine,
        ch_cluster=request.ch_cluster,
        ch_sharding_key=request.ch_sharding_key,
        query_label=request.query_label,
        connection_key=request.connection_key,
        ch_replace_table=False,
        ch_only_shard=request.ch_only_shard,
        ch_creation_policy=request.ch_creation_policy,
    )
    adapter.insert_from_table(
        request.connection,
        request.target_table,
        request.stage_table,
        column_types=request.insert_column_types,
        query_label=request.query_label,
    )


def _finalize_upsert(adapter: Any, request: StageFinalizationRequest) -> None:
    if not request.target_exists:
        adapter.ensure_stage_target_table(
            StageTargetTableRequest(
                connection=request.connection,
                target_table=request.target_table,
                sample_batch=request.sample_batch,
                target_column_types=request.target_column_types,
                gp_distributed_by_key=request.gp_distributed_by_key,
                partition_by=request.partition_by,
                order_by=request.order_by,
                ch_engine=request.ch_engine,
                ch_cluster=request.ch_cluster,
                ch_sharding_key=request.ch_sharding_key,
                query_label=request.query_label,
                connection_key=request.connection_key,
                ch_only_shard=request.ch_only_shard,
                ch_creation_policy=request.ch_creation_policy,
            )
        )
        adapter.insert_from_table(
            request.connection,
            request.target_table,
            request.stage_table,
            column_types=request.insert_column_types,
            query_label=request.query_label,
        )
        return

    adapter.ensure_distributed_target_pair(
        request.connection,
        request.target_table,
        request.sample_batch,
        target_exists=True,
        target_column_types=request.target_column_types,
        insert_column_types=request.insert_column_types,
        gp_distributed_by_key=request.gp_distributed_by_key,
        partition_by=request.partition_by,
        order_by=request.order_by,
        ch_engine=request.ch_engine,
        ch_cluster=request.ch_cluster,
        ch_sharding_key=request.ch_sharding_key,
        query_label=request.query_label,
        connection_key=request.connection_key,
        ch_replace_table=False,
        ch_only_shard=request.ch_only_shard,
        ch_creation_policy=request.ch_creation_policy,
    )
    if request.upsert_partition_column is None:
        raise ValueError("upsert_partition_column is required for ClickHouse write_mode='upsert'.")
    partition_values = adapter.fetch_upsert_partition_values(
        request.connection,
        request.stage_table,
        partition_column=request.upsert_partition_column,
        incoming_stage_tables=request.incoming_stage_tables,
    )
    for sql in adapter.build_upsert_stage_sqls(
        request.target_table,
        request.stage_table,
        columns=list(
            request.insert_column_types
            or request.target_column_types
            or request.sample_batch.columns
        ),
        key_columns=request.key_columns or [],
        column_types=request.insert_column_types,
        ch_cluster=request.ch_cluster,
        ch_only_shard=request.ch_only_shard,
        query_label=request.query_label,
        upsert_partition_column=request.upsert_partition_column,
        final_stage_table=request.final_upsert_stage_table,
        incoming_stage_tables=request.incoming_stage_tables,
        partition_values=partition_values,
    ):
        adapter.execute_command(request.connection, sql)


def _replace_from_stage(adapter: Any, request: StageFinalizationRequest) -> None:
    token = uuid4().hex[:12]
    physical_target = (
        request.target_table
        if request.ch_only_shard
        else adapter.companion_table_name(request.target_table)
    )
    if physical_target is None:
        raise RuntimeError("ClickHouse replacement could not resolve its physical shard table.")
    replacement_physical = suffixed_table(physical_target, f"__replace_{token}")
    replacement_facade = (
        None
        if request.ch_only_shard
        else suffixed_table(request.target_table, f"__replace_{token}")
    )
    backup_physical = suffixed_table(physical_target, f"__backup_{token}")
    backup_facade = (
        None if request.ch_only_shard else suffixed_table(request.target_table, f"__backup_{token}")
    )
    shard_cluster, facade_cluster, distributed_cluster = _clusters(request)
    _cleanup_orphan_replacements(
        adapter,
        request.connection,
        [(physical_target, shard_cluster), (request.target_table, facade_cluster)],
    )
    database, _relation = distributed_table_parts(physical_target)
    use_exchange = supports_exchange(_database_engine_local(request.connection, database))
    expected_rows = _count_rows_routed(adapter, request.connection, request.stage_table)
    target_fingerprint = _target_fingerprint(
        request.connection,
        physical_target,
        shard_cluster,
    )
    cutover_started = False
    cleanup_tables: list[tuple[str, str | None]] = []
    rollback_sql: list[str] = []
    try:
        _create_replacement_physical(
            adapter,
            request,
            replacement_physical,
        )
        if replacement_facade is not None:
            _create_replacement_facade(
                adapter,
                request,
                replacement_facade=replacement_facade,
                replacement_physical=replacement_physical,
                physical_target=physical_target,
                facade_cluster=facade_cluster,
                distributed_cluster=distributed_cluster,
            )
        adapter.insert_from_table(
            request.connection,
            replacement_physical,
            request.stage_table,
            column_types=request.insert_column_types,
            query_label=request.query_label,
        )
        replacement_rows = _count_rows_routed(
            adapter,
            request.connection,
            replacement_physical,
        )
        if replacement_rows != expected_rows:
            raise RuntimeError(
                "ClickHouse replacement row count does not match the staged row count."
            )
        if (
            _target_fingerprint(request.connection, physical_target, shard_cluster)
            != target_fingerprint
        ):
            raise RuntimeError(
                "ClickHouse destination changed during replacement; pause writers and retry."
            )

        physical_cutover, physical_rollback, physical_cleanup = cutover_sqls(
            physical_target,
            replacement_physical,
            backup_physical,
            ch_cluster=shard_cluster,
            use_exchange=use_exchange,
        )
        cutover_sql = list(physical_cutover)
        rollback_sql = list(physical_rollback)
        cleanup_tables.append((physical_cleanup, shard_cluster))
        if replacement_facade is not None and backup_facade is not None:
            facade_cutover, facade_rollback, facade_cleanup = cutover_sqls(
                request.target_table,
                replacement_facade,
                backup_facade,
                ch_cluster=facade_cluster,
                use_exchange=use_exchange,
            )
            cutover_sql.extend(facade_cutover)
            rollback_sql = list(facade_rollback) + rollback_sql
            cleanup_tables.append((facade_cleanup, facade_cluster))

        cutover_started = True
        with local_sql(request.connection):
            execute_reconfiguration_sqls(adapter, request.connection, cutover_sql)
        final_rows = _count_rows_routed(adapter, request.connection, request.target_table)
        if final_rows != expected_rows:
            raise RuntimeError(
                "ClickHouse final target row count does not match the staged row count."
            )
    except Exception:
        if cutover_started:
            try:
                with local_sql(request.connection):
                    execute_reconfiguration_sqls(adapter, request.connection, rollback_sql)
            except Exception as rollback_exc:
                raise AmbiguousSqlReplaceError(
                    "ClickHouse replacement and automatic rollback both failed; "
                    "replacement artifacts were preserved for recovery."
                ) from rollback_exc
        _best_effort_drop(
            adapter,
            request.connection,
            [(replacement_facade, facade_cluster), (replacement_physical, shard_cluster)],
        )
        raise

    _best_effort_drop(adapter, request.connection, cleanup_tables)


def _create_replacement_physical(
    adapter: Any,
    request: StageFinalizationRequest,
    replacement_physical: str,
) -> None:
    with local_sql(request.connection):
        adapter.ensure_stage_target_table(
            StageTargetTableRequest(
                connection=request.connection,
                target_table=replacement_physical,
                sample_batch=request.sample_batch,
                target_column_types=request.target_column_types,
                gp_distributed_by_key=request.gp_distributed_by_key,
                partition_by=request.partition_by,
                order_by=request.order_by,
                ch_engine=request.ch_engine,
                ch_cluster=request.ch_cluster,
                ch_sharding_key=request.ch_sharding_key,
                query_label=request.query_label,
                connection_key=request.connection_key,
                ch_only_shard=True,
                ch_creation_policy=request.ch_creation_policy,
            )
        )


def _create_replacement_facade(
    adapter: Any,
    request: StageFinalizationRequest,
    *,
    replacement_facade: str,
    replacement_physical: str,
    physical_target: str,
    facade_cluster: str | None,
    distributed_cluster: str,
) -> None:
    database_expression, target_relation = split_ch_table_name_for_distributed_engine(
        physical_target
    )
    sql = (
        f"CREATE TABLE {replacement_facade}{cluster_clause(facade_cluster)} "
        f"AS {replacement_physical} ENGINE = Distributed("
        f"{_sql_string_literal(distributed_cluster)}, {database_expression}, "
        f"{_sql_string_literal(target_relation)}, {request.ch_sharding_key})"
    )
    with local_sql(request.connection):
        adapter.execute_command(request.connection, sql)


def _clusters(request: StageFinalizationRequest) -> tuple[str | None, str | None, str]:
    policy = request.ch_creation_policy
    if policy is None:
        cluster = request.ch_cluster
        return cluster, cluster, cluster
    shard_cluster = policy.shard_on_cluster
    facade_cluster = policy.distributed_on_cluster
    distributed_cluster = policy.distributed_cluster or request.ch_cluster
    return shard_cluster, facade_cluster, distributed_cluster


def _database_engine_local(connection: Any, database: str) -> str:
    with local_sql(connection):
        return database_engine(connection, database)


def _count_rows_routed(adapter: Any, connection: Any, table: str) -> int:
    return int(adapter.count_table_rows(connection, table))


def _target_fingerprint(
    connection: Any,
    table: str,
    cluster: str | None,
) -> tuple[tuple[Any, ...], ...]:
    database, relation = distributed_table_parts(table)
    if cluster is None:
        rows = query_local(
            connection,
            "SELECT count(), sum(rows), max(max_block_number), max(data_version) "
            "FROM system.parts "
            f"WHERE database = {_sql_string_literal(database)} "
            f"AND table = {_sql_string_literal(relation)} AND active",
        ).result_rows
        return tuple(tuple(row) for row in rows)
    rows = query_local(
        connection,
        "SELECT _shard_num, _replica_num, count(), sum(rows), "
        "max(max_block_number), max(data_version) FROM clusterAllReplicas("
        f"{_sql_string_literal(cluster)}, system, parts) "
        f"WHERE database = {_sql_string_literal(database)} "
        f"AND table = {_sql_string_literal(relation)} AND active "
        "GROUP BY _shard_num, _replica_num ORDER BY _shard_num, _replica_num",
    ).result_rows
    return tuple(tuple(row) for row in rows)


def _best_effort_drop(
    adapter: Any,
    connection: Any,
    tables: Sequence[tuple[str | None, str | None]],
) -> None:
    for table, cluster in tables:
        if table is None:
            continue
        try:
            with local_sql(connection):
                adapter.execute_command(connection, drop_table_sql(table, cluster))
        except Exception as exc:  # cleanup is recoverable on the next replacement.
            time_print(
                f"Could not remove ClickHouse replacement artifact {table}: {type(exc).__name__}",
                level="warning",
            )


def _cleanup_orphan_replacements(
    adapter: Any,
    connection: Any,
    targets: list[tuple[str, str | None]],
) -> None:
    for target, cluster in targets:
        database, relation = distributed_table_parts(target)
        prefix = f"{relation}__replace_"
        try:
            source = (
                "system.tables"
                if cluster is None
                else f"clusterAllReplicas({_sql_string_literal(cluster)}, system, tables)"
            )
            rows = query_local(
                connection,
                f"SELECT DISTINCT name FROM {source} "
                f"WHERE database = {_sql_string_literal(database)} "
                f"AND startsWith(name, {_sql_string_literal(prefix)})",
            ).result_rows
            for row in rows or []:
                name = str(row[0]) if row else ""
                if not name.startswith(prefix):
                    continue
                orphan = f"{database}.{name}"
                if _replacement_is_active(connection, orphan, cluster):
                    continue
                with local_sql(connection):
                    adapter.execute_command(connection, drop_table_sql(orphan, cluster))
        except Exception as exc:
            time_print(
                f"Could not inspect ClickHouse replacement artifacts for {target}: "
                f"{type(exc).__name__}",
                level="warning",
            )


def _replacement_is_active(
    connection: Any,
    table: str,
    cluster: str | None,
) -> bool:
    source = (
        "system.processes"
        if cluster is None
        else f"clusterAllReplicas({_sql_string_literal(cluster)}, system, processes)"
    )
    rows = query_local(
        connection,
        f"SELECT count() FROM {source} WHERE position(query, {_sql_string_literal(table)}) > 0",
    ).result_rows
    return bool(rows and rows[0] and int(rows[0][0]))


__all__ = ["finalize_stage_table"]
