"""Certify native finite numeric CASTs while retaining explicit textual stages."""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.core.exact_values import is_null

from .ch.numeric_text import is_numeric_text_type as is_ch_numeric_type
from .ch.numeric_text import native_text_cast_value as ch_native_text_cast
from .gp.numeric_text import is_numeric_text_type as is_gp_numeric_type
from .gp.numeric_text import native_text_cast_value as gp_native_text_cast
from .native_values import canonical_native_type
from .trino.numeric_text import is_numeric_text_type as is_trino_numeric_type
from .trino.numeric_text import native_text_cast_value as trino_native_text_cast
from .type_resolution import ColumnProfile, ResolvedColumn, parse_native_type, validate_profile

if TYPE_CHECKING:
    from collections.abc import Callable
    from decimal import Decimal

_TEXT_TYPES = {"gp": {"text", "varchar", "character varying"}, "trino": {"varchar"}}
_NUMERIC_TYPES: dict[str, Callable[[str], bool]] = {
    "gp": is_gp_numeric_type,
    "trino": is_trino_numeric_type,
    "ch": is_ch_numeric_type,
}
_NATIVE_CASTS: dict[str, Callable[[str, str], Decimal | float | None]] = {
    "gp": gp_native_text_cast,
    "trino": trino_native_text_cast,
    "ch": ch_native_text_cast,
}
_INVALID_TEXT = (
    "Explicit text stages into finite numeric columns require native numeric text within "
    "the destination range and scale. For other source syntax, CAST the source expression "
    "AS the destination numeric type in from_sql."
)


def _is_text_stage(native_type: str, backend: str) -> bool:
    if backend == "ch":
        native = parse_native_type(native_type)
        if native.name == "nullable" and len(native.arguments) == 1:
            native = parse_native_type(native.arguments[0])
        return native.name == "string" and not native.arguments
    native = parse_native_type(canonical_native_type(native_type, backend)).base
    return native.name in _TEXT_TYPES[backend]


@lru_cache(maxsize=1024)
def _validate_null_target(native_type: str, backend: str) -> None:
    """Retain encoded native nullability without altering the actual string profile."""
    null_view = ColumnProfile()
    null_view.add(None)
    validate_profile(null_view, native_type, backend)


class _ExplicitNumericTextValidation(ResolvedColumn):
    """Produce only a validation view; final SQL retains the original stage payload."""

    def convert(self, value: Any) -> Decimal | float | None:
        if is_null(value):
            _validate_null_target(self.target_type, self.target_backend)
            return None
        if not isinstance(value, str):
            raise InvalidSqlInputError(_INVALID_TEXT)
        return _NATIVE_CASTS[self.target_backend](value, self.target_type)


def explicit_text_numeric_validation(
    stage: ResolvedColumn, target_type: str, backend: str
) -> ResolvedColumn | None:
    """Select only an authoritative text intermediate and a supported numeric target."""
    if backend not in _NUMERIC_TYPES or stage.reason != "authoritative" or stage.text_fallback:
        return None
    if not _is_text_stage(stage.target_type, backend) or not _NUMERIC_TYPES[backend](target_type):
        return None
    return _ExplicitNumericTextValidation(
        target_type=target_type,
        source_type=stage.target_type,
        source_backend=backend,
        target_backend=backend,
        reason="explicit_text_numeric_validation",
        certified_input_count=stage.certified_input_count,
    )


__all__ = ["explicit_text_numeric_validation"]
