"""Keep native floating-point signed zero at the final psycopg2 boundary."""

from __future__ import annotations

import math
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import numpy as np

from .type_identity import canonical_builtin_type

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_FLOAT_CASTS = {"REAL": "REAL", "DOUBLE PRECISION": "FLOAT8"}


@lru_cache(maxsize=1024)
def _float_parameter_type(native_type: str | None) -> tuple[str, bool] | None:
    canonical = canonical_builtin_type(native_type or "")
    if canonical is None:
        return None
    array = canonical.endswith("[]")
    while canonical.endswith("[]"):
        canonical = canonical[:-2]
    cast_type = _FLOAT_CASTS.get(canonical)
    return (cast_type, array) if cast_type is not None else None


def _negative_zero_parameter(value: Any, cast_type: str) -> Any:
    if not isinstance(value, (float, np.float32, np.float64)):
        return value
    if value != 0 or math.copysign(1, value) >= 0:
        return value
    from psycopg2.extensions import AsIs  # noqa: PLC0415 -- final optional transport adaptation.

    # Finite ordinary floats become NUMERIC literals in psycopg2. An explicit
    # quoted native input preserves the zero sign instead of NUMERIC's unsigned zero.
    return AsIs(f"'-0.0'::{cast_type}")


def _float_array_parameters(value: Any, cast_type: str) -> Any:
    if isinstance(value, list):
        return [_float_array_parameters(item, cast_type) for item in value]
    if isinstance(value, tuple):
        return tuple(_float_array_parameters(item, cast_type) for item in value)
    return _negative_zero_parameter(value, cast_type)


def normalize_float_parameters(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    target_types: Mapping[str, str] | None,
) -> list[tuple[Any, ...]]:
    """Copy outgoing rows, wrapping signed zero only under verified float metadata."""
    if not target_types:
        return [tuple(row) for row in rows]
    fields = {
        index: field
        for index, name in enumerate(columns)
        if (field := _float_parameter_type(target_types.get(name, ""))) is not None
    }
    return [
        tuple(
            _float_array_parameters(value, fields[index][0])
            if index in fields and fields[index][1]
            else _negative_zero_parameter(value, fields[index][0])
            if index in fields
            else value
            for index, value in enumerate(row)
        )
        for row in rows
    ]


__all__ = ["normalize_float_parameters"]
