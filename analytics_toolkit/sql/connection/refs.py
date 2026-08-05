from __future__ import annotations

from typing import Any


def ensure_connection_ref(
    connection_key: str | None,
    connection_ref: dict[str, Any],
) -> Any:
    """Ensure a bounded ref emptied by a failed replacement is usable again."""
    ensure_connection = connection_ref.get("bounded_ensure_connection")
    if callable(ensure_connection) and connection_key is not None:
        ensure_connection(connection_key, connection_ref)
    return connection_ref["connection"]
