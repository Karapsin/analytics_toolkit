from __future__ import annotations

from typing import Any
from uuid import UUID

import pandas as pd


def infer_gp_dataframe_column_type(adapter: Any, series: pd.Series) -> str:
    del adapter
    return _infer_column_type(series, "gp")


def infer_trino_dataframe_column_type(adapter: Any, series: pd.Series) -> str:
    del adapter
    return _infer_column_type(series, "trino")


def infer_ch_dataframe_column_type(adapter: Any, series: pd.Series) -> str:
    del adapter
    return _infer_column_type(series, "ch")


def _infer_column_type(series: pd.Series, backend: str) -> str:
    from analytics_toolkit.sql.core.type_resolution import (  # noqa: PLC0415 - avoid backend registry import cycles
        ColumnProfile,
        resolve_profile,
    )

    profile = ColumnProfile()
    for value in series:
        profile.add(value)
    declared = None
    if pd.api.types.is_unsigned_integer_dtype(series):
        declared = "UInt" + str(series.dtype.itemsize * 8)
    elif pd.api.types.is_integer_dtype(series):
        declared = "Int" + str(series.dtype.itemsize * 8)
    if not profile.families:
        if pd.api.types.is_bool_dtype(series):
            profile.families.add("boolean")
        elif pd.api.types.is_float_dtype(series):
            profile.families.add("float")
        elif pd.api.types.is_datetime64_any_dtype(series):
            profile.families.add("timestamp")
            profile.aware = getattr(series.dtype, "tz", None) is not None
            profile.naive = not profile.aware
    return resolve_profile(profile, backend, source_type=declared, source_backend="ch").target_type


def _infer_common_sql_type(series: pd.Series) -> str:
    return _infer_column_type(series, "gp")


def _is_uuid_series(non_null: pd.Series) -> bool:
    return not non_null.empty and all(isinstance(value, UUID) for value in non_null)
