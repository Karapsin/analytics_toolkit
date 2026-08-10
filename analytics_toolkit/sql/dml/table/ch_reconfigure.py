from __future__ import annotations

# ruff: noqa: EM101, EM102, PLR0913, TRY003
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from analytics_toolkit.general import time_print
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.reconfigure_models import (
    ChReconfiguration,
    ChReconfigureOptions,
)
from analytics_toolkit.sql.backends.ch.wait_policy import resolve_ch_ddl_wait_policy
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.connection.errors import (
    InvalidSqlInputError,
    SqlOperationContext,
    UnsupportedConnectionTypeError,
)
from analytics_toolkit.sql.connection.get_sql_connection import get_sql_connection
from analytics_toolkit.sql.execution.operation_runner import (
    run_connection_operation,
    timed_public_sql_function,
    tracked_sql_operation,
    validate_retry_options,
)
from analytics_toolkit.sql.execution.plans import SqlOperationResult, SqlPlan

if TYPE_CHECKING:
    from collections.abc import Sequence


@timed_public_sql_function
def ch_reconfigure_table(
    db_key: str,
    table: str,
    *,
    ch_engine: str | None = None,
    partition_by: Sequence[str] | str | None = None,
    order_by: Sequence[str] | str | None = None,
    ch_sharding_key: str | None = None,
    ch_distributed_table: bool | None = None,
    ch_distributed_engine_template: str | None = None,
    ch_distributed_cluster: str | None = None,
    ch_shard_on_cluster: str | None = None,
    ch_distributed_on_cluster: str | None = None,
    ch_settings: Mapping[str, str | int | float | bool | None] | None = None,
    ch_ddl_wait_policy: str | None = None,
    reset_partition_by: bool = False,
    reset_order_by: bool = False,
    to_defaults: bool = False,
    validate_row_count: bool = True,
    retry_cnt: int = 5,
    timeout_increment: float = 5,
    query_label: str | None = None,
    dry_run: bool = False,
    return_sql: bool = False,
    return_metadata: bool = False,
) -> SqlPlan | SqlOperationResult | None:
    """Reconfigure a ClickHouse MergeTree table or managed table pair."""

    config = get_connection_config(db_key)
    options = _build_options(
        connection_key=config.connection_key,
        table=table,
        ch_engine=ch_engine,
        partition_by=partition_by,
        order_by=order_by,
        ch_sharding_key=ch_sharding_key,
        ch_distributed_table=ch_distributed_table,
        ch_distributed_engine_template=ch_distributed_engine_template,
        ch_distributed_cluster=ch_distributed_cluster,
        ch_shard_on_cluster=ch_shard_on_cluster,
        ch_distributed_on_cluster=ch_distributed_on_cluster,
        ch_settings=ch_settings,
        reset_partition_by=reset_partition_by,
        reset_order_by=reset_order_by,
        to_defaults=to_defaults,
        regular_defaults=getattr(getattr(config, "ddl_defaults", None), "regular", None),
        ch_ddl_wait_policy=resolve_ch_ddl_wait_policy(
            ch_ddl_wait_policy,
            getattr(config, "ch_ddl_wait_policy", None),
        ),
        validate_row_count=validate_row_count,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
    )
    adapter = get_backend_adapter(config.backend)
    plan_reconfiguration = getattr(adapter, "plan_table_reconfiguration", None)
    execute_reconfiguration = getattr(adapter, "execute_table_reconfiguration", None)
    if not callable(plan_reconfiguration) or not callable(execute_reconfiguration):
        raise UnsupportedConnectionTypeError(
            "ch_reconfigure_table requires a ClickHouse connection."
        )

    def operation(connection_ref: dict[str, Any], attempt: int) -> ChReconfiguration:
        with tracked_sql_operation(
            operation_name="ch_reconfigure_table",
            alias=options.connection_key,
            backend=config.backend,
            phase="plan" if dry_run or return_sql else "reconfigure",
            retry_attempt=attempt,
            query_label=options.query_label,
            preview_sql=None,
        ):
            reconfiguration = plan_reconfiguration(
                connection_ref["connection"],
                options,
            )
            if not dry_run and not return_sql:
                execute_reconfiguration(
                    connection_ref["connection"],
                    reconfiguration,
                    validate_row_count=options.validate_row_count,
                )
            return cast("ChReconfiguration", reconfiguration)

    def context(attempt: int) -> SqlOperationContext:
        return SqlOperationContext(
            operation="ch_reconfigure_table",
            alias=options.connection_key,
            backend=config.backend,
            phase="plan" if dry_run or return_sql else "reconfigure",
            target_table=options.table,
            retry_attempt=attempt,
        )

    time_print(
        f"Planning ClickHouse table reconfiguration for {options.table}",
        connection=options.connection_key,
        backend=config.backend,
    )
    reconfiguration = run_connection_operation(
        operation_name=(f"reconfiguring ClickHouse table {options.connection_key}.{options.table}"),
        connection_key=options.connection_key,
        backend=config.backend,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        open_connection=get_sql_connection,
        operation=operation,
        context_factory=context,
    )
    if dry_run or return_sql:
        return reconfiguration.plan
    if return_metadata:
        return SqlOperationResult(
            rows=None,
            metadata=reconfiguration.plan.metadata,
            plan=reconfiguration.plan,
            data=reconfiguration.result_data(),
        )
    return None


def _build_options(
    *,
    connection_key: str,
    table: str,
    ch_engine: str | None,
    partition_by: Sequence[str] | str | None,
    order_by: Sequence[str] | str | None,
    ch_sharding_key: str | None,
    ch_distributed_table: bool | None,
    ch_distributed_engine_template: str | None,
    ch_distributed_cluster: str | None,
    ch_shard_on_cluster: str | None,
    ch_distributed_on_cluster: str | None,
    ch_settings: Mapping[str, str | int | float | bool | None] | None,
    reset_partition_by: bool,
    reset_order_by: bool,
    to_defaults: bool,
    regular_defaults: object | None,
    ch_ddl_wait_policy: str,
    validate_row_count: bool,
    retry_cnt: int,
    timeout_increment: float,
    query_label: str | None,
) -> ChReconfigureOptions:
    validate_retry_options(retry_cnt, timeout_increment)
    _validate_bool(reset_partition_by, "reset_partition_by")
    _validate_bool(reset_order_by, "reset_order_by")
    _validate_bool(to_defaults, "to_defaults")
    _validate_bool(validate_row_count, "validate_row_count")
    if ch_distributed_table is not None:
        _validate_bool(ch_distributed_table, "ch_distributed_table")
    if reset_partition_by and partition_by is not None:
        raise InvalidSqlInputError("reset_partition_by cannot be combined with partition_by.")
    if reset_order_by and order_by is not None:
        raise InvalidSqlInputError("reset_order_by cannot be combined with order_by.")
    if ch_settings is not None and not isinstance(ch_settings, Mapping):
        raise InvalidSqlInputError("ch_settings must be a mapping when provided.")
    return ChReconfigureOptions(
        connection_key=connection_key,
        table=str(table).strip(),
        ch_engine=ch_engine,
        partition_by=partition_by,
        order_by=order_by,
        ch_sharding_key=ch_sharding_key,
        ch_distributed_table=ch_distributed_table,
        ch_distributed_engine_template=ch_distributed_engine_template,
        ch_distributed_cluster=ch_distributed_cluster,
        ch_shard_on_cluster=ch_shard_on_cluster,
        ch_distributed_on_cluster=ch_distributed_on_cluster,
        ch_settings=dict(ch_settings) if ch_settings is not None else None,
        reset_partition_by=reset_partition_by,
        reset_order_by=reset_order_by,
        to_defaults=to_defaults,
        regular_defaults=cast("Any", regular_defaults),
        ch_ddl_wait_policy=ch_ddl_wait_policy,
        validate_row_count=validate_row_count,
        query_label=query_label,
    )


def _validate_bool(value: object, option_name: str) -> None:
    if not isinstance(value, bool):
        raise InvalidSqlInputError(f"{option_name} must be a boolean.")


__all__ = ["ch_reconfigure_table"]
