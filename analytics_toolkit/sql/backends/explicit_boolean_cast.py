"""Validate explicit text-stage BOOLEAN casts without rewriting staged payloads."""

from __future__ import annotations

from typing import Any

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.core.exact_values import is_null

from .gp.boolean_values import boolean_value as gp_boolean_value
from .gp.boolean_values import is_boolean_type as is_gp_boolean_type
from .native_values import canonical_native_type
from .trino.boolean_values import boolean_value as trino_boolean_value
from .trino.boolean_values import is_boolean_type as is_trino_boolean_type
from .type_resolution import ResolvedColumn, parse_native_type

_TEXT_TYPES = {
    "gp": {"text", "varchar", "character varying"},
    "trino": {"varchar"},
}
_BOOLEAN_PARSERS = {"gp": gp_boolean_value, "trino": trino_boolean_value}
_BOOLEAN_TYPES = {"gp": is_gp_boolean_type, "trino": is_trino_boolean_type}
_INVALID_BOOLEAN = (
    "Explicit text stages into BOOLEAN require supported Boolean text. "
    "For other source conversions, CAST the source expression AS BOOLEAN in from_sql."
)


class _ExplicitBooleanValidation(ResolvedColumn):
    """Return Boolean identities only for the complete-input validation pass."""

    def convert(self, value: Any) -> bool | None:
        if is_null(value):
            return None
        if not isinstance(value, str):
            raise InvalidSqlInputError(_INVALID_BOOLEAN)
        return _BOOLEAN_PARSERS[self.target_backend](value)


def explicit_text_boolean_validation(
    stage: ResolvedColumn, target_type: str, backend: str
) -> ResolvedColumn | None:
    """Select only an explicit textual intermediate of an existing BOOLEAN cast."""
    if backend not in _TEXT_TYPES or stage.reason != "authoritative" or stage.text_fallback:
        return None
    source = parse_native_type(canonical_native_type(stage.target_type, backend)).base.name
    target = canonical_native_type(target_type, backend)
    if source not in _TEXT_TYPES[backend] or not _BOOLEAN_TYPES[backend](target):
        return None
    return _ExplicitBooleanValidation(
        target_type=target_type,
        source_type=stage.target_type,
        source_backend=backend,
        target_backend=backend,
        reason="explicit_text_boolean_validation",
        certified_input_count=stage.certified_input_count,
    )


__all__ = ["explicit_text_boolean_validation"]
