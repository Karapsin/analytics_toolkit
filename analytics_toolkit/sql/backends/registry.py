from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from .base import (
    BackendAdapter,
    BackendCapability,
    BackendName,
    UNSUPPORTED_BACKEND_MESSAGE,
)
from .ch import ClickHouseAdapter
from .gp import GreenplumAdapter
from .trino import TrinoAdapter


BACKEND_ALIASES: Mapping[str, BackendName] = {
    "greenplum": "gp",
    "postgres": "gp",
    "postgresql": "gp",
    "presto": "trino",
    "clickhouse": "ch",
    "clickhouse-connect": "ch",
    "clickhouse_connect": "ch",
}

BACKEND_REGISTRY: dict[BackendName, BackendAdapter] = {
    "gp": GreenplumAdapter(),
    "trino": TrinoAdapter(),
    "ch": ClickHouseAdapter(),
}

BACKEND_ADAPTERS = BACKEND_REGISTRY
SUPPORTED_BACKENDS = set(BACKEND_REGISTRY)


def normalize_backend_name(raw_backend: BackendName | str) -> BackendName:
    backend = str(raw_backend).strip().lower()
    normalized = BACKEND_ALIASES.get(backend, backend)
    if normalized not in BACKEND_REGISTRY:
        from ..connection.errors import UnsupportedConnectionTypeError

        expected = ", ".join(sorted(BACKEND_REGISTRY))
        raise UnsupportedConnectionTypeError(
            f"Unsupported SQL connection backend {raw_backend!r}. "
            f"Expected one of: {expected}."
        )
    return cast(BackendName, normalized)


def require_backend_name(
    raw_backend: str,
    *,
    connection_key: str,
) -> BackendName:
    backend = raw_backend.strip().lower()
    if backend not in BACKEND_REGISTRY:
        from ..connection.errors import UnsupportedConnectionTypeError

        expected = ", ".join(sorted(BACKEND_REGISTRY))
        raise UnsupportedConnectionTypeError(
            f"SQL connection '{connection_key}' has unsupported type {backend!r}. "
            f"Expected one of: {expected}."
        )
    return cast(BackendName, backend)


def get_backend(name_or_key: str) -> BackendAdapter:
    normalized = str(name_or_key).strip().lower()
    backend_name = BACKEND_ALIASES.get(normalized, normalized)
    if backend_name in BACKEND_REGISTRY:
        return BACKEND_REGISTRY[cast(BackendName, backend_name)]

    try:
        from ..connection.config import get_connection_backend

        return BACKEND_REGISTRY[get_connection_backend(name_or_key)]
    except Exception as exc:
        from ..connection.errors import UnsupportedConnectionTypeError

        if isinstance(exc, UnsupportedConnectionTypeError):
            raise
        raise UnsupportedConnectionTypeError(UNSUPPORTED_BACKEND_MESSAGE) from exc


def get_backend_adapter(name_or_key: str) -> BackendAdapter:
    return get_backend(name_or_key)


def get_backend_names() -> tuple[BackendName, ...]:
    return tuple(BACKEND_REGISTRY)


def get_backend_capability(name_or_key: str) -> BackendCapability:
    return get_backend(name_or_key).capability


def backend_capability_map() -> dict[BackendName, BackendCapability]:
    return {
        backend_name: backend.capability
        for backend_name, backend in BACKEND_REGISTRY.items()
    }


def supported_backend_message() -> str:
    return "Expected one of: " + ", ".join(sorted(BACKEND_REGISTRY)) + "."
