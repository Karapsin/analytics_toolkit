"""Certify supported Trino declarations independently of observed cell values."""

from __future__ import annotations

import re
from functools import lru_cache

from sqlglot.errors import SqlglotError

from .date_values import TrinoDateValueError, is_date_type

_SCALARS = frozenset(
    {
        "tinyint",
        "smallint",
        "integer",
        "int",
        "bigint",
        "real",
        "double",
        "double precision",
        "boolean",
        "date",
        "varbinary",
        "uuid",
        "json",
        "ipaddress",
        "interval year to month",
        "interval day to second",
    }
)
_TEMPORAL = re.compile(
    r"(?:timestamp|time)\s*(?:\(\s*([0-9]+)\s*\))?"
    r"(?:\s+(?:with|without)\s+time\s+zone)?",
    re.IGNORECASE | re.ASCII,
)
_MESSAGE = (
    "Invalid or unsupported Trino native type declaration, "
    "precision, parameters or container arity."
)
_MAX_TEMPORAL_PRECISION = 12
_PAIR_ARITY = 2
_MAX_DECIMAL_PRECISION = 38


class TrinoTypeDeclarationError(ValueError):
    analytics_toolkit_sql_retry_safe = False


def _numbers(arguments: tuple[str, ...], minimum: int, maximum: int) -> list[int]:
    if not minimum <= len(arguments) <= maximum or any(
        re.fullmatch(r"[0-9]+", token, flags=re.ASCII) is None for token in arguments
    ):
        raise TrinoTypeDeclarationError(_MESSAGE)
    return [int(token) for token in arguments]


@lru_cache(maxsize=2048)
def validate_type_declaration(native_type: str) -> None:
    """Reject malformed SQL before a NULL or empty input can conceal its schema."""
    try:
        _validate_type_declaration(native_type)
    except (TrinoTypeDeclarationError, TrinoDateValueError):
        raise
    except (TypeError, ValueError, SqlglotError):
        raise TrinoTypeDeclarationError(_MESSAGE) from None


def _validate_container(token: str, family: str, arguments: tuple[str, ...]) -> bool:
    from analytics_toolkit.sql.backends.nested_types import nested_descriptor  # noqa: PLC0415

    if family in {"array", "map"}:
        if len(arguments) != (1 if family == "array" else _PAIR_ARITY):
            raise TrinoTypeDeclarationError(_MESSAGE)
        for child in arguments:
            validate_type_declaration(child)
        return True
    if family == "row":
        kind, fields = nested_descriptor(token)
        if kind != "row" or not arguments or len(fields) != len(arguments):
            raise TrinoTypeDeclarationError(_MESSAGE)
        for _name, child in fields:
            validate_type_declaration(child)
        return True
    return False


def _validate_native_leaf(token: str, family: str) -> bool:
    if family == "date":
        is_date_type(token)
        return True
    if family not in {"json", "ipaddress"}:
        return False
    if family == "json":
        from .json_values import (  # noqa: PLC0415 -- core exact-value import cycle.
            is_json_type as validate_leaf,
        )
    else:
        from .ip_values import is_ip_type as validate_leaf  # noqa: PLC0415 -- native IP grammar.
    try:
        validate_leaf(token)
    except ValueError as error:
        raise TrinoTypeDeclarationError(str(error)) from None
    return True


def _validate_scalar(token: str, family: str, arguments: tuple[str, ...]) -> None:
    if _validate_native_leaf(token, family):
        return
    if family in {"varchar", "char", "character"}:
        values = _numbers(arguments, 0, 1)
        # These bounds follow Trino SPI VarcharType and CharType. Both permit 0.
        # VarcharParametricType also accepts the reserved unbounded-length value.
        maximum = 2**31 - 1 if family == "varchar" else 65536
        if values and not 0 <= values[0] <= maximum:
            raise TrinoTypeDeclarationError(_MESSAGE)
        return
    if family == "decimal":
        values = _numbers(arguments, 0, _PAIR_ARITY)
        precision, scale = (
            (values[0], values[1] if len(values) == _PAIR_ARITY else 0)
            if values
            else (_MAX_DECIMAL_PRECISION, 0)
        )
        if not 1 <= precision <= _MAX_DECIMAL_PRECISION or not 0 <= scale <= precision:
            raise TrinoTypeDeclarationError(_MESSAGE)
        return
    if family not in _SCALARS or arguments or " ".join(token.casefold().split()) != family:
        raise TrinoTypeDeclarationError(_MESSAGE)


def _validate_type_declaration(native_type: str) -> None:
    from analytics_toolkit.sql.backends.type_resolution import parse_native_type  # noqa: PLC0415

    token = native_type.strip()
    temporal = _TEMPORAL.fullmatch(token)
    if temporal is not None:
        if temporal[1] is not None and not 0 <= int(temporal[1]) <= _MAX_TEMPORAL_PRECISION:
            raise TrinoTypeDeclarationError(_MESSAGE)
        return
    native = parse_native_type(token)
    if not _validate_container(token, native.name, native.arguments):
        _validate_scalar(token, native.name, native.arguments)
