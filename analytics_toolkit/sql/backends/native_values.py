"""Dispatch exact conversions for backend-specific scalar and nested values."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from .ch.boolean_values import boolean_value as ch_boolean_value
from .ch.boolean_values import is_boolean_type as is_ch_boolean_type
from .ch.boolean_values import validate_boolean_type_profile as validate_ch_boolean_type_profile
from .ch.client_validation import (
    is_legacy_json_type,
    normalize_legacy_json_value,
    validate_legacy_json_profile,
    validate_legacy_json_value,
)
from .ch.integer_values import integer_value as ch_integer_value
from .ch.integer_values import is_integer_type as is_ch_integer_type
from .ch.integer_values import validate_integer_type_profile as validate_ch_integer_type_profile
from .ch.network_values import convert_network_value as convert_ch_network_value
from .ch.network_values import declared_network_fits
from .ch.network_values import is_network_type as is_ch_network_type
from .ch.network_values import key_validation_frames as ch_key_validation_frames
from .ch.network_values import validate_network_profile as validate_ch_network_profile
from .ch.numeric_text import convert_numeric_text as convert_ch_numeric_text
from .ch.numeric_text import normalize_authoritative_value as normalize_ch_authoritative_value
from .ch.numeric_text import validate_numeric_text_type_profile as validate_ch_numeric_text_profile
from .ch.storage_types import logical_storage_type
from .ch.timestamp_keys import source_key_validation_frame as ch_source_key_validation_frame
from .ch.value_validation import validate_constrained_value
from .decimal_values import convert_decimal_value, decimal_target_type
from .finite_numeric_text import decimal_float_value
from .gp.boolean_values import boolean_value as gp_boolean_value
from .gp.boolean_values import is_boolean_type as is_gp_boolean_type
from .gp.date_values import date_value as gp_date_value
from .gp.date_values import is_date_type as is_gp_date_type
from .gp.date_values import validate_date_profile as validate_gp_date_profile
from .gp.integer_values import integer_text_value as gp_integer_text_value
from .gp.integer_values import integer_value as gp_integer_value
from .gp.integer_values import is_integer_type as is_gp_integer_type
from .gp.json_values import convert_json_value, is_json_type
from .gp.network_values import convert_network_value, is_network_type, validate_network_profile
from .gp.network_values import key_validation_frames as gp_key_validation_frames
from .gp.numeric_text import contains_numeric_text_type as contains_gp_numeric_type
from .gp.numeric_text import convert_authoritative_numeric as convert_gp_authoritative_numeric
from .gp.numeric_text import convert_numeric_text as convert_gp_numeric_text
from .gp.numeric_text import normalize_authoritative_value as normalize_gp_authoritative_value
from .gp.numeric_text import numeric_assignment_value, pending_numeric_assignment
from .gp.numeric_text import validate_numeric_text_type_profile as validate_gp_numeric_text_profile
from .gp.temporal_values import (
    convert_clock_interval_value,
    is_gp_clock_interval_type,
    validate_clock_interval_profile,
)
from .gp.type_domains import declared_numeric_domain_fits
from .nested_types import convert_nested, nested_descriptor
from .trino.boolean_values import (
    boolean_value,
    is_boolean_type,
    validate_boolean_type_profile,
)
from .trino.date_values import (
    date_value,
    is_date_type,
    validate_date_type_profile,
)
from .trino.integer_values import convert_integer_value as convert_trino_integer_value
from .trino.integer_values import (
    validate_integer_type_profile as validate_trino_integer_type_profile,
)
from .trino.ip_values import validate_ip_profile, validate_ip_transport_value
from .trino.json_values import validate_json_profile as validate_trino_json_profile
from .trino.json_values import validate_json_transport_value
from .trino.key_validation import key_validation_frames as trino_key_validation_frames
from .trino.numeric_text import convert_numeric_text as convert_trino_numeric_text
from .trino.numeric_text import normalize_authoritative_value as normalize_trino_authoritative_value
from .trino.numeric_text import (
    validate_numeric_text_type_profile as validate_trino_numeric_text_profile,
)
from .trino.time_values import convert_time_value, is_time_type, validate_time_profile

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    import pandas as pd

    from .type_resolution import ResolvedColumn
    from .value_profiles import ColumnProfile


def source_key_validation_frame(
    df: pd.DataFrame, key_columns: Sequence[str] | None, backend: str
) -> pd.DataFrame:
    """Validate known source instants without changing backend-specific payloads."""
    return ch_source_key_validation_frame(df, key_columns) if backend == "ch" else df


def dataframe_key_validation_frames(  # noqa: PLR0913 -- exported compatibility boundary preserves both context keywords.
    df: pd.DataFrame,
    key_columns: Sequence[str] | None,
    schemas: Sequence[Mapping[str, str] | None],
    backend: str,
    *,
    destination_timezone: str | None = None,
    connection: Any = None,
) -> Iterable[pd.DataFrame]:
    """Supply backend-native key identities without changing dataframe payloads."""
    if backend == "ch":
        options: dict[str, Any] = {"destination_timezone": destination_timezone}
        if connection is not None:
            options["connection"] = connection
        return ch_key_validation_frames(df, key_columns, schemas, **options)
    if backend == "trino":
        return trino_key_validation_frames(
            df, key_columns, schemas, destination_timezone=destination_timezone
        )
    return gp_key_validation_frames(df, key_columns, schemas) if backend == "gp" else ()


def canonical_native_type(native_type: str | None, backend: str | None) -> str:
    value = native_type or ""
    if backend == "gp":
        from .gp.type_identity import canonical_builtin_type  # noqa: PLC0415 -- native grammar.

        return canonical_builtin_type(value) or value
    if backend == "ch":
        value = logical_storage_type(value)
    return decimal_target_type(value, backend or "")


def backend_domain_fits(
    source_type: str, target_type: str, target_backend: str, source_backend: str | None
) -> bool | None:
    if source_backend == "gp":
        numeric_fit = declared_numeric_domain_fits(source_type, target_backend)
        if numeric_fit is not None:
            return numeric_fit
    if source_backend == target_backend == "ch":
        return declared_network_fits(source_type, target_type)
    return None


def validate_backend_declaration(native_type: str, backend: str) -> None:
    """Check complete destination declarations independently of observed cells."""
    if backend == "gp":
        from .gp.type_identity import (  # noqa: PLC0415 - registry cycle.
            canonical_builtin_type,
        )

        canonical_builtin_type(native_type)
    elif backend == "ch":
        from .ch.type_declarations import (  # noqa: PLC0415 - registry cycle.
            validate_type_declaration,
        )

        validate_type_declaration(native_type)
    elif backend == "trino":
        from analytics_toolkit.sql.backends.trino.type_declarations import (  # noqa: PLC0415 -- backend registry initialization.
            validate_type_declaration,
        )

        validate_type_declaration(native_type)


def requires_native_assignment(native_type: str, backend: str) -> bool:
    """Identify native schemas whose values need an owned assignment checkpoint."""
    if backend != "gp":
        return False
    from .gp import native_assignment  # noqa: PLC0415 -- backend-owned checkpoint policy.

    return native_assignment.requires_native_assignment(native_type)


def supports_native_assignment(native_type: str, backend: str) -> bool:
    """Recognize schemas whose pending values may enter an owned checkpoint."""
    if requires_native_assignment(native_type, backend):
        return True
    if backend != "gp":
        return False
    from analytics_toolkit.sql.backends.gp.temporal_assignment import (  # noqa: PLC0415 -- native policy initialization.
        contains_temporal_type,
    )

    return contains_temporal_type(native_type) or contains_gp_numeric_type(native_type)


def pending_native_assignment(
    profile: ColumnProfile,
    native_type: str,
    backend: str,
    *,
    allow_timestamp_precision_loss: bool = True,
    destination_timezone: str | None = None,
) -> bool:
    """Validate unaffected leaves before deferring actual native assignment."""
    if requires_native_assignment(native_type, backend):
        return True
    if backend != "gp":
        return False
    from analytics_toolkit.sql.backends.gp.temporal_assignment import (  # noqa: PLC0415 -- native policy initialization.
        pending_temporal_assignment,
    )

    return pending_temporal_assignment(
        profile,
        native_type,
        allow_timestamp_precision_loss=allow_timestamp_precision_loss,
        destination_timezone=destination_timezone,
    ) or pending_numeric_assignment(
        profile,
        native_type,
        allow_timestamp_precision_loss=allow_timestamp_precision_loss,
        destination_timezone=destination_timezone,
    )


def native_assignment_value(
    value: Any,
    native_type: str,
    backend: str,
    *,
    destination_timezone: str | None = None,
) -> Any:
    """Prepare only a run-owned binding view; native text remains unparsed."""
    if backend != "gp" or requires_native_assignment(native_type, backend):
        return value
    from analytics_toolkit.sql.backends.gp.temporal_assignment import (  # noqa: PLC0415 -- native policy initialization.
        temporal_assignment_value,
    )

    return numeric_assignment_value(
        temporal_assignment_value(value, native_type, destination_timezone=destination_timezone),
        native_type,
        destination_timezone=destination_timezone,
    )


def normalize_authoritative_value(value: Any, native_type: str | None, backend: str) -> Any:
    """Keep deliberate assignment views separate from inferred source families."""
    if backend == "ch":
        return normalize_ch_authoritative_value(value, native_type)
    if backend == "trino":
        return normalize_trino_authoritative_value(value, native_type)
    return normalize_gp_authoritative_value(value, native_type) if backend == "gp" else value


def validate_transport_value(value: Any, native_type: str, backend: str) -> None:
    """Certify the real parameter normalizer while retaining native profile values."""
    if backend == "ch" and is_legacy_json_type(canonical_native_type(native_type, backend)):
        validate_legacy_json_value(value, canonical_native_type(native_type, backend))
    if backend == "trino":
        from .trino.parameters import normalize_value  # noqa: PLC0415 -- native transport.

        validate_json_transport_value(value, native_type)
        validate_ip_transport_value(value, native_type)
        normalize_value(value, native_type)


def require_timestamp_write_capabilities(  # noqa: PLR0913 -- keep authority, certified profiles and run scope explicit.
    backend: str,
    connection: Any,
    table: str,
    schema: Mapping[str, str],
    profiles: Mapping[str, ColumnProfile],
    cache: dict[str, Any],
    *,
    creating: bool = False,
    properties: Mapping[str, Any] | None = None,
    dry_run: bool = False,
) -> dict[str, Any] | None:
    """Dispatch conditional backend storage proofs without coupling load to Trino."""
    if backend != "trino":
        return None
    from .trino.timestamp_capabilities import (  # noqa: PLC0415 - registry cycle.
        require_timestamp_capabilities,
    )

    return require_timestamp_capabilities(
        connection,
        table,
        schema,
        profiles,
        cache,
        creating=creating,
        properties=properties,
        dry_run=dry_run,
    )


def timestamp_capability_plan_options(
    backend: str, schema: Mapping[str, str], profiles: Mapping[str, ColumnProfile] | None = None
) -> dict[str, Any]:
    """Expose a pending metadata step in plans without opening connections."""
    if backend != "trino":
        return {}
    from .trino.timestamp_capabilities import (  # noqa: PLC0415 - registry cycle.
        timestamp_capability_options,
    )

    return timestamp_capability_options(schema, profiles)


def validate_backend_profile(
    profile: ColumnProfile,
    native_type: str,
    backend: str,
    *,
    allow_timestamp_precision_loss: bool,
) -> bool:
    if backend == "ch":
        return (
            validate_ch_numeric_text_profile(native_type)
            or validate_ch_integer_type_profile(native_type)
            or validate_ch_boolean_type_profile(native_type)
            or validate_ch_network_profile(profile, native_type)
            or validate_legacy_json_profile(profile, native_type)
        )
    if backend == "trino":
        return (
            validate_trino_numeric_text_profile(native_type)
            or validate_trino_integer_type_profile(native_type)
            or validate_date_type_profile(native_type)
            or validate_boolean_type_profile(native_type)
            or validate_trino_json_profile(profile, native_type)
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
        validate_gp_numeric_text_profile(native_type)
        or validate_gp_date_profile(profile, native_type)
        or validate_network_profile(profile, native_type)
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
    width = _float_width(target_type, resolution.target_backend)
    if isinstance(value, Decimal) and value.is_finite() and width is not None:
        return True, decimal_float_value(
            value, width, reject_underflow=resolution.target_backend == "gp"
        )
    handled, converted = _convert_backend_value(value, target_type, resolution)
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
            conversion_policy=resolution.conversion_policy,
        )
    return False, value


def _convert_backend_value(
    value: Any, target_type: str, resolution: ResolvedColumn
) -> tuple[bool, Any]:
    if resolution.target_backend == "ch":
        return _convert_ch_value(value, target_type, resolution)
    if resolution.target_backend == "trino":
        return _convert_trino_value(value, target_type)
    if resolution.target_backend == "gp":
        return _convert_gp_value(value, target_type, resolution)
    return False, value


def _convert_ch_value(value: Any, target_type: str, resolution: ResolvedColumn) -> tuple[bool, Any]:
    handled, converted = convert_ch_numeric_text(value, target_type)
    if handled:
        return True, converted
    if is_ch_integer_type(target_type):
        return True, ch_integer_value(value, target_type)
    if is_ch_boolean_type(target_type):
        return True, ch_boolean_value(value)
    if is_ch_network_type(target_type):
        if resolution.source_backend == "ch" and is_ch_network_type(resolution.source_type):
            value = convert_ch_network_value(value, resolution.source_type)
        return True, convert_ch_network_value(value, target_type)
    if is_legacy_json_type(target_type):
        return True, normalize_legacy_json_value(value, target_type)
    return False, value


def _convert_trino_value(value: Any, target_type: str) -> tuple[bool, Any]:
    handled, converted = convert_trino_numeric_text(value, target_type)
    if handled:
        return True, converted
    handled, converted = convert_trino_integer_value(value, target_type)
    if handled:
        return True, converted
    if is_date_type(target_type):
        return True, date_value(value)
    if is_boolean_type(target_type):
        return True, boolean_value(value)
    if is_time_type(target_type):
        return True, convert_time_value(value, target_type)
    return False, value


def _convert_gp_value(value: Any, target_type: str, resolution: ResolvedColumn) -> tuple[bool, Any]:
    converter = (
        convert_gp_authoritative_numeric
        if resolution.reason == "authoritative"
        else convert_gp_numeric_text
    )
    handled, converted = converter(value, target_type)
    if handled:
        return True, converted
    handled, converted = _convert_gp_scalar(value, target_type)
    if handled:
        return True, converted
    if is_json_type(target_type):
        return True, convert_json_value(
            value,
            target_type,
            decoded=resolution.source_backend == "gp" and is_json_type(resolution.source_type),
        )
    return False, value


def _convert_gp_scalar(value: Any, target_type: str) -> tuple[bool, Any]:
    if isinstance(value, (str, Decimal)) and is_gp_integer_type(target_type):
        converted = (
            gp_integer_text_value(value, target_type)
            if isinstance(value, str)
            else gp_integer_value(value, target_type)
        )
        return True, converted
    if is_gp_boolean_type(target_type):
        return True, gp_boolean_value(value)
    if is_gp_date_type(target_type):
        return True, gp_date_value(value)
    if is_network_type(target_type):
        return True, convert_network_value(value, target_type)
    if is_gp_clock_interval_type(target_type):
        return True, convert_clock_interval_value(value, target_type)
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
