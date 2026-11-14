"""Dispatch exact conversions for backend-specific scalar and nested values."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .ch.value_validation import validate_constrained_value
from .decimal_values import convert_decimal_value, decimal_target_type
from .gp.json_values import convert_json_value, is_json_type
from .gp.network_values import convert_network_value, is_network_type, validate_network_profile
from .gp.network_values import key_validation_frames as gp_key_validation_frames
from .gp.temporal_values import (
    convert_clock_interval_value,
    is_gp_clock_interval_type,
    validate_clock_interval_profile,
)
from .nested_types import convert_nested, nested_descriptor
from .trino.ip_values import key_validation_frames as trino_key_validation_frames
from .trino.ip_values import validate_ip_profile, validate_ip_transport_value
from .trino.json_values import validate_json_profile as validate_trino_json_profile
from .trino.json_values import validate_json_transport_value
from .trino.time_values import convert_time_value, is_time_type, validate_time_profile

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    import pandas as pd

    from .type_resolution import ResolvedColumn
    from .value_profiles import ColumnProfile


def dataframe_key_validation_frames(
    df: pd.DataFrame,
    key_columns: Sequence[str] | None,
    schemas: Sequence[Mapping[str, str] | None],
    backend: str,
) -> Iterable[pd.DataFrame]:
    """Supply backend-native key identities without changing dataframe payloads."""
    if backend == "trino":
        return trino_key_validation_frames(df, key_columns, schemas)
    return gp_key_validation_frames(df, key_columns, schemas) if backend == "gp" else ()


def canonical_native_type(native_type: str | None, backend: str | None) -> str:
    value = native_type or ""
    if backend == "gp":
        from .gp.type_identity import canonical_builtin_type  # noqa: PLC0415 -- native grammar.

        return canonical_builtin_type(value) or value
    return decimal_target_type(value, backend or "")


def validate_transport_value(value: Any, native_type: str, backend: str) -> None:
    """Certify the real parameter normalizer while retaining native profile values."""
    if backend == "trino":
        from .trino.parameters import normalize_value  # noqa: PLC0415 -- native transport.

        validate_json_transport_value(value, native_type)
        validate_ip_transport_value(value, native_type)
        normalize_value(value, native_type)


def validate_backend_profile(
    profile: ColumnProfile,
    native_type: str,
    backend: str,
    *,
    allow_timestamp_precision_loss: bool,
) -> bool:
    if backend == "trino":
        return (
            validate_trino_json_profile(profile, native_type)
            or validate_ip_profile(profile, native_type)
            or validate_time_profile(
                profile,
                native_type,
                allow_timestamp_precision_loss=allow_timestamp_precision_loss,
            )
        )
    if backend != "gp":
        return False
    if native_type == '"char"':
        from .gp.type_identity import validate_builtin_profile  # noqa: PLC0415 -- native policy.

        return validate_builtin_profile(profile, native_type)
    from .gp.json_values import validate_json_profile  # noqa: PLC0415 -- native policy.

    return (
        validate_network_profile(profile, native_type)
        or validate_json_profile(profile, native_type)
        or validate_clock_interval_profile(
            profile,
            native_type,
            allow_timestamp_precision_loss=allow_timestamp_precision_loss,
        )
    )


def convert_native_value(value: Any, resolution: ResolvedColumn) -> tuple[bool, Any]:
    """Return whether a specialized codec handled the destination value."""
    from .type_resolution import parse_native_type  # noqa: PLC0415 -- native grammar cycle.

    target_type = canonical_native_type(resolution.target_type, resolution.target_backend)
    if resolution.target_backend == "trino" and is_time_type(target_type):
        return True, convert_time_value(value, target_type)
    if resolution.target_backend == "gp":
        handled, converted = _convert_gp_value(value, target_type, resolution)
        if handled:
            return True, converted
    handled, converted = convert_decimal_value(value, target_type, resolution.target_backend)
    if handled:
        return True, converted
    if parse_native_type(target_type).base.name in {"enum8", "enum16", "fixedstring"}:
        return True, validate_constrained_value(value, target_type)
    if nested_descriptor(target_type)[0]:
        return True, convert_nested(
            value,
            target_type,
            resolution.target_backend,
            destination_timezone=resolution.destination_timezone,
        )
    return False, value


def _convert_gp_value(value: Any, target_type: str, resolution: ResolvedColumn) -> tuple[bool, Any]:
    if is_network_type(target_type):
        return True, convert_network_value(value, target_type)
    if is_gp_clock_interval_type(target_type):
        return True, convert_clock_interval_value(value, target_type)
    if is_json_type(target_type):
        return True, convert_json_value(
            value,
            target_type,
            decoded=resolution.source_backend == "gp" and is_json_type(resolution.source_type),
        )
    return False, value


def _float_width(type_name: str | None, backend: str | None) -> int | None:
    from .type_resolution import (  # noqa: PLC0415 -- native grammar cycle.
        _FLOAT32_PRECISION,
        _FLOAT_TYPES,
        parse_native_type,
    )

    native = parse_native_type(canonical_native_type(type_name, backend)).base
    if native.name not in _FLOAT_TYPES:
        return None
    if native.name in {"float32", "float4", "real", "single"}:
        return 32
    if native.name == "float":
        if backend == "ch":
            return 32
        if native.arguments:
            return 32 if int(native.arguments[0]) <= _FLOAT32_PRECISION else 64
        return 64
    return 64
