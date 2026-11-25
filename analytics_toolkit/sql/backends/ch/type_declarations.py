"""Validate known ClickHouse storage declarations independently of row values."""

from __future__ import annotations

from functools import lru_cache

from .storage_types import logical_storage_type

_UNPARAMETERIZED = frozenset(
    {
        "string",
        "bool",
        "boolean",
        "uuid",
        "ipv4",
        "ipv6",
        "date",
        "date32",
        "nothing",
        "float32",
        "float64",
        "bfloat16",
        *(f"{prefix}{bits}" for prefix in ("int", "uint") for bits in (8, 16, 32, 64, 128, 256)),
    }
)


@lru_cache(maxsize=2048)
def validate_type_declaration(native_type: str) -> None:
    """Keep opaque declarations opaque while rejecting malformed known types."""
    from analytics_toolkit.sql.backends.decimal_values import (  # noqa: PLC0415 -- native grammar initialization cycle.
        validate_decimal_type,
    )
    from analytics_toolkit.sql.backends.nested_types import (  # noqa: PLC0415 -- native grammar initialization cycle.
        nested_descriptor,
    )
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- native grammar initialization cycle.
        parse_native_type,
    )

    storage = logical_storage_type(native_type)
    native = parse_native_type(storage)
    if native.name in _UNPARAMETERIZED and native.arguments:
        message = "ClickHouse native type declaration does not accept type arguments."
        raise ValueError(message)
    validate_decimal_type(storage, "ch")
    kind, children = nested_descriptor(storage)
    if kind:
        for _name, child in children:
            validate_type_declaration(child)
    elif native.name in {"nullable", "lowcardinality"}:
        validate_type_declaration(native.arguments[0])
