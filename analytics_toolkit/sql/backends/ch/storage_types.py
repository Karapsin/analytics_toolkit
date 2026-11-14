"""Logical ClickHouse value domains without changing physical declarations."""

from __future__ import annotations

from functools import lru_cache

from sqlglot import Dialect, Token, TokenType, exp, parse_one
from sqlglot.errors import SqlglotError

_GEO_TYPES = {
    "Point": "Tuple(Float64, Float64)",
    "Ring": "Array(Point)",
    "Polygon": "Array(Ring)",
    "MultiPolygon": "Array(Polygon)",
}
_CONTAINER_ARITY = {"Array": 1, "Map": 2, "Nullable": 1, "LowCardinality": 1}
_AGGREGATE_ARGUMENTS = 2
_MESSAGE = "Invalid ClickHouse structural storage type declaration."


def _tokens(type_name: str) -> list[Token]:
    try:
        tokens = Dialect.get_or_raise("clickhouse").tokenize(type_name)
    except SqlglotError:
        raise ValueError(_MESSAGE) from None
    depth = 0
    for token in tokens:
        if token.token_type == TokenType.L_PAREN:
            depth += 1
        elif token.token_type == TokenType.R_PAREN:
            depth -= 1
            if depth < 0:
                raise ValueError(_MESSAGE)
    if depth:
        raise ValueError(_MESSAGE)
    return tokens


def _raw_field(field: str, *, require_named: bool) -> tuple[str | None, str, str]:
    """Use the grammar for binding and token offsets for literal-preserving slices."""
    try:
        parsed = parse_one(f"Tuple({field})", read="clickhouse", into=exp.DataType)
    except SqlglotError:
        raise ValueError(_MESSAGE) from None
    if len(parsed.expressions) != 1:
        raise ValueError(_MESSAGE)
    node = parsed.expressions[0]
    if isinstance(node, exp.ColumnDef):
        tokens = _tokens(field)
        if len(tokens) < _AGGREGATE_ARGUMENTS or not node.name:
            raise ValueError(_MESSAGE)
        start = tokens[1].start
        return node.name, field[:start], field[start:]
    if require_named:
        raise ValueError(_MESSAGE)
    return None, "", field


def _fields(name: str, arguments: tuple[str, ...]) -> list[tuple[str | None, str, str]]:
    if name == "Tuple" and arguments == ("",):
        return []
    if not arguments or not all(arguments):
        raise ValueError(_MESSAGE)
    fields = [_raw_field(field, require_named=name == "Nested") for field in arguments]
    names = [field_name for field_name, _, _ in fields if field_name is not None]
    if name == "Nested" and len(set(names)) != len(names):
        raise ValueError(_MESSAGE)
    return fields


@lru_cache(maxsize=1024)
def storage_fields(native_type: str) -> tuple[tuple[str, str], ...] | None:
    """Return raw Nested/Tuple children, retaining named field identity and codecs."""
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- native grammar cycle.
        parse_native_type,
    )

    _tokens(native_type)
    native = parse_native_type(native_type)
    name = native.original.partition("(")[0].strip()
    if name not in {"Nested", "Tuple"}:
        return None
    return tuple(
        (field_name if field_name is not None else f"field{index}", child)
        for index, (field_name, _prefix, child) in enumerate(_fields(name, native.arguments))
    )


def _logical_field_storage(native_type: str, name: str, arguments: tuple[str, ...]) -> str:
    fields = _fields(name, arguments)
    children = tuple(prefix + logical_storage_type(child) for _, prefix, child in fields)
    if name == "Nested":
        return f"Array(Tuple({', '.join(children)}))"
    if not fields or children == arguments:
        return native_type
    return f"Tuple({', '.join(children)})"


@lru_cache(maxsize=1024)
def logical_storage_type(native_type: str) -> str:
    """Expand value aliases only; callers retain raw DDL, metadata and codec types."""
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- native grammar cycle.
        parse_native_type,
    )

    _tokens(native_type)
    native = parse_native_type(native_type)
    name = native.original.partition("(")[0].strip()
    arguments = native.arguments
    if name in _GEO_TYPES:
        if arguments:
            raise ValueError(_MESSAGE)
        return logical_storage_type(_GEO_TYPES[name])
    if name == "SimpleAggregateFunction":
        if len(arguments) != _AGGREGATE_ARGUMENTS or not all(arguments):
            raise ValueError(_MESSAGE)
        return logical_storage_type(arguments[1])
    if name in _CONTAINER_ARITY:
        if len(arguments) != _CONTAINER_ARITY[name] or not all(arguments):
            raise ValueError(_MESSAGE)
        children = tuple(logical_storage_type(child) for child in arguments)
        return native_type if children == arguments else f"{name}({', '.join(children)})"
    if name in {"Nested", "Tuple"}:
        return _logical_field_storage(native_type, name, arguments)
    return native_type
