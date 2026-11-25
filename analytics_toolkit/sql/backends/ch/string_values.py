"""Exact String byte identities shared by native codecs and key validation."""

from __future__ import annotations

from codecs import lookup
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


class ClickHouseStringValueError(ValueError):
    analytics_toolkit_sql_retry_safe = False


def _string_shape(native_type: str) -> tuple[str | None, list[tuple[str, str]]]:
    from analytics_toolkit.sql.backends.nested_types import nested_descriptor  # noqa: PLC0415
    from analytics_toolkit.sql.backends.type_resolution import parse_native_type  # noqa: PLC0415

    from .storage_types import logical_storage_type  # noqa: PLC0415

    logical = logical_storage_type(native_type)
    if parse_native_type(logical).base.name in {"string", "fixedstring"}:
        return "string", []
    return nested_descriptor(logical)


def contains_string_type(native_type: str) -> bool:
    kind, fields = _string_shape(native_type)
    if kind == "string":
        return True
    return any(contains_string_type(child) for _name, child in fields)


def iter_string_values(value: Any, native_type: str) -> Iterator[Any]:
    from analytics_toolkit.sql.backends.nested_values import row_items  # noqa: PLC0415

    kind, fields = _string_shape(native_type)
    if kind == "string":
        yield value
    elif value is not None:
        if kind == "array":
            for item in value:
                yield from iter_string_values(item, fields[0][1])
        elif kind in {"tuple", "row"}:
            for item, (_name, child) in zip(row_items(value, fields), fields):
                yield from iter_string_values(item, child)
        elif kind == "map":
            for key, item in value.items():
                yield from iter_string_values(key, fields[0][1])
                yield from iter_string_values(item, fields[1][1])


def map_string_values(value: Any, native_type: str, convert: Callable[[Any], Any]) -> Any:
    from analytics_toolkit.sql.backends.nested_values import row_items  # noqa: PLC0415

    if value is None:
        return None
    kind, fields = _string_shape(native_type)
    if kind == "string":
        return convert(value)
    if kind == "array":
        return [map_string_values(item, fields[0][1], convert) for item in value]
    if kind in {"tuple", "row"}:
        return tuple(
            map_string_values(item, child, convert)
            for item, (_name, child) in zip(row_items(value, fields), fields)
        )
    if kind == "map":
        from analytics_toolkit.sql.backends.map_values import converted_map  # noqa: PLC0415

        pairs = [
            (
                map_string_values(key, fields[0][1], convert),
                map_string_values(item, fields[1][1], convert),
            )
            for key, item in value.items()
        ]
        return converted_map(
            pairs, "ch", mapping_input=isinstance(value, dict), key_type=fields[0][1]
        )
    return value


def writer_string_encoding(connection: Any) -> str:
    if connection is None:
        return "utf-8"
    from .client_validation import (  # noqa: PLC0415 -- selected owned SDK context.
        _active_native_context,
        _native_base_settings,
        client_write_driver,
    )

    if client_write_driver(connection) != "native":
        return "utf-8"
    settings = _native_base_settings(connection)
    if settings is not None:
        return str(settings[0].get("strings_encoding", "utf-8"))
    context = _active_native_context(connection)
    if context is None:
        message = "Native ClickHouse String key validation requires its selected encoder context."
        raise ClickHouseStringValueError(message)
    return str(context.client_settings.get("strings_encoding", "utf-8"))


def requires_string_value_encoding(connection: Any, context: Any) -> bool:
    # Use the settings selected for the next query, rather than a stale context.
    from .client_validation import (  # noqa: PLC0415
        _native_base_settings,
    )

    selected = _native_base_settings(connection)
    settings = selected[0] if selected is not None else context.client_settings
    if settings.get("strings_as_bytes", False):
        return True
    try:
        return lookup(settings.get("strings_encoding", "utf-8")).name != "utf-8"
    except (LookupError, TypeError):
        return True


def _encode_string_key(value: Any, encoding: str) -> bytes:
    if isinstance(value, str):
        return value.encode(encoding)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    raise TypeError


def string_key_bytes(value: Any, encoding: str, *, width: int | None = None) -> bytes:
    try:
        encoded = _encode_string_key(value, encoding)
    except (LookupError, TypeError, UnicodeError):
        message = "Selected input cannot preserve its native ClickHouse String key identity."
        raise ClickHouseStringValueError(message) from None
    if width is not None:
        if len(encoded) > width:
            message = "Selected input exceeds the selected ClickHouse FixedString byte length."
            raise ClickHouseStringValueError(message)
        return encoded.ljust(width, b"\x00")
    return encoded
