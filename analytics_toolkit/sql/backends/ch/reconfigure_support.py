from __future__ import annotations

# ruff: noqa: EM101, EM102, PLR2004, S101, S608, TRY003
from typing import TYPE_CHECKING, Any

from sqlglot import exp

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.ddl.identifiers import (
    _add_table_identifier_suffix,
    _identifier_name,
    _parse_table_name,
)
from analytics_toolkit.sql.execution.labels import apply_query_label
from analytics_toolkit.sql.execution.plans import SqlOperationMetadata, SqlPlan

from .ddl import _sql_string_literal, split_ch_table_name_for_distributed_engine
from .reconfigure_ddl import (
    comparable_create_sql,
    distributed_table_parts,
    normalize_setting_name,
    parse_create_table,
    setting_value_sql,
)
from .reconfigure_execution import cluster_clause
from .wait import _resolve_ch_cluster_name_for_wait

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .reconfigure_models import ChReconfigureOptions


def cutover_sqls(
    original: str,
    replacement: str,
    backup: str,
    *,
    ch_cluster: str | None,
    use_exchange: bool,
) -> tuple[list[str], list[str], str]:
    clause = cluster_clause(ch_cluster)
    if use_exchange:
        exchange = f"EXCHANGE TABLES {original} AND {replacement}{clause}"
        return [exchange], [exchange], replacement
    cutover = [
        f"RENAME TABLE {original} TO {backup}{clause}",
        f"RENAME TABLE {replacement} TO {original}{clause}",
    ]
    rollback = [
        f"RENAME TABLE {original} TO {replacement}{clause}",
        f"RENAME TABLE {backup} TO {original}{clause}",
    ]
    return cutover, rollback, backup


def drop_table_sql(table: str, ch_cluster: str | None) -> str:
    return f"DROP TABLE IF EXISTS {table}{cluster_clause(ch_cluster)}"


def build_setting_alter_sqls(
    table: str,
    settings: Mapping[str, str | int | float | bool | None],
    *,
    ch_cluster: str | None,
) -> list[str]:
    modify: list[str] = []
    reset: list[str] = []
    for name, value in settings.items():
        normalized_name = normalize_setting_name(name)
        if value is None:
            reset.append(normalized_name)
        else:
            modify.append(f"{normalized_name}={setting_value_sql(value)}")
    prefix = f"ALTER TABLE {table}{cluster_clause(ch_cluster)}"
    sqls: list[str] = []
    if modify:
        sqls.append(f"{prefix} MODIFY SETTING {', '.join(modify)}")
    if reset:
        sqls.append(f"{prefix} RESET SETTING {', '.join(reset)}")
    return sqls


def show_create_table(connection: Any, table: str) -> str:
    result = connection.query(f"SHOW CREATE TABLE {table}")
    rows = getattr(result, "result_rows", None) or []
    if not rows or not rows[0]:
        raise InvalidSqlInputError(f"ClickHouse table {table} does not exist.")
    return str(rows[0][0]).strip().rstrip(";")


def show_create_table_on_cluster(connection: Any, table: str, cluster: str) -> str:
    database, relation = distributed_table_parts(table)
    result = connection.query(
        "SELECT create_table_query FROM clusterAllReplicas("
        f"{_sql_string_literal(cluster)}, system, tables) "
        f"WHERE database = {_sql_string_literal(database)} "
        f"AND name = {_sql_string_literal(relation)}"
    )
    rows = [str(row[0]).strip().rstrip(";") for row in (result.result_rows or []) if row]
    if not rows:
        raise InvalidSqlInputError(
            f"ClickHouse table {table} does not exist on cluster {cluster!r}."
        )
    comparable = {comparable_create_sql(parse_create_table(ddl, table)) for ddl in rows}
    if len(comparable) != 1:
        raise InvalidSqlInputError(
            f"ClickHouse table {table} has inconsistent DDL on cluster {cluster!r}."
        )
    return rows[0]


def count_rows(connection: Any, table: str) -> int:
    result = connection.query(f"SELECT count() FROM {table}")
    rows = getattr(result, "result_rows", None) or []
    return int(rows[0][0]) if rows and rows[0] else 0


def count_rows_on_cluster(
    connection: Any,
    table: str,
    cluster: str,
    *,
    query_label: str | None = None,
) -> int:
    database_expression, relation = split_ch_table_name_for_distributed_engine(table)
    sql = (
        "SELECT count(*) FROM cluster("
        f"{_sql_string_literal(cluster)}, "
        f"{database_expression}, {_sql_string_literal(relation)})"
    )
    result = connection.query(apply_query_label(sql, query_label))
    rows = getattr(result, "result_rows", None) or []
    return int(rows[0][0]) if rows and rows[0] else 0


def count_final_rows(
    connection: Any,
    table: str,
    cluster: str | None,
) -> int:
    return (
        count_rows(connection, table)
        if cluster is None
        else count_rows_on_cluster(connection, table, cluster)
    )


def table_database(connection: Any, create: exp.Create) -> str:
    schema = create.this
    assert isinstance(schema, exp.Schema)
    table = schema.this
    assert isinstance(table, exp.Table)
    database = table.args.get("db")
    if isinstance(database, exp.Identifier):
        return _identifier_name(database)
    return str(query_scalar(connection, "SELECT currentDatabase()"))


def database_engine(connection: Any, database: str) -> str:
    sql = f"SELECT engine FROM system.databases WHERE name = {_sql_string_literal(database)}"
    return str(query_scalar(connection, sql))


def query_scalar(connection: Any, sql: str) -> Any:
    result = connection.query(sql)
    rows = getattr(result, "result_rows", None) or []
    if not rows or not rows[0]:
        raise InvalidSqlInputError(f"ClickHouse metadata query returned no rows: {sql}")
    return rows[0][0]


def resolve_optional_cluster(
    connection: Any,
    cluster: str | None,
    option_name: str = "ch_cluster",
) -> str | None:
    if cluster is None:
        return None
    return _resolve_ch_cluster_name_for_wait(
        connection,
        non_empty_string(cluster, option_name),
    )


def is_cross_cluster(connection: Any, source: str | None, target: str | None) -> bool:
    if source is None or target is None or source == target:
        return False
    source_hosts = cluster_hosts(connection, source)
    target_hosts = cluster_hosts(connection, target)
    if not source_hosts:
        raise InvalidSqlInputError(f"ClickHouse source cluster {source!r} has no hosts.")
    if not target_hosts:
        raise InvalidSqlInputError(f"ClickHouse target cluster {target!r} has no hosts.")
    if source_hosts == target_hosts:
        return False
    if source_hosts & target_hosts:
        raise InvalidSqlInputError("Partially overlapping ClickHouse clusters are not supported.")
    return True


def cluster_hosts(connection: Any, cluster: str) -> set[tuple[str, str, int]]:
    result = connection.query(
        "SELECT host_name, host_address, port FROM system.clusters WHERE cluster = "
        f"{_sql_string_literal(cluster)}"
    )
    return {
        (str(row[0]), str(row[1]), int(row[2]))
        for row in (getattr(result, "result_rows", None) or [])
        if len(row) >= 3
    }


def table_exists_on_cluster(connection: Any, table_name: str, cluster: str | None) -> bool:
    if cluster is None:
        return False
    database, relation = distributed_table_parts(table_name)
    result = connection.query(
        "SELECT count() FROM clusterAllReplicas("
        f"{_sql_string_literal(cluster)}, system, tables) "
        f"WHERE database = {_sql_string_literal(database)} "
        f"AND name = {_sql_string_literal(relation)}"
    )
    rows = getattr(result, "result_rows", None) or []
    return bool(rows and rows[0] and int(rows[0][0]))


def normalize_table_name(table: str) -> str:
    normalized = non_empty_string(str(table), "table")
    _parse_table_name(normalized, "clickhouse")
    return normalized


def non_empty_string(value: str, option_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise InvalidSqlInputError(f"{option_name} must not be empty.")
    return normalized


def supports_exchange(database_engine: str) -> bool:
    return database_engine.strip().lower() in {"atomic", "shared"}


def suffixed_table(table: str, suffix: str) -> str:
    return _add_table_identifier_suffix(table, suffix, "clickhouse")


def qualify_like(table: str, relation: str) -> str:
    parsed = _parse_table_name(table, "clickhouse")
    database = parsed.args.get("db")
    relation_identifier = exp.to_identifier(relation)
    if isinstance(database, exp.Identifier):
        return exp.Table(this=relation_identifier, db=database.copy()).sql(dialect="clickhouse")
    return relation_identifier.sql(dialect="clickhouse")


def qualify_with_database(table: str, database: str) -> str:
    parsed = _parse_table_name(table, "clickhouse")
    if isinstance(parsed.args.get("db"), exp.Identifier):
        return table
    parsed.set("db", exp.to_identifier(database))
    return parsed.sql(dialect="clickhouse")


def unquoted_table_name(table: str) -> tuple[str | None, str]:
    parsed = _parse_table_name(table, "clickhouse")
    database = parsed.args.get("db")
    return (
        _identifier_name(database) if isinstance(database, exp.Identifier) else None,
        _identifier_name(parsed.this),
    )


def new_plan(options: ChReconfigureOptions, table: str, strategy: str) -> SqlPlan:
    return SqlPlan(
        operation="ch_reconfigure_table",
        target_alias=options.connection_key,
        target_backend="ch",
        target_table=table,
        options={
            "strategy": strategy,
            "ch_engine": options.ch_engine,
            "partition_by": options.partition_by,
            "order_by": options.order_by,
            "ch_sharding_key": options.ch_sharding_key,
            "ch_distributed_table": options.ch_distributed_table,
            "ch_distributed_engine_template": options.ch_distributed_engine_template,
            "ch_distributed_cluster": options.ch_distributed_cluster,
            "ch_shard_on_cluster": options.ch_shard_on_cluster,
            "ch_distributed_on_cluster": options.ch_distributed_on_cluster,
            "ch_settings": dict(options.ch_settings or {}),
            "reset_partition_by": options.reset_partition_by,
            "reset_order_by": options.reset_order_by,
            "to_defaults": options.to_defaults,
            "validate_row_count": options.validate_row_count,
            "ch_ddl_wait_policy": options.ch_ddl_wait_policy,
        },
        metadata=SqlOperationMetadata(query_label=options.query_label),
    )


def add_sqls(
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


__all__ = [
    "add_sqls",
    "build_setting_alter_sqls",
    "count_final_rows",
    "count_rows",
    "cutover_sqls",
    "database_engine",
    "drop_table_sql",
    "is_cross_cluster",
    "new_plan",
    "normalize_table_name",
    "qualify_like",
    "qualify_with_database",
    "resolve_optional_cluster",
    "show_create_table",
    "show_create_table_on_cluster",
    "suffixed_table",
    "supports_exchange",
    "table_database",
    "table_exists_on_cluster",
    "unquoted_table_name",
]
