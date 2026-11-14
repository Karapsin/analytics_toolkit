"""Certify query values before authoritative creation DDL, then replay them."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from analytics_toolkit.general import time_print
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.query_source_binding import query_may_read_target
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.dml.ddl_options import resolve_cluster_routed_source_staging_ch_policy
from analytics_toolkit.sql.dml.destination_context import freeze_destination_timezone
from analytics_toolkit.sql.dml.load.load_sql_table import insert_rows_batch
from analytics_toolkit.sql.dml.table.table_validation import validate_key_columns_in_columns
from analytics_toolkit.sql.dml.transfer.flow.adaptive_input import (
    AdaptiveTransferContext,
    close_frozen_input,
    configuration_identity_from_configs,
    install_configuration_guard,
    remove_configuration_guard,
    require_frozen_configuration,
    resolve_adaptive_input,
    resolved_source_batches,
)
from analytics_toolkit.sql.dml.transfer.flow.stage_identity import (
    resolve_destination_identity,
    resolve_internal_columns,
)
from analytics_toolkit.sql.dml.transfer.runtime.models import (
    TransferConnectionRefs,
    TransferOptions,
    TransferStageState,
)
from analytics_toolkit.sql.dml.transfer.runtime.retry import run_with_retry
from analytics_toolkit.sql.dml.transfer.schema import inspect_source_query_schema
from analytics_toolkit.sql.execution.backend_options import backend_operation

if TYPE_CHECKING:
    from collections.abc import Iterator

    from analytics_toolkit.sql.execution.plans import SqlOperationMetadata


@dataclass(frozen=True)
class QueryInputRequest:
    alias: str
    backend: str
    target_table: str
    query: str
    schema: dict[str, str] | None = None
    query_label: str | None = None
    force_freeze: bool = False
    target_alias: str | None = None
    target_backend: str | None = None
    source_values_in_python: bool = True


@dataclass
class PreparedQueryInput:
    options: TransferOptions
    state: TransferStageState
    schema: dict[str, str]
    columns: list[str]
    frozen: bool

    def describe(self, metadata: SqlOperationMetadata) -> None:
        if not self.frozen:
            return
        cache = self.options.input_resolution
        metadata.input_count = cache.get("input_count")
        metadata.source_rows = metadata.input_count
        metadata.type_resolutions = [
            {
                "column": column.name,
                "certified_input_count": cache["input_count"],
                **resolution.metadata(),
            }
            for column, resolution in zip(cache["source_schema"], cache["resolutions"])
        ]


def prepare_query_input(
    request: QueryInputRequest,
    connection_ref: dict[str, Any],
    cache: dict[str, Any],
    source_schema: list[Any] | None = None,
    target_connection_ref: dict[str, Any] | None = None,
) -> PreparedQueryInput:
    prepared = cache.get("prepared_query_input")
    if prepared is not None:
        require_frozen_configuration(prepared.options)
        freeze_destination_timezone(
            prepared.options.input_resolution,
            prepared.options.to_db_backend,
            (target_connection_ref or connection_ref)["connection"],
            prepared.schema,
        )
        return cast("PreparedQueryInput", prepared)
    config = get_connection_config(request.alias)
    target_alias = request.target_alias or request.alias
    target_backend = request.target_backend or request.backend
    identity = resolve_destination_identity(request.target_table, target_backend)
    resolution = cache.setdefault(
        "transfer_input_resolution",
        configuration_identity_from_configs([config, get_connection_config(target_alias)]),
    )
    source_schema = resolution.get("source_schema", source_schema)
    if source_schema is None:
        source_schema = inspect_source_query_schema(
            request.backend,
            connection_ref["connection"],
            request.query,
        )
    columns = [column.name for column in source_schema]
    if not columns or len(columns) != len(set(columns)):
        message = "Query creation requires inspectable, uniquely named source columns."
        raise InvalidSqlInputError(message)
    options = TransferOptions(
        from_db_key=request.alias,
        from_db_backend=request.backend,
        to_db_key=target_alias,
        to_db_backend=target_backend,
        source_sql=request.query,
        target_table=request.target_table,
        transfer_id=cache.setdefault("transfer_id", uuid.uuid4().hex),
        canonical_destination_identity=identity.canonical,
        destination_hash=identity.hash_prefix,
        table_schema=request.schema,
        query_label=request.query_label,
        input_resolution=resolution,
        batch_size=10_000,
        retry_cnt=cache.get("retry_cnt", 5),
        timeout_increment=cache.get("timeout_increment", 5),
        source_transfer_staging_schema=config.transfer_staging_schema,
        source_staging_ch_policy=(
            resolve_cluster_routed_source_staging_ch_policy(config)
            if config.transfer_staging_schema is not None
            else None
        ),
        validate_row_count=True,
    )
    cache["guard_options"] = options
    state = TransferStageState(
        target_exists=False,
        source_columns=columns,
        source_column_types={column.name: column.native_type for column in source_schema},
        internal_columns=resolve_internal_columns(columns, target_backend),
    )
    context = AdaptiveTransferContext(
        options.retry_cnt,
        options.retry_cnt,
        force_freeze=request.force_freeze,
        source_values_in_python=request.source_values_in_python,
    )
    frozen = resolve_adaptive_input(
        options,
        TransferConnectionRefs(
            source=connection_ref, target=target_connection_ref or connection_ref
        ),
        state,
        source_schema,
        context,
    )
    prepared = PreparedQueryInput(options, state, state.stage_column_types or {}, columns, frozen)
    if frozen:
        cache["prepared_query_input"] = prepared
    return prepared


def validate_query_columns(
    options: Any, schema: list[Any], adapter: Any, validator: Any
) -> list[str]:
    columns = [column.name for column in schema]
    validator(columns)
    validate_key_columns_in_columns(options.gp_distributed_by_key, columns)
    for expression, name in (
        (options.partition_by, "partition_by"),
        (options.order_by, "order_by"),
    ):
        adapter.validate_ch_columns_in_columns(expression, columns, name, data_name="source query")
    return columns


def prepare_generic_query_input(
    options: Any,
    connection_ref: dict[str, Any],
    source_schema: list[Any],
    target_connection_ref: dict[str, Any] | None = None,
) -> PreparedQueryInput | None:
    if not options.insert_data:
        return None
    overlap = (
        options.source_key == options.target_key
        and options.drop_target_if_exists
        and query_may_read_target(
            options.source_sql,
            options.target_table,
            options.source_backend,
        )
    )
    return prepare_query_input(
        QueryInputRequest(
            options.source_key,
            options.source_backend,
            options.target_table,
            options.source_sql,
            options.table_schema,
            options.query_label,
            force_freeze=overlap,
            target_alias=options.target_key,
            target_backend=options.target_backend,
            source_values_in_python=not get_backend_adapter(
                options.target_backend
            ).should_insert_create_table_from_sql_directly(
                source_backend=options.source_backend,
                source_key=options.source_key,
                target_key=options.target_key,
            ),
        ),
        connection_ref,
        options.input_resolution,
        source_schema,
        target_connection_ref,
    )


def cached_query_schema(cache: dict[str, Any]) -> list[Any] | None:
    prepared = cast("PreparedQueryInput | None", cache.get("prepared_query_input"))
    return (
        cast("list[Any]", prepared.options.input_resolution["source_schema"])
        if prepared is not None
        else None
    )


def replay_query_input(
    prepared: PreparedQueryInput,
    connection_ref: dict[str, Any],
    metadata: SqlOperationMetadata,
) -> int:
    options = prepared.options
    total = 0
    for batch in resolved_source_batches(options, iter):
        require_frozen_configuration(options)
        freeze_destination_timezone(
            options.input_resolution,
            options.to_db_backend,
            connection_ref["connection"],
            prepared.schema,
        )
        total += insert_rows_batch(
            options.to_db_backend,
            connection_ref,
            options.target_table,
            batch.columns,
            batch.rows,
            retry_fn=run_with_retry,
            retry_cnt=options.retry_cnt,
            timeout_increment=options.timeout_increment,
            target_column_types=prepared.schema,
            connection_key=options.to_db_key,
            query_label=options.query_label,
        )
    prepared.describe(metadata)
    metadata.inserted_rows = total
    metadata.affected_rows = total
    metadata.data_write_replay_possible = options.retry_cnt > 1
    return total


@contextmanager
def query_input_scope(cache: dict[str, Any], configs: list[Any]) -> Iterator[None]:
    """Keep the same guarded checkpoint alive through all target attempts."""
    resolution = cache.setdefault(
        "transfer_input_resolution", configuration_identity_from_configs(configs)
    )
    guard_options = TransferOptions(
        from_db_key=configs[0].connection_key,
        from_db_backend=configs[0].backend,
        to_db_key=configs[-1].connection_key,
        to_db_backend=configs[-1].backend,
        source_sql="",
        target_table="",
        input_resolution=resolution,
        transfer_id=cache.setdefault("transfer_id", uuid.uuid4().hex),
    )
    cache["guard_options"] = guard_options
    with backend_operation() as operation:
        install_configuration_guard(guard_options)
        try:
            yield
        finally:
            if operation.preserve_recovery or operation.structural_replay_blocked:
                cache["retain_checkpoint"] = True
            close_query_input(cache)


def close_query_input(cache: dict[str, Any]) -> None:
    prepared = cache.pop("prepared_query_input", None)
    options = prepared.options if prepared is not None else cache.get("guard_options")
    if options is None:
        return
    spool = options.input_resolution.get("spool")
    if spool is not None and cache.get("retain_checkpoint"):
        try:
            manifest = spool.retain(
                {
                    "source_backend": options.from_db_backend,
                    "target_backend": options.to_db_backend,
                    "target_table": options.target_table,
                    "target_schema": prepared.schema
                    if prepared is not None
                    else options.table_schema,
                }
            )
        except Exception as exc:  # noqa: BLE001 - preserve the original structural failure
            remove_configuration_guard(options)
            time_print(
                "Could not certify the retained query checkpoint manifest "
                f"({type(exc).__name__}); recovery input remains retained.",
                connection=options.to_db_key,
                backend=options.to_db_backend,
                phase="recovery",
            )
            return
        time_print(
            f"Retained certified query input checkpoint: {manifest}",
            connection=options.to_db_key,
            backend=options.to_db_backend,
            phase="recovery",
        )
    close_frozen_input(options)
