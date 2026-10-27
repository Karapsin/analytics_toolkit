"""Bounded direct connections used only for selected-host control operations."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator

from .outage_context import internal_io

if TYPE_CHECKING:
    from .outage_topology import ClusterScope, Host


@contextmanager
def host_connection(scope: ClusterScope, host: Host) -> Iterator[Any]:
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
            connection.close()
