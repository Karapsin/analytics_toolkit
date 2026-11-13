"""Greenplum key predicates must distinguish timestamps with and without zones."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from analytics_toolkit.sql.backends.key_literals import (
    render_transfer_literal as _standard_literal,
)


def render_transfer_literal(self: Any, value: Any) -> str:
    # The registry must finish loading before importing core.capabilities.
    from analytics_toolkit.sql.core.exact_values import ExactTemporal  # noqa: PLC0415

    if isinstance(value, datetime) and value.utcoffset() is not None:
        # An ordinary TIMESTAMP literal silently discards its numeric offset.
        return f"TIMESTAMP WITH TIME ZONE '{value.isoformat(sep=' ')}'"
    if isinstance(value, ExactTemporal) and value.calendar_datetime().utcoffset() is not None:
        text = value.isoformat(utc=True).replace("T", " ", 1)
        return f"TIMESTAMP WITH TIME ZONE '{text}'"
    return _standard_literal(self, value)
