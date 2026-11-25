"""Certify native Trino IPADDRESS casts without DNS or interface lookups."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.core.exact_values import is_null, unwrap_enum

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    import pandas as pd

    from analytics_toolkit.sql.backends.value_profiles import ColumnProfile

_IPV4_LENGTH = 4
_IPV6_LENGTH = 16
_IPV6_PARTS = 8
_MAX_SCOPE = 2**31 - 1
_HEX_RADIX = 16
_BMP_LIMIT = 0xFFFF
_MAX_OCTET_DIGITS = 3
_MAX_HEXTET_DIGITS = 4
_MAX_OCTET = 255
_MAPPED_PREFIX = b"\x00" * 10 + b"\xff\xff"
_MESSAGE = (
    "Selected input is incompatible with authoritative Trino IPADDRESS native casts. "
    "Use an unscoped IP literal or exactly four or sixteen packed bytes; scoped IPv6 literals "
    "require a verifiable numeric scope."
)


@dataclass(frozen=True)
class _ParameterShape:
    kind: str
    children: tuple[_ParameterShape, ...] = ()


def is_ip_type(native_type: str) -> bool:
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- native grammar cycle.
        parse_native_type,
    )

    native = parse_native_type(native_type)
    if native.name != "ipaddress":
        return False
    if native.arguments:
        message = "Trino IPADDRESS does not accept type arguments."
        raise ValueError(message)
    return True


def validate_ip_profile(profile: ColumnProfile, native_type: str) -> bool:
    """Recognize supported source families; the transport pass checks each literal."""
    if not is_ip_type(native_type):
        return False
    if not profile.families <= {"string", "binary"} or not profile.utf8_valid:
        raise ValueError(_MESSAGE)
    return True


def validate_ip_transport_value(value: Any, native_type: str) -> None:
    """Preserve bound values while checking IP leaves and SDK container types."""
    if not _contains_ip(native_type):
        return
    try:
        _parameter_shape(value, native_type)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(_MESSAGE) from None


def key_validation_frames(
    df: pd.DataFrame,
    key_columns: Sequence[str] | None,
    schemas: Sequence[Mapping[str, str] | None],
    *,
    destination_timezone: str | None = None,
) -> Iterator[pd.DataFrame]:
    """Keep the legacy import while using complete typed native key views."""
    from .key_validation import (  # noqa: PLC0415 -- compatibility wrapper.
        key_validation_frames as typed_key_validation_frames,
    )

    yield from typed_key_validation_frames(
        df, key_columns, schemas, destination_timezone=destination_timezone
    )


@lru_cache(maxsize=1024)
def _contains_ip(native_type: str) -> bool:
    if is_ip_type(native_type):
        return True
    from analytics_toolkit.sql.backends.nested_types import (  # noqa: PLC0415 -- native container grammar.
        nested_descriptor,
    )

    return any(_contains_ip(native) for _name, native in nested_descriptor(native_type)[1])


def _parameter_shape(value: Any, native_type: str) -> _ParameterShape:
    if is_null(value):
        return _ParameterShape("unknown")
    if is_ip_type(native_type):
        return _ip_shape(unwrap_enum(value))
    if not _contains_ip(native_type):
        return _ParameterShape(native_type.lower())
    from analytics_toolkit.sql.backends.nested_types import (  # noqa: PLC0415 -- native container grammar.
        nested_descriptor,
    )

    kind, fields = nested_descriptor(native_type)
    if kind in {"array", "gp_array"}:
        return _ParameterShape("array", (_common_parameters(value, fields[0][1]),))
    if kind in {"row", "tuple"}:
        items = [value[name] for name, _ in fields] if isinstance(value, dict) else value
        children = tuple(
            _parameter_shape(item, native) for item, (_name, native) in zip(items, fields)
        )
        return _ParameterShape("row", children)
    if _contains_ip(fields[0][1]):
        identities = [_key_identity(key, fields[0][1]) for key, _item in value.items()]
        if len(set(identities)) != len(identities):
            raise ValueError(_MESSAGE)
    return _ParameterShape(
        "map",
        (
            _common_parameters(value.keys(), fields[0][1]),
            _common_parameters(value.values(), fields[1][1]),
        ),
    )


def _ip_shape(value: Any) -> _ParameterShape:
    _packed_ip(value)
    return _ParameterShape("varchar" if isinstance(value, str) else "varbinary")


def _packed_ip(value: Any) -> bytes:
    if isinstance(value, str):
        return _address_bytes(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        packed = bytes(value)
        if len(packed) not in {_IPV4_LENGTH, _IPV6_LENGTH}:
            raise ValueError(_MESSAGE)
        return packed
    raise ValueError(_MESSAGE)


def ip_value_identity(value: Any) -> bytes:
    """Native equality after IPv4 mapping, without replacing the caller's payload."""
    packed = _packed_ip(unwrap_enum(value))
    return _MAPPED_PREFIX + packed if len(packed) == _IPV4_LENGTH else packed


def _key_identity(value: Any, native_type: str) -> Any:
    if is_null(value):
        raise ValueError(_MESSAGE)
    from analytics_toolkit.sql.backends.map_values import (  # noqa: PLC0415 -- shared destination equality.
        _key_identity as destination_key_identity,
    )
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- match written key values.
        ResolvedColumn,
    )

    converted = ResolvedColumn(native_type, target_backend="trino").convert(value)
    return destination_key_identity(converted, native_type, "trino")


def _common_parameters(values: Any, native_type: str) -> _ParameterShape:
    shape = _ParameterShape("unknown")
    for value in values:
        shape = _common_shape(shape, _parameter_shape(value, native_type))
    return shape


def _common_shape(left: _ParameterShape, right: _ParameterShape) -> _ParameterShape:
    if left.kind == "unknown":
        return right
    if right.kind == "unknown":
        return left
    if left.kind != right.kind or len(left.children) != len(right.children):
        raise ValueError(_MESSAGE)
    return _ParameterShape(
        left.kind, tuple(_common_shape(a, b) for a, b in zip(left.children, right.children))
    )


def _digit(char: str, radix: int) -> int:
    # Guava uses Java Character.digit(char, radix), including BMP decimal digits.
    if radix == _HEX_RADIX:
        for first, last in (("a", "f"), ("A", "F"), ("\uff41", "\uff46"), ("\uff21", "\uff26")):
            if first <= char <= last:
                return ord(char) - ord(first) + 10
    if ord(char) <= _BMP_LIMIT and unicodedata.category(char) == "Nd":
        return int(unicodedata.decimal(char))
    raise ValueError(_MESSAGE)


def _ipv4_bytes(value: str) -> bytes:
    parts = value.split(".")
    if len(parts) != _IPV4_LENGTH:
        raise ValueError(_MESSAGE)
    octets = []
    for part in parts:
        if not 1 <= len(part) <= _MAX_OCTET_DIGITS or (len(part) > 1 and part[0] == "0"):
            raise ValueError(_MESSAGE)
        number = 0
        for char in part:
            number = number * 10 + _digit(char, 10)
        if number > _MAX_OCTET:
            raise ValueError(_MESSAGE)
        octets.append(number)
    return bytes(octets)


def _ipv6_bytes(value: str) -> bytes:
    if "." in value:
        index = value.rfind(":")
        packed = _ipv4_bytes(value[index + 1 :])
        value = (
            value[: index + 1]
            + f"{int.from_bytes(packed[:2], 'big'):x}:{int.from_bytes(packed[2:], 'big'):x}"
        )
    if "::" in value:
        if value.count("::") != 1:
            raise ValueError(_MESSAGE)
        left, right = value.split("::")
        first = left.split(":") if left else []
        last = right.split(":") if right else []
        skipped = _IPV6_PARTS - len(first) - len(last)
        if skipped < 1:
            raise ValueError(_MESSAGE)
        parts = [*first, *("0" for _ in range(skipped)), *last]
    else:
        parts = value.split(":")
        if len(parts) != _IPV6_PARTS:
            raise ValueError(_MESSAGE)
    packed_parts = []
    for part in parts:
        if not 1 <= len(part) <= _MAX_HEXTET_DIGITS:
            raise ValueError(_MESSAGE)
        number = 0
        for char in part:
            number = number * 16 + _digit(char, 16)
        packed_parts.append(number.to_bytes(2, "big"))
    return b"".join(packed_parts)


def _address_bytes(value: str) -> bytes:
    address, separator, scope = value.partition("%")
    if ":" not in address:
        if separator:
            raise ValueError(_MESSAGE)
        return _ipv4_bytes(address)
    if separator:
        if "." in address:
            raise ValueError(_MESSAGE)
        number = 0
        for char in scope:
            number = number * 10 + _digit(char, 10)
            if number > _MAX_SCOPE:
                raise ValueError(_MESSAGE)
    packed = _ipv6_bytes(address)
    if separator and packed.startswith(_MAPPED_PREFIX):
        raise ValueError(_MESSAGE)
    return packed
