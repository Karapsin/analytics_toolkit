from __future__ import annotations

# ruff: noqa: BLE001, C901, EM101, EM102, PERF203, PLR0913, PLR0915, PLR2004, S101, S112, S608, TRY003, TRY301
import uuid
from typing import TYPE_CHECKING, Any

from sqlglot import exp

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.ddl.identifiers import (
    _add_table_identifier_suffix,
    _identifier_name,
    _parse_table_name,
)
from analytics_toolkit.sql.execution.plans import SqlOperationMetadata, SqlPlan

from .ddl import _sql_string_literal, build_ch_shard_table_name
from .metadata import extract_clickhouse_distributed_shard_table
from .reconfigure_ddl import (
    comparable_create_sql as _comparable_create_sql,
)
from .reconfigure_ddl import (
    distributed_table_parts as _distributed_table_parts,
)
from .reconfigure_ddl import (
    engine_name as _engine_name,
)
from .reconfigure_ddl import (
    engine_sql as _engine_sql,
)
from .reconfigure_ddl import (
    normalize_setting_name as _normalize_setting_name,
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
    setting_value_sql as _setting_value_sql,
)
from .reconfigure_ddl import (
    transform_create_table as _transform_create_table,
)
from .reconfigure_ddl import (
    transform_distributed_create as _transform_distributed_create,
)
from .reconfigure_execution import cluster_clause as _cluster_clause
from .reconfigure_execution import execute_reconfiguration_sqls as _execute_sqls
from .reconfigure_models import ChReconfiguration, ChReconfigureOptions
from .wait import (
    _resolve_ch_cluster_name_for_wait,
    _wait_for_ch_table,
    _wait_for_ch_table_absence,
    _wait_for_ch_table_absence_on_cluster,
    _wait_for_ch_table_on_cluster,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


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
    source_cluster: str | None

    pair = table_engine.lower() == "distributed"
    if pair:
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
        shard_ddl = _show_create_table(connection, shard_table)
        shard_create = _parse_create_table(shard_ddl, shard_table)
        _require_merge_tree(shard_create, shard_table)
    else:
        _require_merge_tree(table_create, table)
        shard_table = table
        shard_ddl = table_ddl
        shard_create = table_create
        source_cluster = options.ch_source_cluster

    source_cluster_resolved = _resolve_optional_cluster(connection, source_cluster)
    supplied_source_resolved = _resolve_optional_cluster(
        connection,
        options.ch_source_cluster,
    )
    if (
        pair
        and supplied_source_resolved is not None
        and supplied_source_resolved != source_cluster_resolved
    ):
        raise InvalidSqlInputError(
            "ch_source_cluster does not match the cluster stored in the Distributed table."
        )

    target_cluster = options.ch_cluster or source_cluster
    target_cluster_resolved = _resolve_optional_cluster(connection, target_cluster)
    cross_cluster = _is_cross_cluster(
        connection,
        source_cluster_resolved,
        target_cluster_resolved,
    )
    if cross_cluster and not pair:
        raise InvalidSqlInputError(
            "Cross-cluster reconfiguration requires a managed Distributed/_shard pair."
        )

    structural_change = any(
        (
            options.ch_engine is not None,
            options.ch_partition_by is not None,
            options.ch_order_by is not None,
            options.ch_reset_partition_by,
            options.ch_reset_order_by,
        )
    )
    wrapper_change = pair and any(
        (
            options.ch_cluster is not None,
            options.ch_sharding_key is not None,
        )
    )
    settings_change = bool(options.ch_settings)

    desired_structural_shard = _transform_create_table(
        shard_create,
        table_name=shard_table,
        execution_cluster=(target_cluster if cross_cluster else source_cluster),
        ch_engine=options.ch_engine,
        ch_partition_by=options.ch_partition_by,
        ch_order_by=options.ch_order_by,
        ch_settings=None,
        ch_reset_partition_by=options.ch_reset_partition_by,
        ch_reset_order_by=options.ch_reset_order_by,
    )
    desired_shard = _transform_create_table(
        desired_structural_shard,
        table_name=shard_table,
        execution_cluster=(target_cluster if cross_cluster else source_cluster),
        ch_engine=None,
        ch_partition_by=None,
        ch_order_by=None,
        ch_settings=options.ch_settings,
        ch_reset_partition_by=False,
        ch_reset_order_by=False,
    )
    desired_table = (
        _transform_distributed_create(
            table_create,
            table_name=table,
            execution_cluster=source_cluster,
            target_cluster=target_cluster or source_cluster or "{cluster}",
            shard_table=shard_table,
            ch_sharding_key=options.ch_sharding_key,
        )
        if pair
        else desired_shard
    )
    desired_shard_sql = desired_shard.sql(dialect="clickhouse", pretty=True)
    desired_table_sql = desired_table.sql(dialect="clickhouse", pretty=True)

    before_ddl = {"table": table_ddl}
    after_ddl = {"table": desired_table_sql}
    if pair:
        before_ddl["shard"] = shard_ddl
        after_ddl["shard"] = desired_shard_sql

    structural_effective = _comparable_create_sql(
        desired_structural_shard
    ) != _comparable_create_sql(shard_create)
    settings_effective = _comparable_create_sql(desired_shard) != _comparable_create_sql(
        desired_structural_shard
    )
    wrapper_effective = pair and (
        _comparable_create_sql(desired_table) != _comparable_create_sql(table_create)
    )

    if structural_effective or cross_cluster or (wrapper_effective and settings_effective):
        return _plan_rebuild(
            connection=connection,
            options=options,
            table=table,
            shard_table=shard_table,
            table_create=table_create,
            desired_shard=desired_shard,
            desired_table=desired_table,
            pair=pair,
            cross_cluster=cross_cluster,
            source_cluster=source_cluster,
            target_cluster=target_cluster,
            source_cluster_resolved=source_cluster_resolved,
            target_cluster_resolved=target_cluster_resolved,
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
            database_engine=database_engine,
            before_ddl=before_ddl,
            after_ddl=after_ddl,
            token=token,
        )
    if settings_effective:
        return _plan_settings_change(
            options=options,
            table=table,
            shard_table=shard_table,
            source_cluster=source_cluster,
            source_cluster_resolved=source_cluster_resolved,
            database_engine=database_engine,
            before_ddl=before_ddl,
            after_ddl=after_ddl,
        )
    if structural_change or wrapper_change or settings_change:
        return _plan_noop(
            options=options,
            table=table,
            source_cluster=source_cluster,
            target_cluster=target_cluster,
            source_cluster_resolved=source_cluster_resolved,
            target_cluster_resolved=target_cluster_resolved,
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
        final_count = _count_rows(connection, reconfiguration.table)
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
    database_engine: str,
    before_ddl: dict[str, str],
    after_ddl: dict[str, str],
    token: str,
) -> ChReconfiguration:
    temp_table = _suffixed_table(table, f"__reconfigure_{token}")
    backup_table = _suffixed_table(table, f"__backup_{token}")
    temp_create = _retarget_create(desired_table, temp_table, source_cluster)
    create_sql = temp_create.sql(dialect="clickhouse", pretty=True)
    use_exchange = _supports_exchange(database_engine)
    cutover, rollback, cleanup_name = _cutover_sqls(
        table,
        temp_table,
        backup_table,
        ch_cluster=source_cluster,
        use_exchange=use_exchange,
    )
    cleanup_sql = _drop_table_sql(cleanup_name, source_cluster)
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
        database_engine=database_engine,
        before_ddl=before_ddl,
        after_ddl=after_ddl,
        temporary_tables=[temp_table],
        backup_tables=[] if use_exchange else [backup_table],
        cleanup_tables=[(cleanup_name, source_cluster)],
        rollback_sqls=rollback,
        cutover_sqls=cutover,
    )


def _plan_rebuild(
    *,
    connection: Any,
    options: ChReconfigureOptions,
    table: str,
    shard_table: str,
    table_create: exp.Create,
    desired_shard: exp.Create,
    desired_table: exp.Create,
    pair: bool,
    cross_cluster: bool,
    source_cluster: str | None,
    target_cluster: str | None,
    source_cluster_resolved: str | None,
    target_cluster_resolved: str | None,
    database_engine: str,
    before_ddl: dict[str, str],
    after_ddl: dict[str, str],
    token: str,
) -> ChReconfiguration:
    use_exchange = _supports_exchange(database_engine)
    temp_wrapper = _suffixed_table(table, f"__reconfigure_{token}")
    backup_wrapper = _suffixed_table(table, f"__backup_{token}")

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
        replacement_wrapper = _retarget_create(
            desired_table,
            temp_wrapper,
            source_cluster,
        )
        cutover, rollback, cleanup_name = _cutover_sqls(
            table,
            temp_wrapper,
            backup_wrapper,
            ch_cluster=source_cluster,
            use_exchange=use_exchange,
        )
        cleanup_sqls = [
            _drop_table_sql(cleanup_name, source_cluster),
            _drop_table_sql(shard_table, source_cluster),
        ]
        cleanup_tables = [
            (cleanup_name, source_cluster),
            (shard_table, source_cluster),
        ]
        strategy = "cross_cluster_rebuild"
        temporary_tables = [temp_wrapper, replacement_shard]
        backup_tables = [] if use_exchange else [backup_wrapper]
    elif pair:
        replacement_shard = _suffixed_table(shard_table, f"__reconfigure_{token}")
        backup_shard = _suffixed_table(shard_table, f"__backup_{token}")
        replacement_create = _retarget_create(
            desired_shard,
            replacement_shard,
            source_cluster,
        )
        replacement_wrapper = _transform_distributed_create(
            table_create,
            table_name=temp_wrapper,
            execution_cluster=source_cluster,
            target_cluster=target_cluster or source_cluster or "{cluster}",
            shard_table=replacement_shard,
            ch_sharding_key=options.ch_sharding_key,
        )
        cutover, rollback, cleanup_name = _cutover_sqls(
            shard_table,
            replacement_shard,
            backup_shard,
            ch_cluster=source_cluster,
            use_exchange=use_exchange,
        )
        cleanup_sqls = [
            _drop_table_sql(temp_wrapper, source_cluster),
            _drop_table_sql(cleanup_name, source_cluster),
        ]
        cleanup_tables = [
            (temp_wrapper, source_cluster),
            (cleanup_name, source_cluster),
        ]
        strategy = "managed_pair_rebuild"
        temporary_tables = [replacement_shard, temp_wrapper]
        backup_tables = [] if use_exchange else [backup_shard]
    else:
        replacement_shard = _suffixed_table(table, f"__reconfigure_{token}")
        backup_shard = _suffixed_table(table, f"__backup_{token}")
        replacement_create = _retarget_create(desired_shard, replacement_shard, None)
        replacement_wrapper = None
        cutover, rollback, cleanup_name = _cutover_sqls(
            table,
            replacement_shard,
            backup_shard,
            ch_cluster=None,
            use_exchange=use_exchange,
        )
        cleanup_sqls = [_drop_table_sql(cleanup_name, None)]
        cleanup_tables = [(cleanup_name, None)]
        strategy = "local_rebuild"
        temporary_tables = [replacement_shard]
        backup_tables = [] if use_exchange else [backup_shard]

    create_sqls = [replacement_create.sql(dialect="clickhouse", pretty=True)]
    if replacement_wrapper is not None:
        create_sqls.append(replacement_wrapper.sql(dialect="clickhouse", pretty=True))
        copy_target = temp_wrapper
    else:
        copy_target = replacement_shard
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
        database_engine=database_engine,
        before_ddl=before_ddl,
        after_ddl=after_ddl,
        temporary_tables=temporary_tables,
        backup_tables=backup_tables,
        cleanup_tables=cleanup_tables,
        rollback_sqls=rollback,
        cutover_sqls=cutover,
    )


def _new_plan(options: ChReconfigureOptions, table: str, strategy: str) -> SqlPlan:
    return SqlPlan(
        operation="ch_reconfigure_table",
        target_alias=options.connection_key,
        target_backend="ch",
        target_table=table,
        options={
            "strategy": strategy,
            "ch_engine": options.ch_engine,
            "ch_partition_by": options.ch_partition_by,
            "ch_order_by": options.ch_order_by,
            "ch_cluster": options.ch_cluster,
            "ch_source_cluster": options.ch_source_cluster,
            "ch_sharding_key": options.ch_sharding_key,
            "ch_settings": dict(options.ch_settings or {}),
            "ch_reset_partition_by": options.ch_reset_partition_by,
            "ch_reset_order_by": options.ch_reset_order_by,
            "validate_row_count": options.validate_row_count,
        },
        metadata=SqlOperationMetadata(query_label=options.query_label),
    )


def _add_sqls(
    plan: SqlPlan,
    sqls: Sequence[str],
    options: ChReconfigureOptions,
    phase: str,
    target_table: str,
) -> None:
    plan.extend(
        list(sqls),
        alias=options.connection_key,
        backend="ch",
        phase=phase,
        target_table=target_table,
        query_label=options.query_label,
    )
    plan.metadata.statement_count = len(plan.statements)


def _execute_phase(adapter: Any, connection: Any, plan: SqlPlan, phase: str) -> None:
    _execute_sqls(
        adapter,
        connection,
        [statement.sql for statement in plan.statements if statement.phase == phase],
    )


def _wait_for_created_replacement(
    connection: Any,
    reconfiguration: ChReconfiguration,
) -> None:
    if reconfiguration.replacement_table is None:
        return
    _wait_for_ch_table(connection, reconfiguration.replacement_table)
    if reconfiguration.strategy == "cross_cluster_rebuild":
        shard_table = reconfiguration.temporary_tables[-1]
        if reconfiguration.target_cluster is not None:
            _wait_for_ch_table_on_cluster(
                connection,
                shard_table,
                ch_cluster=reconfiguration.target_cluster,
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
    for table_name in reconfiguration.temporary_tables:
        try:
            cluster = (
                reconfiguration.target_cluster
                if reconfiguration.strategy == "cross_cluster_rebuild"
                and table_name != reconfiguration.temporary_tables[0]
                else reconfiguration.source_cluster
            )
            adapter.execute_command(connection, _drop_table_sql(table_name, cluster))
        except Exception:
            continue


def _cutover_sqls(
    original: str,
    replacement: str,
    backup: str,
    *,
    ch_cluster: str | None,
    use_exchange: bool,
) -> tuple[list[str], list[str], str]:
    cluster_clause = _cluster_clause(ch_cluster)
    if use_exchange:
        exchange = f"EXCHANGE TABLES {original} AND {replacement}{cluster_clause}"
        return [exchange], [exchange], replacement
    cutover = [
        f"RENAME TABLE {original} TO {backup}{cluster_clause}",
        f"RENAME TABLE {replacement} TO {original}{cluster_clause}",
    ]
    rollback = [
        f"RENAME TABLE {original} TO {replacement}{cluster_clause}",
        f"RENAME TABLE {backup} TO {original}{cluster_clause}",
    ]
    return cutover, rollback, backup


def _drop_table_sql(table: str, ch_cluster: str | None) -> str:
    return f"DROP TABLE IF EXISTS {table}{_cluster_clause(ch_cluster)}"


def _build_setting_alter_sqls(
    table: str,
    settings: Mapping[str, str | int | float | bool | None],
    *,
    ch_cluster: str | None,
) -> list[str]:
    modify: list[str] = []
    reset: list[str] = []
    for name, value in settings.items():
        normalized_name = _normalize_setting_name(name)
        if value is None:
            reset.append(normalized_name)
        else:
            modify.append(f"{normalized_name}={_setting_value_sql(value)}")
    prefix = f"ALTER TABLE {table}{_cluster_clause(ch_cluster)}"
    sqls: list[str] = []
    if modify:
        sqls.append(f"{prefix} MODIFY SETTING {', '.join(modify)}")
    if reset:
        sqls.append(f"{prefix} RESET SETTING {', '.join(reset)}")
    return sqls


def _show_create_table(connection: Any, table: str) -> str:
    result = connection.query(f"SHOW CREATE TABLE {table}")
    rows = getattr(result, "result_rows", None) or []
    if not rows or not rows[0]:
        raise InvalidSqlInputError(f"ClickHouse table {table} does not exist.")
    return str(rows[0][0]).strip().rstrip(";")


def _count_rows(connection: Any, table: str) -> int:
    result = connection.query(f"SELECT count() FROM {table}")
    rows = getattr(result, "result_rows", None) or []
    return int(rows[0][0]) if rows and rows[0] else 0


def _table_database(connection: Any, create: exp.Create) -> str:
    schema = create.this
    assert isinstance(schema, exp.Schema)
    table = schema.this
    assert isinstance(table, exp.Table)
    database = table.args.get("db")
    if isinstance(database, exp.Identifier):
        return _identifier_name(database)
    value = _query_scalar(connection, "SELECT currentDatabase()")
    return str(value)


def _database_engine(connection: Any, database: str) -> str:
    sql = f"SELECT engine FROM system.databases WHERE name = {_sql_string_literal(database)}"
    value = _query_scalar(connection, sql)
    return str(value)


def _query_scalar(connection: Any, sql: str) -> Any:
    result = connection.query(sql)
    rows = getattr(result, "result_rows", None) or []
    if not rows or not rows[0]:
        raise InvalidSqlInputError(f"ClickHouse metadata query returned no rows: {sql}")
    return rows[0][0]


def _resolve_optional_cluster(connection: Any, cluster: str | None) -> str | None:
    if cluster is None:
        return None
    return _resolve_ch_cluster_name_for_wait(
        connection,
        _non_empty_string(cluster, "ch_cluster"),
    )


def _is_cross_cluster(
    connection: Any,
    source: str | None,
    target: str | None,
) -> bool:
    if source is None or target is None or source == target:
        return False
    source_hosts = _cluster_hosts(connection, source)
    target_hosts = _cluster_hosts(connection, target)
    if not source_hosts:
        raise InvalidSqlInputError(f"ClickHouse source cluster {source!r} has no hosts.")
    if not target_hosts:
        raise InvalidSqlInputError(f"ClickHouse target cluster {target!r} has no hosts.")
    if source_hosts == target_hosts:
        return False
    if source_hosts & target_hosts:
        raise InvalidSqlInputError("Partially overlapping ClickHouse clusters are not supported.")
    return True


def _cluster_hosts(connection: Any, cluster: str) -> set[tuple[str, str, int]]:
    result = connection.query(
        "SELECT host_name, host_address, port FROM system.clusters WHERE cluster = "
        f"{_sql_string_literal(cluster)}"
    )
    return {
        (str(row[0]), str(row[1]), int(row[2]))
        for row in (getattr(result, "result_rows", None) or [])
        if len(row) >= 3
    }


def _table_exists_on_cluster(
    connection: Any,
    table_name: str,
    cluster: str | None,
) -> bool:
    if cluster is None:
        return False
    database, relation = _distributed_table_parts(table_name)
    result = connection.query(
        "SELECT count() FROM clusterAllReplicas("
        f"{_sql_string_literal(cluster)}, system, tables) "
        f"WHERE database = {_sql_string_literal(database)} "
        f"AND name = {_sql_string_literal(relation)}"
    )
    rows = getattr(result, "result_rows", None) or []
    return bool(rows and rows[0] and int(rows[0][0]))


def _normalize_table_name(table: str) -> str:
    normalized = _non_empty_string(str(table), "table")
    _parse_table_name(normalized, "clickhouse")
    return normalized


def _non_empty_string(value: str, option_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise InvalidSqlInputError(f"{option_name} must not be empty.")
    return normalized


def _supports_exchange(database_engine: str) -> bool:
    return database_engine.strip().lower() in {"atomic", "shared"}


def _suffixed_table(table: str, suffix: str) -> str:
    return _add_table_identifier_suffix(table, suffix, "clickhouse")


def _qualify_like(table: str, relation: str) -> str:
    parsed = _parse_table_name(table, "clickhouse")
    database = parsed.args.get("db")
    relation_identifier = exp.to_identifier(relation)
    if isinstance(database, exp.Identifier):
        return exp.Table(this=relation_identifier, db=database.copy()).sql(dialect="clickhouse")
    return relation_identifier.sql(dialect="clickhouse")


def _qualify_with_database(table: str, database: str) -> str:
    parsed = _parse_table_name(table, "clickhouse")
    if isinstance(parsed.args.get("db"), exp.Identifier):
        return table
    parsed.set("db", exp.to_identifier(database))
    return parsed.sql(dialect="clickhouse")


def _unquoted_table_name(table: str) -> tuple[str | None, str]:
    parsed = _parse_table_name(table, "clickhouse")
    database = parsed.args.get("db")
    return (
        _identifier_name(database) if isinstance(database, exp.Identifier) else None,
        _identifier_name(parsed.this),
    )


__all__ = [
    "ChReconfiguration",
    "ChReconfigureOptions",
    "execute_ch_table_reconfiguration",
    "plan_ch_table_reconfiguration",
]
