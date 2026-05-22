from __future__ import annotations

import re
import time
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

import pandas as pd
from sqlglot import exp, parse_one

from ..backend_adapters import get_backend_adapter
from ..capabilities import get_backend_capability
from ..connection.config import resolve_connection_backend
from ..connection.errors import UnsupportedConnectionTypeError
from ..labels import apply_query_label
from ..operation_runner import timed_public_sql_function, tracked_sql_operation
from ..plans import SqlOperationMetadata, SqlOperationResult, SqlPlan
from analytics_toolkit.general import time_print
from .models import CreateSqlTableOptions


@timed_public_sql_function
def create_sql_table(
    connection_type: str,
    connection: Any,
    table_name: str,
    batch: pd.DataFrame,
    column_types: Mapping[str, str] | None = None,
    gp_distributed_by_key: list[str] | None = None,
    ch_partition_by: Sequence[str] | str | None = None,
    ch_order_by: Sequence[str] | str | None = None,
    ch_engine: str = "ReplicatedMergeTree",
    ch_cluster: str = "{cluster}",
    ch_sharding_key: str = "rand()",
    ch_distributed_table: bool = False,
    ch_replace_table: bool = False,
    dry_run: bool = False,
    return_sql: bool = False,
    query_label: str | None = None,
    return_metadata: bool = False,
    table_schema: Mapping[str, str] | None = None,
) -> SqlPlan | SqlOperationResult | None:
    backend = resolve_connection_backend(connection_type)
    options = CreateSqlTableOptions(
        connection_type=connection_type,
        backend=backend,
        connection=connection,
        table_name=table_name,
        batch=batch,
        column_types=column_types,
        table_schema=(
            _resolve_create_column_types(
                table_schema=table_schema,
                column_types=column_types,
                columns=batch.columns,
            )
            if table_schema is not None
            else None
        ),
        gp_distributed_by_key=gp_distributed_by_key,
        ch_partition_by=ch_partition_by,
        ch_order_by=ch_order_by,
        ch_engine=ch_engine,
        ch_cluster=ch_cluster,
        ch_sharding_key=ch_sharding_key,
        ch_distributed_table=ch_distributed_table,
        ch_replace_table=ch_replace_table,
        dry_run=dry_run,
        return_sql=return_sql,
        query_label=query_label,
        return_metadata=return_metadata,
    )
    time_print(f"Creating target table {table_name} on {connection_type}")
    create_sqls = build_create_table_sqls(
        options.backend,
        options.table_name,
        options.batch,
        column_types=options.column_types,
        table_schema=options.table_schema,
        gp_distributed_by_key=options.gp_distributed_by_key,
        ch_partition_by=options.ch_partition_by,
        ch_order_by=options.ch_order_by,
        ch_engine=options.ch_engine,
        ch_cluster=options.ch_cluster,
        ch_sharding_key=options.ch_sharding_key,
        ch_distributed_table=options.ch_distributed_table,
        ch_replace_table=options.ch_replace_table,
        query_label=options.query_label,
    )
    expected_ch_column_types = (
        _build_expected_ch_column_types(
            options.batch,
            _resolve_create_column_types(
                table_schema=options.table_schema,
                column_types=options.column_types,
                columns=options.batch.columns,
            ),
        )
        if options.backend == "ch" and options.ch_distributed_table
        else None
    )
    metadata = SqlOperationMetadata(
        statement_count=len(create_sqls),
        query_label=options.query_label,
    )
    plan = SqlPlan(
        operation="create_table",
        target_alias=options.connection_type,
        target_backend=options.backend,
        target_table=options.table_name,
        metadata=metadata,
    )
    plan.extend(
        create_sqls,
        alias=options.connection_type,
        backend=options.backend,
        phase="create_table",
        target_table=options.table_name,
    )

    if options.dry_run or options.return_sql:
        return plan

    with tracked_sql_operation(
        metadata=metadata,
        operation_name="create_sql_table",
        alias=options.connection_type,
        backend=options.backend,
        phase="create_target",
        query_label=options.query_label,
        preview_sql=create_sqls[0] if create_sqls else None,
    ):
        get_backend_adapter(options.backend).execute_commands(options.connection, create_sqls)
        if options.backend == "ch":
            if options.ch_distributed_table:
                _wait_for_ch_distributed_table_pair(
                    options.connection,
                    options.table_name,
                    ch_cluster=options.ch_cluster,
                    expected_column_types=expected_ch_column_types,
                )
    if options.return_metadata:
        return SqlOperationResult(rows=None, metadata=metadata, plan=plan)
    return None


@timed_public_sql_function
def build_create_table_sql(
    connection_type: str,
    table_name: str,
    batch: pd.DataFrame,
    column_types: Mapping[str, str] | None = None,
    gp_distributed_by_key: list[str] | None = None,
    ch_partition_by: Sequence[str] | str | None = None,
    ch_order_by: Sequence[str] | str | None = None,
    ch_engine: str = "ReplicatedMergeTree",
    ch_cluster: str = "{cluster}",
    ch_sharding_key: str = "rand()",
    ch_distributed_table: bool = False,
    ch_replace_table: bool = False,
    query_label: str | None = None,
    table_schema: Mapping[str, str] | None = None,
) -> str:
    return ";\n".join(
        build_create_table_sqls(
            connection_type,
            table_name,
            batch,
            column_types=column_types,
            table_schema=table_schema,
            gp_distributed_by_key=gp_distributed_by_key,
            ch_partition_by=ch_partition_by,
            ch_order_by=ch_order_by,
            ch_engine=ch_engine,
            ch_cluster=ch_cluster,
            ch_sharding_key=ch_sharding_key,
            ch_distributed_table=ch_distributed_table,
            ch_replace_table=ch_replace_table,
            query_label=query_label,
        )
    )


@timed_public_sql_function
def build_create_table_sqls(
    connection_type: str,
    table_name: str,
    batch: pd.DataFrame,
    column_types: Mapping[str, str] | None = None,
    gp_distributed_by_key: list[str] | None = None,
    ch_partition_by: Sequence[str] | str | None = None,
    ch_order_by: Sequence[str] | str | None = None,
    ch_engine: str = "ReplicatedMergeTree",
    ch_cluster: str = "{cluster}",
    ch_sharding_key: str = "rand()",
    ch_distributed_table: bool = False,
    ch_replace_table: bool = False,
    query_label: str | None = None,
    table_schema: Mapping[str, str] | None = None,
) -> list[str]:
    backend = resolve_connection_backend(connection_type)
    resolved_column_types = _resolve_create_column_types(
        table_schema=table_schema,
        column_types=column_types,
        columns=batch.columns,
    )
    joined_columns = _build_column_definitions(
        backend,
        batch,
        resolved_column_types,
    )
    return _apply_query_label_to_sqls(
        _build_backend_create_table_sqls(
            backend=backend,
            table_name=table_name,
            joined_columns=joined_columns,
            gp_distributed_by_key=gp_distributed_by_key,
            ch_partition_by=ch_partition_by,
            ch_order_by=ch_order_by,
            ch_engine=ch_engine,
            ch_cluster=ch_cluster,
            ch_sharding_key=ch_sharding_key,
            ch_distributed_table=ch_distributed_table,
            ch_replace_table=ch_replace_table,
        ),
        query_label,
    )


def _build_column_definitions(
    backend: str,
    batch: pd.DataFrame,
    column_types: Mapping[str, str] | None,
) -> str:
    column_defs = []
    for column_name in batch.columns:
        db_type = (
            _explicit_column_type(column_types, column_name)
            if column_types is not None
            else _infer_backend_type(backend, batch[column_name])
        )
        column_defs.append(f"{quote_identifier(column_name, backend)} {db_type}")
    return ", ".join(column_defs)


def _build_expected_ch_column_types(
    batch: pd.DataFrame,
    column_types: Mapping[str, str] | None,
) -> dict[str, str]:
    expected: dict[str, str] = {}
    for column_name in batch.columns:
        column_key = str(column_name)
        expected[column_key] = (
            _explicit_column_type(column_types, column_key)
            if column_types is not None
            else _infer_ch_type(batch[column_name])
        )
    return expected


def build_table_schema_column_definitions(
    connection_type: str,
    table_schema: Mapping[str, str],
    columns: Sequence[str] | None = None,
) -> str:
    backend = resolve_connection_backend(connection_type)
    normalized_schema = normalize_table_schema(table_schema, columns=columns)
    schema_batch = pd.DataFrame(columns=list(normalized_schema))
    return _build_column_definitions(backend, schema_batch, normalized_schema)


def normalize_table_schema(
    table_schema: Mapping[str, str] | None,
    columns: Sequence[str] | None = None,
    *,
    option_name: str = "table_schema",
) -> dict[str, str] | None:
    if table_schema is None:
        return None
    if not isinstance(table_schema, Mapping):
        raise TypeError(
            f"{option_name} must be a mapping of column names to SQL types."
        )

    normalized_schema: dict[str, str] = {}
    for column_name, db_type in table_schema.items():
        if not isinstance(column_name, str) or not column_name.strip():
            raise ValueError(f"{option_name} column names must be non-empty strings.")
        if not isinstance(db_type, str):
            raise TypeError(
                f"SQL type for column {column_name!r} in {option_name} "
                "must be a string."
            )
        normalized_type = db_type.strip()
        if not normalized_type:
            raise ValueError(f"SQL type for column {column_name!r} must not be empty.")
        normalized_schema[column_name] = normalized_type

    if not normalized_schema:
        raise ValueError(f"{option_name} must not be empty when provided.")
    if columns is None:
        return normalized_schema

    return validate_table_schema_columns(
        normalized_schema,
        columns,
        option_name=option_name,
    )


def validate_table_schema_columns(
    table_schema: Mapping[str, str],
    columns: Sequence[str],
    *,
    option_name: str = "table_schema",
) -> dict[str, str]:
    column_names = [str(column) for column in columns]
    column_name_set = set(column_names)
    missing_columns = [
        column_name for column_name in column_names if column_name not in table_schema
    ]
    extra_columns = [
        column_name for column_name in table_schema if column_name not in column_name_set
    ]

    if missing_columns:
        raise ValueError(
            f"{option_name} is missing SQL type for column(s): "
            + ", ".join(missing_columns)
        )
    if extra_columns:
        raise ValueError(
            f"{option_name} contains column(s) not present in data: "
            + ", ".join(extra_columns)
        )
    return {column_name: table_schema[column_name] for column_name in column_names}


def _resolve_create_column_types(
    *,
    table_schema: Mapping[str, str] | None,
    column_types: Mapping[str, str] | None,
    columns: Sequence[str],
) -> Mapping[str, str] | None:
    if table_schema is None:
        return column_types

    normalized_schema = normalize_table_schema(table_schema, columns=columns)
    if column_types is None:
        return normalized_schema

    normalized_column_types = _normalize_column_types_for_columns(
        column_types,
        columns,
    )
    if normalized_schema != normalized_column_types:
        raise ValueError(
            "table_schema and column_types must define the same SQL types "
            "when both are provided."
        )
    return normalized_schema


def _normalize_column_types_for_columns(
    column_types: Mapping[str, str],
    columns: Sequence[str],
) -> dict[str, str]:
    if not isinstance(column_types, Mapping):
        raise TypeError("column_types must be a mapping of column names to SQL types.")
    return {
        str(column_name): _explicit_column_type(column_types, str(column_name))
        for column_name in columns
    }


def _infer_backend_type(backend: str, series: pd.Series) -> str:
    try:
        infer_type = _COLUMN_TYPE_INFERERS[backend]
    except KeyError as exc:
        raise UnsupportedConnectionTypeError(
            "Unsupported connection type. Expected one of: 'trino', 'gp', 'ch'."
        ) from exc
    return infer_type(series)


def _build_backend_create_table_sqls(
    *,
    backend: str,
    table_name: str,
    joined_columns: str,
    gp_distributed_by_key: list[str] | None,
    ch_partition_by: Sequence[str] | str | None,
    ch_order_by: Sequence[str] | str | None,
    ch_engine: str,
    ch_cluster: str,
    ch_sharding_key: str,
    ch_distributed_table: bool,
    ch_replace_table: bool,
) -> list[str]:
    try:
        build_sqls = _CREATE_TABLE_SQL_BUILDERS[backend]
    except KeyError as exc:
        raise UnsupportedConnectionTypeError(
            "Unsupported connection type. Expected one of: 'trino', 'gp', 'ch'."
        ) from exc
    return build_sqls(
        table_name=table_name,
        joined_columns=joined_columns,
        gp_distributed_by_key=gp_distributed_by_key,
        ch_partition_by=ch_partition_by,
        ch_order_by=ch_order_by,
        ch_engine=ch_engine,
        ch_cluster=ch_cluster,
        ch_sharding_key=ch_sharding_key,
        ch_distributed_table=ch_distributed_table,
        ch_replace_table=ch_replace_table,
    )


def _build_gp_create_table_sqls(
    *,
    table_name: str,
    joined_columns: str,
    gp_distributed_by_key: list[str] | None,
    **_: object,
) -> list[str]:
    storage_sql = (
        "WITH (appendonly=true,\n"
        "        blocksize=32768,\n"
        "        compresstype=zstd,\n"
        "        compresslevel=4,\n"
        "        orientation=column)"
    )
    if gp_distributed_by_key:
        distribution_sql = (
            f"DISTRIBUTED BY ({column_list_sql(gp_distributed_by_key, 'gp')})"
        )
    else:
        distribution_sql = "DISTRIBUTED RANDOMLY"
    return [
        f"CREATE TABLE {table_name} ({joined_columns}) "
        f"{storage_sql} {distribution_sql}"
    ]


def _build_trino_create_table_sqls(
    *,
    table_name: str,
    joined_columns: str,
    **_: object,
) -> list[str]:
    return [
        f"CREATE TABLE {table_name} ({joined_columns}) "
        "WITH (format = 'PARQUET', object_store_layout_enabled = true)"
    ]


def _build_ch_create_table_sqls(
    *,
    table_name: str,
    joined_columns: str,
    ch_partition_by: Sequence[str] | str | None,
    ch_order_by: Sequence[str] | str | None,
    ch_engine: str,
    ch_cluster: str,
    ch_sharding_key: str,
    ch_distributed_table: bool,
    ch_replace_table: bool,
    **_: object,
) -> list[str]:
    if ch_distributed_table:
        return build_ch_distributed_create_table_sqls(
            table_name=table_name,
            joined_columns=joined_columns,
            ch_partition_by=ch_partition_by,
            ch_order_by=ch_order_by,
            ch_engine=ch_engine,
            ch_cluster=ch_cluster,
            ch_sharding_key=ch_sharding_key,
            ch_replace_table=ch_replace_table,
        )
    return [
        f"CREATE TABLE {table_name} ({joined_columns}) "
        "ENGINE = MergeTree ORDER BY tuple()"
    ]


def _apply_query_label_to_sqls(sqls: list[str], query_label: str | None) -> list[str]:
    return [apply_query_label(sql, query_label) for sql in sqls]


def _explicit_column_type(
    column_types: Mapping[str, str],
    column_name: str,
) -> str:
    try:
        db_type = column_types[column_name]
    except KeyError as exc:
        raise ValueError(f"Missing explicit SQL type for column {column_name!r}.") from exc
    normalized = db_type.strip()
    if not normalized:
        raise ValueError(f"SQL type for column {column_name!r} must not be empty.")
    return normalized


def build_ch_distributed_create_table_sqls(
    table_name: str,
    joined_columns: str,
    ch_partition_by: Sequence[str] | str | None = None,
    ch_order_by: Sequence[str] | str | None = None,
    ch_engine: str = "ReplicatedMergeTree",
    ch_cluster: str = "{cluster}",
    ch_sharding_key: str = "rand()",
    ch_replace_table: bool = False,
) -> list[str]:
    shard_table = build_ch_shard_table_name(table_name)
    cluster_name = _normalize_non_empty_string(ch_cluster, "ch_cluster")
    engine = _normalize_non_empty_string(ch_engine, "ch_engine")
    sharding_key = _normalize_non_empty_string(ch_sharding_key, "ch_sharding_key")
    partition_sql = _build_ch_partition_by_sql(ch_partition_by)
    order_by_sql = _build_ch_order_by_sql(ch_order_by)
    database_name, shard_relation_name = split_ch_table_name_for_distributed_engine(
        shard_table
    )

    cluster_create_statement = (
        "CREATE OR REPLACE TABLE" if ch_replace_table else "CREATE TABLE IF NOT EXISTS"
    )

    shard_sql = (
        f"{cluster_create_statement} {shard_table}\n"
        f"ON CLUSTER {_format_ch_cluster_name(cluster_name)}\n"
        f"({joined_columns})\n"
        f"ENGINE = {engine}\n"
        f"{partition_sql}"
        f"{order_by_sql}"
    )
    local_shard_sql = (
        f"CREATE TABLE IF NOT EXISTS {shard_table}\n"
        f"({joined_columns})\n"
        f"ENGINE = {engine}\n"
        f"{partition_sql}"
        f"{order_by_sql}"
    )
    local_shard_sql = add_explicit_ch_uuid_to_local_replicated_create(
        local_shard_sql
    )
    distributed_sql = (
        f"{cluster_create_statement} {table_name}\n"
        f"ON CLUSTER {_format_ch_cluster_name(cluster_name)}\n"
        f"({joined_columns})\n"
        "ENGINE = Distributed(\n"
        f"    {_sql_string_literal(cluster_name)},\n"
        f"    {database_name},\n"
        f"    {_sql_string_literal(shard_relation_name)},\n"
        f"    {sharding_key}\n"
        ")"
    )
    local_distributed_sql = (
        f"CREATE TABLE IF NOT EXISTS {table_name}\n"
        f"({joined_columns})\n"
        "ENGINE = Distributed(\n"
        f"    {_sql_string_literal(cluster_name)},\n"
        f"    {database_name},\n"
        f"    {_sql_string_literal(shard_relation_name)},\n"
        f"    {sharding_key}\n"
        ")"
    )
    return [shard_sql, local_shard_sql, distributed_sql, local_distributed_sql]


def build_ch_shard_table_name(table_name: str) -> str:
    return _add_table_identifier_suffix(table_name, "_shard", "clickhouse")


def split_ch_table_name_for_distributed_engine(table_name: str) -> tuple[str, str]:
    table = _parse_table_name(table_name, "clickhouse")
    relation_name = _identifier_name(table.this)
    database = table.args.get("db")
    if database is None:
        return "currentDatabase()", relation_name
    return _sql_string_literal(_identifier_name(database)), relation_name


def column_list_sql(columns: Sequence[str], connection_type: str) -> str:
    backend = resolve_connection_backend(connection_type)
    return ", ".join(
        quote_identifier(column_name, backend) for column_name in columns
    )


def quote_identifier(identifier: str, connection_type: str) -> str:
    backend = resolve_connection_backend(connection_type)
    quote_char = get_backend_capability(backend).identifier_quote
    escaped = identifier.replace(quote_char, quote_char * 2)
    return f"{quote_char}{escaped}{quote_char}"


def _add_table_identifier_suffix(table_name: str, suffix: str, dialect: str) -> str:
    table = _parse_table_name(table_name, dialect)
    table_identifier = table.this
    suffixed_identifier = exp.to_identifier(
        f"{_identifier_name(table_identifier)}{suffix}",
        quoted=bool(table_identifier.args.get("quoted")),
    )
    suffixed_table = table.copy()
    suffixed_table.set("this", suffixed_identifier)
    return suffixed_table.sql(dialect=dialect)


def _parse_table_name(table_name: str, dialect: str) -> exp.Table:
    table = parse_one(table_name, read=dialect, into=exp.Table)
    if not isinstance(table, exp.Table) or not isinstance(table.this, exp.Identifier):
        raise ValueError(f"Invalid table name: {table_name}")
    return table


def _identifier_name(identifier: exp.Expression) -> str:
    if not isinstance(identifier, exp.Identifier):
        raise ValueError(f"Invalid table identifier: {identifier}")
    return str(identifier.this)


def _build_ch_partition_by_sql(
    ch_partition_by: Sequence[str] | str | None,
) -> str:
    if ch_partition_by is None:
        return ""
    expression = _normalize_ch_expression(ch_partition_by, "ch_partition_by")
    return f"PARTITION BY {expression}\n"


def _build_ch_order_by_sql(ch_order_by: Sequence[str] | str | None) -> str:
    expression = (
        "tuple()"
        if ch_order_by is None
        else _normalize_ch_expression(ch_order_by, "ch_order_by")
    )
    return f"ORDER BY {expression}"


def _normalize_ch_expression(value: Sequence[str] | str, option_name: str) -> str:
    if isinstance(value, str):
        return _normalize_non_empty_string(value, option_name)

    columns = [_normalize_non_empty_string(column, option_name) for column in value]
    if not columns:
        raise ValueError(f"{option_name} must not be empty when provided.")
    if len(set(columns)) != len(columns):
        raise ValueError(f"{option_name} must not contain duplicate column names.")
    quoted_columns = [quote_identifier(column, "ch") for column in columns]
    if len(quoted_columns) == 1:
        return quoted_columns[0]
    return f"({', '.join(quoted_columns)})"


def _normalize_non_empty_string(value: str, option_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{option_name} must not be empty.")
    return normalized


def _sql_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _format_ch_cluster_name(cluster_name: str) -> str:
    normalized = cluster_name.strip()
    if not normalized:
        return normalized
    if normalized[0] in {"'", '"', "`"}:
        return normalized
    if _is_simple_identifier(normalized):
        return normalized
    return _sql_string_literal(normalized)


def _is_simple_identifier(identifier: str) -> bool:
    if not identifier:
        return False
    if not (identifier[0].isalpha() or identifier[0] == "_"):
        return False
    return all(char.isalnum() or char == "_" for char in identifier)


def _execute_ch_command(connection: Any, sql: str) -> None:
    get_backend_adapter("ch").execute_command(connection, sql)


def _wait_for_ch_table(
    connection: Any,
    table_name: str,
    timeout_seconds: int = 60,
    poll_interval_seconds: float = 1,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        result = connection.query(f"EXISTS TABLE {table_name}")
        if result.result_rows and result.result_rows[0][0]:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"ClickHouse table {table_name} was not visible after "
                f"{timeout_seconds} second(s)."
            )
        time.sleep(poll_interval_seconds)


def _wait_for_ch_distributed_table_pair(
    connection: Any,
    table_name: str,
    ch_cluster: str = "{cluster}",
    timeout_seconds: int = 300,
    poll_interval_seconds: float = 1,
    expected_column_types: Mapping[str, str] | None = None,
) -> None:
    shard_table = build_ch_shard_table_name(table_name)
    _wait_for_ch_table(
        connection,
        table_name,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    _wait_for_ch_table(
        connection,
        shard_table,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    _wait_for_ch_table_on_cluster(
        connection,
        table_name,
        ch_cluster=ch_cluster,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    _wait_for_ch_table_on_cluster(
        connection,
        shard_table,
        ch_cluster=ch_cluster,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    if expected_column_types is not None:
        _wait_for_ch_table_schema_on_cluster(
            connection,
            table_name,
            expected_column_types=expected_column_types,
            ch_cluster=ch_cluster,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        _wait_for_ch_table_schema_on_cluster(
            connection,
            shard_table,
            expected_column_types=expected_column_types,
            ch_cluster=ch_cluster,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )


def _wait_for_ch_distributed_table_pair_absence(
    connection: Any,
    table_name: str,
    ch_cluster: str | None = "{cluster}",
    timeout_seconds: int = 300,
    poll_interval_seconds: float = 1,
) -> None:
    shard_table = build_ch_shard_table_name(table_name)
    if ch_cluster is None:
        _wait_for_ch_table_absence(
            connection,
            table_name,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        _wait_for_ch_table_absence(
            connection,
            shard_table,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        return

    _wait_for_ch_tables_absence_on_cluster(
        connection,
        [table_name, shard_table],
        ch_cluster=ch_cluster,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )


def _wait_for_ch_table_absence(
    connection: Any,
    table_name: str,
    timeout_seconds: int = 60,
    poll_interval_seconds: float = 1,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        result = connection.query(f"EXISTS TABLE {table_name}")
        rows = getattr(result, "result_rows", None) or []
        if not rows or not rows[0] or not rows[0][0]:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"ClickHouse table {table_name} was still visible after "
                f"{timeout_seconds} second(s)."
            )
        time.sleep(poll_interval_seconds)


def _wait_for_ch_table_absence_on_cluster(
    connection: Any,
    table_name: str,
    ch_cluster: str,
    timeout_seconds: int = 300,
    poll_interval_seconds: float = 1,
) -> None:
    _wait_for_ch_tables_absence_on_cluster(
        connection,
        [table_name],
        ch_cluster=ch_cluster,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )


def _wait_for_ch_tables_absence_on_cluster(
    connection: Any,
    table_names: Sequence[str],
    ch_cluster: str,
    timeout_seconds: int = 300,
    poll_interval_seconds: float = 1,
) -> None:
    normalized_table_names = [str(table_name).strip() for table_name in table_names]
    normalized_table_names = [
        table_name for table_name in normalized_table_names if table_name
    ]
    if not normalized_table_names:
        return

    cluster_name = _normalize_non_empty_string(ch_cluster, "ch_cluster")
    cluster_name = _resolve_ch_cluster_name_for_wait(connection, cluster_name)
    cluster_literal = _sql_string_literal(cluster_name)
    expected_hosts_sql = (
        "SELECT count()\n"
        f"FROM clusterAllReplicas({cluster_literal}, system, one)"
    )

    deadline = time.monotonic() + timeout_seconds
    remote_hosts = 0
    expected_hosts = 0
    visible_table_rows: list[tuple[Any, ...]] = []
    last_error: Exception | None = None
    while True:
        try:
            remote_hosts, expected_hosts = _query_ch_cluster_host_counts(
                connection,
                cluster_name=cluster_name,
                remote_hosts_sql=expected_hosts_sql,
            )
            visible_table_rows = _query_ch_cluster_table_rows(
                connection,
                table_names=normalized_table_names,
                ch_cluster=cluster_name,
            )
            if (
                expected_hosts > 0
                and remote_hosts >= expected_hosts
                and not visible_table_rows
            ):
                return
        except Exception as exc:
            last_error = exc

        if time.monotonic() >= deadline:
            table_summary = ", ".join(normalized_table_names)
            message = (
                f"ClickHouse table(s) {table_summary} were still visible on cluster "
                f"{cluster_name!r} after {timeout_seconds} second(s). Last "
                f"observed {len(visible_table_rows)} visible table row(s); reached "
                f"{remote_hosts}/{expected_hosts} expected host(s)."
            )
            leftovers = _format_ch_cluster_table_rows(visible_table_rows)
            if leftovers:
                message = f"{message} Leftover table(s): {leftovers}."
            message = (
                f"{message} To attempt direct local drops on affected cluster "
                "hosts, rerun with ch_retry_per_host_drops=True."
            )
            if last_error is not None:
                raise TimeoutError(message) from last_error
            raise TimeoutError(message)
        time.sleep(poll_interval_seconds)


def _wait_for_ch_table_on_cluster(
    connection: Any,
    table_name: str,
    ch_cluster: str,
    timeout_seconds: int = 300,
    poll_interval_seconds: float = 1,
) -> None:
    cluster_name = _normalize_non_empty_string(ch_cluster, "ch_cluster")
    cluster_name = _resolve_ch_cluster_name_for_wait(connection, cluster_name)
    database_expr, relation_name = split_ch_table_name_for_distributed_engine(
        table_name
    )
    cluster_literal = _sql_string_literal(cluster_name)
    expected_hosts_sql = (
        "SELECT count()\n"
        f"FROM clusterAllReplicas({cluster_literal}, system, one)"
    )
    visible_tables_sql = (
        "SELECT count()\n"
        f"FROM clusterAllReplicas({cluster_literal}, system, tables)\n"
        f"WHERE database = {database_expr}\n"
        f"  AND name = {_sql_string_literal(relation_name)}"
    )

    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while True:
        try:
            expected_hosts = _query_ch_expected_cluster_hosts(
                connection,
                cluster_name=cluster_name,
                remote_hosts_sql=expected_hosts_sql,
            )
            visible_tables = _query_ch_count(connection, visible_tables_sql)
            if expected_hosts > 0 and visible_tables >= expected_hosts:
                return
        except Exception as exc:
            last_error = exc

        if time.monotonic() >= deadline:
            message = (
                f"ClickHouse table {table_name} was not visible on every "
                f"host in cluster {cluster_name!r} after {timeout_seconds} "
                "second(s)."
            )
            if last_error is not None:
                raise TimeoutError(message) from last_error
            raise TimeoutError(message)
        time.sleep(poll_interval_seconds)


def _wait_for_ch_table_schema_on_cluster(
    connection: Any,
    table_name: str,
    *,
    expected_column_types: Mapping[str, str],
    ch_cluster: str,
    timeout_seconds: int = 300,
    poll_interval_seconds: float = 1,
) -> None:
    expected_column_types = normalize_table_schema(
        expected_column_types,
        option_name="expected_column_types",
    )
    if not expected_column_types:
        return

    cluster_name = _normalize_non_empty_string(ch_cluster, "ch_cluster")
    cluster_name = _resolve_ch_cluster_name_for_wait(connection, cluster_name)
    database_expr, relation_name = split_ch_table_name_for_distributed_engine(
        table_name
    )
    cluster_literal = _sql_string_literal(cluster_name)
    expected_hosts_sql = (
        "SELECT count()\n"
        f"FROM clusterAllReplicas({cluster_literal}, system, one)"
    )
    matching_columns_sql = (
        "SELECT count()\n"
        f"FROM clusterAllReplicas({cluster_literal}, system, columns)\n"
        f"WHERE database = {database_expr}\n"
        f"  AND table = {_sql_string_literal(relation_name)}\n"
        f"  AND ({_build_ch_expected_schema_condition(expected_column_types)})"
    )

    deadline = time.monotonic() + timeout_seconds
    expected_hosts = 0
    matching_columns = 0
    last_error: Exception | None = None
    while True:
        try:
            expected_hosts = _query_ch_expected_cluster_hosts(
                connection,
                cluster_name=cluster_name,
                remote_hosts_sql=expected_hosts_sql,
            )
            matching_columns = _query_ch_count(connection, matching_columns_sql)
            expected_column_rows = expected_hosts * len(expected_column_types)
            if expected_hosts > 0 and matching_columns >= expected_column_rows:
                return
        except Exception as exc:
            last_error = exc

        if time.monotonic() >= deadline:
            message = (
                f"ClickHouse table {table_name} schema did not match expected "
                f"columns on every host in cluster {cluster_name!r} after "
                f"{timeout_seconds} second(s). Last observed {matching_columns} "
                f"matching column row(s), expected "
                f"{expected_hosts * len(expected_column_types)}."
            )
            mismatch_details = _describe_ch_cluster_schema_mismatch(
                connection,
                table_name,
                expected_column_types=expected_column_types,
                ch_cluster=cluster_name,
                expected_hosts=expected_hosts,
            )
            if mismatch_details:
                message = f"{message} {mismatch_details}"
            if last_error is not None:
                raise TimeoutError(message) from last_error
            raise TimeoutError(message)
        time.sleep(poll_interval_seconds)


def _build_ch_expected_schema_condition(
    expected_column_types: Mapping[str, str],
) -> str:
    return " OR ".join(
        "("
        f"name = {_sql_string_literal(column_name)} "
        f"AND type = {_sql_string_literal(column_type)}"
        ")"
        for column_name, column_type in expected_column_types.items()
    )


def _describe_ch_cluster_schema_mismatch(
    connection: Any,
    table_name: str,
    *,
    expected_column_types: Mapping[str, str],
    ch_cluster: str,
    expected_hosts: int,
) -> str:
    database_expr, relation_name = split_ch_table_name_for_distributed_engine(
        table_name
    )
    cluster_literal = _sql_string_literal(ch_cluster)
    observed_sql = (
        "SELECT name, type, count()\n"
        f"FROM clusterAllReplicas({cluster_literal}, system, columns)\n"
        f"WHERE database = {database_expr}\n"
        f"  AND table = {_sql_string_literal(relation_name)}\n"
        "GROUP BY name, type\n"
        "ORDER BY name, type"
    )
    try:
        observed_rows = _query_ch_rows(connection, observed_sql)
    except Exception:
        return ""

    observed: dict[str, dict[str, int]] = {}
    for row in observed_rows:
        if len(row) < 3:
            continue
        column_name, column_type, count = row[:3]
        observed.setdefault(str(column_name), {})[str(column_type)] = int(count)

    details: list[str] = []
    for column_name, expected_type in expected_column_types.items():
        type_counts = observed.get(column_name, {})
        if type_counts.get(expected_type, 0) == expected_hosts:
            continue
        if not type_counts:
            observed_summary = "missing"
        else:
            observed_summary = ", ".join(
                f"{column_type} on {count} host(s)"
                for column_type, count in sorted(type_counts.items())
            )
        details.append(
            f"{column_name}: expected {expected_type} on {expected_hosts} "
            f"host(s), observed {observed_summary}"
        )

    extra_columns = sorted(set(observed) - set(expected_column_types))
    if extra_columns:
        details.append("extra column(s): " + ", ".join(extra_columns[:5]))

    if not details:
        return ""
    if len(details) > 6:
        details = details[:6] + ["..."]
    return "Schema mismatch details: " + "; ".join(details)


def _resolve_ch_cluster_name_for_wait(connection: Any, cluster_name: str) -> str:
    unquoted = _strip_sql_wrapping_quotes(cluster_name)
    macro_name = _extract_ch_macro_name(unquoted)
    if macro_name is None:
        return unquoted

    try:
        result = connection.query(f"SELECT getMacro({_sql_string_literal(macro_name)})")
    except Exception as exc:
        raise ValueError(
            f"Could not resolve ClickHouse cluster macro {unquoted!r}. "
            "Pass ch_cluster with the concrete cluster name, for example "
            "ch_cluster='core'."
        ) from exc

    rows = getattr(result, "result_rows", None) or []
    if rows and rows[0] and rows[0][0] is not None:
        resolved = str(rows[0][0]).strip()
        if resolved:
            return resolved

    raise ValueError(
        f"Could not resolve ClickHouse cluster macro {unquoted!r}. "
        "Pass ch_cluster with the concrete cluster name, for example "
        "ch_cluster='core'."
    )


def _strip_sql_wrapping_quotes(value: str) -> str:
    if len(value) < 2 or value[0] != value[-1] or value[0] not in {"'", '"', "`"}:
        return value
    inner = value[1:-1]
    if value[0] == "'":
        return inner.replace("''", "'")
    return inner


def _extract_ch_macro_name(value: str) -> str | None:
    match = re.fullmatch(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
    if match is None:
        return None
    return match.group(1)


def add_explicit_ch_uuid_to_local_replicated_create(sql: str) -> str:
    if re.search(r"\bON\s+CLUSTER\b", sql, flags=re.IGNORECASE):
        return sql
    if not re.search(r"\bENGINE\s*=\s*Replicated", sql, flags=re.IGNORECASE):
        return sql
    if re.search(r"\bUUID\s+'[^']+'", sql, flags=re.IGNORECASE):
        return sql

    table_header, separator, rest = sql.partition("\n(")
    if not separator:
        return sql
    return f"{table_header}\nUUID '{uuid.uuid4()}'{separator}{rest}"


def _query_ch_count(connection: Any, sql: str) -> int:
    result = connection.query(sql)
    rows = getattr(result, "result_rows", None) or []
    if not rows:
        return 0
    return int(rows[0][0])


def _query_ch_cluster_table_rows(
    connection: Any,
    *,
    table_names: Sequence[str],
    ch_cluster: str,
) -> list[tuple[Any, ...]]:
    conditions: list[str] = []
    for table_name in table_names:
        database_expr, relation_name = split_ch_table_name_for_distributed_engine(
            table_name
        )
        conditions.append(
            f"(database = {database_expr} "
            f"AND name = {_sql_string_literal(relation_name)})"
        )
    if not conditions:
        return []

    cluster_literal = _sql_string_literal(ch_cluster)
    sql = (
        "SELECT hostName(), database, name, engine\n"
        f"FROM clusterAllReplicas({cluster_literal}, system, tables)\n"
        f"WHERE {' OR '.join(conditions)}\n"
        "ORDER BY hostName(), database, name"
    )
    return [row for row in _query_ch_rows(connection, sql) if len(row) >= 4]


def _format_ch_cluster_table_rows(rows: Sequence[Sequence[Any]]) -> str:
    formatted: list[str] = []
    for row in rows:
        if len(row) < 4:
            continue
        host, database, table_name, engine = row[:4]
        formatted.append(f"{host}: {database}.{table_name} ({engine})")
    if not formatted:
        return ""
    if len(formatted) > 10:
        formatted = formatted[:10] + ["..."]
    return "; ".join(formatted)


def _query_ch_expected_cluster_hosts(
    connection: Any,
    *,
    cluster_name: str,
    remote_hosts_sql: str,
) -> int:
    _, expected_hosts = _query_ch_cluster_host_counts(
        connection,
        cluster_name=cluster_name,
        remote_hosts_sql=remote_hosts_sql,
    )
    return expected_hosts


def _query_ch_cluster_host_counts(
    connection: Any,
    *,
    cluster_name: str,
    remote_hosts_sql: str,
) -> tuple[int, int]:
    remote_hosts = _query_ch_count(connection, remote_hosts_sql)
    configured_hosts_sql = (
        "SELECT count()\n"
        "FROM system.clusters\n"
        f"WHERE cluster = {_sql_string_literal(cluster_name)}"
    )
    try:
        configured_hosts = _query_ch_count(connection, configured_hosts_sql)
    except Exception:
        configured_hosts = 0
    return remote_hosts, max(remote_hosts, configured_hosts)


def _query_ch_rows(connection: Any, sql: str) -> list[tuple[Any, ...]]:
    result = connection.query(sql)
    return list(getattr(result, "result_rows", None) or [])


def _infer_gp_type(series: pd.Series) -> str:
    return _infer_common_sql_type(series)


def _infer_trino_type(series: pd.Series) -> str:
    common_type = _infer_common_sql_type(series)
    if common_type == "DOUBLE PRECISION":
        return "DOUBLE"
    if common_type == "TEXT":
        return "VARCHAR"
    return common_type


def _infer_ch_type(series: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(series):
        base_type = "Bool"
    elif pd.api.types.is_integer_dtype(series):
        base_type = "Int64"
    elif pd.api.types.is_float_dtype(series):
        base_type = "Float64"
    elif pd.api.types.is_datetime64_any_dtype(series):
        base_type = "DateTime64(6)"
    else:
        non_null = series.dropna()
        if not non_null.empty and all(isinstance(value, Decimal) for value in non_null):
            base_type = "Float64"
        elif not non_null.empty and all(
            hasattr(value, "year")
            and hasattr(value, "month")
            and hasattr(value, "day")
            for value in non_null
        ):
            base_type = "Date"
        else:
            base_type = "String"

    if series.isna().any():
        return f"Nullable({base_type})"
    return base_type


def _infer_common_sql_type(series: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(series):
        return "BOOLEAN"
    if pd.api.types.is_integer_dtype(series):
        return "BIGINT"
    if pd.api.types.is_float_dtype(series):
        return "DOUBLE PRECISION"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "TIMESTAMP"

    non_null = series.dropna()
    if not non_null.empty and all(
        hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day")
        for value in non_null
    ):
        return "DATE"
    return "TEXT"


_COLUMN_TYPE_INFERERS = {
    "gp": _infer_gp_type,
    "trino": _infer_trino_type,
    "ch": _infer_ch_type,
}


_CREATE_TABLE_SQL_BUILDERS = {
    "gp": _build_gp_create_table_sqls,
    "trino": _build_trino_create_table_sqls,
    "ch": _build_ch_create_table_sqls,
}
