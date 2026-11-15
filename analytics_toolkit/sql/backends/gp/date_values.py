"""Certify native Greenplum DATE assignments without choosing a session timezone."""

from __future__ import annotations

import datetime as dt
import re
from functools import lru_cache
from typing import TYPE_CHECKING, Any, cast

import numpy as np

from .type_identity import canonical_builtin_type

if TYPE_CHECKING:
    from analytics_toolkit.sql.backends.value_profiles import ColumnProfile

_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", flags=re.ASCII)
_INVALID_DATE = (
    "Greenplum authoritative date schema requires a valid canonical YYYY-MM-DD string "
    "or native calendar value. "
    "For other date syntax, CAST the source expression AS DATE in from_sql."
)


class GreenplumDateValueError(ValueError):
    """Invalid DATE input cannot be repaired by replaying its assignment."""

    analytics_toolkit_sql_retry_safe = False


@lru_cache(maxsize=1024)
def is_date_type(native_type: str | None) -> bool:
    return canonical_builtin_type(native_type or "") == "DATE"


def _datetime_parameter(value: Any) -> dt.datetime:
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 -- registry initialization.
        ExactTemporal,
    )

    try:
        if isinstance(value, ExactTemporal):
            return value.calendar_datetime()
        import pandas as pd  # noqa: PLC0415 -- NumPy needs a native psycopg datetime parameter.

        converted = pd.Timestamp(value)
        converted.to_pydatetime(warn=False)
    except (TypeError, ValueError, OverflowError):
        raise GreenplumDateValueError(_INVALID_DATE) from None
    else:
        return cast("dt.datetime", converted)


def date_value(value: Any) -> dt.date | None:
    """Retain native datetime parameters so the server owns their DATE cast."""
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 -- registry initialization.
        ExactTemporal,
        is_null,
    )

    if is_null(value):
        return None
    if isinstance(value, dt.date):
        # datetime and pandas.Timestamp inherit date. Keep their native clocks
        # and offsets intact; the connection's server session performs the cast.
        return value
    if isinstance(value, (ExactTemporal, np.datetime64)):
        return _datetime_parameter(value)
    if isinstance(value, str) and _ISO_DATE.fullmatch(value) is not None:
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            raise GreenplumDateValueError(_INVALID_DATE) from None
    raise GreenplumDateValueError(_INVALID_DATE)


@lru_cache(maxsize=1024)
def contains_date_type(native_type: str) -> bool:
    """Recognize supported built-in arrays without adopting user-defined types."""
    if is_date_type(native_type):
        return True
    canonical = canonical_builtin_type(native_type)
    if canonical is None:
        return False
    from analytics_toolkit.sql.backends.nested_types import (  # noqa: PLC0415 -- recursive native grammar.
        nested_descriptor,
    )

    return any(contains_date_type(child) for _name, child in nested_descriptor(canonical)[1])


def normalize_authoritative_date(value: Any, native_type: str | None) -> Any:
    """Normalize only a declared built-in DATE-containing assignment view."""
    if native_type is None or not contains_date_type(native_type):
        return value
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 -- registry initialization.
        is_null,
    )

    if is_null(value):
        return None
    if is_date_type(native_type):
        return date_value(value)
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- recursive codec cycle.
        ResolvedColumn,
    )

    return ResolvedColumn(native_type, target_backend="gp").convert(value)


def validate_date_profile(profile: ColumnProfile, native_type: str) -> bool:
    """Permit native date and timestamp parameters for an explicit GP DATE cast."""
    if not is_date_type(native_type):
        return False
    if not profile.families <= {"date", "timestamp"}:
        raise GreenplumDateValueError(_INVALID_DATE)
    return True


__all__ = [
    "GreenplumDateValueError",
    "contains_date_type",
    "date_value",
    "is_date_type",
    "normalize_authoritative_date",
    "validate_date_profile",
]
