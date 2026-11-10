"""Resolved Hive-stage types and exact values, including nested Trino types."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from typing import Any, cast
from uuid import UUID

from sqlglot import exp
from sqlglot.errors import ParseError

_DECIMAL128_PRECISION = 38
_MAP_ARITY = 2
_HIVE_TIMESTAMP_PRECISION = 6
_FACTORIES = {
    "VARCHAR": "string",
    "CHAR": "string",
    "TEXT": "string",
    "UUID": "string",
    "JSON": "string",
    "BINARY": "binary",
    "VARBINARY": "binary",
    "BOOLEAN": "bool_",
    "TINYINT": "int64",
    "SMALLINT": "int64",
    "INT": "int64",
    "BIGINT": "int64",
    "FLOAT": "float64",
    "DOUBLE": "float64",
    "DATE": "date32",
}
_STRING_FAMILIES = frozenset({"VARCHAR", "CHAR", "TEXT", "UUID", "TIMESTAMPTZ"})
_CONTAINERS = frozenset({"ARRAY", "MAP", "STRUCT"})


@lru_cache(maxsize=1)
def _values() -> Any:
    # The backend registry imports this module before core.capabilities exists.
    from analytics_toolkit.sql.core import exact_values  # noqa: PLC0415

    return exact_values


@lru_cache(maxsize=1024)
def _parse(column_type: str) -> exp.DataType | None:
    try:
        return exp.DataType.build(column_type, dialect="trino")
    except (ParseError, ValueError):
        return None


def _family(data_type: exp.DataType) -> str:
    return str(data_type.this.value)


def _children(data_type: exp.DataType, expected: int) -> list[exp.DataType]:
    children = data_type.expressions
    if len(children) != expected or any(not isinstance(item, exp.DataType) for item in children):
        message = "Invalid nested Trino type in resolved Parquet stage schema."
        raise ValueError(message)
    return [cast("exp.DataType", item) for item in children]


def _row_fields(data_type: exp.DataType) -> list[tuple[str, exp.DataType]]:
    fields: list[tuple[str, exp.DataType]] = []
    named = {item.name for item in data_type.expressions if isinstance(item, exp.ColumnDef)}
    for index, item in enumerate(data_type.expressions):
        if isinstance(item, exp.ColumnDef):
            name, child = item.name, item.args.get("kind")
        else:
            name, child = f"_field_{index}", item
            while name in named:
                name += "_"
        if not isinstance(child, exp.DataType) or name in {field[0] for field in fields}:
            message = "ROW stage fields require distinct names and supported native types."
            raise ValueError(message)
        fields.append((name, child))
    if not fields:
        message = "A resolved ROW Parquet stage type must have at least one field."
        raise ValueError(message)
    return fields


def _decimal_parameters(data_type: exp.DataType) -> tuple[int, int]:
    values = [int(item.name) for item in data_type.expressions]
    return (values[0], values[1] if len(values) > 1 else 0) if values else (38, 0)


def arrow_type_for_trino_stage(pa: Any, column_type: str | None) -> Any | None:
    """Keep the historical ``None`` result for unknown or unresolved types."""
    data_type = _parse(column_type) if column_type is not None else None
    return _arrow_type(pa, data_type) if data_type is not None else None


def _arrow_type(pa: Any, data_type: exp.DataType) -> Any | None:
    family = _family(data_type)
    if family in _CONTAINERS:
        return _nested_arrow_type(pa, data_type)
    if family in {"TIMESTAMP", "TIMESTAMPTZ"}:
        return pa.string() if _timestamp_as_text(data_type) else pa.timestamp("us")
    if family == "DECIMAL":
        precision, scale = _decimal_parameters(data_type)
        factory = pa.decimal128 if precision <= _DECIMAL128_PRECISION else pa.decimal256
        return factory(precision, scale)
    factory_name = _FACTORIES.get(family)
    return getattr(pa, factory_name)() if factory_name is not None else None


def _nested_arrow_type(pa: Any, data_type: exp.DataType) -> Any | None:
    family = _family(data_type)
    if family == "STRUCT":
        fields = [(name, _arrow_type(pa, child)) for name, child in _row_fields(data_type)]
        return (
            pa.struct([pa.field(name, child) for name, child in fields])
            if all(child is not None for _, child in fields)
            else None
        )
    children = _children(data_type, 1 if family == "ARRAY" else _MAP_ARITY)
    child_types = [_arrow_type(pa, child) for child in children]
    if any(child is None for child in child_types):
        return None
    return pa.list_(child_types[0]) if family == "ARRAY" else pa.map_(*child_types)


def _supported(data_type: exp.DataType) -> bool:
    family = _family(data_type)
    if family == "STRUCT":
        return all(_supported(child) for _, child in _row_fields(data_type))
    if family in {"ARRAY", "MAP"}:
        return all(
            _supported(child)
            for child in _children(data_type, 1 if family == "ARRAY" else _MAP_ARITY)
        )
    return family in _FACTORIES or family in {"TIMESTAMP", "TIMESTAMPTZ", "DECIMAL"}


@lru_cache(maxsize=1024)
def require_supported_stage_type(column_type: str) -> exp.DataType:
    data_type = _parse(column_type)
    if data_type is None or not _supported(data_type):
        message = (
            "Unsupported resolved Trino Parquet stage type. "
            "Resolve the column to a supported native type or an explicit VARCHAR encoding."
        )
        raise ValueError(message)
    return data_type


def hive_stage_type(column_type: str) -> str:
    data_type = require_supported_stage_type(column_type)
    family = _family(data_type)
    if family in _CONTAINERS or family in {"UUID", "TIMESTAMP", "TIMESTAMPTZ", "BINARY", "TEXT"}:
        return str(_stage_type(data_type).sql(dialect="trino"))
    return column_type


def _stage_type(data_type: exp.DataType) -> exp.DataType:
    family = _family(data_type)
    if family == "UUID" or _timestamp_as_text(data_type):
        return exp.DataType.build("VARCHAR", dialect="trino")
    if family == "TIMESTAMP":
        return exp.DataType.build("TIMESTAMP(6)", dialect="trino")
    converted = data_type.copy()
    if family == "STRUCT":
        original_fields = data_type.expressions
        converted.set(
            "expressions",
            [
                exp.ColumnDef(
                    this=(
                        original_fields[index].this.copy()
                        if isinstance(original_fields[index], exp.ColumnDef)
                        else exp.to_identifier(name, quoted=True)
                    ),
                    kind=_stage_type(child),
                )
                for index, (name, child) in enumerate(_row_fields(data_type))
            ],
        )
    elif family in {"ARRAY", "MAP"}:
        converted.set("expressions", [_stage_type(child) for child in data_type.expressions])
    return converted


def parquet_scalar(value: Any, column_type: str | None = None) -> Any:
    if _values().is_null(value):
        return None
    if column_type is None:
        return str(value) if isinstance(value, UUID) else _binary_scalar(value)
    return _normalize(value, require_supported_stage_type(column_type))


def _normalize(value: Any, data_type: exp.DataType) -> Any:
    if _values().is_null(value):
        return None
    family = _family(data_type)
    if family == "ARRAY":
        child = _children(data_type, 1)[0]
        return [_normalize(item, child) for item in _sequence(value)]
    if family == "MAP":
        return _normalize_map(value, data_type)
    if family == "STRUCT":
        return _normalize_row(value, data_type)
    return _normalize_scalar(value, data_type)


def _sequence(value: Any) -> Sequence[Any]:
    to_list = getattr(value, "tolist", None)
    values = to_list() if callable(to_list) else value
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray, memoryview)):
        message = "An ARRAY or positional ROW stage value must be a sequence."
        raise TypeError(message)
    return values


def _normalize_map(value: Any, data_type: exp.DataType) -> list[tuple[Any, Any]]:
    key_type, value_type = _children(data_type, _MAP_ARITY)
    pairs = (
        value.items()
        if isinstance(value, (Mapping, _values().OrderedMap, _values().OrderedMapPairs))
        else _sequence(value)
    )
    normalized = []
    for raw_pair in pairs:
        pair = _sequence(raw_pair)
        if len(pair) != _MAP_ARITY or _values().is_null(pair[0]):
            message = "MAP stage values require key/value pairs with non-NULL keys."
            raise ValueError(message)
        normalized.append((_normalize(pair[0], key_type), _normalize(pair[1], value_type)))
    return normalized


def _normalize_row(value: Any, data_type: exp.DataType) -> dict[str, Any]:
    fields = _row_fields(data_type)
    if isinstance(value, Mapping):
        if set(value) != {name for name, _ in fields}:
            message = "ROW stage mapping keys must match the resolved field names."
            raise ValueError(message)
        return {name: _normalize(value[name], child) for name, child in fields}
    values = _sequence(value)
    if len(values) != len(fields):
        message = "ROW stage value width must match the resolved field count."
        raise ValueError(message)
    return {name: _normalize(item, child) for (name, child), item in zip(fields, values)}


def _timestamp_precision(data_type: exp.DataType) -> int:
    return (
        int(data_type.expressions[0].name) if data_type.expressions else _HIVE_TIMESTAMP_PRECISION
    )


def _timestamp_as_text(data_type: exp.DataType) -> bool:
    family = _family(data_type)
    return family == "TIMESTAMPTZ" or (
        family == "TIMESTAMP" and _timestamp_precision(data_type) > _HIVE_TIMESTAMP_PRECISION
    )


def _normalize_scalar(value: Any, data_type: exp.DataType) -> Any:
    family = _family(data_type)
    if family in _STRING_FAMILIES or _timestamp_as_text(data_type):
        precision = (
            _timestamp_precision(data_type) if family in {"TIMESTAMP", "TIMESTAMPTZ"} else None
        )
        return _values().typed_text(value, fractional_scale=precision)
    if family in {"FLOAT", "DOUBLE"} and isinstance(value, Decimal):
        return float(value)
    if family == "TIMESTAMP":
        if isinstance(value, _values().ExactTemporal):
            return value.calendar_datetime()
        if isinstance(value, datetime) and hasattr(value, "nanosecond"):
            # Inferred microsecond schemas are certified as aligned already.
            # An authoritative lower precision retains the caller's conversion.
            return cast("Any", value).to_pydatetime(warn=False)
    return _binary_scalar(value)


def _binary_scalar(value: Any) -> Any:
    return bytes(value) if isinstance(value, (bytearray, memoryview)) else value
