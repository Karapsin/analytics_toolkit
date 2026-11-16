"""Validate native text-stage integer casts without changing staging payloads."""

from __future__ import annotations

from typing import Any

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.core.exact_values import is_null

from .gp.integer_values import integer_text_value as gp_integer_text_value
from .gp.integer_values import is_integer_type as is_gp_integer_type
from .native_values import canonical_native_type
from .trino.integer_values import integer_text_value as trino_integer_text_value
from .trino.integer_values import is_integer_type as is_trino_integer_type
from .type_resolution import ResolvedColumn, parse_native_type

_TEXT_TYPES = {
    "gp": {"text", "varchar", "character varying"},
    "trino": {"varchar"},
}
_INTEGER_TYPES = {"gp": is_gp_integer_type, "trino": is_trino_integer_type}
_INTEGER_PARSERS = {"gp": gp_integer_text_value, "trino": trino_integer_text_value}
_INVALID_INTEGER = (
    "Explicit text stages into integer columns require native integer text within the "
    "destination range. For other source conversions, CAST the source expression "
    "AS the destination integer type in from_sql."
)


class _ExplicitIntegerValidation(ResolvedColumn):
    """Return final integer identities for validation, leaving stage rows textual."""

    def convert(self, value: Any) -> int | None:
        if is_null(value):
            return None
        if not isinstance(value, str):
            raise InvalidSqlInputError(_INVALID_INTEGER)
        target = canonical_native_type(self.target_type, self.target_backend)
        return _INTEGER_PARSERS[self.target_backend](value, target)


def explicit_text_integer_validation(
    stage: ResolvedColumn, target_type: str, backend: str
) -> ResolvedColumn | None:
    """Select only an explicit text intermediate and a native final integer type."""
    if backend not in _TEXT_TYPES or stage.reason != "authoritative" or stage.text_fallback:
        return None
    source = parse_native_type(canonical_native_type(stage.target_type, backend)).base.name
    target = canonical_native_type(target_type, backend)
    if source not in _TEXT_TYPES[backend] or not _INTEGER_TYPES[backend](target):
        return None
    return _ExplicitIntegerValidation(
        target_type=target_type,
        source_type=stage.target_type,
        source_backend=backend,
        target_backend=backend,
        reason="explicit_text_integer_validation",
        certified_input_count=stage.certified_input_count,
    )


__all__ = ["explicit_text_integer_validation"]
