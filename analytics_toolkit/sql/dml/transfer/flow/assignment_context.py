"""Bind value-dependent GP assignments to the first actual writer session."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.gp.native_assignment import (
    capture_native_assignment_context,
    require_native_assignment_context,
    requires_assignment_session_context,
)

if TYPE_CHECKING:
    from analytics_toolkit.sql.dml.transfer.runtime.models import TransferOptions


def require_pending_assignment_context(options: TransferOptions, connection: Any) -> Any:
    cached = options.input_resolution
    if not any(
        requires_assignment_session_context(native)
        for native in cached.get("native_assignment_context_types", ())
    ):
        return None
    expected = cached.get("native_assignment_session_context")
    adapter = get_backend_adapter("gp")
    if expected is None:
        expected = capture_native_assignment_context(
            adapter, connection, query_label=options.query_label
        )
        cached["native_assignment_session_context"] = expected
    else:
        require_native_assignment_context(
            adapter, connection, expected, query_label=options.query_label
        )
    return expected
