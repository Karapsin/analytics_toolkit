"""Cursor-local exact decoding for internal GP transfer sources only."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from analytics_toolkit.sql.backends.native_scalars import (
    JsonDocument,
    NativeDecimalNaN,
    NativeFloatNaN,
)

from .source_arrays import SourceArrayError, decode_array_text
from .source_temporals import decode_interval_text, decode_time_text

if TYPE_CHECKING:
    from collections.abc import Callable

_ARRAY_ELEMENTS = {
    1000: 16,
    1001: 17,
    1002: 18,
    1003: 19,
    1005: 21,
    1007: 23,
    1009: 25,
    1014: 1042,
    1015: 1043,
    1016: 20,
    1021: 700,
    1022: 701,
    1028: 26,
    1115: 1114,
    1182: 1082,
    1183: 1083,
    1185: 1184,
    1187: 1186,
    1231: 1700,
    199: 114,
    2951: 2950,
    3807: 3802,
    1041: 869,
    651: 650,
}


def _json_document(value: str | None, _cursor: Any) -> JsonDocument | None:
    return None if value is None else JsonDocument(value)


def _numeric_value(value: str | None, _cursor: Any) -> Decimal | None:
    if value is None:
        return None
    numeric = Decimal(value)
    return NativeDecimalNaN(numeric) if numeric.is_qnan() else numeric


def _float_value(value: str | None, _cursor: Any) -> float | None:
    if value is None:
        return None
    numeric = float(value)
    return NativeFloatNaN(numeric) if math.isnan(numeric) else numeric


def _boolean_value(text: str) -> bool:
    if text not in {"t", "f"}:
        message = "Cannot preserve the native source Boolean array."
        raise SourceArrayError(message)
    return text == "t"


_VALUE_DECODERS: dict[int, Callable[[str], Any]] = {
    16: _boolean_value,
    20: int,
    21: int,
    23: int,
    26: int,
    2950: UUID,
}
_CURSOR_DECODERS: dict[int, Callable[[str, Any], Any]] = {
    114: _json_document,
    3802: _json_document,
    1700: _numeric_value,
    700: _float_value,
    701: _float_value,
    1083: decode_time_text,
    1186: decode_interval_text,
}


def _decode_scalar(oid: int, text: str, cursor: Any) -> Any:
    if oid in _VALUE_DECODERS:
        return _VALUE_DECODERS[oid](text)
    if oid in _CURSOR_DECODERS:
        return _CURSOR_DECODERS[oid](text, cursor)
    if oid in {18, 19, 25, 1042, 1043, 869, 650}:
        return text
    from psycopg2 import extensions  # noqa: PLC0415 -- optional native driver.

    if not isinstance(cursor, extensions.cursor) or oid not in extensions.string_types:
        message = "Cannot delegate the native source scalar decoder without its driver cursor."
        raise SourceArrayError(message)
    return extensions.string_types[oid](text, cursor)


def _array_decoder(oid: int) -> Any:
    def decode(value: str | None, cursor: Any) -> Any:
        if value is None:
            return None
        try:
            return decode_array_text(value, lambda text: _decode_scalar(oid, text, cursor))
        except (TypeError, ValueError, OverflowError):
            message = "Cannot preserve the exact native source array representation."
            raise SourceArrayError(message) from None

    return decode


def _registration_cursor(cursor: Any) -> Any:
    from analytics_toolkit.sql.execution.metadata_cancellation import (  # noqa: PLC0415
        _MetadataConnection,
    )
    from analytics_toolkit.sql.execution.observation import _ObservedConnection  # noqa: PLC0415

    seen = set()
    while isinstance(cursor, (_ObservedConnection, _MetadataConnection)):
        if id(cursor) in seen:
            message = "Cannot resolve the native source cursor through a cyclic wrapper."
            raise ValueError(message)
        seen.add(id(cursor))
        cursor = (
            vars(cursor)["_target"]
            if isinstance(cursor, _ObservedConnection)
            else cursor.connection
        )
    return cursor


def prepare_exact_source_cursor(connection: Any, cursor: Any) -> None:
    """Override lossy source decoders without changing ordinary public reads."""
    from psycopg2.extensions import new_type, register_type  # noqa: PLC0415 -- optional driver.

    del connection
    cursor = _registration_cursor(cursor)
    for oid, name in ((114, "JSON"), (3802, "JSONB")):
        register_type(new_type((oid,), f"ATK_EXACT_{name}", _json_document), cursor)
    register_type(new_type((1083,), "ATK_EXACT_TIME", decode_time_text), cursor)
    register_type(new_type((1186,), "ATK_EXACT_INTERVAL", decode_interval_text), cursor)
    register_type(new_type((1700,), "ATK_EXACT_NUMERIC", _numeric_value), cursor)
    register_type(new_type((700, 701), "ATK_EXACT_FLOAT", _float_value), cursor)
    for array_oid, scalar_oid in _ARRAY_ELEMENTS.items():
        register_type(
            new_type((array_oid,), f"ATK_EXACT_ARRAY_{array_oid}", _array_decoder(scalar_oid)),
            cursor,
        )
