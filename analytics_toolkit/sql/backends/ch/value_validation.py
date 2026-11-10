"""Exact validation of ClickHouse constrained scalar types before publication."""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from numbers import Integral
from typing import Any

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError


@lru_cache(maxsize=1024)
def enum_members(type_name: str) -> tuple[frozenset[str], frozenset[int]]:
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 - native grammar cycle
        parse_native_type,
    )

    parsed = parse_native_type(type_name).base
    bits = _ENUM_WIDTHS[parsed.name]
    labels = []
    codes = []
    for argument in parsed.arguments:
        try:
            expression = parse_one(argument, read="clickhouse")
        except ParseError as exc:
            msg = "ClickHouse Enum requires static labels and integer codes."
            raise ValueError(msg) from exc
        if (
            not isinstance(expression, exp.EQ)
            or not isinstance(expression.left, exp.Literal)
            or not expression.left.is_string
        ):
            msg = "ClickHouse Enum requires static string labels and integer codes."
            raise ValueError(msg)
        try:
            code = int(expression.right.sql(dialect="clickhouse"))
        except (TypeError, ValueError) as exc:
            msg = "ClickHouse Enum requires static integer codes."
            raise ValueError(msg) from exc
        if not -(2 ** (bits - 1)) <= code < 2 ** (bits - 1):
            msg = "ClickHouse Enum code exceeds its declared integer width."
            raise ValueError(msg)
        labels.append(str(expression.left.this))
        codes.append(code)
    if not labels or len(set(labels)) != len(labels) or len(set(codes)) != len(codes):
        msg = "ClickHouse Enum labels and codes must be nonempty and unique."
        raise ValueError(msg)
    return frozenset(labels), frozenset(codes)


_ENUM_WIDTHS = {"enum8": 8, "enum16": 16}


def validate_constrained_value(value: Any, type_name: str) -> Any:
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 - native grammar cycle
        parse_native_type,
    )

    parsed = parse_native_type(type_name).base
    if parsed.name == "fixedstring":
        if (
            len(parsed.arguments) != 1
            or not parsed.arguments[0].isdigit()
            or int(parsed.arguments[0]) < 1
        ):
            msg = "ClickHouse FixedString requires a positive byte length."
            raise ValueError(msg)
        if not isinstance(value, (str, bytes, bytearray, memoryview)):
            msg = "Selected input requires strings or bytes for authoritative FixedString."
            raise ValueError(msg)
        try:
            encoded = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        except UnicodeEncodeError as exc:
            msg = "Selected input is not UTF-8 encodable for authoritative FixedString."
            raise ValueError(msg) from exc
        if len(encoded) > int(parsed.arguments[0]):
            msg = "Selected input exceeds authoritative FixedString byte length."
            raise ValueError(msg)
        return value if isinstance(value, str) else encoded
    if parsed.name in _ENUM_WIDTHS:
        labels, codes = enum_members(type_name)
        candidate = value.value if isinstance(value, Enum) else value
        if isinstance(candidate, str) and candidate in labels:
            return candidate
        if (
            isinstance(candidate, Integral)
            and not isinstance(candidate, bool)
            and int(candidate) in codes
        ):
            return int(candidate)
        msg = "Selected input is not a member of the authoritative ClickHouse Enum."
        raise ValueError(msg)
    return value
