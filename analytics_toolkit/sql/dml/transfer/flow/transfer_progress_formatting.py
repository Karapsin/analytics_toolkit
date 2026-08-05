from __future__ import annotations

import math
import re
from datetime import date, datetime
from datetime import time as datetime_time
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from collections.abc import Sequence

MAX_SLICE_TAG_LENGTH = 240
MAX_SLICE_KEY_NAME_LENGTH = 48
MAX_SLICE_KEY_VALUE_LENGTH = 64
MIN_SLICE_TAG_LENGTH = 12
_SENSITIVE_KEY_PARTS = frozenset(
    {
        "accesskey",
        "apikey",
        "authorization",
        "credential",
        "password",
        "passwd",
        "privatekey",
        "secret",
        "token",
    }
)
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$")


def format_slice_tag(
    slice_position: int,
    slice_count: int,
    key_values: Sequence[tuple[str, object]] | None = None,
    *,
    max_length: int = MAX_SLICE_TAG_LENGTH,
) -> str:
    """Build a bounded, stable tag without logging arbitrary object reprs."""
    _require_positive_int(slice_position, name="slice_position")
    _require_positive_int(slice_count, name="slice_count")
    if slice_position > slice_count:
        msg = "slice_position cannot exceed slice_count"
        raise ValueError(msg)
    _require_positive_int(max_length, name="max_length")
    if max_length < MIN_SLICE_TAG_LENGTH:
        msg = f"max_length must be at least {MIN_SLICE_TAG_LENGTH}"
        raise ValueError(msg)
    bounded_length = min(max_length, MAX_SLICE_TAG_LENGTH)
    parts = [
        f"{_safe_key_name(name)}:{_safe_key_value(name, value)}"
        for name, value in (key_values or ())
    ]
    key_fragment = f" key={','.join(parts)}" if parts else ""
    tag = f"[slice={slice_position}/{slice_count}{key_fragment}]"
    if len(tag) <= bounded_length:
        return tag
    return f"{tag[: bounded_length - 2]}…]"


def _safe_key_name(name: str) -> str:
    safe_name = "".join(
        character if character.isascii() and (character.isalnum() or character in "_.-") else "_"
        for character in str(name)
    )
    safe_name = safe_name[:MAX_SLICE_KEY_NAME_LENGTH]
    return safe_name or "key"


def _safe_key_value(name: str, value: object) -> str:
    normalized_name = "".join(character for character in name.lower() if character.isalnum())
    if any(part in normalized_name for part in _SENSITIVE_KEY_PARTS):
        return "<redacted>"
    scalar_value = _safe_scalar_value(value)
    if scalar_value is not None:
        return scalar_value
    if isinstance(value, (str, date, datetime, datetime_time, Decimal, UUID)):
        return _safe_string_value(str(value))
    type_name = _safe_key_name(type(value).__name__)
    return f"<{type_name}>"


def _safe_scalar_value(value: object) -> str | None:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _safe_float_value(value)
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    return None


def _safe_float_value(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    return repr(value)


def _safe_string_value(value: str) -> str:
    if _looks_sensitive_string(value):
        return "<redacted>"
    escaped = _escape_log_string(value)
    if len(escaped) > MAX_SLICE_KEY_VALUE_LENGTH:
        escaped = f"{escaped[: MAX_SLICE_KEY_VALUE_LENGTH - 1]}…"
    return f"'{escaped}'"


def _looks_sensitive_string(value: str) -> bool:
    lowered = value.lower()
    if lowered.startswith(("basic ", "bearer ")):
        return True
    if _TOKEN_PATTERN.fullmatch(value):
        return True
    if "://" in value and "@" in value:
        authority = value.split("://", 1)[1].split("@", 1)[0]
        return ":" in authority
    return False


def _escape_log_string(value: str) -> str:
    escaped: list[str] = []
    for character in value:
        if character == "'":
            escaped.append("\\'")
        elif character == "\\":
            escaped.append("\\\\")
        elif character == "\n":
            escaped.append("\\n")
        elif character == "\r":
            escaped.append("\\r")
        elif character == "\t":
            escaped.append("\\t")
        elif character.isprintable():
            escaped.append(character)
        else:
            escaped.append(f"\\u{ord(character):04x}")
    return "".join(escaped)


def _require_positive_int(value: int, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        msg = f"{name} must be a built-in positive integer"
        raise ValueError(msg)
