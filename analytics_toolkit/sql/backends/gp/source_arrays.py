"""Exact PostgreSQL array text, including dimensions and nondefault bounds."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, NoReturn

from analytics_toolkit.sql.backends.native_scalars import ExactArray, ExactInterval

if TYPE_CHECKING:
    from collections.abc import Callable

_DIMENSION = re.compile(r"\[([+-]?[0-9]+):([+-]?[0-9]+)\]", flags=re.ASCII)
_MAX_RANK = 6


class SourceArrayError(ValueError):
    """An incomplete or unsupported source array cannot become certified input."""

    analytics_toolkit_sql_retry_safe = False


def _invalid_array() -> NoReturn:
    message = "Cannot preserve the exact native source array representation."
    raise SourceArrayError(message)


@dataclass
class _ArrayReader:
    text: str
    decode: Callable[[str], Any]
    index: int = 0

    def skip_space(self) -> None:
        while self.index < len(self.text) and self.text[self.index] in " \t\r\n\v\f":
            self.index += 1

    def peek(self) -> str:
        self.skip_space()
        return self.text[self.index : self.index + 1]

    def token(self) -> tuple[str, bool]:
        quoted = self.peek() == '"'
        if quoted:
            self.index += 1
        result = []
        while self.index < len(self.text):
            char = self.text[self.index]
            if char == "\\":
                self.index += 1
                if self.index >= len(self.text):
                    _invalid_array()
                result.append(self.text[self.index])
                self.index += 1
            elif quoted and char == '"':
                self.index += 1
                return "".join(result), True
            elif not quoted and char in ",}":
                return "".join(result).strip(), False
            else:
                result.append(char)
                self.index += 1
        _invalid_array()

    def array(self, depth: int = 1) -> tuple[tuple[int, ...], list[Any]]:
        if depth > _MAX_RANK or self.peek() != "{":
            _invalid_array()
        self.index += 1
        if self.peek() == "}":
            self.index += 1
            return (0,), []
        count = 0
        shape: tuple[int, ...] | None = None
        flattened = []
        while True:
            if self.peek() == "{":
                child_shape, values = self.array(depth + 1)
            else:
                token, quoted = self.token()
                if not quoted and not token:
                    _invalid_array()
                values = [None if not quoted and token.upper() == "NULL" else self.decode(token)]
                child_shape = ()
            if shape is not None and child_shape != shape:
                _invalid_array()
            shape = child_shape
            flattened.extend(values)
            count += 1
            separator = self.peek()
            if separator not in {",", "}"}:
                _invalid_array()
            self.index += 1
            if separator == "}":
                return (count, *(shape or ())), flattened


def decode_array_text(value: str, decode: Callable[[str], Any]) -> ExactArray:
    """Decode source tokens once while preserving physical SQL array identity."""
    reader = _ArrayReader(value, decode)
    dimensions: list[int] = []
    bounds: list[int] = []
    while reader.peek() == "[":
        match = _DIMENSION.match(value, reader.index)
        if match is None or len(dimensions) >= _MAX_RANK:
            _invalid_array()
        lower, upper = int(match[1]), int(match[2])
        if not -(2**31) <= lower <= upper < 2**31:
            _invalid_array()
        dimensions.append(upper - lower + 1)
        bounds.append(lower)
        reader.index = match.end()
    if dimensions:
        if reader.peek() != "=":
            _invalid_array()
        reader.index += 1
    shape, cells = reader.array()
    if reader.peek() or (dimensions and tuple(dimensions) != shape):
        _invalid_array()
    if not cells and shape == (0,):
        return ExactArray((), (), ())
    return ExactArray(tuple(cells), shape, tuple(bounds or [1] * len(shape)))


def interval_transport(value: ExactInterval) -> str:
    """Keep independent calendar components in native PostgreSQL input syntax."""
    if not (
        -(2**31) <= value.months < 2**31
        and -(2**31) <= value.days < 2**31
        and -(2**63) <= value.microseconds < 2**63
    ):
        message = "Native interval components exceed their storage domain."
        raise ValueError(message)
    return f"{value.months:+d} mons {value.days:+d} days {value.microseconds:+d} microseconds"


def _array_token(value: Any) -> str:
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 -- codec cycle.
        ExactClock,
        ExactTemporal,
        is_null,
    )

    if is_null(value):
        return "NULL"
    if isinstance(value, ExactInterval):
        text = interval_transport(value)
    elif isinstance(value, (ExactClock, ExactTemporal)):
        text = value.isoformat()
    elif isinstance(value, (bytes, bytearray, memoryview)):
        text = "\\x" + bytes(value).hex()
    elif isinstance(value, (dt.datetime, dt.date, dt.time)):
        text = value.isoformat()
    elif isinstance(value, bool):
        text = "t" if value else "f"
    else:
        text = str(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def array_transport(value: ExactArray) -> str:
    """Render actual source bounds without rebasing a native array to one."""
    if len(value.dimensions) > _MAX_RANK:
        _invalid_array()
    if not value.dimensions:
        return "{}"
    cells = iter(value.values)

    def block(dimensions: tuple[int, ...]) -> str:
        if len(dimensions) == 1:
            return "{" + ",".join(_array_token(next(cells)) for _ in range(dimensions[0])) + "}"
        return "{" + ",".join(block(dimensions[1:]) for _ in range(dimensions[0])) + "}"

    prefix = "".join(
        f"[{lower}:{lower + size - 1}]" for size, lower in zip(value.dimensions, value.lower_bounds)
    )
    return prefix + "=" + block(value.dimensions)


def map_array_values(value: ExactArray, convert: Callable[[Any], Any]) -> ExactArray:
    """Own converted flat cells while retaining the original physical topology."""
    return replace(value, values=tuple(convert(cell) for cell in value.values))
