from __future__ import annotations

# ruff: noqa: E501, EM101, EM102, I001, PLR0913, PLR2004, S101, TRY003

import string
import warnings
from dataclasses import dataclass, replace
from typing import Any

import sqlglot
from sqlglot import expressions as exp
from sqlglot.errors import SqlglotError

from analytics_toolkit.sql.backends.ch.ddl import (
    _build_order_by_sql,
    _build_partition_by_sql,
    _format_ch_cluster_name,
    _sql_string_literal,
    add_explicit_ch_uuid_to_local_replicated_create,
    build_ch_shard_table_name,
    split_ch_table_name_for_distributed_engine,
)
from analytics_toolkit.sql.backends.ch.metadata import extract_clickhouse_function_args
from analytics_toolkit.sql.connection.ddl_defaults import (
    ClickHouseScopeDefaults,
    MISSING_DDL_VALUE,
)
from analytics_toolkit.sql.connection.errors import SqlConfigError
from analytics_toolkit.sql.execution.validation import validate_positive_number

from .wait_policy import ChDdlWaitPolicy, resolve_ch_ddl_wait_policy, waits_for_shard

DEFAULT_DDL_READY_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class ClickHouseCreationPolicy:
    create_distributed_pair: bool
    shard_engine: str
    shard_on_cluster: str | None
    distributed_engine_template: str | None
    distributed_cluster: str | None
    distributed_on_cluster: str | None
    sharding_key: str | None
    ddl_ready_timeout_seconds: float
    ddl_ready_timeout_extension_cnt: int = 1
    ddl_ready_timeout_increment_seconds: float = 0.0
    ddl_wait_policy: ChDdlWaitPolicy = "wait_all"


def resolve_clickhouse_creation_policy(
    scope: ClickHouseScopeDefaults,
    *,
    ch_engine: str | None,
    ch_cluster: str | None,
    ch_sharding_key: str | None,
    ch_distributed_table: bool | None,
    ch_only_shard: bool,
    ch_distributed_engine_template: str | None,
    ch_distributed_cluster: str | None,
    ch_shard_on_cluster: str | None | object = MISSING_DDL_VALUE,
    ch_distributed_on_cluster: str | None | object = MISSING_DDL_VALUE,
    warn_ch_cluster: bool = True,
    ch_ddl_ready_timeout_seconds: float | None = None,
    connection_ddl_ready_timeout_seconds: float | None = None,
    ch_ddl_ready_timeout_extension_cnt: int | None = None,
    connection_ddl_ready_timeout_extension_cnt: int | None = None,
    ch_ddl_wait_policy: str | None = None,
    connection_ddl_wait_policy: str | None = None,
) -> ClickHouseCreationPolicy:
    if ch_cluster is not None and warn_ch_cluster:
        warnings.warn(
            "ch_cluster is deprecated; use ch_shard_on_cluster, "
            "ch_distributed_on_cluster, and ch_distributed_cluster.",
            DeprecationWarning,
            stacklevel=3,
        )
    pair = (
        False
        if ch_only_shard
        else (
            ch_distributed_table
            if ch_distributed_table is not None
            else scope.create_distributed_pair
        )
    )
    if pair is MISSING_DDL_VALUE:
        _missing("create_distributed_pair", "ch_distributed_table")
    engine = ch_engine if ch_engine is not None else scope.shard.engine
    if engine is MISSING_DDL_VALUE:
        _missing("shard.engine", "ch_engine")
    shard_cluster = _resolve_cluster_override(
        ch_shard_on_cluster, ch_cluster, scope.shard.on_cluster
    )
    if shard_cluster is MISSING_DDL_VALUE:
        _missing("shard.on_cluster", "ch_shard_on_cluster")

    distributed = scope.distributed
    template = (
        ch_distributed_engine_template
        if ch_distributed_engine_template is not None
        else distributed.engine_template
    )
    routing = _first_present(ch_distributed_cluster, ch_cluster, distributed.cluster)
    distributed_cluster = _resolve_cluster_override(
        ch_distributed_on_cluster, ch_cluster, distributed.on_cluster
    )
    sharding = _first_present(ch_sharding_key, distributed.sharding_key)
    if pair:
        if template is MISSING_DDL_VALUE:
            _missing("distributed.engine_template", "ch_distributed_engine_template")
        if distributed_cluster is MISSING_DDL_VALUE:
            _missing("distributed.on_cluster", "ch_distributed_on_cluster")
        validate_distributed_template(str(template))
    resolved_shard_cluster = None if shard_cluster is None else str(shard_cluster)
    ready_timeout = validate_positive_number(
        ch_ddl_ready_timeout_seconds
        if ch_ddl_ready_timeout_seconds is not None
        else (
            connection_ddl_ready_timeout_seconds
            if connection_ddl_ready_timeout_seconds is not None
            else DEFAULT_DDL_READY_TIMEOUT_SECONDS
        ),
        "ch_ddl_ready_timeout_seconds",
    )
    ready_timeout_extension_cnt = _validate_non_negative_int(
        ch_ddl_ready_timeout_extension_cnt
        if ch_ddl_ready_timeout_extension_cnt is not None
        else (
            connection_ddl_ready_timeout_extension_cnt
            if connection_ddl_ready_timeout_extension_cnt is not None
            else 1
        ),
        "ch_ddl_ready_timeout_extension_cnt",
    )
    return ClickHouseCreationPolicy(
        bool(pair),
        str(engine),
        resolved_shard_cluster,
        None if template is MISSING_DDL_VALUE else str(template),
        None if routing is MISSING_DDL_VALUE else str(routing),
        distributed_cluster
        if distributed_cluster is None
        else (None if distributed_cluster is MISSING_DDL_VALUE else str(distributed_cluster)),
        None if sharding is MISSING_DDL_VALUE else str(sharding),
        ready_timeout,
        ready_timeout_extension_cnt,
        0.0,
        resolve_ch_ddl_wait_policy(ch_ddl_wait_policy, connection_ddl_wait_policy),
    )


def prepare_cluster_routed_transfer_staging_policy(
    config: Any,
    policy: ClickHouseCreationPolicy,
) -> ClickHouseCreationPolicy:
    routing = getattr(config, "cluster_routing", None)
    if routing is None:
        return policy
    if policy.create_distributed_pair:
        raise SqlConfigError(
            "ClickHouse cluster_routing requires transfer staging with "
            "ddl_defaults.staging.create_distributed_pair=false."
        )
    try:
        engine = sqlglot.parse_one(policy.shard_engine, read="clickhouse")
    except SqlglotError as exc:
        raise SqlConfigError(
            "ClickHouse ddl_defaults.staging.shard.engine must be a valid engine expression."
        ) from exc
    engine_name = str(getattr(engine, "name", "")).lower()
    if engine_name != "mergetree":
        raise SqlConfigError(
            "ClickHouse cluster_routing requires transfer staging engine MergeTree; "
            f"{policy.shard_engine!r} is incompatible because transfer stages are read "
            "across all replicas."
        )
    wait_policy = policy.ddl_wait_policy
    if not waits_for_shard(wait_policy):
        wait_policy = "wait_shard"
    return replace(
        policy,
        shard_on_cluster=routing.cluster,
        ddl_wait_policy=wait_policy,
    )


def _validate_non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be an integer of at least 0.")
    return int(value)


def _resolve_cluster_override(
    dedicated: str | None | object, legacy: str | None, configured: str | None | object
) -> str | None | object:
    if dedicated is not None and dedicated is not MISSING_DDL_VALUE:
        return dedicated
    if legacy is not None:
        return legacy
    return configured


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value is not MISSING_DDL_VALUE:
            return value
    return values[-1] if values and values[-1] is None else MISSING_DDL_VALUE


def _missing(field: str, argument: str) -> None:
    raise SqlConfigError(
        f"Missing ClickHouse DDL setting '{field}'. Pass {argument} or add it under the connection's ddl_defaults."
    )


def validate_distributed_template(template: str) -> None:
    allowed = {"cluster", "database", "shard_table", "sharding_key"}
    try:
        parsed_fields = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise SqlConfigError(
            "ClickHouse distributed.engine_template has malformed braces."
        ) from exc
    for _literal, field, spec, conversion in parsed_fields:
        if field is None:
            continue
        if field not in allowed or spec or conversion:
            raise SqlConfigError(
                f"ClickHouse distributed.engine_template has unsupported placeholder {{{field}}}."
            )
    sample = template.format(
        cluster="'cluster'", database="'database'", shard_table="'table'", sharding_key="rand()"
    )
    try:
        tree = sqlglot.parse_one(sample, read="clickhouse")
    except Exception as exc:
        raise SqlConfigError(
            "ClickHouse distributed.engine_template must be a valid SQL expression."
        ) from exc
    if (
        not isinstance(tree, exp.Anonymous)
        or tree.name.lower() != "distributed"
        or len(tree.expressions) < 3
    ):
        raise SqlConfigError(
            "ClickHouse distributed.engine_template must be a Distributed(...) engine with at least three arguments."
        )


def build_policy_create_sqls(
    *,
    table_name: str,
    joined_columns: str,
    partition_by: Any,
    order_by: Any,
    policy: ClickHouseCreationPolicy,
    ch_only_shard: bool,
    ch_replace_table: bool,
    if_not_exists: bool = True,
) -> list[str]:
    statement = (
        "CREATE OR REPLACE TABLE"
        if ch_replace_table
        else "CREATE TABLE IF NOT EXISTS"
        if if_not_exists
        else "CREATE TABLE"
    )
    shard_name = (
        build_ch_shard_table_name(table_name)
        if policy.create_distributed_pair and not ch_only_shard
        else table_name
    )
    shard_sql = _physical_sql(
        statement,
        shard_name,
        joined_columns,
        partition_by,
        order_by,
        policy.shard_engine,
        policy.shard_on_cluster,
    )
    commands = [shard_sql]
    if policy.shard_on_cluster is not None:
        commands.append(
            _physical_sql(
                "CREATE TABLE IF NOT EXISTS",
                shard_name,
                joined_columns,
                partition_by,
                order_by,
                policy.shard_engine,
                None,
            )
        )
    if not policy.create_distributed_pair or ch_only_shard:
        return commands
    engine = _render_distributed_engine(policy, shard_name)
    distributed_sql = _create_sql(
        statement, table_name, joined_columns, engine, policy.distributed_on_cluster
    )
    commands.append(distributed_sql)
    if policy.distributed_on_cluster is not None:
        commands.append(
            _create_sql("CREATE TABLE IF NOT EXISTS", table_name, joined_columns, engine, None)
        )
    return commands


def build_policy_create_as_sqls(
    *,
    table_name: str,
    source_sql: str,
    partition_by: Any,
    order_by: Any,
    policy: ClickHouseCreationPolicy,
    ch_only_shard: bool,
    if_not_exists: bool,
) -> tuple[list[str], bool]:
    """Build ClickHouse CTAS commands and report whether a final insert is needed."""
    statement = "CREATE TABLE IF NOT EXISTS" if if_not_exists else "CREATE TABLE"
    create_pair = policy.create_distributed_pair and not ch_only_shard
    shard_name = build_ch_shard_table_name(table_name) if create_pair else table_name
    empty = "EMPTY " if create_pair else ""
    shard_sql = _physical_as_sql(
        statement,
        shard_name,
        source_sql,
        partition_by,
        order_by,
        policy.shard_engine,
        policy.shard_on_cluster,
        empty=empty,
    )
    commands = [shard_sql]
    if policy.shard_on_cluster is not None:
        commands.append(
            _physical_as_sql(
                "CREATE TABLE IF NOT EXISTS",
                shard_name,
                source_sql,
                partition_by,
                order_by,
                policy.shard_engine,
                None,
                empty="EMPTY ",
            )
        )
    if not create_pair:
        return commands, False

    engine = _render_distributed_engine(policy, shard_name)
    commands.append(
        _create_as_sql(
            statement,
            table_name,
            source_sql,
            engine,
            policy.distributed_on_cluster,
        )
    )
    if policy.distributed_on_cluster is not None:
        commands.append(
            _create_as_sql(
                "CREATE TABLE IF NOT EXISTS",
                table_name,
                source_sql,
                engine,
                None,
            )
        )
    return commands, True


def _physical_sql(
    statement: str,
    table: str,
    columns: str,
    partition_by: Any,
    order_by: Any,
    engine: str,
    cluster: str | None,
) -> str:
    cluster_sql = "" if cluster is None else f"\nON CLUSTER {_format_ch_cluster_name(cluster)}"
    sql = f"{statement} {table}{cluster_sql}\n({columns})\nENGINE = {engine}\n{_build_partition_by_sql(partition_by)}{_build_order_by_sql(order_by)}"
    return add_explicit_ch_uuid_to_local_replicated_create(sql)


def _physical_as_sql(
    statement: str,
    table: str,
    source_sql: str,
    partition_by: Any,
    order_by: Any,
    engine: str,
    cluster: str | None,
    *,
    empty: str,
) -> str:
    cluster_sql = "" if cluster is None else f"\nON CLUSTER {_format_ch_cluster_name(cluster)}"
    sql = (
        f"{statement} {table}{cluster_sql}\n"
        f"ENGINE = {engine}\n"
        f"{_build_partition_by_sql(partition_by)}{_build_order_by_sql(order_by)}\n"
        f"{empty}AS {source_sql}"
    )
    return add_explicit_ch_uuid_to_local_replicated_create(sql)


def _create_sql(statement: str, table: str, columns: str, engine: str, cluster: str | None) -> str:
    cluster_sql = "" if cluster is None else f"\nON CLUSTER {_format_ch_cluster_name(cluster)}"
    return f"{statement} {table}{cluster_sql}\n({columns})\nENGINE = {engine}"


def _create_as_sql(
    statement: str,
    table: str,
    source_sql: str,
    engine: str,
    cluster: str | None,
) -> str:
    cluster_sql = "" if cluster is None else f"\nON CLUSTER {_format_ch_cluster_name(cluster)}"
    return f"{statement} {table}{cluster_sql}\nENGINE = {engine}\nEMPTY AS {source_sql}"


def _render_distributed_engine(policy: ClickHouseCreationPolicy, shard_name: str) -> str:
    assert policy.distributed_engine_template is not None
    database, relation = split_ch_table_name_for_distributed_engine(shard_name)
    template = policy.distributed_engine_template.format(
        cluster=_sql_string_literal(policy.distributed_cluster or ""),
        database=database,
        shard_table=_sql_string_literal(relation),
        sharding_key=policy.sharding_key or "rand()",
    )
    sqlglot.parse_one(template, read="clickhouse")
    args = extract_clickhouse_function_args(template, "Distributed")
    if args is None or len(args) < 3:
        raise SqlConfigError(
            "ClickHouse distributed.engine_template must be a Distributed(...) engine with at least three arguments."
        )
    if policy.distributed_cluster is not None:
        args[0] = _sql_string_literal(policy.distributed_cluster)
    args[1] = database
    args[2] = _sql_string_literal(relation)
    if policy.sharding_key is not None:
        if len(args) >= 4:
            args[3] = policy.sharding_key
        else:
            args.append(policy.sharding_key)
    rendered_args = ",\n".join(f"    {argument}" for argument in args)
    return f"Distributed(\n{rendered_args}\n)"
