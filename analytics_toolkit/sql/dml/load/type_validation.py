"""Profile complete dataframe columns before destination mutation."""

from __future__ import annotations

from decimal import Decimal
from numbers import Integral, Real
from typing import TYPE_CHECKING, Any

import pandas as pd

from analytics_toolkit.sql.backends.conversion_profiles import validate_converted_rows
from analytics_toolkit.sql.core.type_resolution import (
    ColumnProfile,
    ResolvedColumn,
    decimal_domain,
    integer_domain,
    validate_profile,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


def _schema_value(value: Any, target_type: str) -> Any:
    # Caller-requested numeric schemas have historically accepted float inputs.
    # Validate the deliberate conversion rather than changing inferred types.
    if isinstance(value, Real) and not isinstance(value, Integral):
        if decimal_domain(target_type) is not None:
            return Decimal(str(value))
        if integer_domain(target_type) is not None and float(value).is_integer():
            return int(float(value))
    return value


def validate_dataframe_schema(
    df: pd.DataFrame,
    schema: Mapping[str, str],
    backend: str,
    *,
    allow_timestamp_precision_loss: bool = True,
    destination_timezone: str | None = None,
) -> None:
    for index, name in enumerate(df.columns):
        if str(name) not in schema:
            continue
        profile = ColumnProfile(
            native_type=schema[str(name)],
            source_backend=backend,
            destination_timezone=destination_timezone,
            temporal_type=schema[str(name)],
        )
        for value in df.iloc[:, index]:
            profile.add(_schema_value(value, schema[str(name)]))
        validate_profile(
            profile,
            schema[str(name)],
            backend,
            allow_timestamp_precision_loss=allow_timestamp_precision_loss,
            destination_timezone=destination_timezone,
        )
        resolution = ResolvedColumn(
            schema[str(name)], target_backend=backend, destination_timezone=destination_timezone
        )
        validate_converted_rows(
            ((_schema_value(value, schema[str(name)]),) for value in df.iloc[:, index]),
            [resolution],
            backend=backend,
            destination_timezone=destination_timezone,
        )


def convert_dataframe_schema(
    df: pd.DataFrame,
    schema: Mapping[str, str],
    backend: str | None = None,
    *,
    destination_timezone: str | None = None,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    columns = []
    metadata = []
    for index, name in enumerate(df.columns):
        native_type = schema[str(name)]
        resolution = ResolvedColumn(
            native_type,
            certified_input_count=len(df),
            target_backend=backend or "",
            destination_timezone=destination_timezone,
        )
        columns.append(
            pd.Series(
                [
                    resolution.convert(_schema_value(value, native_type))
                    for value in df.iloc[:, index]
                ],
                dtype=object,
            )
        )
        metadata.append({"column": str(name), **resolution.metadata()})
    result = pd.concat(columns, axis=1) if columns else pd.DataFrame(index=df.index)
    result.columns = df.columns
    result.index = df.index
    return result, metadata
