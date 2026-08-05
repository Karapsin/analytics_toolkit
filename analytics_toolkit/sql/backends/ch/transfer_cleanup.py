from __future__ import annotations

from typing import Any


def open_transfer_host_connection(adapter: Any, connection_key: str, host: str) -> Any:
    del adapter
    # Kept lazy because connection setup imports the backend registry, which imports
    # this adapter module while constructing the registry.
    from analytics_toolkit.sql.connection.get_sql_connection import (  # noqa: PLC0415
        get_ch_connection_for_host,
    )

    return get_ch_connection_for_host(connection_key, host)


def needs_bounded_replace_preclear(adapter: Any, only_shard: object) -> bool:
    del adapter
    return not only_shard


def build_creation_policy_cleanup_sqls(
    adapter: Any,
    table_name: str,
    creation_policy: Any,
    *,
    query_label: str | None = None,
    if_exists: bool = True,
) -> list[str]:
    del adapter
    if creation_policy is None:
        return []
    # Kept lazy because lifecycle imports the partially initialized CH adapter.
    from analytics_toolkit.sql.backends.ch.lifecycle import (  # noqa: PLC0415
        build_drop_ch_creation_policy_table_sqls,
    )

    return build_drop_ch_creation_policy_table_sqls(
        table_name,
        creation_policy,
        query_label=query_label,
        if_exists=if_exists,
    )


def preclear_distributed_replace_target(
    adapter: Any,
    table_name: str,
    cluster: str,
    **options: Any,
) -> bool:
    del adapter
    only_shard = bool(options["only_shard"])
    if only_shard:
        return False
    # Kept lazy because lifecycle imports the partially initialized CH adapter.
    from analytics_toolkit.sql.backends.ch.lifecycle import (  # noqa: PLC0415
        drop_ch_distributed_table_pair_bounded,
    )

    drop_ch_distributed_table_pair_bounded(
        table_name,
        cluster,
        query_label=options["query_label"],
        ch_retry_per_host_drops=options["retry_per_host_drops"],
        connection_runner=options["connection_runner"],
        host_connection_runner=options["host_connection_runner"],
    )
    return True
