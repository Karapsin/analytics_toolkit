"""Drop verified policy resources with independent placement and bounded recovery."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from sqlglot import exp, parse_one

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.execution.backend_options import current_backend_query_label

from .dialect import ClickHouseDialect
from .drop_execution import DropExecutionOptions, execute_verified_drop
from .drop_layout import VerifiedDropLayout, certify_explicit_drop

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True)
class DropResource:
    table: str
    cluster: str | None


def execute_policy_target_drop(
    adapter: Any,
    connection: Any,
    drop_sqls: Sequence[str],
    *,
    target_key: str,
    retry_per_host_drops: bool = True,
) -> None:
    if not drop_sqls:
        return
    plan = certify_drop_statements(connection, drop_sqls, target_key=target_key)
    execute_verified_drop(
        adapter,
        connection,
        plan,
        DropExecutionOptions(
            wait_for_absence=True,
            retry_per_host_drops=retry_per_host_drops,
            query_label=current_backend_query_label(),
        ),
    )


def certify_drop_statements(
    connection: Any, drop_sqls: Sequence[str], *, target_key: str
) -> VerifiedDropLayout:
    """Bind every explicit statement to actual complete resource identities before mutation."""
    resources = list(dict.fromkeys(_drop_resource(sql) for sql in drop_sqls))
    exists_by_resource = {
        resource: all(
            bool(parse_one(sql, read=ClickHouseDialect).args.get("exists"))
            for sql in drop_sqls
            if _drop_resource(sql) == resource
        )
        for resource in resources
    }
    certified = [
        certify_explicit_drop(
            connection,
            resource.table,
            resource.cluster,
            connection_key=target_key,
            if_exists=exists_by_resource[resource],
        )
        for resource in resources
    ]
    return VerifiedDropLayout(
        tuple(
            replace(selected, if_exists=exists_by_resource[requested])
            for requested, certificate in zip(resources, certified)
            for selected in certificate.resources
        ),
        certified[0].config,
    )


def _drop_resource(sql: str) -> DropResource:
    tree = parse_one(sql, read=ClickHouseDialect)
    tables = tree.args.get("tables") or [tree.this]
    if (
        not isinstance(tree, exp.Drop)
        or str(tree.args.get("kind", "")).upper() != "TABLE"
        or len(tables) != 1
        or not isinstance(tables[0], exp.Table)
        or not isinstance(tables[0].this, exp.Identifier)
    ):
        message = "Policy cleanup requires one static DROP TABLE resource per statement."
        raise InvalidSqlInputError(message)
    cluster = tree.args.get("cluster")
    if cluster is not None and (
        not isinstance(cluster, exp.OnCluster)
        or not isinstance(cluster.this, (exp.Literal, exp.Identifier))
    ):
        message = "Policy cleanup requires a static cluster identifier."
        raise InvalidSqlInputError(message)
    return DropResource(
        tables[0].sql(dialect="clickhouse"),
        cluster.this.name if cluster is not None else None,
    )


__all__ = ["execute_policy_target_drop"]
