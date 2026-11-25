"""Clear only the resources and execution scopes of the verified existing layout."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.execution.backend_options import preflight_sql_statements
from analytics_toolkit.sql.execution.labels import apply_query_label

from .drop_layout import (
    ResourceInspection,
    VerifiedDropLayout,
    _config,
    _require_managed_relationship,
    inspect_resource,
    require_drop_configuration,
    require_drop_resource,
)
from .reconfigure_execution import cluster_clause
from .routing import local_sql

if TYPE_CHECKING:
    from .layout import ExistingClickHouseLayout


def truncate_existing_layout(
    connection: Any,
    layout: ExistingClickHouseLayout,
    *,
    connection_key: str,
    query_label: str | None = None,
) -> None:
    if layout.create_distributed_pair and not layout.owns_physical:
        message = (
            "Cannot truncate externally owned ClickHouse storage through its facade; "
            "the storage table must be named explicitly."
        )
        raise InvalidSqlInputError(message)
    physical = inspect_resource(
        connection,
        layout.physical_table,
        layout.shard_on_cluster,
        "physical",
        ResourceInspection(operation="TRUNCATE"),
    )
    if physical is None:
        message = "The verified ClickHouse storage disappeared before truncation."
        raise InvalidSqlInputError(message)
    resources = [physical]
    if layout.create_distributed_pair:
        facade = inspect_resource(
            connection,
            layout.logical_table,
            layout.distributed_on_cluster,
            "facade",
            ResourceInspection(operation="TRUNCATE"),
        )
        if facade is None:
            message = "The verified ClickHouse facade disappeared before truncation."
            raise InvalidSqlInputError(message)
        _require_managed_relationship(connection, facade, physical)
        resources.append(facade)
    plan = VerifiedDropLayout(tuple(resources), _config(connection_key))
    statements = [
        apply_query_label(
            f"TRUNCATE TABLE {resource.table}{cluster_clause(resource.cluster)}", query_label
        )
        for resource in resources
    ]
    preflight_sql_statements(connection, statements)
    for resource in resources:
        require_drop_resource(connection, resource, operation="TRUNCATE")
    for resource, statement in zip(resources, statements):
        require_drop_configuration(plan)
        require_drop_resource(connection, resource, operation="TRUNCATE")
        with local_sql(connection):
            connection.command(statement)
