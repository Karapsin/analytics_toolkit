"""Decode PostgreSQL send-format floats without involving text output precision."""

from __future__ import annotations

import math
import struct
from typing import Any

from analytics_toolkit.sql.backends.native_scalars import ExactArray, NativeFloatNaN

_FLOAT32_BYTES = 4
_WORD_BYTES = 4
_MAX_ARRAY_RANK = 6


class SourceFloatWireError(ValueError):
    """An internal source cannot certify its exact native float representation."""

    analytics_toolkit_sql_retry_safe = False


def _invalid() -> SourceFloatWireError:
    return SourceFloatWireError(
        "Cannot certify the exact native Greenplum float source representation."
    )


def decode_float(value: Any, width: int) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (bytes, bytearray, memoryview)) or len(value) != width:
        raise _invalid()
    result = struct.unpack("!f" if width == _FLOAT32_BYTES else "!d", value)[0]
    return NativeFloatNaN(result) if math.isnan(result) else result


class _ArrayReader:
    def __init__(self, value: bytes | bytearray | memoryview):
        self.raw = memoryview(value)
        self.offset = 0

    @property
    def remaining(self) -> int:
        return len(self.raw) - self.offset

    def integer(self) -> int:
        if self.remaining < _WORD_BYTES:
            raise _invalid()
        result = int(struct.unpack_from("!i", self.raw, self.offset)[0])
        self.offset += _WORD_BYTES
        return result

    def cell(self, width: int) -> float | None:
        length = self.integer()
        if length == -1:
            return None
        if length != width or self.remaining < width:
            raise _invalid()
        result = decode_float(self.raw[self.offset : self.offset + width], width)
        self.offset += width
        return result


def _array_shape(reader: _ArrayReader, element_oid: int) -> tuple[list[int], list[int], int, int]:
    rank, has_null, actual_oid = reader.integer(), reader.integer(), reader.integer()
    if not 0 <= rank <= _MAX_ARRAY_RANK or has_null not in {0, 1} or actual_oid != element_oid:
        raise _invalid()
    dimensions: list[int] = []
    bounds: list[int] = []
    count = 1 if rank else 0
    for _index in range(rank):
        length, lower = reader.integer(), reader.integer()
        if length < 0:
            raise _invalid()
        dimensions.append(length)
        bounds.append(lower)
        count *= length
    # Each element needs at least its four-byte length, even when NULL.
    if count > reader.remaining // _WORD_BYTES:
        raise _invalid()
    return dimensions, bounds, count, has_null


def decode_float_array(value: Any, width: int, element_oid: int) -> ExactArray | None:
    if value is None:
        return None
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise _invalid()
    reader = _ArrayReader(value)
    dimensions, bounds, count, has_null = _array_shape(reader, element_oid)
    cells = tuple(reader.cell(width) for _index in range(count))
    if reader.remaining or (None in cells and not has_null):
        raise _invalid()
    return ExactArray(cells, tuple(dimensions), tuple(bounds))
