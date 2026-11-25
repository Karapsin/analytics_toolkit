"""Bounded direct connections used only for selected-host control operations."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator, Protocol

from analytics_toolkit.sql.connection.errors import ClickHouseClusterTopologyError
from analytics_toolkit.sql.execution.retry_cleanup import cleanup_preserving_error

from .outage_context import internal_io

if TYPE_CHECKING:
    from collections.abc import Sequence

    from analytics_toolkit.sql.execution.backend_options import BackendOperationState

    from .outage_topology import Host


class HostConnectionScope(Protocol):
    @property
    def config(self) -> Any: ...

    @property
    def client(self) -> Any: ...

    @property
    def hosts(self) -> Sequence[Host]: ...

    @property
    def state(self) -> BackendOperationState: ...


@contextmanager
def host_connection(scope: HostConnectionScope, host: Host) -> Iterator[Any]:
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
        error: BaseException | None = None
        try:
            yield connection
        except BaseException as exc:
            error = exc
            raise
        finally:
            cleanup_preserving_error(connection.close, error=error, phase="close_host")
