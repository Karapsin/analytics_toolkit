"""Greenplum-native metadata retains arrays and intentional JSON declarations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from analytics_toolkit.sql.backends.models import SourceColumn


def map_same_backend_source_type_to_target(adapter: Any, column: SourceColumn) -> str:
    native = (column.native_type or "").strip()
    if native.endswith("[]") or native.lower() in {"json", "jsonb"}:
        return native
    return str(adapter.map_source_type_to_target(column))
