"""Exact ClickHouse network domains and transport-independent native values."""

from __future__ import annotations

from ipaddress import IPv4Address, IPv6Address
from numbers import Integral
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    import pandas as pd

    from analytics_toolkit.sql.backends.value_profiles import ColumnProfile

_IPV4 = 4
_IPV6 = 6
_V4_BYTES = 4
_V6_BYTES = 16
_OCTET_DIGITS = 3
_OCTET_MAX = 255
_MAPPED_BASE = 0xFFFF << 32
_BITS = {_IPV4: 32, _IPV6: 128}
_MESSAGE = (
    "Selected input is incompatible with authoritative ClickHouse IPv4/IPv6 values. "
    "Use unscoped address text, native address objects, packed bytes or an unsigned integer "
    "within the declared address width."
)


def _version(native_type: str) -> int | None:
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- native grammar cycle.
        parse_native_type,
    )

    from .storage_types import logical_storage_type  # noqa: PLC0415 -- CH alias value domains.

    native = parse_native_type(logical_storage_type(native_type))
    while native.name in {"nullable", "lowcardinality"}:
        if len(native.arguments) != 1 or not native.arguments[0].strip():
            message = "ClickHouse network wrappers require exactly one native type argument."
            raise ValueError(message)
        native = parse_native_type(native.arguments[0])
    if native.name not in {"ipv4", "ipv6"}:
        return None
    if native.arguments:
        message = "ClickHouse IPv4 and IPv6 do not accept type arguments."
        raise ValueError(message)
    return _IPV4 if native.name == "ipv4" else _IPV6


def is_network_type(native_type: str) -> bool:
    return _version(native_type) is not None


def convert_network_value(value: Any, native_type: str) -> IPv4Address | IPv6Address | None:
    """Use declared integer width; promote explicit IPv4 forms to mapped IPv6."""
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 -- exact-value registry cycle.
        is_null,
        unwrap_enum,
    )

    version = _version(native_type)
    if version is None:
        raise ValueError(_MESSAGE)
    value = unwrap_enum(value)
    if is_null(value):
        return None
    if (isinstance(value, str) and "%" in value) or (
        isinstance(value, IPv6Address) and getattr(value, "scope_id", None) is not None
    ):
        message = "ClickHouse IP addresses cannot store scope identifiers."
        raise ValueError(message)
    try:
        return _native_address(value, version)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(_MESSAGE) from None


def _native_address(value: Any, version: int) -> IPv4Address | IPv6Address:
    if isinstance(value, (Integral, np.bool_)):
        return IPv4Address(int(value)) if version == _IPV4 else IPv6Address(int(value))
    return _ipv4(value) if version == _IPV4 else _ipv6(value)


def _ipv4(value: Any) -> IPv4Address:
    if isinstance(value, IPv4Address):
        return IPv4Address(int(value))
    if isinstance(value, str):
        parts = value.split(".")
        if len(parts) != _V4_BYTES or any(
            not 1 <= len(part) <= _OCTET_DIGITS
            or any(char not in "0123456789" for char in part)
            or (len(part) > 1 and part[0] == "0")
            or int(part) > _OCTET_MAX
            for part in parts
        ):
            raise ValueError(_MESSAGE)
        return IPv4Address(bytes(int(part) for part in parts))
    if isinstance(value, (bytes, bytearray, memoryview)):
        packed = bytes(value)
        if len(packed) == _V4_BYTES:
            return IPv4Address(packed)
    raise ValueError(_MESSAGE)


def _ipv6(value: Any) -> IPv6Address:
    if isinstance(value, IPv6Address):
        return IPv6Address(int(value))
    if isinstance(value, IPv4Address):
        return IPv6Address(_MAPPED_BASE | int(value))
    if isinstance(value, str):
        if ":" not in value:
            return IPv6Address(_MAPPED_BASE | int(_ipv4(value)))
        if "." in value:
            _ipv4(value.rsplit(":", 1)[-1])
        return IPv6Address(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        packed = bytes(value)
        if len(packed) == _V4_BYTES:
            return IPv6Address(_MAPPED_BASE | int(IPv4Address(packed)))
        if len(packed) == _V6_BYTES:
            return IPv6Address(packed)
    raise ValueError(_MESSAGE)


def observe_network_value(profile: ColumnProfile, value: Any) -> None:
    """Collect complete domain facts only for CH native network hints."""
    if profile.source_backend != "ch" or not is_network_type(profile.native_type or ""):
        return
    try:
        convert_network_value(value, profile.native_type or "")
    except ValueError:
        profile.native_constraints_valid = False


def validate_network_profile(profile: ColumnProfile, native_type: str) -> bool:
    version = _version(native_type)
    if version is None:
        return False
    hint = _version(profile.native_type or "") if profile.source_backend == "ch" else None
    observed = hint == version or (hint == _IPV4 and version == _IPV6)
    if (
        not profile.native_constraints_valid
        or not profile.utf8_valid
        or profile.text_has_nul
        or (profile.families - {"string", "integer", "boolean", "binary"} and not observed)
    ):
        raise ValueError(_MESSAGE)
    if profile.minimum_integer is not None and not (
        0 <= profile.minimum_integer <= (profile.maximum_integer or 0) < 2 ** _BITS[version]
    ):
        raise ValueError(_MESSAGE)
    return True


def network_key_identity(value: Any, native_type: str) -> bytes:
    address = convert_network_value(value, native_type)
    if address is None:
        message = "Native ClickHouse network keys cannot be SQL NULL."
        raise ValueError(message)
    return address.packed


def declared_network_fits(source_type: str, target_type: str) -> bool | None:
    source, target = _version(source_type), _version(target_type)
    return target >= source if source is not None and target is not None else None


def key_validation_frames(
    df: pd.DataFrame,
    key_columns: Sequence[str] | None,
    schemas: Sequence[Mapping[str, str] | None],
    *,
    destination_timezone: str | None = None,
) -> Iterator[pd.DataFrame]:
    """Use native key equality in network-bearing views without changing payloads."""
    if not key_columns:
        return
    import pandas as pd  # noqa: PLC0415 -- object series for native key identities.

    from analytics_toolkit.sql.backends.map_values import (  # noqa: PLC0415 -- shared native key equality.
        _key_identity,
    )
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- native conversion cycle.
        ResolvedColumn,
    )

    seen: set[tuple[tuple[str, str], ...]] = set()
    for schema in schemas:
        types = schema or {}
        signature = tuple((name, types.get(name, "")) for name in key_columns)
        if signature in seen or not any(
            is_network_type(types.get(name, "")) for name in key_columns
        ):
            continue
        seen.add(signature)
        validation = df.loc[:, list(key_columns)].copy()
        for name in key_columns:
            if name not in types:
                continue
            resolution = ResolvedColumn(
                types[name], target_backend="ch", destination_timezone=destination_timezone
            )
            identities = [
                _key_identity(resolution.convert(value), types[name], "ch", destination_timezone)
                for value in df[name]
            ]
            validation[name] = pd.Series(identities, index=df.index, dtype=object)
        yield validation
