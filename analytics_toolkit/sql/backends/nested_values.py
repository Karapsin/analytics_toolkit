"""Validate resolved row structure before positional conversion can discard data."""

from __future__ import annotations

from typing import Any

import numpy as np

from analytics_toolkit.sql.core.exact_input import array_values
from analytics_toolkit.sql.core.exact_values import unwrap_enum


def array_item(value: Any) -> Any:
    value = unwrap_enum(value)
    return array_values(value)


def row_items(value: Any, fields: list[tuple[str, str]]) -> list[Any]:
    value = unwrap_enum(value)
    if isinstance(value, dict):
        if len(value) != len(fields) or set(value) != {name for name, _native in fields}:
            msg = "Row fields must match the resolved destination schema."
            raise ValueError(msg)
        return [value[name] for name, _native in fields]
    if isinstance(value, np.ndarray):
        if value.ndim != 1:
            msg = "Row values require a one-dimensional positional sequence."
            raise ValueError(msg)
        value = array_values(value)
    if not isinstance(value, (list, tuple)) or len(value) != len(fields):
        msg = "Row arity must match the resolved destination schema."
        raise ValueError(msg)
    return list(value)
