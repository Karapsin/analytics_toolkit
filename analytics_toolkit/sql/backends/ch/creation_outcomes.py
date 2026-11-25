"""Prove an optional deployment was skipped without claiming native ownership."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlglot import exp, parse_one

from analytics_toolkit.sql.connection.errors import ClickHouseClusterTopologyError
from analytics_toolkit.sql.ddl.creation_resources import (
    SkippedCreation,
    current_scoped_creation_resource,
    record_skipped_creation,
)

from .dialect import ClickHouseDialect
from .managed_routing import _resolve_cluster_name


@dataclass(frozen=True)
class SkippedOptionalFacadeCreation(SkippedCreation):
    cluster: str


def record_skipped_optional_facade(scope: Any, database: str, table: str) -> None:
    """Only a verified empty advisory deployment may suppress nominal capture."""
    resource = current_scoped_creation_resource()
    if resource is None:
        return
    target = parse_one(resource.name, read=ClickHouseDialect, into=exp.Table)
    valid = (
        scope.availability_optional
        and not scope.reachable
        and resource.backend == "ch"
        and resource.connection_key == scope.config.connection_key
        and resource.scope.kind == "cluster"
        and isinstance(resource.scope.cluster, str)
        and _resolve_cluster_name(scope.client, resource.scope.cluster) == scope.cluster
        and (target.db or scope.config.database, target.name) == (database, table)
    )
    if not valid:
        message = "Cannot certify the exact optional facade creation that was skipped."
        raise ClickHouseClusterTopologyError(message)
    record_skipped_creation(SkippedOptionalFacadeCreation(resource, scope.cluster))


__all__ = ["SkippedOptionalFacadeCreation", "record_skipped_optional_facade"]
