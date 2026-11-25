"""Resolve prospective server type aliases without creating a destination table."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping


def resolve_prospective_schema(connection: Any, schema: Mapping[str, str]) -> dict[str, str]:
    """Ask the destination for canonical native type names for prospective declarations."""
    from .client_validation import (  # noqa: PLC0415 -- preserve one deterministic error contract.
        ClientWriteValidationError,
    )
    from .key_literals import _string  # noqa: PLC0415 -- backend-owned literal escaping.
    from .routing import query_local  # noqa: PLC0415 -- introspection stays on the coordinator.

    if not schema:
        return {}
    expressions = [
        f"toTypeName(defaultValueOfTypeName({_string(native)}))" for native in schema.values()
    ]
    result = query_local(connection, "SELECT " + ", ".join(expressions))
    rows = getattr(result, "result_rows", None)
    if (
        not isinstance(rows, (list, tuple))
        or len(rows) != 1
        or not isinstance(rows[0], (list, tuple))
        or len(rows[0]) != len(schema)
        or any(not isinstance(native, str) or not native.strip() for native in rows[0])
    ):
        message = "Cannot verify prospective ClickHouse writer types from destination metadata."
        raise ClientWriteValidationError(message)
    return dict(zip(schema, rows[0]))
