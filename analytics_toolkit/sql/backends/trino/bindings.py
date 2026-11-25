"""Parameter expressions for source-native values requiring deliberate SQL casts."""

from __future__ import annotations

import datetime as dt
from typing import Any

from analytics_toolkit.sql.backends.native_scalars import JsonDocument


def _special(value: Any, native: str) -> bool:
    from .json_values import is_json_type  # noqa: PLC0415 -- backend initialization.

    if isinstance(value, JsonDocument) and is_json_type(native):
        return True
    from analytics_toolkit.sql.core.exact_values import ExactClock  # noqa: PLC0415

    if isinstance(value, ExactClock):
        return True
    if isinstance(value, dt.time) and value.utcoffset() is None:
        return True
    if isinstance(value, (list, tuple)):
        return any(_special(item, "JSON") for item in value)
    if isinstance(value, dict):
        return any(_special(key, "JSON") or _special(item, "JSON") for key, item in value.items())
    return False


def _binding(value: Any, native: str | None) -> tuple[str, list[Any]]:
    from .json_values import is_json_type  # noqa: PLC0415

    if native and isinstance(value, JsonDocument) and is_json_type(native):
        # CAST(VARCHAR AS JSON) creates a JSON string; native source documents
        # require parsing instead. Ordinary dataframe strings keep CAST semantics.
        return "JSON_PARSE(?)", [str(value)]
    from analytics_toolkit.sql.core.exact_values import ExactClock  # noqa: PLC0415

    if isinstance(value, ExactClock):
        return f"CAST(CAST(? AS TIME({value.scale})) AS {native})", [value.isoformat()]
    if native and _special(value, native):
        return _container_binding(value, native)
    return (f"CAST(? AS {native})" if native else "?"), [value]


def _container_binding(value: Any, native: str) -> tuple[str, list[Any]]:
    from analytics_toolkit.sql.backends.nested_types import nested_descriptor  # noqa: PLC0415

    kind, fields = nested_descriptor(native)
    if kind == "array":
        children = [_binding(item, fields[0][1]) for item in value]
        expression = "ARRAY[" + ", ".join(child for child, _args in children) + "]"
    elif kind in {"row", "tuple"}:
        items = [value[name] for name, _native in fields] if isinstance(value, dict) else value
        children = [_binding(item, child) for item, (_name, child) in zip(items, fields)]
        expression = "ROW(" + ", ".join(child for child, _args in children) + ")"
    elif kind == "map":
        keys = [_binding(key, fields[0][1]) for key in value]
        values = [_binding(item, fields[1][1]) for item in value.values()]
        children = keys + values
        expression = (
            "MAP(ARRAY["
            + ", ".join(child for child, _args in keys)
            + "], ARRAY["
            + ", ".join(child for child, _args in values)
            + "])"
        )
    else:
        return f"CAST(? AS {native})", [value]
    return f"CAST({expression} AS {native})", [item for _sql, args in children for item in args]


def parameterized_values(
    columns: Any,
    rows: Any,
    column_types: dict[str, str] | None,
) -> tuple[str, list[Any]]:
    """Keep standard parameters intact; expand only exact native transport cells."""
    types = column_types or {}
    expressions: list[str] = []
    parameters: list[Any] = []
    for row in rows:
        bindings = [_binding(value, types.get(column)) for column, value in zip(columns, row)]
        expressions.append("(" + ", ".join(sql for sql, _args in bindings) + ")")
        parameters.extend(value for _sql, args in bindings for value in args)
    return ", ".join(expressions), parameters
