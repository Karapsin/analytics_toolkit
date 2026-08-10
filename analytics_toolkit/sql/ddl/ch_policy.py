from __future__ import annotations

from typing import Any

from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.creation_policy import (
    resolve_clickhouse_creation_policy,
)
from analytics_toolkit.sql.connection.ddl_defaults import legacy_clickhouse_scope


def resolve_create_ch_policy(
    config: Any,
    *,
    ddl_scope: str = "regular",
    **overrides: Any,
) -> Any:
    if not get_backend_adapter(config.backend).supports_distributed_table_targets():
        return None
    defaults = getattr(config, "ddl_defaults", None)
    scope = (
        getattr(defaults, ddl_scope)
        if defaults is not None
        else legacy_clickhouse_scope(staging=ddl_scope == "staging")
    )
    overrides.update(
        connection_ddl_ready_timeout_seconds=config.ddl_ready_timeout_seconds,
        connection_ddl_ready_timeout_extension_cnt=getattr(
            config, "ddl_ready_timeout_extension_cnt", None
        ),
        connection_ddl_wait_policy=getattr(config, "ch_ddl_wait_policy", None),
    )
    return resolve_clickhouse_creation_policy(scope, **overrides)


def regular_ddl_properties(config: Any) -> Any:
    defaults = getattr(config, "ddl_defaults", None)
    return None if defaults is None else defaults.regular


__all__ = ["regular_ddl_properties", "resolve_create_ch_policy"]
