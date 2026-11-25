"""Validate every required deployment before the first statement of a batch."""

# ruff: noqa: SLF001 -- this module implements OutageClient's private batch boundary.

from __future__ import annotations

from typing import Any

from sqlglot import exp, parse_one

from analytics_toolkit.sql.connection.errors import ClickHouseClusterTopologyError
from analytics_toolkit.sql.execution.backend_options import backend_operation

from .cluster_functions import cluster_names
from .outage_passthrough import modeled_statement
from .routing import _LOCAL_SQL_EXECUTION, _identifier_value


def is_facade_create(statement: Any) -> bool:
    engine = statement.find(exp.EngineProperty) if isinstance(statement, exp.Create) else None
    return (
        engine is not None
        and isinstance(engine.this, exp.Anonymous)
        and engine.this.name.lower() == "distributed"
    )


def _statement_clusters(session: Any, sqls: Any) -> tuple[list[Any], list[str], set[str]]:
    parsed = []
    clusters: list[str] = []
    physical: set[str] = set()
    for sql in sqls:
        modeled = modeled_statement(session, sql, parse_one)
        if modeled is None:
            continue
        statement, special = modeled
        parsed.append(statement)
        names = special[1] if special else cluster_names(statement)
        clusters.extend(names)
        if isinstance(statement, exp.Create) and not is_facade_create(statement):
            physical.update(names)
        if is_facade_create(statement):
            engine = statement.find(exp.EngineProperty).this
            cluster = _identifier_value(engine.expressions[0])
            if not cluster:
                message = "Cannot verify the Distributed routing cluster before creation."
                raise ClickHouseClusterTopologyError(message)
            clusters.append(cluster)
    return parsed, clusters, physical


def preflight_statements(client: Any, sqls: Any) -> None:
    from .outage_client import _destructive, _named_target  # noqa: PLC0415 -- client cycle.

    with backend_operation() as state:
        session = client._get_session(state)
        parsed, clusters, physical = _statement_clusters(session, sqls)
        configured = client._config.cluster_routing
        if not clusters and configured is not None and not _LOCAL_SQL_EXECUTION.get():
            clusters.append(configured.cluster)
        destructive = [item for item in parsed if _destructive(item)]
        for cluster in dict.fromkeys(clusters):
            scope = session.scope(cluster)
            requires_full = any(
                not (target is not None and client._owned_private(session, target))
                for item in destructive
                for target in [_named_target(item, client._config.database)]
            )
            if requires_full:
                client._require_execution_hosts(session, scope, "Operation execution hosts")
            elif session.policy == "stop":
                if cluster in physical:
                    scope.require_all(scope.reachable, "Physical creation execution hosts")
                else:
                    scope.require_available(scope.reachable, "Operation execution hosts")
            for statement in destructive:
                client._preflight_destructive_tables(session, statement, cluster)
        for statement in destructive:
            client._check_destructive(session, _named_target(statement, client._config.database))


__all__ = ["is_facade_create", "preflight_statements"]
