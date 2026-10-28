"""Default target-creation policy shared by backend adapters."""

from __future__ import annotations

from typing import Any


def should_ensure_load_target_table(
    adapter: Any,
    target_exists: bool,  # noqa: FBT001 - adapter API compatibility
    *,
    connection: Any = None,
) -> bool:
    del adapter, connection
    return not target_exists
