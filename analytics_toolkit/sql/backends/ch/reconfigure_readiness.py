"""Protect acknowledged facade inserts and synchronize off-side replicas."""

from __future__ import annotations

from numbers import Integral
from typing import TYPE_CHECKING, Any, cast

from sqlglot import exp

from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.execution.labels import apply_query_label

from .dialect import ClickHouseDialect
from .identifiers import quote_identifier
from .outage_statements import exchange_or_rename
from .reconfigure_ddl import distributed_table_parts
from .reconfigure_execution import SYNCHRONOUS_CLUSTER_COMMAND_SETTINGS
from .reconfigure_facade_scope import inspect_facade_scope, read_rows
from .reconfigure_physical import (
    ClusterPhysicalStorage,
    PhysicalRebuildExecutionScope,
    require_physical_identity,
)
from .reconfigure_support import unquoted_table_name
from .routing import local_sql
from .scope_hosts import verified_scope_host

if TYPE_CHECKING:
    from analytics_toolkit.sql.connection.config import ChConfig

    from .reconfigure_models import ChReconfiguration
    from .reconfigure_physical import PhysicalStorage

_QUEUE_FIELDS = 2


def _changes_facade(change: ChReconfiguration) -> bool:
    if not change.source_pair:
        return False
    identity = unquoted_table_name(change.table)
    for statement in change.cutover_sqls:
        parsed = exchange_or_rename(statement)
        if parsed is not None and any(
            unquoted_table_name(table.sql(dialect=ClickHouseDialect)) == identity
            for table in parsed[0].expressions
        ):
            return True
    return False


def _require_empty(connection: Any, table: str) -> None:
    database, relation = distributed_table_parts(table)
    query = (
        exp.select(
            exp.Sum(this=exp.column("data_files")), exp.Sum(this=exp.column("broken_data_files"))
        )
        .from_("system.distribution_queue")
        .where(
            exp.column("database").eq(exp.Literal.string(database)),
            exp.column("table").eq(exp.Literal.string(relation)),
        )
    )
    rows = read_rows(connection, query.sql(dialect=ClickHouseDialect))
    if (
        len(rows) != 1
        or len(rows[0]) != _QUEUE_FIELDS
        or any(
            isinstance(value, bool) or not isinstance(value, Integral) or value < 0
            for value in rows[0]
        )
    ):
        message = "Cannot verify complete ClickHouse facade insert queue metadata."
        raise InvalidSqlInputError(message)
    if any(rows[0]):
        message = (
            "ClickHouse facade has a pending insert queue. Drain it with SYSTEM FLUSH "
            "DISTRIBUTED before reconfiguration; no facade cutover was submitted."
        )
        raise InvalidSqlInputError(message)


def require_empty_facade_queues(connection: Any, change: ChReconfiguration) -> None:
    """Check each selected facade host directly, before creation and before cutover."""
    if not _changes_facade(change):
        return
    if change.distributed_on_cluster is None:
        _require_empty(connection, change.table)
        return
    config = cast("ChConfig", get_connection_config(cast("str", change.plan.target_alias)))
    scope = inspect_facade_scope(
        connection, change.distributed_on_cluster, coordinator_required=False
    )
    for position in scope.identities:
        with verified_scope_host(connection, scope, config, position) as worker:
            _require_empty(worker, change.table)


def synchronize_reconfiguration_replicas(connection: Any, change: ChReconfiguration) -> None:
    execution = change.physical_execution
    if not isinstance(execution, PhysicalRebuildExecutionScope) or not execution.target.replicated:
        return
    require_physical_identity(connection, execution)
    synchronize_verified_replicas(
        connection,
        execution.target,
        table=execution.payload.replacement,
        query_label=change.plan.metadata.query_label,
    )


def synchronize_verified_replicas(
    connection: Any,
    storage: PhysicalStorage,
    *,
    table: str | None = None,
    query_label: str | None = None,
) -> None:
    """Synchronize a certified native storage scope without inventing ownership."""
    if not storage.replicated:
        return
    cluster = None
    if isinstance(storage, ClusterPhysicalStorage):
        scope = inspect_facade_scope(connection, storage.scope.cluster, coordinator_required=False)
        if scope != storage.scope:
            message = "Physical replica endpoint scope changed before synchronization."
            raise InvalidSqlInputError(message)
        cluster = scope.cluster
    sql = apply_query_label(
        "SYSTEM SYNC REPLICA"
        f"{' ON CLUSTER ' + quote_identifier(cluster) if cluster is not None else ''} "
        f"{table or storage.table}",
        query_label,
    )
    with local_sql(connection):
        connection.command(
            sql,
            settings={
                **SYNCHRONOUS_CLUSTER_COMMAND_SETTINGS,
                "distributed_ddl_output_mode": "throw",
                "receive_timeout": SYNCHRONOUS_CLUSTER_COMMAND_SETTINGS[
                    "distributed_ddl_task_timeout"
                ],
            },
        )


__all__ = [
    "require_empty_facade_queues",
    "synchronize_reconfiguration_replicas",
    "synchronize_verified_replicas",
]
