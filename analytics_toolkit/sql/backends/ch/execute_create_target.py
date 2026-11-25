"""Resolve execute-create skip decisions at the exact configured target role."""

from __future__ import annotations

from typing import Any

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.execution.backend_options import backend_operation

from .availability import bind_creation_availability
from .outage_context import internal_io
from .routing import query_local


def execute_create_target_exists(adapter: Any, connection: Any, options: Any) -> bool:
    """Check the named logical target without adopting a deterministic companion."""
    del adapter
    bind_creation_availability(connection, options.ch_creation_policy)
    with internal_io():
        result = query_local(connection, f"EXISTS TABLE {options.table_name}")
    rows = result.result_rows
    if rows and rows[0][0]:
        return True
    policy = options.ch_creation_policy
    cluster = (
        policy.distributed_on_cluster
        if policy.create_distributed_pair and not options.ch_only_shard
        else policy.shard_on_cluster
    )
    if cluster is None:
        return False
    # Outage metadata imports creation policies while the adapter registry initializes.
    from analytics_toolkit.sql.connection.config import get_connection_config  # noqa: PLC0415

    from .outage_session import OutageSession  # noqa: PLC0415
    from .outage_topology import query_rows, warn_degraded  # noqa: PLC0415
    from .reconfigure_support import unquoted_table_name  # noqa: PLC0415

    database, relation = unquoted_table_name(options.table_name)
    with backend_operation() as state:
        session = (
            connection._get_session(state)  # noqa: SLF001 - Reuse this backend's operation session.
            if getattr(connection, "_ch_outage_client", False) is True
            else OutageSession(connection, get_connection_config(options.connection_key), state)
        )
        if database is None:
            rows = query_rows(session.client, "SELECT currentDatabase()")
            if len(rows) != 1 or len(rows[0]) != 1 or not rows[0][0]:
                message = "Cannot verify the current database for the ClickHouse creation target."
                raise InvalidSqlInputError(message)
            database = str(rows[0][0])
        scope = session.scope(cluster)
        status = scope.record(scope.reachable, f"{database}.{relation}")
        if session.policy == "stop":
            scope.require_available(scope.reachable, "Creation target preflight")
        elif not status["complete"]:
            warn_degraded(state, session.config, status, "check creation target")
        return bool(scope.objects(database, relation))


__all__ = ["execute_create_target_exists"]
