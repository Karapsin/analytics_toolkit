from __future__ import annotations

from decimal import Decimal
from typing import Any

import pandas as pd


def infer_gp_dataframe_column_type(adapter: Any, series: pd.Series) -> str:
    del adapter
    return _infer_common_sql_type(series)


def infer_trino_dataframe_column_type(adapter: Any, series: pd.Series) -> str:
    del adapter
    common_type = _infer_common_sql_type(series)
    if common_type == "DOUBLE PRECISION":
        return "DOUBLE"
    if common_type == "TEXT":
        return "VARCHAR"
    return common_type


def infer_ch_dataframe_column_type(adapter: Any, series: pd.Series) -> str:
    del adapter
    if pd.api.types.is_bool_dtype(series):
        base_type = "Bool"
    elif pd.api.types.is_integer_dtype(series):
        base_type = "Int64"
    elif pd.api.types.is_float_dtype(series):
        base_type = "Float64"
    elif pd.api.types.is_datetime64_any_dtype(series):
        base_type = "DateTime64(6)"
    else:
        non_null = series.dropna()
        if not non_null.empty and all(isinstance(value, Decimal) for value in non_null):
            base_type = "Float64"
        elif not non_null.empty and all(
            hasattr(value, "year")
            and hasattr(value, "month")
            and hasattr(value, "day")
            for value in non_null
        ):
            base_type = "Date"
        else:
            base_type = "String"

    if series.isna().any():
        return f"Nullable({base_type})"
    return base_type


def _infer_common_sql_type(series: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(series):
        return "BOOLEAN"
    if pd.api.types.is_integer_dtype(series):
        return "BIGINT"
    if pd.api.types.is_float_dtype(series):
        return "DOUBLE PRECISION"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "TIMESTAMP"

    non_null = series.dropna()
    if not non_null.empty and all(
        hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day")
        for value in non_null
    ):
        return "DATE"
    return "TEXT"
