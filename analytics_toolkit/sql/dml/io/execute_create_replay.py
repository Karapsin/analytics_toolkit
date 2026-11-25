"""Keep acknowledged creation and certified self-source input across attempts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from sqlglot import exp, parse_one

from analytics_toolkit.sql.backends import creation_resources
from analytics_toolkit.sql.backends.query_source_binding import query_may_read_target
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.core.identifiers import TableIdentifier
from analytics_toolkit.sql.ddl.api import _create_sql_table_with_connection
from analytics_toolkit.sql.ddl.ownership import (
    CreationOwnership,
    acknowledge_created_resource,
    acknowledge_created_table_resource,
    capture_creation_ownership,
    execute_owned_create_sqls,
)
from analytics_toolkit.sql.dml.query_input import (
    QueryInputRequest,
    cached_query_schema,
    mark_query_input_mutation,
    prepare_query_input,
    validate_query_columns,
)
from analytics_toolkit.sql.dml.table.create_ownership import record_target_recovery
from analytics_toolkit.sql.dml.table.create_table_from_sql import _validate_source_columns
from analytics_toolkit.sql.dml.transfer.schema import inspect_source_query_schema
from analytics_toolkit.sql.execution.labels import apply_query_label

if TYPE_CHECKING:
    from analytics_toolkit.sql.dml.query_input import PreparedQueryInput
    from analytics_toolkit.sql.execution.plans import SqlOperationMetadata

    from .execute_create import _ExecuteCreateOptions


@dataclass
class ExecuteCreateReplay:
    ownership: CreationOwnership = field(default_factory=CreationOwnership)
    creation_complete: bool = False
    connection_ref: dict[str, Any] = field(default_factory=dict)
    metadata: SqlOperationMetadata | None = None


def execute_create_replay_state(options: _ExecuteCreateOptions) -> ExecuteCreateReplay:
    return cast(
        "ExecuteCreateReplay",
        options.input_resolution.setdefault("execute_create_replay", ExecuteCreateReplay()),
    )


def prepare_execute_create_input(
    options: _ExecuteCreateOptions,
    adapter: Any,
    connection_ref: dict[str, Any],
    metadata: SqlOperationMetadata,
) -> PreparedQueryInput | None:
    if not options.drop_if_exists or not query_may_read_target(
        options.source_sql, options.table_name, options.backend
    ):
        return None
    cache = options.input_resolution
    schema = cached_query_schema(cache) or inspect_source_query_schema(
        options.backend,
        connection_ref["connection"],
        apply_query_label(options.source_sql, options.query_label),
    )
    validate_query_columns(options, schema, adapter, _validate_source_columns)
    prepared = prepare_query_input(
        QueryInputRequest(
            options.connection_key,
            options.backend,
            options.table_name,
            options.source_sql,
            query_label=options.query_label,
            force_freeze=True,
        ),
        connection_ref,
        cache,
        schema,
    )
    prepared.describe(metadata)
    return prepared


def require_execute_create_resources(
    state: ExecuteCreateReplay, adapter: Any, connection: Any, target: str
) -> None:
    resources = state.ownership.scoped_resources
    if not resources:
        message = f"Retaining created target {target}: no native creation identity is available."
        raise InvalidSqlInputError(message)
    for resource in resources:
        creation_resources.require_created_resource(adapter, connection, resource)


def _plain_create_sqls(adapter: Any, sqls: list[str]) -> list[str]:
    statements = []
    seen: set[tuple[str, ...]] = set()
    for sql in sqls:
        tree = parse_one(sql, read=adapter.sqlglot_dialect)
        if not isinstance(tree, exp.Create):
            message = "Execute-create requires statically named CREATE TABLE statements."
            raise InvalidSqlInputError(message)
        table = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
        identity = TableIdentifier.parse(
            table.sql(dialect=adapter.sqlglot_dialect), adapter.backend
        )
        key = creation_resources.creation_identity_key(adapter, identity)
        if key not in seen:
            tree.set("exists", value=False)
            seen.add(key)
        statements.append(tree.sql(dialect=adapter.sqlglot_dialect))
    return statements


def create_owned_execute_target(
    options: _ExecuteCreateOptions,
    adapter: Any,
    connection: Any,
    state: ExecuteCreateReplay,
    *,
    creation: tuple[list[str], PreparedQueryInput | None],
) -> None:
    create_sqls, prepared = creation
    with capture_creation_ownership(state.ownership):
        if prepared is None:
            execute_owned_create_sqls(
                adapter,
                connection,
                _plain_create_sqls(adapter, create_sqls),
                acknowledge_created_resource,
                on_scoped_resource_created=acknowledge_created_table_resource,
                connection_key=options.connection_key,
            )
        else:
            _create_sql_table_with_connection(
                options.backend,
                connection,
                options.table_name,
                connection_key=options.connection_key,
                table_schema=prepared.schema,
                gp_distributed_by_key=options.gp_distributed_by_key,
                gp_partitions=options.gp_partitions,
                partition_by=options.partition_by,
                order_by=options.order_by,
                ddl_properties=options.ddl_properties,
                ch_creation_policy=options.ch_creation_policy,
                ch_only_shard=options.ch_only_shard,
                query_label=options.query_label,
                on_resource_created=acknowledge_created_resource,
                on_scoped_resource_created=acknowledge_created_table_resource,
            )
    state.creation_complete = True


def prepare_execute_create_target(
    options: _ExecuteCreateOptions,
    adapter: Any,
    connection: Any,
    state: ExecuteCreateReplay,
    prepared: PreparedQueryInput | None,
) -> None:
    if state.creation_complete:
        return
    if prepared is not None:
        mark_query_input_mutation(options.input_resolution)
    prepare_target = getattr(adapter, "prepare_execute_create_target", None)
    if callable(prepare_target):
        prepare_target(connection, options)
    else:
        adapter.execute_commands(connection, options.drop_sqls)


def execute_initial_creation(
    options: _ExecuteCreateOptions,
    adapter: Any,
    connection: Any,
    state: ExecuteCreateReplay,
    creation: tuple[list[str], PreparedQueryInput | None],
) -> Any:
    if state.creation_complete:
        return None
    create_sqls, prepared = creation
    if options.insert_after_create or prepared is not None:
        create_owned_execute_target(options, adapter, connection, state, creation=creation)
    elif len(create_sqls) != 1:
        adapter.execute_commands(connection, create_sqls)
    else:
        return adapter.execute_command(connection, create_sqls[0])
    return None


def protect_unfinished_execute_create(
    options: _ExecuteCreateOptions, state: ExecuteCreateReplay, error: Exception
) -> None:
    if state.ownership.submitted and not state.creation_complete:
        record_target_recovery(state.ownership, error, options.table_name)
