from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from analytics_toolkit.sql.backends import get_backend
from analytics_toolkit.sql.backends.ch.creation_policy import (
    resolve_clickhouse_creation_policy,
)
from analytics_toolkit.sql.connection.ddl_defaults import legacy_clickhouse_scope


@dataclass(frozen=True)
class OperationDdlDefaults:
    regular_properties: Mapping[str, Any] | None
    staging_properties: Mapping[str, Any] | None
    parquet_properties: Mapping[str, Any] | None
    regular_ch_policy: Any
    staging_ch_policy: Any


def resolve_operation_ddl(config: Any, **clickhouse_overrides: Any) -> OperationDdlDefaults:
    defaults = getattr(config, "ddl_defaults", None)
    if not get_backend(config.backend).supports_distributed_table_targets():
        if clickhouse_overrides.get("ch_ddl_wait_policy") is not None:
            message = "ch_ddl_wait_policy requires a ClickHouse target."
            raise ValueError(message)
        return OperationDdlDefaults(
            getattr(defaults, "regular", None),
            getattr(defaults, "staging", None),
            getattr(defaults, "parquet_staging", None),
            None,
            None,
        )
    regular = defaults.regular if defaults is not None else legacy_clickhouse_scope()
    staging = defaults.staging if defaults is not None else legacy_clickhouse_scope(staging=True)
    connection_ready_timeout = getattr(config, "ddl_ready_timeout_seconds", None)
    connection_ready_extension_cnt = getattr(
        config,
        "ddl_ready_timeout_extension_cnt",
        None,
    )
    connection_wait_policy = getattr(config, "ch_ddl_wait_policy", None)
    return OperationDdlDefaults(
        None,
        None,
        None,
        resolve_clickhouse_creation_policy(
            regular,
            connection_ddl_ready_timeout_seconds=connection_ready_timeout,
            connection_ddl_ready_timeout_extension_cnt=connection_ready_extension_cnt,
            connection_ddl_wait_policy=connection_wait_policy,
            **clickhouse_overrides,
        ),
        resolve_clickhouse_creation_policy(
            staging,
            ch_engine=None,
            ch_cluster=None,
            ch_sharding_key=None,
            ch_distributed_table=None,
            ch_only_shard=False,
            ch_distributed_engine_template=None,
            ch_distributed_cluster=None,
            ch_shard_on_cluster=None,
            ch_distributed_on_cluster=None,
            connection_ddl_ready_timeout_seconds=connection_ready_timeout,
            connection_ddl_ready_timeout_extension_cnt=connection_ready_extension_cnt,
            connection_ddl_wait_policy=connection_wait_policy,
        ),
    )
