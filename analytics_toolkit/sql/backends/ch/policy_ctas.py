"""Query creation using the same resolved policy as schema/dataframe creation."""

from __future__ import annotations

import importlib
from dataclasses import asdict
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.core.type_resolution import native_candidates
from analytics_toolkit.sql.ddl.ownership import (
    acknowledge_created_resource,
    execute_owned_create_sqls,
)
from analytics_toolkit.sql.ddl.schema import build_table_schema_column_definitions
from analytics_toolkit.sql.dml.query_input import (
    PreparedQueryInput,
    QueryInputRequest,
    prepare_query_input,
    query_input_scope,
    replay_query_input,
)
from analytics_toolkit.sql.dml.transfer.runtime.retry import close_connection_ref
from analytics_toolkit.sql.execution.labels import apply_query_label
from analytics_toolkit.sql.execution.plans import SqlOperationMetadata, SqlOperationResult, SqlPlan

from .create_table_as import (
    _annotate_ch_cte_join_exception as annotate_query_exception,
)
from .create_table_as import (
    _infer_ch_query_columns as infer_query_columns,
)
from .create_table_as import (
    _inspect_ch_query_column_names as inspect_query_column_names,
)
from .create_table_as import (
    _normalize_single_query as normalize_single_query,
)
from .creation_policy import build_policy_create_sqls
from .layout import inspect_existing_layout
from .lifecycle import build_drop_ch_creation_policy_table_sqls
from .policy_drop import execute_policy_target_drop
from .query_source_scope import query_overlaps_layout

if TYPE_CHECKING:
    from analytics_toolkit.sql.connection.config import ChConfig

legacy: Any = importlib.import_module("analytics_toolkit.sql.backends.ch.create_table_as")


def create_table_as_with_policy(*, policy: Any, **options: Any) -> Any:
    if options.get("input_resolution") is not None or options["dry_run"] or options["return_sql"]:
        return _create_table_as_with_policy(policy=policy, **options)
    config = get_connection_config(options["target_key"])
    options["input_resolution"] = {}
    with query_input_scope(options["input_resolution"], [config, config]):
        return _create_table_as_with_policy(policy=policy, **options)


def _create_table_as_with_policy(*, policy: Any, **options: Any) -> Any:
    table = options["target_table"]
    query = normalize_single_query(options["source_sql"])
    label = options["query_label"]
    dry = options["dry_run"] or options["return_sql"]
    schema = options.get("table_schema")
    replace_target = options.get("ch_replace_table", False)
    metadata = SqlOperationMetadata(query_label=label)
    plan = SqlPlan(
        operation="ch_create_table_as",
        target_alias=options["target_key"],
        target_backend="ch",
        target_table=table,
        options={
            "ch_creation_policy": asdict(policy),
            "insert_data": options["insert_data"],
            "ch_engine": policy.shard_engine,
            "ch_cluster": policy.distributed_cluster,
            "ch_sharding_key": policy.sharding_key,
            "ch_only_shard": options["ch_only_shard"],
            "partition_by": options["partition_by"],
            "order_by": options["order_by"],
            "drop_target_if_exists": options["drop_target_if_exists"],
            "table_schema": schema,
            "ch_replace_table": replace_target,
        },
        metadata=metadata,
    )
    connection_ref: dict[str, Any] = {"connection": None}
    connection: Any = None
    prepared: PreparedQueryInput | None = None
    adapter = get_backend_adapter("ch")
    exists = False
    try:
        if not dry:
            connection = legacy.get_sql_connection(options["target_key"])
            connection_ref["connection"] = connection
            exists = adapter.table_exists(connection, table, connection_key=options["target_key"])
            if exists and not options["drop_target_if_exists"] and not replace_target:
                message = f"Target table {table} already exists."
                raise InvalidSqlInputError(message)
        joined, schema, prepared = _resolve_population_columns(
            connection_ref,
            policy,
            options,
            plan,
            (metadata, exists),
        )
        connection = connection_ref["connection"]
        drop_sqls = _drop_statements(connection, policy, options, plan, exists=exists)
        create_sqls = build_policy_create_sqls(
            table_name=table,
            joined_columns=joined,
            partition_by=options["partition_by"],
            order_by=options["order_by"],
            policy=policy,
            ch_only_shard=options["ch_only_shard"],
            ch_replace_table=replace_target,
            if_not_exists=False,
        )
        for phase, sqls in (("drop_target", drop_sqls), ("create_target", create_sqls)):
            for sql in sqls:
                plan.add(
                    apply_query_label(sql, label),
                    alias=options["target_key"],
                    backend="ch",
                    phase=phase,
                    target_table=table,
                    query_label=label,
                )
        _add_population_plan(plan, options, query, prepared)
        metadata.statement_count = len(plan.statements)
        if dry:
            return plan
        if prepared is not None and prepared.frozen:
            plan.options["input_frozen"] = True
            plan.options["certified_input_count"] = metadata.input_count
        _execute_creation(connection, policy, options, schema, (drop_sqls, create_sqls))
        _populate_target(connection_ref, options, prepared, metadata)
    finally:
        if connection_ref["connection"] is not None:
            close_connection_ref(connection_ref, options["target_key"], "target")
    return (
        SqlOperationResult(rows=None, metadata=metadata, plan=plan)
        if options["return_metadata"]
        else None
    )


def _resolve_population_columns(
    connection_ref: dict[str, Any],
    policy: Any,
    options: Any,
    plan: SqlPlan,
    state: tuple[SqlOperationMetadata, bool],
) -> tuple[str, Any, PreparedQueryInput | None]:
    metadata, exists = state
    connection = connection_ref["connection"]
    schema = options.get("table_schema")
    query = normalize_single_query(options["source_sql"])
    dry = options["dry_run"] or options["return_sql"]
    prepared = None
    try:
        if not dry and options["insert_data"]:
            prepared = _prepare_population(connection_ref, policy, options, query, exists=exists)
            schema = prepared.schema
            prepared.describe(metadata)
            joined = build_table_schema_column_definitions("ch", schema, columns=prepared.columns)
        else:
            joined = _query_columns(connection, schema, query, plan)
            if dry and options["insert_data"]:
                plan.options["pending_input_validation"] = True
                plan.options["client_write_validation_pending"] = True
                plan.options["client_write_validation_condition"] = "python_value_replay"
                plan.options["pending_source_overlap_check"] = bool(
                    options["drop_target_if_exists"] or options.get("ch_replace_table")
                )
                if schema is None:
                    plan.options["type_resolution_pending"] = True
                    plan.options["type_resolution"] = {
                        "status": "pending",
                        "native_candidates": native_candidates("ch"),
                        "dry_run_profiles_input": False,
                    }
    except Exception as exc:
        annotate_query_exception(exc, query, backend="ch")
        raise
    return joined, schema, prepared


def _add_population_plan(
    plan: SqlPlan,
    options: Any,
    query: str,
    prepared: PreparedQueryInput | None,
) -> None:
    if not options["insert_data"]:
        return
    table = options["target_table"]
    sql = f"INSERT INTO {table}\n{query}"
    if prepared is not None and prepared.frozen:
        columns = ", ".join(get_backend_adapter("ch").quote_identifier(c) for c in prepared.columns)
        # Identifiers are quoted; this plan marker contains no data values.
        sql = f"INSERT INTO {table} ({columns}) VALUES <certified input replay>"  # noqa: S608
        plan.options["population_method"] = "certified_input_replay"
    plan.add(
        apply_query_label(sql, options["query_label"]),
        alias=options["target_key"],
        backend="ch",
        phase="insert_target",
        target_table=table,
        query_label=options["query_label"],
    )


def _execute_creation(
    connection: Any,
    policy: Any,
    options: Any,
    schema: Any,
    sqls: tuple[list[str], list[str]],
) -> None:
    adapter = get_backend_adapter("ch")
    table = options["target_table"]
    label = options["query_label"]
    drop_sqls, create_sqls = sqls
    if options.get("ch_replace_table"):
        adapter.preflight_create_table_replacement(
            connection,
            SimpleNamespace(
                ch_replace_table=True,
                ch_creation_policy=policy,
                table_name=table,
                connection_key=options["target_key"],
                query_label=label,
                ch_only_shard=options["ch_only_shard"],
            ),
        )
    execute_policy_target_drop(
        adapter,
        connection,
        [apply_query_label(sql, label) for sql in drop_sqls],
        target_key=options["target_key"],
        retry_per_host_drops=options.get("ch_retry_per_host_drops", True),
    )
    execute_owned_create_sqls(
        adapter,
        connection,
        [apply_query_label(sql, label) for sql in create_sqls],
        acknowledge_created_resource,
    )
    adapter.after_create_table(
        connection,
        table,
        ch_cluster=policy.distributed_cluster,
        ch_distributed_table=policy.create_distributed_pair,
        ch_only_shard=options["ch_only_shard"],
        expected_column_types=schema,
        ch_creation_policy=policy,
    )


def _populate_target(
    connection_ref: dict[str, Any],
    options: Any,
    prepared: PreparedQueryInput | None,
    metadata: SqlOperationMetadata,
) -> None:
    if not options["insert_data"]:
        return
    query = normalize_single_query(options["source_sql"])
    try:
        if prepared is not None and prepared.frozen:
            replay_query_input(prepared, connection_ref, metadata)
        else:
            sql = f"INSERT INTO {options['target_table']}\n{query}"
            connection_ref["connection"].command(apply_query_label(sql, options["query_label"]))
    except Exception as exc:
        annotate_query_exception(exc, query, backend="ch")
        raise


def _prepare_population(
    connection_ref: dict[str, Any], policy: Any, options: Any, query: str, *, exists: bool
) -> PreparedQueryInput:
    config = get_connection_config(options["target_key"])
    overlap = False
    if exists and (options["drop_target_if_exists"] or options.get("ch_replace_table")):
        layout = inspect_existing_layout(
            connection_ref["connection"],
            options["target_table"],
            distributed_on_cluster=policy.distributed_on_cluster,
        )
        overlap = query_overlaps_layout(
            query,
            layout,
            connection_ref["connection"],
            cast("ChConfig", config).database,
        )
    return prepare_query_input(
        QueryInputRequest(
            options["target_key"],
            "ch",
            options["target_table"],
            query,
            options.get("table_schema"),
            options["query_label"],
            force_freeze=overlap,
        ),
        connection_ref,
        options["input_resolution"],
    )


def _query_columns(connection: Any, schema: Any, query: str, plan: SqlPlan) -> str:
    if schema is not None:
        columns = (
            list(schema) if connection is None else inspect_query_column_names(connection, query)
        )
        return build_table_schema_column_definitions("ch", schema, columns=columns)
    if connection is None:
        plan.options["pending_schema_inspection"] = True
        return "<query schema>"
    return infer_query_columns(connection, query)


def _drop_statements(
    connection: Any, policy: Any, options: Any, plan: SqlPlan, *, exists: bool
) -> list[str]:
    if not options["drop_target_if_exists"]:
        return []
    table = options["target_table"]
    if connection is None:
        plan.options["pending_existing_layout_verification"] = True
        return build_drop_ch_creation_policy_table_sqls(table, policy)
    if not exists:
        return []
    layout = inspect_existing_layout(
        connection, table, distributed_on_cluster=policy.distributed_on_cluster
    )
    return build_drop_ch_creation_policy_table_sqls(table, layout)


__all__ = ["create_table_as_with_policy"]
