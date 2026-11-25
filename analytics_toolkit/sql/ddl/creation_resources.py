"""Immutable acknowledgements for one created object in its actual scope."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


@dataclass(frozen=True)
class CreationScope:
    kind: Literal["local", "cluster", "host"] = "local"
    cluster: str | None = None
    endpoint: tuple[str, int] | None = None


@dataclass(frozen=True)
class CreatedTableResource:
    name: str
    backend: str
    scope: CreationScope = field(default_factory=CreationScope)
    connection_key: str | None = None
    configuration_digest: str = field(default="", repr=False)
    certificate: Any = field(default=None, repr=False)


class CreatedResourceChangedError(InvalidSqlInputError):
    """Retain a resource when its acknowledged object can no longer be proved."""


@dataclass(frozen=True)
class SkippedCreation:
    """A backend proved that this candidate had no physical CREATE submission."""

    resource: CreatedTableResource


_SCOPED_OBSERVER: ContextVar[Any] = ContextVar("sql_scoped_creation_observer", default=None)
_SCOPED_RESOURCE: ContextVar[CreatedTableResource | None] = ContextVar(
    "sql_scoped_creation_resource", default=None
)


def current_scoped_creation_resource() -> CreatedTableResource | None:
    return _SCOPED_RESOURCE.get()


@contextmanager
def submitted_creation_resource(resource: CreatedTableResource) -> Iterator[None]:
    token = _SCOPED_RESOURCE.set(resource)
    try:
        yield
    finally:
        _SCOPED_RESOURCE.reset(token)


def scoped_creation_count() -> int:
    observer = _SCOPED_OBSERVER.get()
    return 0 if observer is None else len(observer[1])


def skipped_creation_count() -> int:
    observer = _SCOPED_OBSERVER.get()
    return 0 if observer is None else len(observer[2])


def record_skipped_creation(outcome: SkippedCreation) -> bool:
    """Keep an explicit backend skip separate from physical acknowledgements."""
    observer = _SCOPED_OBSERVER.get()
    if observer is None:
        return False
    if current_scoped_creation_resource() != outcome.resource:
        message = "A skipped creation outcome does not match the submitted candidate."
        raise InvalidSqlInputError(message)
    observer[2].append(outcome)
    return True


@contextmanager
def scoped_creation_observer(
    callback: Callable[[CreatedTableResource], None],
) -> Iterator[list[CreatedTableResource]]:
    records: list[CreatedTableResource] = []
    token = _SCOPED_OBSERVER.set((callback, records, []))
    try:
        yield records
    finally:
        _SCOPED_OBSERVER.reset(token)


def record_created_table(resource: CreatedTableResource) -> bool:
    """Called by a backend immediately after a physical CREATE acknowledgement."""
    observer = _SCOPED_OBSERVER.get()
    if observer is None:
        return False
    callback, records, _skipped = observer
    records.append(resource)
    callback(resource)
    return True


def execute_scoped_create(
    adapter: Any, connection: Any, sql: str, resource: CreatedTableResource
) -> None:
    """Let the backend report physical acknowledgements before readiness waits."""
    from analytics_toolkit.sql.backends.creation_resources import (  # noqa: PLC0415 -- backend capability dispatch after registry initialization.
        execute_scoped_create as execute_backend_create,
    )

    execute_backend_create(adapter, connection, sql, resource)


def configuration_digest(connection_key: str | None) -> str:
    if connection_key is None:
        return ""
    from analytics_toolkit.sql.connection.config import get_connection_config  # noqa: PLC0415
    from analytics_toolkit.sql.dml.transfer.flow.adaptive_input import (  # noqa: PLC0415 -- shared canonical configuration identity.
        configuration_digest_from_configs,
    )

    return configuration_digest_from_configs([get_connection_config(connection_key)])


def require_resource_configuration(resource: CreatedTableResource) -> None:
    if resource.configuration_digest != configuration_digest(resource.connection_key):
        message = f"Retaining created resource {resource.name}: connection identity changed."
        raise CreatedResourceChangedError(message)


def resource_retention_message(resource: CreatedTableResource) -> str:
    scope = resource.scope
    location: str = scope.kind
    if scope.cluster is not None:
        location += f" cluster={scope.cluster}"
    if scope.endpoint is not None:
        location += f" endpoint={scope.endpoint[0]}:{scope.endpoint[1]}"
    return f"Retaining acknowledged resource {resource.name} ({location}); ownership is unverified."
