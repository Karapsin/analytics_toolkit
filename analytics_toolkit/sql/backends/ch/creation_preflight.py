"""Authorize replacement DDL against verified existing storage before submission."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError, SqlConfigError

from .ddl import build_ch_shard_table_name
from .layout import inspect_existing_layout
from .reconfigure_support import resolve_optional_cluster, table_exists_on_cluster
from .reconfigure_topology import endpoints
from .replace_layout import prepare_replacement_layout


def preflight_create_table_replacement(adapter: Any, connection: Any, options: Any) -> None:
    if not options.ch_replace_table:
        return
    policy = options.ch_creation_policy
    if policy is None:
        message = "ClickHouse replacement requires a resolved creation policy."
        raise SqlConfigError(message)
    exists = adapter.table_exists(
        connection, options.table_name, connection_key=options.connection_key
    )
    layout = (
        inspect_existing_layout(
            connection, options.table_name, distributed_on_cluster=policy.distributed_on_cluster
        )
        if exists
        else None
    )
    if layout is not None and not layout.owns_physical:
        message = (
            "Cannot structurally replace a facade whose referenced storage is externally owned."
        )
        raise InvalidSqlInputError(message)
    desired_pair = policy.create_distributed_pair and not options.ch_only_shard
    same_storage = layout is not None and _same_storage_scope(
        connection, layout.shard_on_cluster, policy.shard_on_cluster
    )
    if desired_pair and (layout is None or not layout.create_distributed_pair or not same_storage):
        _reject_companion_collision(adapter, connection, options)
    if layout is not None and layout.create_distributed_pair and desired_pair and not same_storage:
        message = (
            "Physical placement differs from the verified existing storage. "
            "Use ch_reconfigure_table to migrate the verified layout before replacing its schema."
        )
        raise InvalidSqlInputError(message)
    if layout is not None and layout.create_distributed_pair and desired_pair:
        same_facade_scope = _same_storage_scope(
            connection, layout.distributed_on_cluster, policy.distributed_on_cluster
        )
        if not same_facade_scope:
            _reject_facade_collision(connection, options)
            if layout.distributed_on_cluster is not None:
                message = (
                    "Facade placement differs from its verified existing scope. "
                    "Reconfigure that layout before replacing its schema."
                )
                raise InvalidSqlInputError(message)
    if layout is not None and layout.create_distributed_pair != desired_pair:
        prepare_replacement_layout(
            adapter,
            SimpleNamespace(
                connection=connection,
                target_table=options.table_name,
                connection_key=options.connection_key,
                query_label=options.query_label,
                ch_only_shard=options.ch_only_shard,
                ch_creation_policy=policy,
            ),
        )


def _reject_companion_collision(adapter: Any, connection: Any, options: Any) -> None:
    companion = build_ch_shard_table_name(options.table_name)
    cluster = resolve_optional_cluster(connection, options.ch_creation_policy.shard_on_cluster)
    exists = (
        table_exists_on_cluster(connection, companion, cluster)
        if cluster is not None
        else adapter.table_exists(connection, companion, connection_key=options.connection_key)
    )
    if exists:
        message = (
            f"Destination companion {companion} already exists; no replacement DDL was submitted."
        )
        raise InvalidSqlInputError(message)


def _same_storage_scope(connection: Any, original: str | None, desired: str | None) -> bool:
    original = resolve_optional_cluster(connection, original)
    desired = resolve_optional_cluster(connection, desired)
    if original == desired:
        return True
    if original is None or desired is None:
        return False
    return endpoints(connection, original) == endpoints(connection, desired)


def _reject_facade_collision(connection: Any, options: Any) -> None:
    cluster = resolve_optional_cluster(
        connection, options.ch_creation_policy.distributed_on_cluster
    )
    if cluster is not None and table_exists_on_cluster(connection, options.table_name, cluster):
        message = (
            "The desired facade scope already contains an unowned target; no DDL was submitted."
        )
        raise InvalidSqlInputError(message)


__all__ = ["preflight_create_table_replacement"]
