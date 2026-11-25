"""Validate explicit text-stage DATE casts without rewriting staged payloads."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.core.exact_values import is_null

from .native_values import canonical_native_type
from .type_resolution import ResolvedColumn, parse_native_type

_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", flags=re.ASCII)
_TEXT_TYPES = {
    "gp": {"text", "varchar", "character varying"},
    "trino": {"varchar"},
}
_INVALID_DATE = (
    "Explicit text stages into DATE require canonical YYYY-MM-DD values. "
    "For other backend date syntax, CAST the source expression AS DATE in from_sql."
)


class _ExplicitDateValidation(ResolvedColumn):
    """Return date identities only for the complete-input validation pass."""

    def convert(self, value: Any) -> dt.date | None:
        if is_null(value):
            return None
        if not isinstance(value, str) or _ISO_DATE.fullmatch(value) is None:
            raise InvalidSqlInputError(_INVALID_DATE)
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            raise InvalidSqlInputError(_INVALID_DATE) from None


def explicit_text_date_validation(
    stage: ResolvedColumn, target_type: str, backend: str
) -> ResolvedColumn | None:
    """Select only the deliberate textual intermediate of an existing DATE cast."""
    if backend not in _TEXT_TYPES or stage.reason != "authoritative" or stage.text_fallback:
        return None
    source = parse_native_type(canonical_native_type(stage.target_type, backend)).base.name
    target = parse_native_type(canonical_native_type(target_type, backend)).base.name
    if source not in _TEXT_TYPES[backend] or target != "date":
        return None
    return _ExplicitDateValidation(
        target_type=target_type,
        source_type=stage.target_type,
        source_backend=backend,
        target_backend=backend,
        reason="explicit_text_date_validation",
        certified_input_count=stage.certified_input_count,
    )


__all__ = ["explicit_text_date_validation"]
