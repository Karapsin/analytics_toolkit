from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re
from typing import Any

from ...backend_adapters import ch_cluster_clause, get_backend_adapter
from ...connection.config import get_connection_config, resolve_connection_backend
from ...connection.errors import (
    InvalidSqlInputError,
    SqlOperationContext,
    UnsupportedConnectionTypeError,
    sql_preview,
)
from ...connection.get_sql_connection import get_sql_connection
from ...ddl.clickhouse import build_ch_shard_table_name
from ...execution.labels import apply_query_label
from ...execution.operation_runner import (
    run_connection_operation,
    timed_public_sql_function,
    tracked_sql_operation,
    validate_retry_options,
)
from ...execution.plans import SqlOperationMetadata, SqlOperationResult, SqlPlan
from .models import DropManyPartitionsOptions
from analytics_toolkit.general import time_print


@dataclass(frozen=True)
class _GpPartitionDefinition:
    name: str
    start: str | None = None
    end: str | None = None
    value: str | None = None

@dataclass(frozen=True)
class _GpCreateManyPartitionsOptions:
    connection_key: str
    backend: str
    target_table: str
    intervals: Sequence[Mapping[str, Any]] | None = None
    values: Sequence[str] | None = None
    days: Sequence[str] | None = None
    weeks: Sequence[str] | None = None
    months: Sequence[str] | None = None
    years: Sequence[str] | None = None
    name_template: str = "p_{}"
    retry_cnt: int = 5
    timeout_increment: int | float = 5
    dry_run: bool = False
    return_sql: bool = False
    only_generate_sql: bool = False
    return_metadata: bool = False
    query_label: str | None = None

def build_drop_many_partitions_sqls(
    connection_type: str,
    table: str,
    partition_keys_list: Sequence[str],
    trino_partition_column: str | None = None,
    gp_truncate: bool = False,
    ch_cluster: str = "{cluster}",
    query_label: str | None = None,
) -> list[str]:
    backend = resolve_connection_backend(connection_type)
    target_table = _validate_non_empty_table_name(table)
    partition_keys = _validate_partition_keys(partition_keys_list)
    normalized_trino_partition_column = _normalize_partition_column(
        trino_partition_column
    )
    _validate_drop_many_partitions_options(
        backend,
        trino_partition_column=normalized_trino_partition_column,
        gp_truncate=gp_truncate,
    )
    return [
        apply_query_label(sql, query_label)
        for sql in _build_drop_many_partitions_sqls_for_backend(
            backend,
            target_table,
            partition_keys,
            partition_column=normalized_trino_partition_column,
            gp_truncate=gp_truncate,
            ch_cluster=ch_cluster,
        )
    ]

def _build_gp_create_many_partitions_sqls(
    table: str,
    *,
    intervals: Sequence[Mapping[str, Any]] | None = None,
    values: Sequence[str] | None = None,
    days: Sequence[str] | None = None,
    weeks: Sequence[str] | None = None,
    months: Sequence[str] | None = None,
    years: Sequence[str] | None = None,
    name_template: str = "p_{}",
    query_label: str | None = None,
) -> list[str]:
    target_table = _validate_non_empty_table_name(table)
    partitions = _normalize_gp_create_partitions(
        intervals=intervals,
        values=values,
        days=days,
        weeks=weeks,
        months=months,
        years=years,
        name_template=name_template,
    )
    return [
        apply_query_label(
            _build_gp_create_partition_sql(target_table, partition),
            query_label,
        )
        for partition in partitions
    ]

@timed_public_sql_function
def gp_create_many_partitions(
    db_key: str,
    table: str,
    *,
    intervals: Sequence[Mapping[str, Any]] | None = None,
    values: Sequence[str] | None = None,
    days: Sequence[str] | None = None,
    weeks: Sequence[str] | None = None,
    months: Sequence[str] | None = None,
    years: Sequence[str] | None = None,
    name_template: str = "p_{}",
    retry_cnt: int = 5,
    timeout_increment: int | float = 5,
    query_label: str | None = None,
    dry_run: bool = False,
    return_sql: bool = False,
    only_generate_sql: bool = False,
    return_metadata: bool = False,
) -> str | SqlPlan | SqlOperationResult | None:
    options = _build_gp_create_many_partitions_options(
        db_key=db_key,
        table=table,
        intervals=intervals,
        values=values,
        days=days,
        weeks=weeks,
        months=months,
        years=years,
        name_template=name_template,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
        dry_run=dry_run,
        return_sql=return_sql,
        only_generate_sql=only_generate_sql,
        return_metadata=return_metadata,
    )
    plan = build_gp_create_many_partitions_plan(options)
    if options.only_generate_sql:
        return _format_sql_statements(plan.sqls)
    if options.dry_run or options.return_sql:
        return plan

    metadata = plan.metadata
    time_print(
        f"Creating {metadata.statement_count} partition(s) on {options.target_table}",
        connection=options.connection_key,
        backend=options.backend,
    )

    def operation(connection_ref: dict[str, Any], attempt: int) -> None:
        with tracked_sql_operation(
            metadata=metadata,
            operation_name="gp_create_many_partitions",
            alias=options.connection_key,
            backend=options.backend,
            phase="create_partitions",
            retry_attempt=attempt,
            query_label=options.query_label,
            preview_sql=plan.sqls[0] if plan.sqls else None,
        ):
            get_backend_adapter(options.backend).execute_commands(
                connection_ref["connection"],
                plan.sqls,
            )
            metadata.affected_rows = None

    def context(attempt: int) -> SqlOperationContext:
        return SqlOperationContext(
            operation="gp_create_many_partitions",
            alias=options.connection_key,
            backend=options.backend,
            phase="create_partitions",
            target_table=options.target_table,
            retry_attempt=attempt,
            sql_preview=sql_preview(plan.sqls[0] if plan.sqls else None),
        )

    run_connection_operation(
        operation_name=(
            f"creating partitions on {options.connection_key}.{options.target_table}"
        ),
        connection_key=options.connection_key,
        backend=options.backend,
        retry_cnt=options.retry_cnt,
        timeout_increment=options.timeout_increment,
        open_connection=get_sql_connection,
        operation=operation,
        context_factory=context,
    )
    if options.return_metadata:
        return SqlOperationResult(rows=None, metadata=metadata, plan=plan)
    return None

@timed_public_sql_function
def drop_paritions(
    db_key: str,
    table: str,
    partition_keys_list: list[str],
    trino_partition_column: str | None = None,
    gp_truncate: bool = False,
    *,
    retry_cnt: int = 5,
    timeout_increment: int | float = 5,
    query_label: str | None = None,
    dry_run: bool = False,
    return_sql: bool = False,
    return_metadata: bool = False,
) -> SqlPlan | SqlOperationResult | None:
    options = _build_drop_many_partitions_options(
        db_key=db_key,
        table=table,
        partition_keys_list=partition_keys_list,
        trino_partition_column=trino_partition_column,
        gp_truncate=gp_truncate,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
        dry_run=dry_run,
        return_sql=return_sql,
        return_metadata=return_metadata,
    )
    plan = build_drop_many_partitions_plan(options)
    if options.dry_run or options.return_sql:
        return plan

    metadata = plan.metadata
    time_print(
        f"Dropping {len(options.partition_keys)} partition(s) "
        f"from {options.target_table}",
        connection=options.connection_key,
        backend=options.backend,
    )

    def operation(connection_ref: dict[str, Any], attempt: int) -> None:
        with tracked_sql_operation(
            metadata=metadata,
            operation_name="drop_paritions",
            alias=options.connection_key,
            backend=options.backend,
            phase="drop_partitions",
            retry_attempt=attempt,
            query_label=options.query_label,
            preview_sql=plan.sqls[0] if plan.sqls else None,
        ):
            get_backend_adapter(options.backend).execute_commands(
                connection_ref["connection"],
                plan.sqls,
            )
            metadata.affected_rows = None

    def context(attempt: int) -> SqlOperationContext:
        return SqlOperationContext(
            operation="drop_paritions",
            alias=options.connection_key,
            backend=options.backend,
            phase="drop_partitions",
            target_table=options.target_table,
            retry_attempt=attempt,
            sql_preview=sql_preview(plan.sqls[0] if plan.sqls else None),
        )

    run_connection_operation(
        operation_name=(
            f"dropping partitions from {options.connection_key}.{options.target_table}"
        ),
        connection_key=options.connection_key,
        backend=options.backend,
        retry_cnt=options.retry_cnt,
        timeout_increment=options.timeout_increment,
        open_connection=get_sql_connection,
        operation=operation,
        context_factory=context,
    )
    if options.return_metadata:
        return SqlOperationResult(rows=None, metadata=metadata, plan=plan)
    return None

def build_gp_create_many_partitions_plan(
    options: _GpCreateManyPartitionsOptions,
) -> SqlPlan:
    sqls = _build_gp_create_many_partitions_sqls(
        options.target_table,
        intervals=options.intervals,
        values=options.values,
        days=options.days,
        weeks=options.weeks,
        months=options.months,
        years=options.years,
        name_template=options.name_template,
        query_label=options.query_label,
    )
    plan = SqlPlan(
        operation="gp_create_many_partitions",
        target_alias=options.connection_key,
        target_backend=options.backend,
        target_table=options.target_table,
        options={
            "partition_input": _selected_gp_create_partition_input(
                intervals=options.intervals,
                values=options.values,
                days=options.days,
                weeks=options.weeks,
                months=options.months,
                years=options.years,
            ),
            "name_template": options.name_template,
        },
        metadata=SqlOperationMetadata(
            statement_count=len(sqls),
            query_label=options.query_label,
        ),
    )
    plan.extend(
        sqls,
        alias=options.connection_key,
        backend=options.backend,
        phase="create_partitions",
        target_table=options.target_table,
    )
    return plan

def _build_gp_create_many_partitions_options(
    *,
    db_key: str,
    table: str,
    intervals: Sequence[Mapping[str, Any]] | None,
    values: Sequence[str] | None,
    days: Sequence[str] | None,
    weeks: Sequence[str] | None,
    months: Sequence[str] | None,
    years: Sequence[str] | None,
    name_template: str,
    retry_cnt: int,
    timeout_increment: int | float,
    query_label: str | None,
    dry_run: bool,
    return_sql: bool,
    only_generate_sql: bool,
    return_metadata: bool,
) -> _GpCreateManyPartitionsOptions:
    config = get_connection_config(db_key)
    if config.backend != "gp":
        raise UnsupportedConnectionTypeError(
            "gp_create_many_partitions requires a gp connection, "
            f"got '{config.backend}'."
        )
    validate_retry_options(retry_cnt, timeout_increment)

    target_table = _validate_non_empty_table_name(table)
    _normalize_gp_create_partitions(
        intervals=intervals,
        values=values,
        days=days,
        weeks=weeks,
        months=months,
        years=years,
        name_template=name_template,
    )
    return _GpCreateManyPartitionsOptions(
        connection_key=config.connection_key,
        backend=config.backend,
        target_table=target_table,
        intervals=intervals,
        values=values,
        days=days,
        weeks=weeks,
        months=months,
        years=years,
        name_template=name_template,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        dry_run=dry_run,
        return_sql=return_sql,
        only_generate_sql=only_generate_sql,
        return_metadata=return_metadata,
        query_label=query_label,
    )

def _format_sql_statements(sqls: Sequence[str]) -> str:
    return ";\n".join(statement.rstrip(";") for statement in sqls)

def build_drop_many_partitions_plan(
    options: DropManyPartitionsOptions,
) -> SqlPlan:
    sqls = build_drop_many_partitions_sqls(
        options.backend,
        options.target_table,
        options.partition_keys,
        trino_partition_column=options.trino_partition_column,
        gp_truncate=options.gp_truncate,
        ch_cluster=options.ch_cluster,
        query_label=options.query_label,
    )
    plan = SqlPlan(
        operation="drop_paritions",
        target_alias=options.connection_key,
        target_backend=options.backend,
        target_table=options.target_table,
        options={
            "partition_keys": options.partition_keys,
            "trino_partition_column": options.trino_partition_column,
            "gp_truncate": options.gp_truncate,
            "ch_cluster": options.ch_cluster,
        },
        metadata=SqlOperationMetadata(
            statement_count=len(sqls),
            query_label=options.query_label,
        ),
    )
    plan.extend(
        sqls,
        alias=options.connection_key,
        backend=options.backend,
        phase="drop_partitions",
        target_table=options.target_table,
    )
    return plan

def _build_drop_many_partitions_options(
    *,
    db_key: str,
    table: str,
    partition_keys_list: Sequence[str],
    trino_partition_column: str | None,
    gp_truncate: bool,
    retry_cnt: int,
    timeout_increment: int | float,
    query_label: str | None,
    dry_run: bool,
    return_sql: bool,
    return_metadata: bool,
) -> DropManyPartitionsOptions:
    config = get_connection_config(db_key)
    validate_retry_options(retry_cnt, timeout_increment)

    target_table = _validate_non_empty_table_name(table)
    partition_keys = _validate_partition_keys(partition_keys_list)
    normalized_trino_partition_column = _normalize_partition_column(
        trino_partition_column
    )
    _validate_drop_many_partitions_options(
        config.backend,
        trino_partition_column=normalized_trino_partition_column,
        gp_truncate=gp_truncate,
    )
    return DropManyPartitionsOptions(
        connection_key=config.connection_key,
        backend=config.backend,
        target_table=target_table,
        partition_keys=partition_keys,
        trino_partition_column=normalized_trino_partition_column,
        gp_truncate=gp_truncate,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        dry_run=dry_run,
        return_sql=return_sql,
        return_metadata=return_metadata,
        query_label=query_label,
    )

def _build_drop_many_partitions_sqls_for_backend(
    backend: str,
    table: str,
    partition_keys: list[str],
    *,
    partition_column: str | None,
    gp_truncate: bool,
    ch_cluster: str,
) -> list[str]:
    if backend == "gp":
        action = "TRUNCATE" if gp_truncate else "DROP"
        return [
            f"ALTER TABLE {table} {action} PARTITION FOR ({_sql_string_literal(key)})"
            for key in partition_keys
        ]
    if backend == "trino":
        if partition_column is None:
            raise InvalidSqlInputError(
                "trino_partition_column is required for Trino partition deletes."
            )
        partition_values = ", ".join(
            f"DATE {_sql_string_literal(key)}" for key in partition_keys
        )
        return [
            f"DELETE FROM {table}\nWHERE {partition_column} IN ({partition_values})"
        ]
    if backend == "ch":
        shard_table = build_ch_shard_table_name(table)
        cluster_clause = ch_cluster_clause(ch_cluster)
        return [
            f"ALTER TABLE {shard_table}{cluster_clause} "
            f"DROP PARTITION {_sql_string_literal(key)}"
            for key in partition_keys
        ]
    raise UnsupportedConnectionTypeError(
        "Unsupported connection type. Expected one of: 'trino', 'gp', 'ch'."
    )

def _normalize_gp_create_partitions(
    *,
    intervals: Sequence[Mapping[str, Any]] | None,
    values: Sequence[str] | None,
    days: Sequence[str] | None,
    weeks: Sequence[str] | None,
    months: Sequence[str] | None,
    years: Sequence[str] | None,
    name_template: str,
) -> list[_GpPartitionDefinition]:
    _validate_gp_partition_name_template(name_template)
    selected = _selected_gp_create_partition_input(
        intervals=intervals,
        values=values,
        days=days,
        weeks=weeks,
        months=months,
        years=years,
    )
    if selected == "intervals":
        return _normalize_gp_interval_partitions(intervals, name_template)
    if selected == "values":
        return _normalize_gp_value_partitions(values, name_template)
    if selected == "days":
        return _normalize_gp_period_partitions(days, "days", name_template)
    if selected == "weeks":
        return _normalize_gp_period_partitions(weeks, "weeks", name_template)
    if selected == "months":
        return _normalize_gp_period_partitions(months, "months", name_template)
    if selected == "years":
        return _normalize_gp_period_partitions(years, "years", name_template)
    raise AssertionError(f"Unexpected Greenplum partition input: {selected}")

def _selected_gp_create_partition_input(
    *,
    intervals: Sequence[Mapping[str, Any]] | None,
    values: Sequence[str] | None,
    days: Sequence[str] | None,
    weeks: Sequence[str] | None,
    months: Sequence[str] | None,
    years: Sequence[str] | None,
) -> str:
    provided = {
        name: value
        for name, value in {
            "intervals": intervals,
            "values": values,
            "days": days,
            "weeks": weeks,
            "months": months,
            "years": years,
        }.items()
        if value is not None
    }
    if len(provided) != 1:
        raise InvalidSqlInputError(
            "Exactly one of intervals, values, days, weeks, months, or years "
            "must be provided."
        )
    return next(iter(provided))

def _normalize_gp_interval_partitions(
    intervals: Sequence[Mapping[str, Any]] | None,
    name_template: str,
) -> list[_GpPartitionDefinition]:
    interval_items = _validate_gp_partition_sequence(intervals, "intervals")
    partitions: list[_GpPartitionDefinition] = []
    for index, item in enumerate(interval_items):
        if not isinstance(item, Mapping):
            raise InvalidSqlInputError(
                "intervals must contain mappings with start and end values."
            )
        start = _parse_gp_partition_date(item.get("start"), "interval start")
        end = _parse_gp_partition_date(item.get("end"), "interval end")
        if end <= start:
            raise InvalidSqlInputError("Interval end must be after interval start.")

        raw_name = item.get("name")
        if raw_name is None:
            name = _render_gp_partition_name(name_template, start.isoformat())
        else:
            name = _validate_gp_partition_identifier(
                raw_name,
                f"intervals[{index}].name",
            )
        partitions.append(
            _GpPartitionDefinition(
                name=name,
                start=start.isoformat(),
                end=end.isoformat(),
            )
        )
    return partitions

def _normalize_gp_value_partitions(
    values: Sequence[str] | None,
    name_template: str,
) -> list[_GpPartitionDefinition]:
    value_items = _validate_gp_partition_sequence(values, "values")
    partitions: list[_GpPartitionDefinition] = []
    for value in value_items:
        if not isinstance(value, str):
            raise InvalidSqlInputError("values must contain strings.")
        normalized = value.strip()
        if not normalized:
            raise InvalidSqlInputError("values must not contain empty strings.")
        partitions.append(
            _GpPartitionDefinition(
                name=_render_gp_partition_name(name_template, normalized),
                value=normalized,
            )
        )
    return partitions

def _normalize_gp_period_partitions(
    raw_values: Sequence[str] | None,
    input_name: str,
    name_template: str,
) -> list[_GpPartitionDefinition]:
    date_values = _validate_gp_partition_sequence(raw_values, input_name)
    partitions: list[_GpPartitionDefinition] = []
    for raw_value in date_values:
        start = _parse_gp_partition_date(raw_value, input_name)
        end = _next_gp_partition_period_start(start, input_name)
        partitions.append(
            _GpPartitionDefinition(
                name=_render_gp_partition_name(name_template, start.isoformat()),
                start=start.isoformat(),
                end=end.isoformat(),
            )
        )
    return partitions

def _validate_gp_partition_sequence(
    value: Sequence[Any] | None,
    argument_name: str,
) -> list[Any]:
    if value is None:
        raise InvalidSqlInputError(f"{argument_name} must be provided.")
    if isinstance(value, (str, bytes)) or isinstance(value, Mapping):
        raise InvalidSqlInputError(
            f"{argument_name} must be a non-empty sequence."
        )
    try:
        items = list(value)
    except TypeError as exc:
        raise InvalidSqlInputError(
            f"{argument_name} must be a non-empty sequence."
        ) from exc
    if not items:
        raise InvalidSqlInputError(
            f"{argument_name} must be a non-empty sequence."
        )
    return items

def _parse_gp_partition_date(value: Any, argument_name: str) -> date:
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    elif isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            raise InvalidSqlInputError(
                f"{argument_name} must contain ISO date strings."
            )
        try:
            parsed = date.fromisoformat(normalized)
        except ValueError as exc:
            raise InvalidSqlInputError(
                f"{argument_name} must contain valid ISO date strings."
            ) from exc
    else:
        raise InvalidSqlInputError(
            f"{argument_name} must contain ISO date strings."
        )
    return parsed

def _next_gp_partition_period_start(start: date, input_name: str) -> date:
    if input_name == "days":
        return start + timedelta(days=1)
    if input_name == "weeks":
        if start.weekday() != 0:
            raise InvalidSqlInputError("weeks values must be Monday week starts.")
        return start + timedelta(days=7)
    if input_name == "months":
        if start.day != 1:
            raise InvalidSqlInputError("months values must be month starts.")
        year = start.year + (1 if start.month == 12 else 0)
        month = 1 if start.month == 12 else start.month + 1
        return date(year, month, 1)
    if input_name == "years":
        if start.month != 1 or start.day != 1:
            raise InvalidSqlInputError("years values must be year starts.")
        return date(start.year + 1, 1, 1)
    raise AssertionError(f"Unexpected Greenplum period input: {input_name}")

def _validate_gp_partition_name_template(name_template: str) -> None:
    if not isinstance(name_template, str):
        raise InvalidSqlInputError("name_template must be a string.")
    remainder = name_template.replace("{}", "", 1)
    if name_template.count("{}") != 1 or "{" in remainder or "}" in remainder:
        raise InvalidSqlInputError(
            "name_template must contain exactly one {} placeholder."
        )

def _render_gp_partition_name(name_template: str, value: str) -> str:
    sanitized = _sanitize_gp_partition_name_token(value)
    return _validate_gp_partition_identifier(
        name_template.format(sanitized),
        "partition name",
    )

def _sanitize_gp_partition_name_token(value: str) -> str:
    sanitized = re.sub(r"[^0-9A-Za-z_]+", "_", str(value).strip())
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")
    return sanitized or "partition"

def _validate_gp_partition_identifier(value: Any, argument_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidSqlInputError(f"{argument_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise InvalidSqlInputError(f"{argument_name} must not be empty.")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", normalized) is None:
        raise InvalidSqlInputError(
            f"{argument_name} must be an unquoted SQL identifier."
        )
    return normalized

def _build_gp_create_partition_sql(
    table: str,
    partition: _GpPartitionDefinition,
) -> str:
    if partition.value is not None:
        return (
            f"ALTER TABLE {table} ADD PARTITION {partition.name} "
            f"VALUES ({_sql_string_literal(partition.value)})"
        )
    if partition.start is None or partition.end is None:
        raise InvalidSqlInputError(
            "Range partitions require both start and end values."
        )
    return (
        f"ALTER TABLE {table} ADD PARTITION {partition.name} "
        f"START ({_sql_string_literal(partition.start)}) INCLUSIVE "
        f"END ({_sql_string_literal(partition.end)}) EXCLUSIVE"
    )

def _validate_non_empty_table_name(table: str) -> str:
    normalized = str(table).strip()
    if not normalized:
        raise InvalidSqlInputError("Table name must not be empty.")
    return normalized

def _validate_partition_keys(partition_keys_list: Sequence[str]) -> list[str]:
    if isinstance(partition_keys_list, (str, bytes)) or not partition_keys_list:
        raise InvalidSqlInputError(
            "partition_keys_list must be a non-empty sequence of strings."
        )

    partition_keys: list[str] = []
    for partition_key in partition_keys_list:
        if not isinstance(partition_key, str):
            raise InvalidSqlInputError("Partition values must be strings.")
        normalized = partition_key.strip()
        if not normalized:
            raise InvalidSqlInputError("Partition values must not be empty.")
        partition_keys.append(normalized)
    return partition_keys

def _normalize_partition_column(partition_column: str | None) -> str | None:
    if partition_column is None:
        return None
    normalized = str(partition_column).strip()
    return normalized or None

def _validate_drop_many_partitions_options(
    backend: str,
    *,
    trino_partition_column: str | None,
    gp_truncate: bool,
) -> None:
    if gp_truncate and backend != "gp":
        raise UnsupportedConnectionTypeError(
            "gp_truncate=True is only supported for Greenplum connections."
        )
    if backend == "trino" and trino_partition_column is None:
        raise InvalidSqlInputError(
            "trino_partition_column is required for Trino partition deletes."
        )

def _sql_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
