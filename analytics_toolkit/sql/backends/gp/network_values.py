"""Certify Greenplum INET's PostgreSQL 9.4 native input grammar without DNS."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.core.exact_values import is_null, unwrap_enum

from .type_identity import canonical_builtin_type

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    import pandas as pd

    from analytics_toolkit.sql.backends.value_profiles import ColumnProfile

_V4_BYTES = 4
_V6_BYTES = 16
_V4_BITS = 32
_V6_BITS = 128
_OCTET_MAX = 255
_HEXTET_DIGITS = 4
_HEX = "0123456789abcdef"
_MESSAGE = (
    "Selected input is incompatible with authoritative Greenplum INET values. "
    "Use a native IPv4 or IPv6 string with a valid optional prefix length."
)


def is_network_type(native_type: str) -> bool:
    """Recognize the selected built-in scalar, never a lookalike custom type."""
    return canonical_builtin_type(native_type) == "INET"


def _native_network_value(value: Any) -> Any:
    if isinstance(value, str) or is_null(value):
        return value
    from psycopg2.extras import Inet  # noqa: PLC0415 -- optional native driver wrapper.

    return value.addr if isinstance(value, Inet) else value


def normalize_authoritative_network(value: Any, native_type: str) -> Any:
    """Own the known driver's input spelling before profiling, without DNS."""
    if not is_network_type(native_type):
        return value
    owned = _native_network_value(unwrap_enum(value))
    if is_null(owned):
        return None
    network_value_identity(owned, native_type)
    return owned


def validate_network_profile(profile: ColumnProfile, native_type: str) -> bool:
    if not is_network_type(native_type):
        return False
    if not profile.families <= {"string"} or not profile.utf8_valid or profile.text_has_nul:
        raise ValueError(_MESSAGE)
    return True


def convert_network_value(value: Any, native_type: str) -> Any:
    """Validate exactly the string sent to psycopg2, preserving caller spelling."""
    if not is_network_type(native_type):
        raise ValueError(_MESSAGE)
    value = unwrap_enum(value)
    if is_null(value):
        return None
    return normalize_authoritative_network(value, native_type)


def network_value_identity(value: Any, native_type: str = "INET") -> tuple[int, bytes, int]:
    """Native INET equality includes family, complete address and prefix length."""
    value = _native_network_value(unwrap_enum(value))
    if (
        not is_network_type(native_type)
        or not isinstance(value, str)
        or not value.isascii()
        or "\x00" in value
    ):
        raise ValueError(_MESSAGE)
    try:
        if ":" in value:
            packed, bits = _ipv6(value)
            family = 6
        else:
            packed, bits = _ipv4(value)
            family = 4
    except (ValueError, OverflowError, IndexError):
        raise ValueError(_MESSAGE) from None
    return family, packed, bits


def key_validation_frames(
    df: pd.DataFrame,
    key_columns: Sequence[str] | None,
    schemas: Sequence[Mapping[str, str] | None],
) -> Iterator[pd.DataFrame]:
    """Compare native INET and clock keys in owned views, preserving bound values."""
    if not key_columns:
        return
    import pandas as pd  # noqa: PLC0415 -- typed validation-only object series.

    from .temporal_values import (  # noqa: PLC0415 -- reuse exact native clock conversion.
        _temporal_type,
        _time_parts,
        convert_clock_interval_value,
    )

    seen: set[tuple[tuple[str, str], ...]] = set()
    for schema in schemas:
        types = schema or {}
        columns = tuple(
            name for name in key_columns if name in types and is_network_type(types[name])
        )
        signature = tuple((name, types.get(name, "")) for name in key_columns)
        if not columns or signature in seen:
            continue
        seen.add(signature)
        validation = df.loc[:, list(key_columns)].copy()
        for name in key_columns:
            identities: list[Any]
            if name in columns:
                identities = [
                    network_value_identity(unwrap_enum(value), types[name]) for value in df[name]
                ]
            else:
                clock = _temporal_type(types.get(name, ""))
                if clock is None or clock.family != "time":
                    continue
                identities = [
                    convert_clock_interval_value(value, types[name]) for value in df[name]
                ]
                if (
                    clock.with_timezone
                    and len({_time_parts(value)[1] is not None for value in identities}) > 1
                ):
                    message = (
                        "Mixed naive and offset-aware Greenplum TIMETZ key values "
                        "cannot be certified. "
                        "Supply an explicit offset for every TIMETZ key value."
                    )
                    raise ValueError(message)
            validation[name] = pd.Series(identities, index=df.index, dtype=object)
        yield validation


def _decimal(value: str, maximum: int, *, leading_zeroes: bool = True) -> int:
    if not value or (not leading_zeroes and len(value) > 1 and value[0] == "0"):
        raise ValueError(_MESSAGE)
    number = 0
    for char in value:
        if char not in "0123456789":
            raise ValueError(_MESSAGE)
        number = number * 10 + ord(char) - ord("0")
        if number > maximum:
            raise ValueError(_MESSAGE)
    return number


def _ipv4(value: str) -> tuple[bytes, int]:
    address, slash, mask = value.partition("/")
    # inet_net_pton_ipv4 permits a final dot before its prefix or end-of-input.
    address = address[:-1] if address.endswith(".") else address
    parts = address.split(".")
    if not 1 <= len(parts) <= _V4_BYTES:
        raise ValueError(_MESSAGE)
    octets = [_decimal(part, _OCTET_MAX) for part in parts]
    bits = _decimal(mask, _V4_BITS) if slash else _V4_BITS
    if (not slash and len(parts) != _V4_BYTES) or bits // 8 > len(parts):
        raise ValueError(_MESSAGE)
    return bytes([*octets, *([0] * (_V4_BYTES - len(octets)))]), bits


def _embedded_ipv4(value: str) -> tuple[bytes, int | None]:
    address, slash, mask = value.partition("/")
    parts = address.split(".")
    if len(parts) > _V4_BYTES or (not slash and not parts[-1]):
        raise ValueError(_MESSAGE)
    # PG9.4 getv4 accepts empty fields before a dot or prefix as zero.
    octets = [_decimal(part, _OCTET_MAX, leading_zeroes=False) if part else 0 for part in parts]
    bits = _decimal(mask, _V6_BITS, leading_zeroes=False) if slash else None
    return bytes([*octets, *([0] * (_V4_BYTES - len(octets)))]), bits


def _ipv6(value: str) -> tuple[bytes, int]:  # noqa: C901, PLR0912, PLR0915 -- PG9.4 inet_cidr_pton_ipv6 state machine.
    packed = bytearray(_V6_BYTES)
    position = 0
    compressed: int | None = None
    index = 0
    if value.startswith(":"):
        if not value.startswith("::"):
            raise ValueError(_MESSAGE)
        index = 1
    token_start = index
    digits = 0
    number = 0
    saw_digit = False
    bits: int | None = None
    while index < len(value):
        char = value[index]
        index += 1
        if char.lower() in _HEX:
            number = number * 16 + _HEX.index(char.lower())
            digits += 1
            if digits > _HEXTET_DIGITS:
                raise ValueError(_MESSAGE)
            saw_digit = True
            continue
        if char == ":":
            token_start = index
            if not saw_digit:
                if compressed is not None:
                    raise ValueError(_MESSAGE)
                compressed = position
                continue
            if index == len(value) or position + 2 > _V6_BYTES:
                raise ValueError(_MESSAGE)
            packed[position : position + 2] = number.to_bytes(2, "big")
            position += 2
            saw_digit = False
            digits = 0
            number = 0
            continue
        if char == "." and position + _V4_BYTES <= _V6_BYTES:
            tail, bits = _embedded_ipv4(value[token_start:])
            packed[position : position + _V4_BYTES] = tail
            position += _V4_BYTES
            saw_digit = False
            break
        if char == "/":
            bits = _decimal(value[index:], _V6_BITS, leading_zeroes=False)
            break
        raise ValueError(_MESSAGE)
    if saw_digit:
        if position + 2 > _V6_BYTES:
            raise ValueError(_MESSAGE)
        packed[position : position + 2] = number.to_bytes(2, "big")
        position += 2
    if compressed is not None:
        if position == _V6_BYTES:
            raise ValueError(_MESSAGE)
        packed = packed[:compressed] + bytearray(_V6_BYTES - position) + packed[compressed:position]
        position = _V6_BYTES
    if position != _V6_BYTES:
        raise ValueError(_MESSAGE)
    return bytes(packed), _V6_BITS if bits is None else bits
