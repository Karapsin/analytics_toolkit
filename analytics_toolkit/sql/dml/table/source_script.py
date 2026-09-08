from __future__ import annotations

import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

import sqlparse

from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.source_script import (
    execute_source_setup,
    source_materialization_sqls,
)
from analytics_toolkit.sql.backends.transfer_stage import execute_transfer_materialization
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.dml.io.query_writes import (
    _normalize_result_statements,
    _validate_result_query,
)
from analytics_toolkit.sql.dml.load.stage import build_stage_table_name, cleanup_stage_table
from analytics_toolkit.sql.dml.transfer.flow.stage_identity import resolve_destination_identity
from analytics_toolkit.sql.execution.labels import apply_query_label
from analytics_toolkit.sql.execution.operation_runner import tracked_sql_operation
from analytics_toolkit.sql.execution.plans import SqlOperationMetadata, SqlOperationResult, SqlPlan

from .create_from_sql_plan import _build_create_table_from_sql_plan

if TYPE_CHECKING:
    from .models import CreateTableFromSqlOptions


def normalize_source_script(sql: str) -> tuple[tuple[str, ...], str]:
    if isinstance(sql, str) and not sql.strip():
        msg = "sql must not be empty."
        raise InvalidSqlInputError(msg)
    statements = []
    for statement in _normalize_result_statements(sql):
        tokens = [
            token for parsed in sqlparse.parse(statement) for token in cast("Any", parsed).flatten()
        ]
        while tokens and (
            tokens[-1].is_whitespace
            or tokens[-1].ttype in sqlparse.tokens.Comment
            or tokens[-1].value == ";"
        ):
            tokens.pop()
        if tokens:
            statements.append("".join(token.value for token in tokens))
    if not statements:
        msg = "sql must not be empty."
        raise InvalidSqlInputError(msg)
    if len(statements) > 1:
        _validate_result_query(statements[-1])
    return tuple(statements[:-1]), statements[-1]


def validate_generated_source(sql: str) -> None:
    setup, _ = normalize_source_script(sql)
    if setup:
        msg = "only_generate_sql does not support setup statements; use dry_run or return_sql."
        raise InvalidSqlInputError(msg)


def validate_script_staging(config: Any, setup: tuple[str, ...], *, insert_data: bool) -> None:
    if setup and insert_data and not config.transfer_staging_schema:
        msg = (
            "SQL scripts with insert_data=True require transfer_staging_schema "
            "on the source connection."
        )
        raise InvalidSqlInputError(msg)


def _stage_name(options: CreateTableFromSqlOptions, *, suffix: str) -> str:
    config = get_connection_config(options.source_key)
    return build_stage_table_name(
        options.source_backend,
        "source_result",
        transfer_staging_schema=config.transfer_staging_schema,
        random_suffix=suffix,
        destination_hash=resolve_destination_identity(
            options.target_table, options.target_backend
        ).hash_prefix,
    )


def build_script_plan(options: CreateTableFromSqlOptions) -> SqlPlan:
    stage = _stage_name(options, suffix="create_source") if options.insert_data else None
    source_sql = f"SELECT * FROM {stage}" if stage else options.source_sql  # noqa: S608
    plan = _build_create_table_from_sql_plan(
        source_key=options.source_key,
        source_backend=options.source_backend,
        target_key=options.target_key,
        target_backend=options.target_backend,
        target_table=options.target_table,
        source_sql=source_sql,
        table_schema=options.table_schema,
        insert_data=options.insert_data,
        drop_target_if_exists=options.drop_target_if_exists,
        gp_distributed_by_key=options.gp_distributed_by_key,
        gp_partitions=options.gp_partitions,
        partition_by=options.partition_by,
        order_by=options.order_by,
        ch_engine=options.ch_engine,
        ch_cluster=options.ch_cluster,
        ch_sharding_key=options.ch_sharding_key,
        ch_only_shard=options.ch_only_shard,
        query_label=options.query_label,
        ddl_properties=dict(options.ddl_properties or {}),
        ch_creation_policy=options.ch_creation_policy,
    )
    target_steps = plan.statements
    plan.statements = []
    plan.extend(
        list(options.setup_sqls),
        alias=options.source_key,
        backend=options.source_backend,
        phase="setup",
        query_label=options.query_label,
    )
    if stage:
        create_sql, populate_sql = source_materialization_sqls(
            get_backend_adapter(options.source_backend),
            stage,
            options.source_sql,
            policy=options.source_staging_ch_policy,
            query_label=options.query_label,
        )
        plan.add(
            create_sql,
            alias=options.source_key,
            backend=options.source_backend,
            phase="materialize_source",
            target_table=stage,
        )
        if populate_sql is not None:
            plan.add(
                populate_sql,
                alias=options.source_key,
                backend=options.source_backend,
                phase="populate_source_stage",
                target_table=stage,
            )
            plan.options["source_stage_wait_policy"] = (
                options.source_staging_ch_policy.ddl_wait_policy
            )
        plan.options["source_stage_name_is_placeholder"] = True
    plan.statements.extend(target_steps)
    if stage:
        adapter = get_backend_adapter(options.source_backend)
        cleanup_sqls = (
            adapter.build_creation_policy_cleanup_sqls(
                stage,
                options.source_staging_ch_policy,
                query_label=options.query_label,
                if_exists=True,
            )
            if options.source_staging_ch_policy is not None
            else [adapter.drop_table_sql(stage, if_exists=True)]
        )
        plan.extend(
            cleanup_sqls,
            alias=options.source_key,
            backend=options.source_backend,
            phase="cleanup_source_stage",
            target_table=stage,
            query_label=options.query_label,
        )
    return plan


def execute_script_attempt(
    options: CreateTableFromSqlOptions, target_adapter: Any, attempt: int
) -> object:
    from . import create_table_from_sql as creation  # noqa: PLC0415

    source_adapter = get_backend_adapter(options.source_backend)
    config = get_connection_config(options.source_key)
    connection = creation.get_sql_connection(options.source_key)
    stage: str | None = None
    delegated = False
    result: object = None
    failure: BaseException | None = None
    metadata = SqlOperationMetadata(query_label=options.query_label)
    try:
        with tracked_sql_operation(
            metadata=metadata,
            operation_name="create_table_from_sql",
            alias=options.source_key,
            backend=options.source_backend,
            phase="prepare_source",
            retry_attempt=attempt,
            query_label=options.query_label,
            preview_sql=options.source_sql,
        ):
            prepared = [
                source_adapter.prepare_sql(config, apply_query_label(sql, options.query_label))
                for sql in options.setup_sqls
            ]
            execute_source_setup(source_adapter, connection, prepared)
            source_sql = source_adapter.prepare_sql(config, options.source_sql)
            if options.insert_data:
                # Register ownership before CTAS so partial creation is also cleaned up.
                stage = _allocate_stage(options, connection)
                _materialize_source(options, source_adapter, connection, stage, source_sql)
                source_sql = f"SELECT * FROM {stage}"  # noqa: S608
        delegated = True
        result = creation._execute_generic_create_table_from_sql_attempt(  # noqa: SLF001
            options=replace(options, source_sql=source_sql),
            target_adapter=target_adapter,
            attempt=attempt,
            source_connection=connection,
        )
    except BaseException as exc:  # noqa: BLE001
        failure = exc
        if not delegated:
            source_adapter.rollback_quietly(connection)
    finally:
        if not delegated:
            creation._close_connection_quietly(  # noqa: SLF001
                connection, connection_key=options.source_key, backend=options.source_backend
            )

    if stage:
        try:
            _cleanup_stage(options, stage, attempt)
        except Exception as exc:
            if failure is not None:
                if not isinstance(failure, Exception):
                    raise failure from exc
                exc.__cause__ = failure
            return creation._UnsafeAttemptFailure(exc, attempt)  # noqa: SLF001
    if failure is not None:
        raise failure
    return _add_script_metadata(result, metadata, stage, attempt)


def _add_script_metadata(
    result: object, metadata: SqlOperationMetadata, stage: str | None, attempt: int
) -> object:
    if isinstance(result, SqlOperationResult):
        result.metadata.elapsed_seconds = (result.metadata.elapsed_seconds or 0) + (
            metadata.elapsed_seconds or 0
        )
        result.metadata.source_stage_count = int(stage is not None)
        if stage:
            result.metadata.stage_tables = [stage, *(result.metadata.stage_tables or [])]
        result.metadata.retry_attempts = attempt
    return result


def _cleanup_stage(options: CreateTableFromSqlOptions, stage: str, attempt: int) -> None:
    from . import create_table_from_sql as creation  # noqa: PLC0415

    connection = creation.get_sql_connection(options.source_key)
    try:
        with tracked_sql_operation(
            operation_name="create_table_from_sql",
            alias=options.source_key,
            backend=options.source_backend,
            phase="cleanup_source_stage",
            retry_attempt=attempt,
            query_label=options.query_label,
        ):
            cleanup_stage_table(
                options.source_backend,
                connection,
                stage,
                query_label=options.query_label,
                ch_creation_policy=options.source_staging_ch_policy,
            )
    finally:
        creation._close_connection_quietly(  # noqa: SLF001
            connection, connection_key=options.source_key, backend=options.source_backend
        )


def _allocate_stage(options: CreateTableFromSqlOptions, connection: Any) -> str:
    from . import create_table_from_sql as creation  # noqa: PLC0415

    candidate = _stage_name(options, suffix=f"{uuid.uuid4().hex}__source")
    if creation.table_exists(options.source_backend, connection, candidate, options.source_key):
        message = "Source staging table name collision; retry with a fresh name."
        raise RuntimeError(message)
    return candidate


def _materialize_source(
    options: CreateTableFromSqlOptions, adapter: Any, connection: Any, stage: str, source_sql: str
) -> None:
    create_sql, populate_sql = source_materialization_sqls(
        adapter,
        stage,
        source_sql,
        policy=options.source_staging_ch_policy,
        query_label=options.query_label,
    )
    execute_transfer_materialization(adapter, options.source_backend, connection, create_sql)
    if populate_sql is not None:
        adapter.after_create_table(
            connection,
            stage,
            ch_only_shard=True,
            ch_creation_policy=options.source_staging_ch_policy,
        )
        execute_transfer_materialization(adapter, options.source_backend, connection, populate_sql)
