"""Greenplum-native metadata retains arrays and intentional JSON declarations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from analytics_toolkit.sql.backends.models import SourceColumn


def map_same_backend_source_type_to_target(adapter: Any, column: SourceColumn) -> str:
    from .type_identity import canonical_builtin_type  # noqa: PLC0415 -- adapter initialization.

    native = (column.native_type or "").strip()
    if (
        native.endswith("[]")
        or canonical_builtin_type(native) is not None
        or native.lower() == "cidr"
    ):
        return native
    return str(adapter.map_source_type_to_target(column))
