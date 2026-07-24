from __future__ import annotations

# ruff: noqa: E501, EM101, EM102, I001, PLR0913, PLR2004, S101, TRY003

import string
import warnings
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import expressions as exp

from analytics_toolkit.sql.backends.ch.ddl import (
    _build_order_by_sql,
    _build_partition_by_sql,
    _format_ch_cluster_name,
    _sql_string_literal,
    build_ch_shard_table_name,
    split_ch_table_name_for_distributed_engine,
)
from analytics_toolkit.sql.connection.ddl_defaults import (
    ClickHouseScopeDefaults,
    MISSING_DDL_VALUE,
)
from analytics_toolkit.sql.connection.errors import SqlConfigError


@dataclass(frozen=True)
class ClickHouseCreationPolicy:
    create_distributed_pair: bool
    shard_engine: str
    shard_on_cluster: str | None
    distributed_engine_template: str | None
    distributed_cluster: str | None
    distributed_on_cluster: str | None
    sharding_key: str | None


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
) -> ClickHouseCreationPolicy:
    if ch_cluster is not None and warn_ch_cluster:
        warnings.warn(
            "ch_cluster is deprecated; use ch_shard_on_cluster, "
            "ch_distributed_on_cluster, and ch_distributed_cluster.",
            DeprecationWarning,
            stacklevel=3,
        )
    pair = (
        ch_distributed_table if ch_distributed_table is not None else scope.create_distributed_pair
    )
    if pair is MISSING_DDL_VALUE:
        _missing("create_distributed_pair", "ch_distributed_table")
    if ch_only_shard:
        pair = False
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
    )


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
) -> list[str]:
    statement = "CREATE OR REPLACE TABLE" if ch_replace_table else "CREATE TABLE IF NOT EXISTS"
    shard_name = (
        build_ch_shard_table_name(table_name)
        if (policy.create_distributed_pair or ch_only_shard)
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
    return f"{statement} {table}{cluster_sql}\n({columns})\nENGINE = {engine}\n{_build_partition_by_sql(partition_by)}{_build_order_by_sql(order_by)}"


def _create_sql(statement: str, table: str, columns: str, engine: str, cluster: str | None) -> str:
    cluster_sql = "" if cluster is None else f"\nON CLUSTER {_format_ch_cluster_name(cluster)}"
    return f"{statement} {table}{cluster_sql}\n({columns})\nENGINE = {engine}"


def _render_distributed_engine(policy: ClickHouseCreationPolicy, shard_name: str) -> str:
    assert policy.distributed_engine_template is not None
    database, relation = split_ch_table_name_for_distributed_engine(shard_name)
    template = policy.distributed_engine_template.format(
        cluster=_sql_string_literal(policy.distributed_cluster or ""),
        database=database,
        shard_table=_sql_string_literal(relation),
        sharding_key=policy.sharding_key or "rand()",
    )
    tree = sqlglot.parse_one(template, read="clickhouse")
    args = list(tree.expressions)
    if policy.distributed_cluster is not None:
        args[0] = exp.Literal.string(policy.distributed_cluster)
    args[1] = sqlglot.parse_one(database, read="clickhouse")
    args[2] = exp.Literal.string(relation)
    if policy.sharding_key is not None:
        shard_expr = sqlglot.parse_one(policy.sharding_key, read="clickhouse")
        if len(args) >= 4:
            args[3] = shard_expr
        else:
            args.append(shard_expr)
    tree.set("expressions", args)
    rendered_args = ",\n".join(f"    {argument.sql(dialect='clickhouse')}" for argument in args)
    return f"Distributed(\n{rendered_args}\n)"
