from __future__ import annotations

# ruff: noqa: EM101, TC001, TRY003
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError, SqlConfigError

from .creation_policy import resolve_clickhouse_creation_policy
from .reconfigure_models import ChReconfigureOptions

if TYPE_CHECKING:
    from .creation_policy import ClickHouseCreationPolicy


@dataclass(frozen=True)
class DesiredReconfigurePolicy:
    create_distributed_pair: bool
    shard_engine: str
    shard_on_cluster: str | None
    distributed_engine_template: str | None
    distributed_cluster: str | None
    distributed_on_cluster: str | None
    sharding_key: str | None


def resolve_desired_reconfigure_policy(
    options: ChReconfigureOptions,
    *,
    source_pair: bool,
    source_shard_engine: str,
    source_shard_cluster: str | None,
    source_distributed_cluster: str | None,
) -> DesiredReconfigurePolicy:
    requested_pair = (
        source_pair if options.ch_distributed_table is None else options.ch_distributed_table
    )
    needs_creation_defaults = options.to_defaults or (requested_pair and not source_pair)
    configured = None
    if needs_creation_defaults:
        if options.regular_defaults is None:
            raise SqlConfigError(
                "ClickHouse regular ddl_defaults are required for to_defaults or "
                "local-to-pair conversion."
            )
        configured = resolve_clickhouse_creation_policy(
            options.regular_defaults,
            ch_engine=options.ch_engine,
            ch_cluster=None,
            ch_sharding_key=options.ch_sharding_key,
            ch_distributed_table=options.ch_distributed_table,
            ch_only_shard=False,
            ch_distributed_engine_template=options.ch_distributed_engine_template,
            ch_distributed_cluster=options.ch_distributed_cluster,
            ch_shard_on_cluster=options.ch_shard_on_cluster,
            ch_distributed_on_cluster=options.ch_distributed_on_cluster,
            warn_ch_cluster=False,
        )

    if options.to_defaults:
        configured = cast("ClickHouseCreationPolicy", configured)
        target_pair = configured.create_distributed_pair
    else:
        target_pair = requested_pair
    shard_engine = (
        configured.shard_engine
        if options.to_defaults and configured is not None
        else (options.ch_engine or source_shard_engine)
    )
    shard_cluster = (
        configured.shard_on_cluster
        if options.to_defaults and configured is not None
        else (options.ch_shard_on_cluster or source_shard_cluster)
    )

    if configured is not None and (options.to_defaults or not source_pair):
        template = configured.distributed_engine_template
        routing = configured.distributed_cluster
        facade_cluster = configured.distributed_on_cluster
        sharding_key = configured.sharding_key
    else:
        template = options.ch_distributed_engine_template
        routing = options.ch_distributed_cluster or source_distributed_cluster
        facade_cluster = options.ch_distributed_on_cluster
        sharding_key = options.ch_sharding_key

    if target_pair and not routing:
        raise InvalidSqlInputError(
            "A managed Distributed table requires ch_distributed_cluster or "
            "ddl_defaults.regular.distributed.cluster."
        )
    return DesiredReconfigurePolicy(
        create_distributed_pair=target_pair,
        shard_engine=shard_engine,
        shard_on_cluster=shard_cluster,
        distributed_engine_template=template,
        distributed_cluster=routing,
        distributed_on_cluster=facade_cluster,
        sharding_key=sharding_key,
    )


__all__ = ["DesiredReconfigurePolicy", "resolve_desired_reconfigure_policy"]
