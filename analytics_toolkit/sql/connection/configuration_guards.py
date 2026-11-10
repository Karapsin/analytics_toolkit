"""Fail closed before opening clients for an operation with frozen identities."""

from __future__ import annotations

from typing import Any

from analytics_toolkit.sql.execution.backend_options import current_backend_operation


def check_connection_configuration(config: Any) -> None:
    state = current_backend_operation()
    if state is None:
        return
    # Take a stable callback set; callbacks may inspect configuration or unwind
    # their own registration. Compare the actual parsed object to be opened,
    # rather than rereading a configuration that could change again meanwhile.
    with state.lock:
        guards = tuple(state.resources.get("connection_configuration_guards", {}).values())
    for guard in guards:
        guard(config.connection_key, config)
