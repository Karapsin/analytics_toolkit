"""Endpoint-local copy and engine-aware physical counts for reconfiguration."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.dml.transfer.runtime.retry import close_connection_ref
from analytics_toolkit.sql.execution.backend_options import (
    current_backend_operation,
    current_backend_query_label,
)
from analytics_toolkit.sql.execution.cancellation import raise_if_cancelled
from analytics_toolkit.sql.execution.labels import apply_query_label

from .ddl import _sql_string_literal
from .outage_context import internal_io
from .reconfigure_ddl import distributed_table_parts
from .reconfigure_facade_scope import read_rows
from .reconfigure_physical import (
    ClusterPhysicalStorage,
    PhysicalRebuildExecutionScope,
    certify_physical_table,
    require_physical_identity,
)
from .reconfigure_support import count_final_rows, count_rows
from .recovery_endpoints import RecoveryEndpoint

if TYPE_CHECKING:
    from collections.abc import Iterator

    from .reconfigure_models import ChReconfiguration
    from .reconfigure_physical import PhysicalCopyPlan, PhysicalExecutionScope, PhysicalStorage

_IDENTITY_FIELDS = 2


def _configuration_matches(execution: PhysicalExecutionScope, config: Any) -> None:
    if config != execution.config:
        message = "ClickHouse configuration changed during physical reconfiguration."
        raise InvalidSqlInputError(message)


@contextmanager
def _configuration_guard(execution: PhysicalExecutionScope) -> Iterator[None]:
    operation = current_backend_operation()
    key = ("physical_reconfiguration", id(execution))

    def guard(alias: str, config: Any) -> None:
        if alias == execution.config.connection_key:
            _configuration_matches(execution, config)

    if operation is not None:
        with operation.lock:
            operation.resources.setdefault("connection_configuration_guards", {})[key] = guard
    try:
        yield
    finally:
        if operation is not None:
            with operation.lock:
                operation.resources.get("connection_configuration_guards", {}).pop(key, None)


def _rows(connection: Any, sql: str) -> list[Any]:
    raise_if_cancelled()
    return read_rows(connection, apply_query_label(sql, current_backend_query_label()))


def _open_workers(stack: ExitStack, connection: Any, execution: PhysicalExecutionScope) -> None:
    from analytics_toolkit.sql.connection.get_sql_connection import (  # noqa: PLC0415 - avoid backend factory cycle
        get_ch_connection_for_host,
    )

    if not isinstance(execution, PhysicalRebuildExecutionScope):
        return
    copy = execution.payload.copy
    if copy is None:
        return
    for endpoint in copy.endpoints:
        raise_if_cancelled()
        _configuration_matches(execution, get_connection_config(execution.config.connection_key))
        if endpoint.coordinator:
            worker = connection
        else:
            address = RecoveryEndpoint(endpoint.hostname, endpoint.native[1])
            with internal_io():
                worker = get_ch_connection_for_host(execution.config.connection_key, address)
            stack.callback(close_connection_ref, {"connection": worker}, address.identity, "host")
        identities = _rows(worker, "SELECT serverUUID(), tcpPort()")
        expected_uuid, expected_port = endpoint.identity
        if (
            len(identities) != 1
            or len(identities[0]) != _IDENTITY_FIELDS
            or str(identities[0][0]) != str(expected_uuid)
            or int(identities[0][1]) != expected_port
        ):
            message = "Cannot verify the selected physical worker endpoint identity."
            raise InvalidSqlInputError(message)
        execution.connections[endpoint.position] = worker


@contextmanager
def physical_execution_scope(connection: Any, change: ChReconfiguration) -> Iterator[None]:
    execution = change.physical_execution
    if execution is None:
        yield
        return
    with _configuration_guard(execution), ExitStack() as stack:
        try:
            require_physical_identity(connection, execution)
            _open_workers(stack, connection, execution)
            yield
        finally:
            execution.connections.clear()


def copy_physical_data(adapter: Any, connection: Any, change: ChReconfiguration) -> bool:
    """Return true when the explicitly endpoint-scoped copy was executed."""
    execution = change.physical_execution
    if not isinstance(execution, PhysicalRebuildExecutionScope):
        return False
    _configuration_matches(execution, get_connection_config(execution.config.connection_key))
    certify_physical_table(connection, execution.target, execution.payload.replacement)
    copy = execution.payload.copy
    if copy is None:
        return False
    for endpoint in copy.endpoints:
        raise_if_cancelled()
        _configuration_matches(execution, get_connection_config(execution.config.connection_key))
        with internal_io():
            adapter.execute_command(execution.connections[endpoint.position], copy.sql)
    return True


def _count_sql(storage: PhysicalStorage, table: str) -> str:
    if not isinstance(storage, ClusterPhysicalStorage):
        return f"SELECT count() FROM {table}"  # noqa: S608 - parser-rendered table identity
    database, relation = distributed_table_parts(table)
    function = "cluster" if storage.replicated else "clusterAllReplicas"
    return (
        f"SELECT count() FROM {function}("  # noqa: S608 - quoted metadata identities
        f"{_sql_string_literal(storage.scope.cluster)}, "
        f"{_sql_string_literal(database)}, {_sql_string_literal(relation)})"
    )


def _endpoint_counts(
    execution: PhysicalRebuildExecutionScope, copy: PhysicalCopyPlan, table: str
) -> dict[str, int]:
    counts = {}
    for endpoint in copy.endpoints:
        rows = _rows(
            execution.connections[endpoint.position],
            f"SELECT count() FROM {table}",  # noqa: S608 - parser-rendered table identity
        )
        if len(rows) != 1 or len(rows[0]) != 1:
            message = "Incomplete physical endpoint row-count result."
            raise InvalidSqlInputError(message)
        identity = RecoveryEndpoint(endpoint.hostname, endpoint.native[1]).identity
        counts[identity] = int(rows[0][0])
    return counts


def count_reconfiguration_rows(
    connection: Any,
    change: ChReconfiguration,
    phase: str,
    table: str,
    *,
    enabled: bool,
) -> int | None:
    execution = change.physical_execution
    if not isinstance(execution, PhysicalRebuildExecutionScope):
        if phase == "final":
            return count_final_rows(connection, change.table, change.final_count_cluster)
        return count_rows(connection, table)
    if not enabled:
        return None
    _configuration_matches(execution, get_connection_config(execution.config.connection_key))
    storage = execution.source if phase in {"source", "stable_source"} else execution.target
    table = execution.payload.replacement if phase == "replacement" else storage.table
    if execution.payload.copy is not None:
        counts = _endpoint_counts(execution, execution.payload.copy, table)
        if phase != "source" and counts != execution.counts["source"]:
            message = "Physical per-host row counts changed during reconfiguration."
            raise RuntimeError(message)
        execution.counts[phase] = counts
        change.plan.options["physical_row_counts"] = {
            name: dict(values) for name, values in execution.counts.items()
        }
        return sum(counts.values())
    rows = _rows(connection, _count_sql(storage, table))
    if len(rows) != 1 or len(rows[0]) != 1:
        message = "Incomplete physical row-count result."
        raise InvalidSqlInputError(message)
    return int(rows[0][0])


def validate_staged_row_counts(
    source: int | None, stable_source: int | None, replacement: int | None, *, enabled: bool
) -> None:
    if enabled and stable_source != source:
        message = (
            "ClickHouse source row count changed during reconfiguration; pause writers and retry."
        )
        raise RuntimeError(message)
    if enabled and replacement != source:
        message = "ClickHouse replacement row count does not match the source table."
        raise RuntimeError(message)


__all__ = [
    "copy_physical_data",
    "count_reconfiguration_rows",
    "physical_execution_scope",
    "validate_staged_row_counts",
]
