from __future__ import annotations

# ruff: noqa: PLR0913

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from itertools import product
from typing import Any

import sqlparse

from ..runtime.models import TransferSlice


_INVALID_SIMPLE_KEY_MESSAGE = (
    "transfer_keys string/list entries must be simple placeholder names such as 'event_date'."
)


@dataclass(frozen=True)
class TransferKey:
    name: str
    expression: str


def normalize_transfer_slices(
    *,
    source_sql: str,
    source_table: str | None = None,
    transfer_keys: str | Sequence[str] | Mapping[str, str] | None,
    transfer_key_values: Sequence[Any] | Mapping[str, Sequence[Any]] | None,
    concurrency: int,
    allow_unkeyed_concurrency: bool = False,
) -> tuple[
    list[str] | None,
    dict[str, str] | None,
    dict[str, list[Any]] | None,
    list[TransferSlice] | None,
    int,
]:
    resolved_concurrency = normalize_transfer_concurrency(concurrency)
    if transfer_keys is None:
        if transfer_key_values is not None:
            raise ValueError("transfer_key_values requires transfer_keys.")
        if resolved_concurrency > 1 and not allow_unkeyed_concurrency:
            raise ValueError(
                "concurrency > 1 without transfer_keys requires transfer_staging_schema on from_db."
            )
        return None, None, None, None, resolved_concurrency

    keys = normalize_transfer_keys(transfer_keys)
    if transfer_key_values is None:
        raise ValueError("transfer_keys requires explicit transfer_key_values.")
    values_by_key = normalize_transfer_key_values(keys, transfer_key_values)
    values_product = list(product(*(values_by_key[key.name] for key in keys)))
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
            source_table=source_table,
            transfer_keys=keys,
            values=values,
        )
        for index, values in enumerate(values_product)
    ]
    key_names = [key.name for key in keys]
    key_expressions = {key.name: key.expression for key in keys}
    return key_names, key_expressions, values_by_key, slices, resolved_concurrency


def normalize_transfer_concurrency(concurrency: int) -> int:
    if isinstance(concurrency, bool) or not isinstance(concurrency, int):
        raise ValueError("concurrency must be a positive integer.")
    if concurrency < 1:
        raise ValueError("concurrency must be a positive integer.")
    return concurrency


def normalize_transfer_keys(
    transfer_keys: str | Sequence[str] | Mapping[str, str],
) -> list[TransferKey]:
    if isinstance(transfer_keys, Mapping):
        return _normalize_transfer_key_mapping(transfer_keys)
    if isinstance(transfer_keys, str):
        raw_keys = [transfer_keys]
    else:
        raw_keys = list(transfer_keys)
    if not raw_keys:
        raise ValueError("transfer_keys must contain at least one placeholder name.")

    keys: list[TransferKey] = []
    seen: set[str] = set()
    for raw_key in raw_keys:
        if not isinstance(raw_key, str):
            raise ValueError("transfer_keys entries must be strings.")
        name = raw_key.strip()
        _validate_simple_transfer_key_name(name, raw_entry=raw_key)
        if name in seen:
            raise ValueError("transfer_keys placeholder names must be unique.")
        seen.add(name)
        keys.append(TransferKey(name=name, expression=name))
    return keys


def _normalize_transfer_key_mapping(
    transfer_keys: Mapping[str, str],
) -> list[TransferKey]:
    if not transfer_keys:
        raise ValueError("transfer_keys must contain at least one placeholder name.")
    keys: list[TransferKey] = []
    seen: set[str] = set()
    for raw_name, raw_expression in transfer_keys.items():
        if not isinstance(raw_name, str):
            raise ValueError("transfer_keys mapping keys must be strings.")
        name = raw_name.strip()
        _validate_simple_transfer_key_name(name, raw_entry=raw_name)
        if name in seen:
            raise ValueError("transfer_keys placeholder names must be unique.")
        seen.add(name)
        if not isinstance(raw_expression, str):
            raise ValueError("transfer_keys mapping values must be strings.")
        expression = raw_expression.strip()
        if not expression:
            raise ValueError(
                f"transfer_keys expression for placeholder {name!r} must not be empty."
            )
        keys.append(TransferKey(name=name, expression=expression))
    return keys


def _validate_simple_transfer_key_name(name: str, *, raw_entry: str) -> None:
    if name and name.isidentifier():
        return
    raise ValueError(
        _INVALID_SIMPLE_KEY_MESSAGE
        + f"\nInvalid entry: {raw_entry!r}.\n"
        + "For SQL expressions, use mapping form:\n"
        + "  transfer_keys={'user_id_suffix': 'right(user_id, 1)'}"
    )


def normalize_transfer_key_values(
    keys: list[TransferKey],
    transfer_key_values: Sequence[Any] | Mapping[str, Sequence[Any]],
) -> dict[str, list[Any]]:
    key_names = [key.name for key in keys]
    if isinstance(transfer_key_values, Mapping):
        provided_keys = set(transfer_key_values)
        expected_keys = set(key_names)
        missing = expected_keys - provided_keys
        extra = provided_keys - expected_keys
        if missing or extra:
            details: list[str] = []
            if missing:
                details.append("missing: " + ", ".join(sorted(map(str, missing))))
            if extra:
                details.append("extra: " + ", ".join(sorted(map(str, extra))))
            raise ValueError(
                "transfer_key_values keys must exactly match transfer key placeholder names ("
                + "; ".join(details)
                + ")."
            )
        return {
            key: _normalize_single_key_values(transfer_key_values[key], key) for key in key_names
        }

    if len(keys) != 1:
        raise ValueError("Multiple transfer_keys require mapping transfer_key_values.")
    key_name = keys[0].name
    return {key_name: _normalize_single_key_values(transfer_key_values, key_name)}


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
    source_table: str | None = None,
    transfer_keys: list[TransferKey],
    values: tuple[Any, ...],
) -> TransferSlice:
    predicate_sql = build_transfer_slice_predicate(transfer_keys, values)
    if source_table is None:
        source_sql = render_transfer_slice_source_sql(
            source_sql,
            transfer_keys=transfer_keys,
            values=values,
        )
    else:
        source_sql = render_transfer_table_slice_source_sql(
            source_table,
            predicate_sql=predicate_sql,
        )
    validate_single_rendered_slice_statement(source_sql)
    return TransferSlice(
        index=index,
        values=values,
        predicate_sql=predicate_sql,
        source_sql=source_sql,
        label=f"slice-{index:05d}",
    )


def build_transfer_slice_predicate(
    keys: list[TransferKey],
    values: tuple[Any, ...],
) -> str:
    if len(keys) != len(values):
        raise ValueError("transfer key and value counts must match.")
    predicates = []
    for key, value in zip(keys, values):
        if value is None:
            predicates.append(f"({key.expression}) IS NULL")
        else:
            predicates.append(f"({key.expression}) = {render_transfer_literal(value)}")
    return "\n  AND ".join(predicates)


def render_transfer_table_slice_source_sql(
    source_table: str,
    *,
    predicate_sql: str,
) -> str:
    return f"SELECT * FROM {source_table}\nWHERE {predicate_sql}"


def render_transfer_slice_source_sql(
    source_sql: str,
    *,
    transfer_keys: list[TransferKey],
    values: tuple[Any, ...],
) -> str:
    if len(transfer_keys) != len(values):
        raise ValueError("transfer key and value counts must match.")
    for key in transfer_keys:
        token = "{" + key.name + "}"
        count = source_sql.count(token)
        if count == 0:
            raise ValueError(
                "transfer_keys requires one predicate placeholder per key in from_sql.\n"
                f"Missing placeholder: {token}\n\n"
                "Add it where the source backend should filter rows, for example:\n"
                f"  ... WHERE ... AND {token}\n\n"
                f"If from_sql is an f-string, escape braces as {{{{{key.name}}}}}.\n"
                f'The placeholder is replaced with "({key.expression}) = <value>" '
                f'or "({key.expression}) IS NULL".'
            )
    rendered_sql = source_sql
    for key, value in zip(transfer_keys, values):
        token = "{" + key.name + "}"
        rendered_sql = rendered_sql.replace(
            token,
            build_transfer_slice_predicate([key], (value,)),
        )
    return rendered_sql


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


def validate_single_rendered_slice_statement(source_sql: str) -> None:
    statements = [statement for statement in sqlparse.split(source_sql) if statement.strip()]
    if len(statements) != 1:
        raise ValueError("transfer_keys rendered slice SQL must contain exactly one SQL statement.")
