"""Default target-creation policy shared by backend adapters."""

from __future__ import annotations

from typing import Any


def preflight_new_target(
    adapter: Any, connection: Any, table_name: str, *, creation_policy: Any = None
) -> None:
    """Default backends need no additional new-target scope validation."""
    del adapter, connection, table_name, creation_policy


def should_ensure_load_target_table(
    adapter: Any,
    target_exists: bool,  # noqa: FBT001 - adapter API compatibility
    *,
    connection: Any = None,
) -> bool:
    del adapter, connection
    return not target_exists
