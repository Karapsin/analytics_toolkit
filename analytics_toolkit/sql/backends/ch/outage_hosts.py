"""Bounded direct connections used only for selected-host control operations."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator

from analytics_toolkit.sql.connection.errors import ClickHouseClusterTopologyError

from .outage_context import internal_io

if TYPE_CHECKING:
    from .outage_topology import ClusterScope, Host


@contextmanager
def host_connection(scope: ClusterScope, host: Host) -> Iterator[Any]:
    if (
        scope.config.driver != "native"
        and len({item.port for item in scope.hosts if item.name == host.name}) > 1
    ):
        scope.state.preserve_recovery = True
        msg = (
            f"HTTP endpoint {host.name!r} cannot distinguish the configured native instances. "
            "Cleanup is incomplete; recovery artifacts are retained."
        )
        raise ClickHouseClusterTopologyError(msg)
    if host.is_local:
        yield scope.client
        return
    from analytics_toolkit.sql.connection.get_sql_connection import (  # noqa: PLC0415 - factory imports backend
        get_ch_connection_for_host,
    )

    with internal_io():
        connection = get_ch_connection_for_host(
            scope.config.connection_key,
            host.name,
            port=host.port if scope.config.driver == "native" else None,
        )
        try:
            yield connection
        finally:
            from analytics_toolkit.sql.dml.transfer.runtime.retry import (  # noqa: PLC0415
                close_connection_ref,
            )

            close_connection_ref({"connection": connection}, scope.config.connection_key, "host")
