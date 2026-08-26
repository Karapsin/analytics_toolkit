from __future__ import annotations

# ruff: noqa: C901, I001, PLR0911, PLR0912, RUF034, SIM105

import datetime as dt
import json
import math
import uuid
from decimal import Decimal
from typing import Any, Mapping, Sequence

import pandas as pd


def normalize_scalar(value: Any, *, decimal_scale: int = 4) -> Any:
    """Normalize driver-specific values into stable logical integration values."""
    if value is None:
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return f"{value:.{decimal_scale}f}"
    if isinstance(value, uuid.UUID):
        return str(value).lower()
    if isinstance(value, dt.datetime):
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        return timestamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, float):
        if math.isnan(value):
            return None
        return value
    if isinstance(value, (dict, list)):
        return canonical_json(value)
    if hasattr(value, "item"):
        try:
            return normalize_scalar(value.item(), decimal_scale=decimal_scale)
        except (TypeError, ValueError):
            pass
    return value


def canonical_json(value: Any) -> str:
    if isinstance(value, str):
        value = json.loads(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_records(
    frame: pd.DataFrame,
    *,
    json_columns: Sequence[str] = (),
    decimal_columns: Sequence[str] = (),
    date_columns: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Preserve dataframe column order while normalizing every returned record."""
    records: list[dict[str, Any]] = []
    for raw_record in frame.to_dict(orient="records"):
        record: dict[str, Any] = {}
        for column in frame.columns:
            value = raw_record[column]
            if column in date_columns and value is not None:
                value = pd.Timestamp(value).date().isoformat()
            if column in json_columns and value is not None:
                try:
                    value = canonical_json(value)
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            record[str(column)] = normalize_scalar(
                value,
                decimal_scale=4 if column in decimal_columns else 4,
            )
        records.append(record)
    return records


def assert_exact_frame(
    actual: pd.DataFrame,
    expected: pd.DataFrame,
    *,
    json_columns: Sequence[str] = (),
    decimal_columns: Sequence[str] = (),
    date_columns: Sequence[str] = (),
) -> None:
    assert list(actual.columns) == list(expected.columns)
    actual_records = normalize_records(
        actual,
        json_columns=json_columns,
        decimal_columns=decimal_columns,
        date_columns=date_columns,
    )
    expected_records = normalize_records(
        expected,
        json_columns=json_columns,
        decimal_columns=decimal_columns,
        date_columns=date_columns,
    )
    assert actual_records == expected_records, {
        "actual": actual_records,
        "expected": expected_records,
    }


def schema_contains(actual: Mapping[str, str], expected: Mapping[str, Sequence[str]]) -> None:
    normalized = {name: value.lower().replace(" ", "") for name, value in actual.items()}
    for column, accepted in expected.items():
        assert column in normalized
        assert any(token.lower().replace(" ", "") in normalized[column] for token in accepted), (
            column,
            actual[column],
            accepted,
        )
