"""Prove absence before claiming disposable stages or accepting a new target."""

from __future__ import annotations

from typing import Any

from sqlglot import exp, parse_one

from analytics_toolkit.sql.connection.errors import ClickHouseClusterTopologyError

from .outage_session import private_table
from .routing import _RoutingClickHouse


def claim_private_create(
    session: Any, statement: exp.Expr, target: tuple[str, str] | None, clusters: list[str]
) -> None:
    if not isinstance(statement, exp.Create) or target is None or not private_table(target[1]):
        return
    key = ("ch_owned", session.config.connection_key, target)
    if session.state.resources.get(key):
        return
    database, table = target
    existing = (
        any(session.scope(cluster).objects(database, table) for cluster in clusters)
        if clusters
        else bool(session.local_objects(database, table))
    )
    if not existing:
        # Reserve a verified-absent, uniquely named private table before submission.
        # Lost CREATE acknowledgements must not turn disposable data into live data.
        session.state.resources[key] = True
        if not clusters:
            session.state.resources[("ch_owned_local", session.config.connection_key, target)] = (
                True
            )


def assert_new_target(
    session: Any, table_name: str, *, policy: Any, cluster: str | None, only_shard: bool
) -> None:
    from .ddl import build_ch_shard_table_name  # noqa: PLC0415 - DDL imports adapter

    if policy is None:
        from analytics_toolkit.sql.ddl.ch_policy import (  # noqa: PLC0415 - DDL imports adapter
            resolve_create_ch_policy,
        )

        policy = resolve_create_ch_policy(
            session.config,
            ch_engine=None,
            ch_cluster=cluster,
            ch_sharding_key=None,
            ch_distributed_table=not only_shard,
            ch_only_shard=only_shard,
            ch_distributed_engine_template=None,
            ch_distributed_cluster=None,
            warn_ch_cluster=False,
        )
    roles = [(table_name, policy.distributed_on_cluster)] if policy.create_distributed_pair else []
    roles.append(
        (
            build_ch_shard_table_name(table_name) if policy.create_distributed_pair else table_name,
            policy.shard_on_cluster,
        )
    )
    for name, creation_cluster in roles:
        table = parse_one(name, into=exp.Table, read=_RoutingClickHouse)
        database = table.db or session.config.database
        if not database:
            msg = "A database is required to prove a target is new."
            raise ClickHouseClusterTopologyError(msg)
        if creation_cluster is None:
            objects = session.local_objects(database, table.name)
        else:
            scope = session.scope(creation_cluster)
            if session.policy == "stop":
                scope.require_all(scope.reachable, "New target preflight")
            objects = scope.objects(database, table.name)
        if objects:
            msg = (
                f"Target {table_name} is not genuinely new: {name} already exists in its "
                "creation scope. Reconcile the existing or partial table pair before loading."
            )
            raise ClickHouseClusterTopologyError(msg)
