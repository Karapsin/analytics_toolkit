"""Exact scalar literals for predicates issued to a source backend."""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def render_transfer_literal(self: Any, value: Any) -> str:
    # The backend registry loads before core.capabilities is initialized.
    from analytics_toolkit.sql.core.exact_values import ExactTemporal  # noqa: PLC0415

    del self
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (datetime, ExactTemporal)):
        return _timestamp_literal(value)
    if isinstance(value, date):
        return f"DATE '{value.isoformat()}'"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, (int, float, Decimal)):
        return _number_literal(value)
    message = (
        "transfer_key_values supports only None, str, date, datetime, int, "
        "float, bool, and Decimal values."
    )
    raise ValueError(message)


def _timestamp_literal(value: Any) -> str:
    if isinstance(value, datetime):
        text = value.isoformat(sep=" ")
    else:
        text = value.isoformat(utc=value.timezone is not None).replace("T", " ", 1)
    return f"TIMESTAMP '{text}'"


def _number_literal(value: float | Decimal) -> str:
    if isinstance(value, float):
        if not math.isfinite(value):
            message = "transfer_key_values float values must be finite."
            raise ValueError(message)
        return repr(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            message = "transfer_key_values Decimal values must be finite."
            raise ValueError(message)
        return format(value, "f")
    return str(value)
