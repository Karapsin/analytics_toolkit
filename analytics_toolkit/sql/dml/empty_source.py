from __future__ import annotations

from typing import Literal, cast

EmptySourcePolicy = Literal["replace", "keep", "error"]


class EmptySourceError(ValueError):
    """A SQL load produced no rows while empty inputs were configured as errors."""


def validate_empty_source_policy(value: str | None) -> EmptySourcePolicy | None:
    if value is None:
        return None
    if value not in {"replace", "keep", "error"}:
        message = "empty_source_policy must be one of: 'replace', 'keep', 'error'."
        raise ValueError(message)
    return cast("EmptySourcePolicy", value)


def resolve_empty_source_policy(
    value: EmptySourcePolicy | None,
    *,
    write_mode: str,
) -> EmptySourcePolicy:
    if value is not None:
        return value
    if write_mode in {"replace", "truncate_insert"}:
        return "replace"
    return "keep"


__all__ = [
    "EmptySourceError",
    "EmptySourcePolicy",
    "resolve_empty_source_policy",
    "validate_empty_source_policy",
]
