from __future__ import annotations

# ruff: noqa: BLE001, C901, EM101, EM102, F401, PERF203, PLR0912, PLR0913, PLR0915, S101, S112, S608, TC002, TRY003, TRY301
import uuid
from typing import TYPE_CHECKING, Any

from sqlglot import exp

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

if TYPE_CHECKING:
    from analytics_toolkit.sql.execution.plans import SqlPlan

from .ddl import build_ch_shard_table_name
from .metadata import extract_clickhouse_distributed_shard_table
from .reconfigure_conversion import plan_topology_conversion as _plan_topology_conversion
from .reconfigure_ddl import (
    comparable_create_sql as _comparable_create_sql,
)
from .reconfigure_ddl import (
    engine_name as _engine_name,
)
from .reconfigure_ddl import (
    engine_sql as _engine_sql,
)
from .reconfigure_ddl import (
    parse_create_table as _parse_create_table,
)
from .reconfigure_ddl import (
    require_merge_tree as _require_merge_tree,
)
from .reconfigure_ddl import (
    retarget_create as _retarget_create,
)
from .reconfigure_ddl import (
    transform_create_table as _transform_create_table,
)
from .reconfigure_ddl import (
    transform_distributed_create as _transform_distributed_create,
)
from .reconfigure_execution import execute_reconfiguration_sqls as _execute_sqls
from .reconfigure_models import ChReconfiguration, ChReconfigureOptions
from .reconfigure_policy import resolve_desired_reconfigure_policy
from .reconfigure_support import (
    add_sqls as _add_sqls,
)
from .reconfigure_support import (
    build_setting_alter_sqls as _build_setting_alter_sqls,
)
from .reconfigure_support import (
    count_final_rows as _count_final_rows,
)
from .reconfigure_support import (
    count_rows as _count_rows,
)
from .reconfigure_support import (
    cutover_sqls as _cutover_sqls,
)
from .reconfigure_support import (
    database_engine as _database_engine,
)
from .reconfigure_support import (
    drop_table_sql as _drop_table_sql,
)
from .reconfigure_support import (
    is_cross_cluster as _is_cross_cluster,
)
from .reconfigure_support import (
    new_plan as _new_plan,
)
from .reconfigure_support import (
    non_empty_string as _non_empty_string,
)
from .reconfigure_support import (
    normalize_table_name as _normalize_table_name,
)
from .reconfigure_support import (
    qualify_like as _qualify_like,
)
from .reconfigure_support import (
    qualify_with_database as _qualify_with_database,
)
from .reconfigure_support import (
    query_scalar as _query_scalar,
)
from .reconfigure_support import (
    resolve_optional_cluster as _resolve_optional_cluster,
)
from .reconfigure_support import (
    show_create_table as _show_create_table,
)
from .reconfigure_support import (
    show_create_table_on_cluster as _show_create_table_on_cluster,
)
from .reconfigure_support import (
    suffixed_table as _suffixed_table,
)
from .reconfigure_support import (
    supports_exchange as _supports_exchange,
)
from .reconfigure_support import (
    table_database as _table_database,
)
from .reconfigure_support import (
    table_exists_on_cluster as _table_exists_on_cluster,
)
from .reconfigure_support import (
    unquoted_table_name as _unquoted_table_name,
)
from .reconfigure_wait import wait_for_created_replacements
from .wait import (
    _wait_for_ch_table,
    _wait_for_ch_table_absence,
    _wait_for_ch_table_absence_on_cluster,
    _wait_for_ch_table_on_cluster,
)


def _wait_for_created_replacement(
    connection: Any,
    reconfiguration: ChReconfiguration,
) -> None:
    wait_for_created_replacements(
        connection,
        reconfiguration,
        wait_local=_wait_for_ch_table,
        wait_cluster=_wait_for_ch_table_on_cluster,
    )


def plan_ch_table_reconfiguration(
    adapter: Any,
    connection: Any,
    options: ChReconfigureOptions,
) -> ChReconfiguration:
    del adapter
    table = _normalize_table_name(options.table)
    table_ddl = _show_create_table(connection, table)
    table_create = _parse_create_table(table_ddl, table)
    table_engine = _engine_name(table_create)
    database_name = _table_database(connection, table_create)
    database_engine = _database_engine(connection, database_name)
    token = uuid.uuid4().hex[:10]

    source_pair = table_engine.lower() == "distributed"
    source_cluster: str | None = None
    if source_pair:
        shard_ref = extract_clickhouse_distributed_shard_table(
            _engine_sql(table_create),
            database_name,
        )
        if shard_ref is None:
            raise InvalidSqlInputError(
                f"ClickHouse table {table} has an unsupported Distributed engine definition."
            )
        qualified_table = _qualify_with_database(table, database_name)
        shard_table = _qualify_like(qualified_table, shard_ref.table)
        expected_shard = build_ch_shard_table_name(qualified_table)
        if _unquoted_table_name(shard_table) != _unquoted_table_name(expected_shard):
            raise InvalidSqlInputError(
                f"ClickHouse table {table} is not a managed Distributed/_shard pair."
            )
        source_cluster = shard_ref.cluster
        source_cluster_resolved = _resolve_optional_cluster(
            connection,
            source_cluster,
            "ch_distributed_cluster",
        )
        try:
            shard_ddl = _show_create_table(connection, shard_table)
        except Exception:
            if source_cluster_resolved is None:
                raise
            shard_ddl = _show_create_table_on_cluster(
                connection,
                shard_table,
                source_cluster_resolved,
            )
        shard_create = _parse_create_table(shard_ddl, shard_table)
        _require_merge_tree(shard_create, shard_table)
    else:
        _require_merge_tree(table_create, table)
        shard_table = table
        shard_ddl = table_ddl
        shard_create = table_create
        source_cluster_resolved = None

    policy = resolve_desired_reconfigure_policy(
        options,
        source_pair=source_pair,
        source_shard_engine=_engine_sql(shard_create),
        source_shard_cluster=source_cluster,
        source_distributed_cluster=source_cluster,
    )
    target_pair = policy.create_distributed_pair
    target_cluster = policy.shard_on_cluster
    target_cluster_resolved = _resolve_optional_cluster(
        connection,
        target_cluster,
        "ch_shard_on_cluster",
    )
    cross_cluster = _is_cross_cluster(
        connection,
        source_cluster_resolved,
        target_cluster_resolved,
    )
    topology_change = source_pair != target_pair
    if topology_change and cross_cluster:
        raise InvalidSqlInputError(
            "Topology conversion and physical-cluster relocation must be performed in separate "
            "ch_reconfigure_table calls."
        )
    if not source_pair and not topology_change and target_cluster is not None:
        raise InvalidSqlInputError(
            "Cluster-scoped physical-table relocation requires a managed Distributed/_shard pair."
        )

    desired_shard_table = (
        build_ch_shard_table_name(_qualify_with_database(table, database_name))
        if target_pair
        else _qualify_with_database(table, database_name)
    )

    structural_change = any(
        (
            options.ch_engine is not None,
            options.partition_by is not None,
            options.order_by is not None,
            options.reset_partition_by,
            options.reset_order_by,
            options.to_defaults,
        )
    )
    wrapper_change = target_pair and any(
        (
            options.ch_sharding_key is not None,
            options.ch_distributed_engine_template is not None,
            options.ch_distributed_cluster is not None,
            options.to_defaults,
        )
    )
    settings_change = bool(options.ch_settings)

    desired_structural_shard = _transform_create_table(
        shard_create,
        table_name=desired_shard_table,
        execution_cluster=target_cluster,
        ch_engine=policy.shard_engine,
        ch_partition_by=options.partition_by,
        ch_order_by=options.order_by,
        ch_settings=None,
        ch_reset_partition_by=options.reset_partition_by,
        ch_reset_order_by=options.reset_order_by,
    )
    desired_shard = _transform_create_table(
        desired_structural_shard,
        table_name=desired_shard_table,
        execution_cluster=target_cluster,
        ch_engine=None,
        ch_partition_by=None,
        ch_order_by=None,
        ch_settings=options.ch_settings,
        ch_reset_partition_by=False,
        ch_reset_order_by=False,
    )
    desired_table = (
        _transform_distributed_create(
            table_create if source_pair else shard_create,
            table_name=table,
            execution_cluster=policy.distributed_on_cluster,
            target_cluster=policy.distributed_cluster or "{cluster}",
            shard_table=desired_shard_table,
            ch_sharding_key=policy.sharding_key,
            ch_distributed_engine_template=policy.distributed_engine_template,
        )
        if target_pair
        else desired_shard
    )
    desired_shard_sql = desired_shard.sql(dialect="clickhouse", pretty=True)
    desired_table_sql = desired_table.sql(dialect="clickhouse", pretty=True)

    before_ddl = {"table": table_ddl}
    after_ddl = {"table": desired_table_sql}
    if source_pair:
        before_ddl["shard"] = shard_ddl
    if target_pair:
        after_ddl["shard"] = desired_shard_sql

    structural_effective = _comparable_create_sql(
        desired_structural_shard
    ) != _comparable_create_sql(shard_create)
    settings_effective = _comparable_create_sql(desired_shard) != _comparable_create_sql(
        desired_structural_shard
    )
    wrapper_effective = target_pair and (
        not source_pair
        or _comparable_create_sql(desired_table) != _comparable_create_sql(table_create)
    )

    facade_changes = topology_change or (source_pair and wrapper_effective)
    if (
        facade_changes
        and source_pair
        and options.ch_distributed_on_cluster is None
        and not options.to_defaults
    ):
        raise InvalidSqlInputError(
            "ch_distributed_on_cluster is required when an existing Distributed facade "
            "must be recreated or removed."
        )

    if topology_change:
        return _plan_topology_conversion(
            options=options,
            table=table,
            source_shard_table=shard_table,
            desired_shard_table=desired_shard_table,
            table_create=table_create,
            desired_shard=desired_shard,
            desired_table=desired_table,
            source_pair=source_pair,
            source_cluster=source_cluster,
            target_cluster=target_cluster,
            source_cluster_resolved=source_cluster_resolved,
            target_cluster_resolved=target_cluster_resolved,
            distributed_on_cluster=policy.distributed_on_cluster,
            distributed_cluster=policy.distributed_cluster,
            database_engine=database_engine,
            before_ddl=before_ddl,
            after_ddl=after_ddl,
            token=token,
        )

    if structural_effective or cross_cluster or (wrapper_effective and settings_effective):
        return _plan_rebuild(
            connection=connection,
            options=options,
            table=table,
            shard_table=desired_shard_table,
            desired_shard=desired_shard,
            desired_table=desired_table,
            pair=target_pair,
            cross_cluster=cross_cluster,
            wrapper_effective=wrapper_effective,
            source_cluster=source_cluster,
            target_cluster=target_cluster,
            source_cluster_resolved=source_cluster_resolved,
            target_cluster_resolved=target_cluster_resolved,
            distributed_on_cluster=policy.distributed_on_cluster,
            distributed_cluster=policy.distributed_cluster,
            database_engine=database_engine,
            before_ddl=before_ddl,
            after_ddl=after_ddl,
            token=token,
        )
    if wrapper_effective:
        return _plan_wrapper_recreate(
            options=options,
            table=table,
            desired_table=desired_table,
            source_cluster=source_cluster,
            target_cluster=target_cluster,
            source_cluster_resolved=source_cluster_resolved,
            target_cluster_resolved=target_cluster_resolved,
            distributed_on_cluster=policy.distributed_on_cluster,
            distributed_cluster=policy.distributed_cluster,
            database_engine=database_engine,
            before_ddl=before_ddl,
            after_ddl=after_ddl,
            token=token,
        )
    if settings_effective:
        return _plan_settings_change(
            options=options,
            table=table,
            shard_table=desired_shard_table,
            source_cluster=target_cluster,
            source_cluster_resolved=target_cluster_resolved,
            pair=target_pair,
            distributed_on_cluster=policy.distributed_on_cluster,
            distributed_cluster=policy.distributed_cluster,
            database_engine=database_engine,
            before_ddl=before_ddl,
            after_ddl=after_ddl,
        )
    if (
        structural_change
        or wrapper_change
        or settings_change
        or options.ch_distributed_table is not None
        or options.ch_shard_on_cluster is not None
        or options.ch_distributed_on_cluster is not None
    ):
        return _plan_noop(
            options=options,
            table=table,
            source_cluster=source_cluster,
            target_cluster=target_cluster,
            source_cluster_resolved=source_cluster_resolved,
            target_cluster_resolved=target_cluster_resolved,
            source_pair=source_pair,
            target_pair=target_pair,
            distributed_on_cluster=policy.distributed_on_cluster,
            distributed_cluster=policy.distributed_cluster,
            database_engine=database_engine,
            before_ddl=before_ddl,
            after_ddl=after_ddl,
        )
    raise InvalidSqlInputError("At least one ClickHouse table change must be provided.")


def execute_ch_table_reconfiguration(
    adapter: Any,
    connection: Any,
    reconfiguration: ChReconfiguration,
    *,
    validate_row_count: bool,
) -> None:
    metadata = reconfiguration.plan.metadata
    if reconfiguration.strategy == "no_op":
        reconfiguration.cleanup_complete = True
        metadata.row_count_validated = None
        return
    if reconfiguration.strategy == "settings":
        _execute_phase(adapter, connection, reconfiguration.plan, "alter_settings")
        reconfiguration.cleanup_complete = True
        metadata.row_count_validated = None
        return

    source_count = _count_rows(connection, reconfiguration.source_table)
    reconfiguration.source_count = source_count
    metadata.source_rows = source_count
    cutover_started = False
    try:
        _execute_phase(adapter, connection, reconfiguration.plan, "create_replacement")
        _wait_for_created_replacement(connection, reconfiguration)
        _execute_phase(adapter, connection, reconfiguration.plan, "copy_data")

        if reconfiguration.replacement_table is not None:
            replacement_count = _count_rows(
                connection,
                reconfiguration.replacement_table,
            )
            reconfiguration.replacement_count = replacement_count
            metadata.staged_rows = replacement_count
        else:  # pragma: no cover - executable rebuild plans always stage a replacement
            replacement_count = source_count

        stable_source_count = _count_rows(connection, reconfiguration.source_table)
        if validate_row_count and stable_source_count != source_count:
            raise RuntimeError(
                "ClickHouse source row count changed during reconfiguration; "
                "pause writers and retry."
            )
        if validate_row_count and replacement_count != source_count:
            raise RuntimeError("ClickHouse replacement row count does not match the source table.")

        cutover_started = True
        _execute_phase(adapter, connection, reconfiguration.plan, "cutover")
        final_count = _count_final_rows(
            connection, reconfiguration.table, reconfiguration.final_count_cluster
        )
        reconfiguration.final_count = final_count
        metadata.final_target_rows = final_count
        if validate_row_count and final_count != source_count:
            raise RuntimeError(
                "ClickHouse row count changed during cutover; the original table will be restored."
            )
        metadata.row_count_validated = validate_row_count
    except Exception:
        if cutover_started and reconfiguration.rollback_sqls:
            try:
                _execute_sqls(adapter, connection, reconfiguration.rollback_sqls)
            except Exception as rollback_exc:
                raise RuntimeError(
                    "ClickHouse reconfiguration failed and automatic rollback also failed."
                ) from rollback_exc
        _best_effort_cleanup(adapter, connection, reconfiguration)
        raise

    try:
        _execute_phase(adapter, connection, reconfiguration.plan, "cleanup")
        _wait_for_cleanup(connection, reconfiguration)
    except Exception as exc:
        reconfiguration.cleanup_error = f"{type(exc).__name__}: {exc}"
        return
    reconfiguration.cleanup_complete = True


def _plan_noop(
    *,
    options: ChReconfigureOptions,
    table: str,
    source_cluster: str | None,
    target_cluster: str | None,
    source_cluster_resolved: str | None,
    target_cluster_resolved: str | None,
    source_pair: bool,
    target_pair: bool,
    distributed_on_cluster: str | None,
    distributed_cluster: str | None,
    database_engine: str,
    before_ddl: dict[str, str],
    after_ddl: dict[str, str],
) -> ChReconfiguration:
    return ChReconfiguration(
        plan=_new_plan(options, table, "no_op"),
        strategy="no_op",
        table=table,
        source_table=table,
        replacement_table=None,
        source_cluster=source_cluster,
        target_cluster=target_cluster,
        source_cluster_resolved=source_cluster_resolved,
        target_cluster_resolved=target_cluster_resolved,
        source_pair=source_pair,
        target_pair=target_pair,
        distributed_on_cluster=distributed_on_cluster,
        distributed_cluster=distributed_cluster,
        database_engine=database_engine,
        before_ddl=before_ddl,
        after_ddl=after_ddl,
    )


def _plan_settings_change(
    *,
    options: ChReconfigureOptions,
    table: str,
    shard_table: str,
    source_cluster: str | None,
    source_cluster_resolved: str | None,
    pair: bool,
    distributed_on_cluster: str | None,
    distributed_cluster: str | None,
    database_engine: str,
    before_ddl: dict[str, str],
    after_ddl: dict[str, str],
) -> ChReconfiguration:
    sqls = _build_setting_alter_sqls(
        shard_table,
        options.ch_settings or {},
        ch_cluster=source_cluster,
    )
    plan = _new_plan(options, table, "settings")
    _add_sqls(plan, sqls, options, phase="alter_settings", target_table=shard_table)
    return ChReconfiguration(
        plan=plan,
        strategy="settings",
        table=table,
        source_table=table,
        replacement_table=None,
        source_cluster=source_cluster,
        target_cluster=source_cluster,
        source_cluster_resolved=source_cluster_resolved,
        target_cluster_resolved=source_cluster_resolved,
        source_pair=pair,
        target_pair=pair,
        distributed_on_cluster=distributed_on_cluster,
        distributed_cluster=distributed_cluster,
        database_engine=database_engine,
        before_ddl=before_ddl,
        after_ddl=after_ddl,
    )


def _plan_wrapper_recreate(
    *,
    options: ChReconfigureOptions,
    table: str,
    desired_table: exp.Create,
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
    temp_table = _suffixed_table(table, f"__reconfigure_{token}")
    backup_table = _suffixed_table(table, f"__backup_{token}")
    temp_create = _retarget_create(desired_table, temp_table, distributed_on_cluster)
    create_sql = temp_create.sql(dialect="clickhouse", pretty=True)
    use_exchange = _supports_exchange(database_engine)
    cutover, rollback, cleanup_name = _cutover_sqls(
        table,
        temp_table,
        backup_table,
        ch_cluster=distributed_on_cluster,
        use_exchange=use_exchange,
    )
    cleanup_sql = _drop_table_sql(cleanup_name, distributed_on_cluster)
    plan = _new_plan(options, table, "wrapper_recreate")
    _add_sqls(plan, [create_sql], options, "create_replacement", temp_table)
    _add_sqls(plan, cutover, options, "cutover", table)
    _add_sqls(plan, [cleanup_sql], options, "cleanup", cleanup_name)
    return ChReconfiguration(
        plan=plan,
        strategy="wrapper_recreate",
        table=table,
        source_table=table,
        replacement_table=temp_table,
        source_cluster=source_cluster,
        target_cluster=target_cluster,
        source_cluster_resolved=source_cluster_resolved,
        target_cluster_resolved=target_cluster_resolved,
        source_pair=True,
        target_pair=True,
        distributed_on_cluster=distributed_on_cluster,
        distributed_cluster=distributed_cluster,
        database_engine=database_engine,
        before_ddl=before_ddl,
        after_ddl=after_ddl,
        temporary_tables=[temp_table],
        temporary_table_scopes=[(temp_table, distributed_on_cluster)],
        backup_tables=[] if use_exchange else [backup_table],
        cleanup_tables=[(cleanup_name, distributed_on_cluster)],
        rollback_sqls=rollback,
        cutover_sqls=cutover,
    )


def _plan_rebuild(
    *,
    connection: Any,
    options: ChReconfigureOptions,
    table: str,
    shard_table: str,
    desired_shard: exp.Create,
    desired_table: exp.Create,
    pair: bool,
    cross_cluster: bool,
    wrapper_effective: bool,
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
    use_exchange = _supports_exchange(database_engine)
    copy_wrapper = _suffixed_table(table, f"__copy_{token}")
    facade_replacement = _suffixed_table(table, f"__reconfigure_{token}")
    facade_backup = _suffixed_table(table, f"__backup_{token}")
    create_sqls: list[str] = []
    cleanup_sqls: list[str] = []
    cleanup_tables: list[tuple[str, str | None]] = []
    temporary_scopes: list[tuple[str, str | None]] = []
    backup_tables: list[str] = []
    cutover: list[str] = []
    rollback: list[str] = []

    if cross_cluster:
        assert pair
        if _table_exists_on_cluster(connection, shard_table, target_cluster_resolved):
            raise InvalidSqlInputError(
                f"Destination cluster already contains {shard_table}; refusing to overwrite it."
            )
        replacement_shard = shard_table
        replacement_create = _retarget_create(
            desired_shard,
            replacement_shard,
            target_cluster,
        )
        copy_create = _retarget_create(
            desired_table,
            copy_wrapper,
            None,
        )
        facade_create = _retarget_create(
            desired_table,
            facade_replacement,
            distributed_on_cluster,
        )
        facade_cutover, facade_rollback, facade_cleanup = _cutover_sqls(
            table,
            facade_replacement,
            facade_backup,
            ch_cluster=distributed_on_cluster,
            use_exchange=use_exchange,
        )
        cutover.extend(facade_cutover)
        rollback.extend(facade_rollback)
        create_sqls.extend(
            [
                replacement_create.sql(dialect="clickhouse", pretty=True),
                copy_create.sql(dialect="clickhouse", pretty=True),
                facade_create.sql(dialect="clickhouse", pretty=True),
            ]
        )
        cleanup_sqls.extend(
            [
                _drop_table_sql(copy_wrapper, None),
                _drop_table_sql(facade_cleanup, distributed_on_cluster),
                _drop_table_sql(shard_table, source_cluster),
            ]
        )
        cleanup_tables.extend(
            [
                (copy_wrapper, None),
                (facade_cleanup, distributed_on_cluster),
                (shard_table, source_cluster),
            ]
        )
        strategy = "cross_cluster_rebuild"
        temporary_scopes.extend(
            [
                (replacement_shard, target_cluster),
                (copy_wrapper, None),
                (facade_replacement, distributed_on_cluster),
            ]
        )
        if not use_exchange:
            backup_tables.append(facade_backup)
    elif pair:
        replacement_shard = _suffixed_table(shard_table, f"__reconfigure_{token}")
        backup_shard = _suffixed_table(shard_table, f"__backup_{token}")
        replacement_create = _retarget_create(
            desired_shard,
            replacement_shard,
            target_cluster,
        )
        copy_create = _transform_distributed_create(
            desired_table,
            table_name=copy_wrapper,
            execution_cluster=None,
            target_cluster=distributed_cluster or target_cluster or "{cluster}",
            shard_table=replacement_shard,
            ch_sharding_key=None,
        )
        shard_cutover, shard_rollback, shard_cleanup = _cutover_sqls(
            shard_table,
            replacement_shard,
            backup_shard,
            ch_cluster=target_cluster,
            use_exchange=use_exchange,
        )
        cutover.extend(shard_cutover)
        rollback[:0] = shard_rollback
        create_sqls.extend(
            [
                replacement_create.sql(dialect="clickhouse", pretty=True),
                copy_create.sql(dialect="clickhouse", pretty=True),
            ]
        )
        cleanup_sqls.extend(
            [
                _drop_table_sql(copy_wrapper, None),
                _drop_table_sql(shard_cleanup, target_cluster),
            ]
        )
        cleanup_tables.extend([(copy_wrapper, None), (shard_cleanup, target_cluster)])
        temporary_scopes.extend([(replacement_shard, target_cluster), (copy_wrapper, None)])
        if not use_exchange:
            backup_tables.append(backup_shard)
        if wrapper_effective:
            facade_create = _retarget_create(
                desired_table,
                facade_replacement,
                distributed_on_cluster,
            )
            facade_cutover, facade_rollback, facade_cleanup = _cutover_sqls(
                table,
                facade_replacement,
                facade_backup,
                ch_cluster=distributed_on_cluster,
                use_exchange=use_exchange,
            )
            create_sqls.append(facade_create.sql(dialect="clickhouse", pretty=True))
            cutover.extend(facade_cutover)
            rollback[:0] = facade_rollback
            cleanup_sqls.append(_drop_table_sql(facade_cleanup, distributed_on_cluster))
            cleanup_tables.append((facade_cleanup, distributed_on_cluster))
            temporary_scopes.append((facade_replacement, distributed_on_cluster))
            if not use_exchange:
                backup_tables.append(facade_backup)
        strategy = "managed_pair_rebuild"
    else:
        replacement_shard = _suffixed_table(table, f"__reconfigure_{token}")
        backup_shard = _suffixed_table(table, f"__backup_{token}")
        replacement_create = _retarget_create(
            desired_shard,
            replacement_shard,
            target_cluster,
        )
        cutover, rollback, cleanup_name = _cutover_sqls(
            table,
            replacement_shard,
            backup_shard,
            ch_cluster=target_cluster,
            use_exchange=use_exchange,
        )
        create_sqls.append(replacement_create.sql(dialect="clickhouse", pretty=True))
        cleanup_sqls.append(_drop_table_sql(cleanup_name, target_cluster))
        cleanup_tables.append((cleanup_name, target_cluster))
        temporary_scopes.append((replacement_shard, target_cluster))
        strategy = "local_rebuild"
        if not use_exchange:
            backup_tables.append(backup_shard)

    copy_target = copy_wrapper if pair else replacement_shard
    copy_sql = f"INSERT INTO {copy_target} SELECT * FROM {table}"

    plan = _new_plan(options, table, strategy)
    _add_sqls(plan, create_sqls, options, "create_replacement", copy_target)
    _add_sqls(plan, [copy_sql], options, "copy_data", copy_target)
    _add_sqls(plan, cutover, options, "cutover", table)
    _add_sqls(plan, cleanup_sqls, options, "cleanup", table)
    return ChReconfiguration(
        plan=plan,
        strategy=strategy,
        table=table,
        source_table=table,
        replacement_table=copy_target,
        source_cluster=source_cluster,
        target_cluster=target_cluster,
        source_cluster_resolved=source_cluster_resolved,
        target_cluster_resolved=target_cluster_resolved,
        source_pair=pair,
        target_pair=pair,
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
    )


def _execute_phase(adapter: Any, connection: Any, plan: SqlPlan, phase: str) -> None:
    _execute_sqls(
        adapter,
        connection,
        [statement.sql for statement in plan.statements if statement.phase == phase],
    )


def _wait_for_cleanup(connection: Any, reconfiguration: ChReconfiguration) -> None:
    for table_name, cluster in reconfiguration.cleanup_tables:
        if cluster is None:
            _wait_for_ch_table_absence(connection, table_name)
        else:
            _wait_for_ch_table_absence_on_cluster(
                connection,
                table_name,
                ch_cluster=cluster,
            )


def _best_effort_cleanup(
    adapter: Any,
    connection: Any,
    reconfiguration: ChReconfiguration,
) -> None:
    scopes = getattr(reconfiguration, "temporary_table_scopes", None) or [
        (table_name, reconfiguration.source_cluster)
        for table_name in reconfiguration.temporary_tables
    ]
    for table_name, cluster in scopes:
        try:
            adapter.execute_command(connection, _drop_table_sql(table_name, cluster))
        except Exception:
            continue


__all__ = [
    "ChReconfiguration",
    "ChReconfigureOptions",
    "execute_ch_table_reconfiguration",
    "plan_ch_table_reconfiguration",
]
