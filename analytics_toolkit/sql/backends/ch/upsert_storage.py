"""Bind verified target storage without changing independently placed upsert stages."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from sqlglot import exp, parse_one

from analytics_toolkit.sql.connection.errors import ClickHouseClusterTopologyError

from .dialect import ClickHouseDialect
from .managed_routing import ManagedPairResolver, ManagedTableRoute

if TYPE_CHECKING:
    from collections.abc import Iterator

    from .layout import ExistingClickHouseLayout


@dataclass(frozen=True)
class VerifiedUpsertStorage:
    connection: object
    layout: ExistingClickHouseLayout
    logical: tuple[str, str]
    physical: tuple[str, str]
    sharding_key: str | None

    def matches(self, database: str, table: str) -> bool:
        return (database, table) in {self.logical, self.physical}


def _identifier(table_name: str, default_database: str = "") -> tuple[str, str]:
    table = parse_one(table_name, read=ClickHouseDialect, into=exp.Table)
    if not isinstance(table.this, exp.Identifier) or table.catalog:
        message = "Verified upsert storage requires static database/table identifiers."
        raise ValueError(message)
    return table.db or default_database, table.name


_STORAGE: ContextVar[VerifiedUpsertStorage | None] = ContextVar(
    "analytics_toolkit_verified_upsert_storage", default=None
)


@contextmanager
def verified_upsert_storage(
    connection: object, layout: ExistingClickHouseLayout, *, sharding_key: str | None = None
) -> Iterator[None]:
    physical = _identifier(layout.physical_table)
    logical = _identifier(layout.logical_table, physical[0])
    token = _STORAGE.set(VerifiedUpsertStorage(connection, layout, logical, physical, sharding_key))
    try:
        yield
    finally:
        _STORAGE.reset(token)


def current_upsert_storage(connection: object) -> VerifiedUpsertStorage | None:
    binding = _STORAGE.get()
    return binding if binding is not None and binding.connection is connection else None


class _StorageResolver(ManagedPairResolver):
    def __init__(
        self, resolver: ManagedPairResolver, binding: VerifiedUpsertStorage, sharding_key: str
    ) -> None:
        self._resolver = resolver
        self._binding = binding
        self._sharding_key = sharding_key

    def invalidate(self) -> None:
        self._resolver.invalidate()

    def resolve(self, *, cluster: str, database: str, table: str) -> ManagedTableRoute:
        binding = self._binding
        if not binding.matches(database, table):
            return self._resolver.resolve(cluster=cluster, database=database, table=table)
        database, table = binding.physical
        scope = binding.layout.shard_on_cluster
        if scope is None:
            return ManagedTableRoute(database, table, "local")
        resolve = getattr(self._resolver, "resolve_physical", self._resolver.resolve)
        resolved = resolve(cluster=scope, database=database, table=table)
        if resolved.mode == "local":
            message = "Verified upsert physical storage requires complete routed coverage."
            raise ClickHouseClusterTopologyError(message)
        return replace(
            resolved,
            cluster=scope,
            sharding_key=(
                binding.sharding_key
                if binding.layout.create_distributed_pair
                else binding.sharding_key or resolved.sharding_key or self._sharding_key
            ),
        )


def upsert_storage_resolver(
    connection: object, resolver: ManagedPairResolver, sharding_key: str
) -> ManagedPairResolver:
    binding = current_upsert_storage(connection)
    return _StorageResolver(resolver, binding, sharding_key) if binding is not None else resolver


def upsert_write_scope(
    connection: object, statement: exp.Expr, routing: Any, target: tuple[str, str] | None
) -> tuple[Any, tuple[str, str] | None]:
    """Select actual destination availability checks after source SQL was routed separately."""
    from .routing import ChClusterRouting  # noqa: PLC0415 -- routing consumes this module.

    binding = current_upsert_storage(connection)
    if (
        binding is None
        or not isinstance(statement, exp.Insert)
        or target is None
        or not binding.matches(*target)
    ):
        return routing, target
    cluster = binding.layout.shard_on_cluster
    if cluster is None:
        return None, binding.physical
    return ChClusterRouting(cluster, binding.sharding_key or "rand()"), binding.physical


def apply_upsert_write_key(
    connection: object, session: Any, target: tuple[str, str] | None
) -> None:
    binding = current_upsert_storage(connection)
    if (
        binding is not None
        and binding.layout.create_distributed_pair
        and target is not None
        and binding.matches(*target)
    ):
        session.insert_key = binding.sharding_key
