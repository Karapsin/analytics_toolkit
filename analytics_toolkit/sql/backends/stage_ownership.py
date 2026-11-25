"""Cleanup only individually acknowledged, run-owned stage resources."""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from copy import copy
from dataclasses import is_dataclass, replace
from typing import Any

from analytics_toolkit.sql.ddl.creation_resources import (
    CreatedResourceChangedError,
    CreatedTableResource,
    resource_retention_message,
)


def single_resource_policy(policy: Any) -> Any:
    """Snapshot stages never own a Distributed companion."""
    if policy is None or not getattr(policy, "create_distributed_pair", False):
        return policy
    if is_dataclass(policy) and not isinstance(policy, type):
        return replace(policy, create_distributed_pair=False)
    scoped = copy(policy)
    scoped.create_distributed_pair = False
    return scoped


def drop_owned_resource(
    adapter: Any,
    connection: Any,
    resource: str | CreatedTableResource,
    *,
    logical_stage: str,
    creation_policy: Any = None,
) -> None:
    if isinstance(resource, CreatedTableResource):
        if resource.backend != adapter.backend:
            raise CreatedResourceChangedError(resource_retention_message(resource))
        if resource.backend == "gp":
            from .gp.creation_identity import drop_created_resource  # noqa: PLC0415

            drop_created_resource(adapter, connection, resource)
            return
        if resource.backend == "ch":
            from .ch.created_resources import drop_created_resource  # noqa: PLC0415

            drop_created_resource(adapter, connection, resource)
            return
        raise CreatedResourceChangedError(resource_retention_message(resource))
    from analytics_toolkit.sql.core.identifiers import (  # noqa: PLC0415 -- registry cycle.
        TableIdentifier,
    )

    identifier = TableIdentifier.parse(resource, adapter.backend)
    if adapter.backend in {"gp", "trino"}:
        identifier = TableIdentifier(
            tuple(
                part if quoted else part.lower()
                for part, quoted in zip(identifier.parts, identifier.quoted)
            ),
            identifier.quoted,
        )
    table = identifier.render_quoted(adapter.backend)
    scope = ""
    if adapter.backend == "ch" and creation_policy is not None:
        from .ch.ddl import _format_ch_cluster_name  # noqa: PLC0415 -- backend registry cycle.

        logical = TableIdentifier.parse(logical_stage, adapter.backend)
        is_facade = identifier.parts == logical.parts and creation_policy.create_distributed_pair
        cluster = (
            creation_policy.distributed_on_cluster
            if is_facade
            else creation_policy.shard_on_cluster
        )
        if cluster is not None:
            scope = " ON CLUSTER " + _format_ch_cluster_name(cluster)
    context: AbstractContextManager[Any] = nullcontext()
    if adapter.backend == "ch" and not scope:
        from .ch.routing import local_sql  # noqa: PLC0415 -- backend registry cycle.

        context = local_sql(connection)
    with context:
        adapter.execute_command(connection, f"DROP TABLE IF EXISTS {table}{scope}")
