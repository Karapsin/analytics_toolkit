"""Deliberate Trino DATE assignments retain native local-calendar semantics."""

from __future__ import annotations

import datetime as dt
import re
from functools import lru_cache
from typing import Any, cast

import numpy as np
import pandas as pd

_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", flags=re.ASCII)
_INVALID_DATE = (
    "Trino DATE requires a valid canonical YYYY-MM-DD string or native calendar value. "
    "For other backend date syntax, CAST the source expression AS DATE in from_sql."
)
_INVALID_TYPE = "Trino DATE requires the unparameterized DATE type without wrapper syntax."


class TrinoDateValueError(ValueError):
    """Invalid DATE input cannot be repaired by replaying its assignment."""

    analytics_toolkit_sql_retry_safe = False


@lru_cache(maxsize=1024)
def is_date_type(native_type: str | None) -> bool:
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- native grammar cycle.
        parse_native_type,
    )

    native = parse_native_type(native_type)
    if native.base.name != "date":
        return False
    if native.name != "date" or native.arguments or native.original.casefold() != "date":
        raise TrinoDateValueError(_INVALID_TYPE)
    return True


def date_value(value: Any) -> dt.date:
    """Match typed DATE parameters without inventing a timezone or epoch unit."""
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 -- registry initialization.
        ExactTemporal,
    )

    try:
        if isinstance(value, ExactTemporal):
            return value.calendar_datetime().date()
        if isinstance(value, (dt.datetime, pd.Timestamp)):
            return cast("dt.datetime", value).date()
        if isinstance(value, np.datetime64):
            return pd.Timestamp(value).date()
        if isinstance(value, dt.date):
            return value
        if isinstance(value, str):
            token = value.strip()
            if _ISO_DATE.fullmatch(token) is not None:
                return dt.date.fromisoformat(token)
    except (TypeError, ValueError, OverflowError):
        raise TrinoDateValueError(_INVALID_DATE) from None
    raise TrinoDateValueError(_INVALID_DATE)


@lru_cache(maxsize=1024)
def contains_date_type(native_type: str) -> bool:
    from analytics_toolkit.sql.backends.nested_types import (  # noqa: PLC0415 -- recursive native grammar.
        nested_descriptor,
    )

    if is_date_type(native_type):
        return True
    children = [contains_date_type(child) for _name, child in nested_descriptor(native_type)[1]]
    return any(children)


def validate_date_type_profile(native_type: str) -> bool:
    """Validate declarations even for empty input; retain ordinary family checks."""
    contains_date_type(native_type)
    return False


def normalize_authoritative_date(value: Any, native_type: str | None) -> Any:
    """Normalize only a declared DATE-containing validation or assignment view."""
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 -- registry initialization.
        is_null,
    )

    if native_type is None or not contains_date_type(native_type):
        return value
    if is_null(value):
        return None
    if is_date_type(native_type):
        return date_value(value)
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- recursive codec cycle.
        ResolvedColumn,
    )

    # Existing nested codecs check converted MAP pairs before constructing a dict.
    return ResolvedColumn(native_type, target_backend="trino").convert(value)


__all__ = [
    "TrinoDateValueError",
    "contains_date_type",
    "date_value",
    "is_date_type",
    "normalize_authoritative_date",
    "validate_date_type_profile",
]
