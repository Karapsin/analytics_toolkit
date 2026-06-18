from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from itertools import product
from typing import Any

import sqlparse

from ..runtime.models import TransferSlice

_TRANSFER_SOURCE_ALIAS = "analytics_toolkit_transfer_source"


def normalize_transfer_slices(
    *,
    source_sql: str,
    transfer_keys: str | Sequence[str] | None,
    transfer_key_values: Sequence[Any] | Mapping[str, Sequence[Any]] | None,
    concurrency: int,
) -> tuple[list[str] | None, dict[str, list[Any]] | None, list[TransferSlice] | None, int]:
    resolved_concurrency = normalize_transfer_concurrency(concurrency)
    if transfer_keys is None:
        if transfer_key_values is not None:
            raise ValueError("transfer_key_values requires transfer_keys.")
        if resolved_concurrency > 1:
            raise ValueError("concurrency > 1 requires transfer_keys.")
        return None, None, None, resolved_concurrency

    keys = normalize_transfer_keys(transfer_keys)
    if transfer_key_values is None:
        raise ValueError("transfer_keys requires explicit transfer_key_values.")
    values_by_key = normalize_transfer_key_values(keys, transfer_key_values)
    values_product = list(product(*(values_by_key[key] for key in keys)))
    if not values_product:
        raise ValueError("transfer_key_values must generate at least one slice.")

    seen_values: set[tuple[Any, ...]] = set()
    for values in values_product:
        if values in seen_values:
            raise ValueError("transfer_key_values generated duplicate key tuples.")
        seen_values.add(values)

    stripped_source_sql = strip_one_trailing_semicolon(source_sql)
    validate_single_source_statement(stripped_source_sql)
    slices = [
        build_transfer_slice(
            index=index,
            source_sql=stripped_source_sql,
            transfer_keys=keys,
            values=values,
        )
        for index, values in enumerate(values_product)
    ]
    return keys, values_by_key, slices, resolved_concurrency


def normalize_transfer_concurrency(concurrency: int) -> int:
    if isinstance(concurrency, bool) or not isinstance(concurrency, int):
        raise ValueError("concurrency must be a positive integer.")
    if concurrency < 1:
        raise ValueError("concurrency must be a positive integer.")
    return concurrency


def normalize_transfer_keys(transfer_keys: str | Sequence[str]) -> list[str]:
    if isinstance(transfer_keys, str):
        keys = [transfer_keys.strip()]
    else:
        keys = []
        for key in transfer_keys:
            if not isinstance(key, str):
                raise ValueError("transfer_keys entries must be strings.")
            keys.append(key.strip())
    if not keys or any(not key for key in keys):
        raise ValueError("transfer_keys must contain at least one non-empty expression.")
    if len(set(keys)) != len(keys):
        raise ValueError("transfer_keys expressions must be unique.")
    return keys


def normalize_transfer_key_values(
    keys: list[str],
    transfer_key_values: Sequence[Any] | Mapping[str, Sequence[Any]],
) -> dict[str, list[Any]]:
    if isinstance(transfer_key_values, Mapping):
        provided_keys = set(transfer_key_values)
        expected_keys = set(keys)
        missing = expected_keys - provided_keys
        extra = provided_keys - expected_keys
        if missing or extra:
            details: list[str] = []
            if missing:
                details.append("missing: " + ", ".join(sorted(missing)))
            if extra:
                details.append("extra: " + ", ".join(sorted(extra)))
            raise ValueError(
                "transfer_key_values keys must exactly match transfer_keys ("
                + "; ".join(details)
                + ")."
            )
        return {
            key: _normalize_single_key_values(transfer_key_values[key], key)
            for key in keys
        }

    if len(keys) != 1:
        raise ValueError("Multiple transfer_keys require mapping transfer_key_values.")
    return {keys[0]: _normalize_single_key_values(transfer_key_values, keys[0])}


def _normalize_single_key_values(values: Sequence[Any], key: str) -> list[Any]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
        raise ValueError(f"transfer_key_values for {key!r} must be a non-empty sequence.")
    normalized = list(values)
    if not normalized:
        raise ValueError(f"transfer_key_values for {key!r} must not be empty.")
    for value in normalized:
        render_transfer_literal(value)
    return normalized


def build_transfer_slice(
    *,
    index: int,
    source_sql: str,
    transfer_keys: list[str],
    values: tuple[Any, ...],
) -> TransferSlice:
    predicate_sql = build_transfer_slice_predicate(transfer_keys, values)
    wrapped_source_sql = (
        f"SELECT *\n"
        f"FROM ({source_sql}) AS {_TRANSFER_SOURCE_ALIAS}\n"
        f"WHERE {predicate_sql}"
    )
    return TransferSlice(
        index=index,
        values=values,
        predicate_sql=predicate_sql,
        source_sql=wrapped_source_sql,
        label=f"slice-{index:05d}",
    )


def build_transfer_slice_predicate(keys: list[str], values: tuple[Any, ...]) -> str:
    if len(keys) != len(values):
        raise ValueError("transfer key and value counts must match.")
    predicates = []
    for key, value in zip(keys, values):
        if value is None:
            predicates.append(f"({key}) IS NULL")
        else:
            predicates.append(f"({key}) = {render_transfer_literal(value)}")
    return "\n  AND ".join(predicates)


def render_transfer_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        return f"TIMESTAMP '{value.isoformat(sep=' ')}'"
    if isinstance(value, date):
        return f"DATE '{value.isoformat()}'"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("transfer_key_values float values must be finite.")
        return repr(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("transfer_key_values Decimal values must be finite.")
        return format(value, "f")
    raise ValueError(
        "transfer_key_values supports only None, str, date, datetime, int, "
        "float, bool, and Decimal values."
    )


def strip_one_trailing_semicolon(source_sql: str) -> str:
    stripped = source_sql.strip()
    if stripped.endswith(";"):
        return stripped[:-1].strip()
    return stripped


def validate_single_source_statement(source_sql: str) -> None:
    statements = [statement for statement in sqlparse.split(source_sql) if statement.strip()]
    if len(statements) != 1:
        raise ValueError("transfer_keys requires from_sql to contain exactly one SQL statement.")
