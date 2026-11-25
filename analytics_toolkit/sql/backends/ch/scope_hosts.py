"""Open one captured native endpoint without weakening its instance proof."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.execution.backend_options import (
    BackendOperationState,
    current_backend_operation,
)

from .outage_hosts import host_connection
from .outage_topology import Host
from .reconfigure_facade_scope import read_rows

if TYPE_CHECKING:
    from collections.abc import Iterator

    from analytics_toolkit.sql.connection.config import ChConfig

    from .reconfigure_facade_scope import FacadeScope

_IDENTITY_FIELDS = 2


@dataclass(frozen=True)
class _VerifiedHostScope:
    config: ChConfig
    client: Any
    hosts: tuple[Host, ...]
    state: BackendOperationState


@contextmanager
def verified_scope_host(
    connection: Any, scope: FacadeScope, config: ChConfig, position: int
) -> Iterator[Any]:
    if get_connection_config(config.connection_key) != config:
        message = "ClickHouse configuration changed during native endpoint validation."
        raise InvalidSqlInputError(message)
    hosts = tuple(
        Host(index, member[0], member[1], member[2], endpoint.native[1], index == scope.coordinator)
        for index, (member, endpoint) in enumerate(zip(scope.membership, scope.endpoints), 1)
    )
    selected = _VerifiedHostScope(
        config, connection, hosts, current_backend_operation() or BackendOperationState()
    )
    with host_connection(selected, hosts[position - 1]) as worker:
        identities = read_rows(worker, "SELECT serverUUID(), tcpPort()")
        expected = scope.identities[position]
        if (
            len(identities) != 1
            or len(identities[0]) != _IDENTITY_FIELDS
            or str(identities[0][0]) != str(expected[0])
            or identities[0][1] != expected[1]
        ):
            message = "The captured native endpoint identity changed before execution."
            raise InvalidSqlInputError(message)
        yield worker


__all__ = ["verified_scope_host"]
