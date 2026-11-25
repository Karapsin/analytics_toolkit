"""Complete-value profiles and exact, backend-aware destination resolution.

Profiles contain aggregate facts, never samples or private values. A caller must
finish the selected input before using adaptive resolution to publish data.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from numbers import Integral
from typing import Any, cast
from uuid import UUID

import numpy as np

from analytics_toolkit.sql.core.exact_values import (
    ExactTemporal,
    integer_text,
    is_native_float_nan,
    is_null,
    truncate_temporal,
    typed_text,
    unwrap_enum,
)

from .authoritative_types import resolve_authoritative_profile
from .ch.integer_timestamps import validate_integer_timestamp_range
from .ch.value_validation import _permits_null
from .decimal_values import (
    _decimal_parameter,
    required_fractional_scale,
    resolve_decimal_type,
    validate_decimal_type,
)
from .inferred_types import declared_target as _declared_target
from .inferred_types import resolve_column_type
from .native_values import (
    _float_width,
    backend_domain_fits,
    canonical_native_type,
    convert_native_value,
    native_assignment_value,
    supports_native_assignment,
    validate_backend_declaration,
    validate_backend_profile,
)
from .numeric_profiles import inferred_numeric_value
from .observed_types import resolve_observed_type as _resolve_native
from .temporal_context import TIMEZONE_POLICY_VERSION
from .temporal_values import timestamp_scale, timestamp_value, validate_timestamp_range
from .text_types import binary_profile_fits
from .value_profiles import ColumnProfile

__all__ = [
    "ColumnProfile",
    "NativeType",
    "ResolvedColumn",
    "_decimal_parameter",
    "_declared_target",
    "_float_width",
    "_permits_null",
    "_resolve_native",
    "decimal_domain",
    "declared_domain_fits",
    "integer_domain",
    "is_timestamp_type",
    "native_candidates",
    "parse_native_type",
    "requires_adaptive_resolution",
    "resolve_column_type",
    "resolve_decimal_type",
    "resolve_profile",
    "timestamp_scale",
    "validate_profile",
]

_DECIMAL_ARGUMENT_COUNT = 2
_TRINO_TIMESTAMP_SCALE = 6
_MAX_TEMPORAL_SCALE = 9
_GP_DECLARED_NUMERIC_PRECISION = 1000
_GP_NUMERIC_INTEGRAL_DIGITS = 131072
_GP_NUMERIC_FRACTIONAL_DIGITS = 16383
_MAP_ARGUMENT_COUNT = 2
_FLOAT_TYPES = {
    "float",
    "float32",
    "float64",
    "real",
    "single",
    "float4",
    "float8",
    "double",
    "double precision",
}
_FLOAT32_BITS = 32
_FLOAT32_PRECISION = 24
_FLOAT32_LIMIT = Decimal(2**128 - 2**104)
_FLOAT64_LIMIT = Decimal.from_float(float.fromhex("0x1.fffffffffffffp+1023"))


@dataclass(frozen=True)
class NativeType:
    name: str
    arguments: tuple[str, ...] = ()
    original: str = ""

    @property
    def base(self) -> NativeType:
        if self.name.lower() in {"nullable", "lowcardinality"} and len(self.arguments) == 1:
            return parse_native_type(self.arguments[0]).base
        return self


@lru_cache(maxsize=1024)
def parse_native_type(  # noqa: C901, PLR0912 - balanced native type grammar
    value: str | None,
) -> NativeType:
    text = (value or "").strip()
    temporal = re.fullmatch(
        r"(timestamp|time)\s*(?:\(\s*([0-9]+)\s*\))?\s+(with|without)\s+time\s+zone",
        text,
        flags=re.IGNORECASE | re.ASCII,
    )
    if temporal:
        family, precision, awareness = temporal.groups()
        return NativeType(
            f"{family.lower()} {awareness.lower()} time zone",
            (precision,) if precision else (),
            text,
        )
    match = re.fullmatch(r"([A-Za-z_][\w ]*?)(?:\((.*)\))?", text, flags=re.DOTALL)
    if match is None:
        return NativeType(text.lower(), original=text)
    name, contents = match.groups()
    if contents is None:
        return NativeType(name.strip().lower(), original=text)
    arguments: list[str] = []
    start = 0
    depth = 0
    quote = ""
    escaped = False
    for index, char in enumerate(contents):
        if escaped:
            escaped = False
        elif quote:
            if char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            arguments.append(contents[start:index].strip())
            start = index + 1
    if depth != 0 or quote:
        msg = "Unbalanced native SQL type."
        raise ValueError(msg)
    arguments.append(contents[start:].strip())
    return NativeType(name.strip().lower(), tuple(arguments), text)


def integer_domain(type_name: str | None, *, backend: str | None = None) -> tuple[int, int] | None:
    base = parse_native_type(canonical_native_type(type_name, backend)).base
    name = base.name
    widths = {
        "tinyint": 8,
        "smallint": 16,
        "integer": 32,
        "int": 32,
        "int2": 16,
        "int4": 32,
        "bigint": 64,
        "int8": 64,
    }
    # Native Int8 is ClickHouse's eight-bit type; PostgreSQL int8 is disambiguated
    # by source metadata adapters before this helper when necessary.
    match = re.fullmatch(r"(u?)int(8|16|32|64|128|256)", name)
    if match:
        bits = int(match.group(2))
        unsigned = bool(match.group(1))
    elif name in widths:
        bits, unsigned = widths[name], False
    else:
        return None
    return (0, 2**bits - 1) if unsigned else (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1)


def decimal_domain(type_name: str | None) -> tuple[int, int] | None:
    base = parse_native_type(type_name).base
    if (
        base.name in {"decimal", "numeric", "number"}
        and len(base.arguments) == _DECIMAL_ARGUMENT_COUNT
    ):
        return _decimal_parameter(base.arguments[0]), _decimal_parameter(base.arguments[1])
    match = re.fullmatch(r"decimal(32|64|128|256)", base.name)
    if match and len(base.arguments) == 1:
        return {32: 9, 64: 18, 128: 38, 256: 76}[int(match.group(1))], _decimal_parameter(
            base.arguments[0]
        )
    return None


def is_timestamp_type(type_name: str | None) -> bool:
    name = parse_native_type(type_name).base.name
    return name.startswith(("timestamp", "datetime")) or name == "timestamptz"


def _convert_exact_timestamp(value: ExactTemporal, target_type: str, backend: str) -> Any:
    if (
        backend != "ch"
        and is_timestamp_type(target_type)
        and timestamp_scale(target_type) <= _TRINO_TIMESTAMP_SCALE
    ):
        try:
            return value.calendar_datetime()
        except OverflowError:
            return value
    return (
        value
        if backend == "ch"
        or (backend == "trino" and timestamp_scale(target_type) > _MAX_TEMPORAL_SCALE)
        else value.timestamp()
    )


@dataclass(frozen=True)
class ResolvedColumn:
    target_type: str
    source_type: str = ""
    source_backend: str = ""
    reason: str = "native"
    text_fallback: bool = False
    certified_input_count: int = 0
    target_backend: str = ""
    destination_timezone: str | None = None
    native_assignment_required: bool = False
    native_assignment_precision_loss: bool = False
    conversion_policy: str = ""

    def convert(self, value: Any) -> Any:  # noqa: C901, PLR0911 - preserve explicit destination conversions
        value = unwrap_enum(value)
        if is_null(value):
            return None
        if self.native_assignment_required and supports_native_assignment(
            self.target_type, self.target_backend
        ):
            return native_assignment_value(
                value,
                self.target_type,
                self.target_backend,
                destination_timezone=self.destination_timezone,
            )
        target_type = canonical_native_type(self.target_type, self.target_backend)
        if self.conversion_policy == "inferred_exact":
            value = inferred_numeric_value(value, target_type)
        native, value = convert_native_value(value, self)
        if native:
            return value
        textual = parse_native_type(target_type).base.name in {
            "string",
            "text",
            "varchar",
            "char",
            "character",
            "character varying",
        }
        native_bytes = (
            isinstance(value, (bytes, bytearray, memoryview))
            and parse_native_type(target_type).base.name == "string"
        )
        if self.text_fallback or (textual and not native_bytes):
            scale = (
                timestamp_scale(self.source_type) if is_timestamp_type(self.source_type) else None
            )
            return typed_text(
                value,
                source_backend=self.source_backend,
                source_type=self.source_type,
                fractional_scale=scale,
            )
        if (
            self.target_backend in {"gp", "trino"}
            and parse_native_type(target_type).base.name in {"bytea", "varbinary"}
            and isinstance(value, str)
        ):
            return value.encode("utf-8")
        if is_timestamp_type(target_type):
            value = timestamp_value(
                truncate_temporal(value, timestamp_scale(target_type)),
                target_type,
                self.destination_timezone,
            )
        if isinstance(value, ExactTemporal):
            return _convert_exact_timestamp(value, target_type, self.target_backend)
        if parse_native_type(target_type).base.name in _FLOAT_TYPES | {"boolean", "bool"}:
            return (
                bool(value)
                if parse_native_type(target_type).base.name in {"boolean", "bool"}
                else value
                if is_native_float_nan(value)
                else float(value)
            )
        if integer_domain(
            target_type, backend=self.target_backend or None
        ) is not None and isinstance(value, (Integral, np.bool_)):
            return int(value)
        if parse_native_type(target_type).base.name == "uuid" and isinstance(value, str):
            return UUID(value)
        return value

    def metadata(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "destination_type": self.target_type,
            "reason": self.reason,
            "encoding_version": 1 if self.text_fallback else None,
            "certified_input_count": self.certified_input_count,
            "destination_timezone": self.destination_timezone,
            "timezone_policy_version": TIMEZONE_POLICY_VERSION,
            **({"native_assignment_required": True} if self.native_assignment_required else {}),
            **({"conversion_policy": self.conversion_policy} if self.conversion_policy else {}),
        }


def resolve_profile(  # noqa: PLR0913 - schema authority and exact conversion metadata
    profile: ColumnProfile,
    target_backend: str,
    *,
    source_type: str | None = None,
    source_backend: str = "",
    authoritative_type: str | None = None,
    allow_timestamp_precision_loss: bool = True,
    destination_timezone: str | None = None,
    allow_pending_native_assignment: bool = False,
) -> ResolvedColumn:
    destination_timezone = destination_timezone or profile.destination_timezone
    if authoritative_type is not None:
        return resolve_authoritative_profile(
            profile,
            authoritative_type,
            target_backend,
            source_type=source_type,
            source_backend=source_backend,
            allow_timestamp_precision_loss=allow_timestamp_precision_loss,
            destination_timezone=destination_timezone,
            allow_pending_native_assignment=allow_pending_native_assignment,
        )
    target = _declared_target(source_type, source_backend, target_backend)
    if target is None or not declared_domain_fits(
        source_type, target, target_backend, source_backend=source_backend or None
    ):
        target = _resolve_native(profile, target_backend, source_type, source_backend)
    from .inferred_types import certify_native_candidate  # noqa: PLC0415 - candidate certification

    target = certify_native_candidate(
        profile,
        target,
        target_backend,
        source_type,
        source_backend,
        destination_timezone=destination_timezone,
    )
    fallback = target is None
    if fallback and not profile.text_encoding_valid:
        msg = "Selected input has no supported exact native representation or text encoding."
        raise ValueError(msg)
    if target is None:
        target = {"gp": "TEXT", "trino": "VARCHAR", "ch": "String"}[target_backend]
    if (
        target_backend == "ch"
        and profile.null_count
        and not target.startswith(("Nullable(", "Array(", "Tuple(", "Map("))
    ):
        target = f"Nullable({target})"
    return ResolvedColumn(
        target,
        source_type or "",
        source_backend,
        "no exact native representation" if fallback else "complete value profile",
        fallback,
        profile.count,
        target_backend,
        destination_timezone,
        conversion_policy="inferred_exact",
    )


def _integer_type(  # noqa: PLR0911 - exact backend integer candidates
    minimum: int, maximum: int, backend: str, source_type: str | None
) -> str | None:
    if backend == "ch":
        source = parse_native_type(source_type).base
        declared_domain = integer_domain(source_type)
        if declared_domain and declared_domain[0] <= minimum <= maximum <= declared_domain[1]:
            match = re.fullmatch(r"(u?)int(8|16|32|64|128|256)", source.name)
            if match:
                return ("UInt" if match.group(1) else "Int") + match.group(2)
        for bits in (64, 128, 256):
            if -(2 ** (bits - 1)) <= minimum <= maximum < 2 ** (bits - 1):
                return f"Int{bits}"
            if 0 <= minimum <= maximum < 2**bits:
                return f"UInt{bits}"
        return None
    if -(2**63) <= minimum <= maximum < 2**63:
        return "BIGINT"
    precision = max(len(integer_text(abs(minimum))), len(integer_text(abs(maximum))))
    if precision <= {"gp": 1000, "trino": 38}[backend]:
        return f"{'NUMERIC' if backend == 'gp' else 'DECIMAL'}({precision}, 0)"
    if backend == "gp" and precision <= _GP_NUMERIC_INTEGRAL_DIGITS:
        return "NUMERIC"
    return None


def validate_profile(  # noqa: C901, PLR0912, PLR0913, PLR0915 - authoritative family contracts
    profile: ColumnProfile,
    target_type: str,
    backend: str,
    *,
    allow_timestamp_precision_loss: bool = True,
    source_type: str | None = None,
    source_backend: str | None = None,
    destination_timezone: str | None = None,
) -> None:
    """Validate authoritative targets without widening or changing their family."""
    validate_backend_declaration(target_type, backend)
    destination_timezone = destination_timezone or profile.destination_timezone
    target_type = canonical_native_type(target_type, backend)
    validate_decimal_type(target_type, backend)
    outer_type = parse_native_type(target_type)
    base = outer_type.base
    if (
        base.name in {"text", "varchar", "char", "character", "character varying", "string"}
        and not profile.text_encoding_valid
    ):
        msg = "Selected input cannot be encoded exactly for authoritative text."
        raise ValueError(msg)
    if backend == "ch" and profile.null_count and not _permits_null(target_type):
        msg = "Selected input contains NULL for a nonnullable authoritative column."
        raise ValueError(msg)
    if validate_backend_profile(
        profile,
        target_type,
        backend,
        allow_timestamp_precision_loss=allow_timestamp_precision_loss,
    ):
        return
    if (
        base.name in {"varchar", "char", "character", "character varying"}
        and base.arguments
        and base.arguments[0].isdigit()
        and profile.maximum_text_length > int(base.arguments[0])
    ):
        msg = "Selected input exceeds authoritative text length."
        raise ValueError(msg)
    if (
        base.name in {"binary", "varbinary"}
        and base.arguments
        and base.arguments[0].isdigit()
        and profile.maximum_byte_length > int(base.arguments[0])
    ):
        msg = "Selected input exceeds authoritative binary length."
        raise ValueError(msg)
    from .nested_types import validate_nested  # noqa: PLC0415 - native recursive schemas

    if validate_nested(
        profile,
        target_type,
        backend,
        allow_timestamp_precision_loss=allow_timestamp_precision_loss,
        destination_timezone=destination_timezone,
        source_type=source_type,
        source_backend=source_backend,
    ):
        return
    if base.name in {"enum8", "enum16", "fixedstring"}:
        from .ch.value_validation import (  # noqa: PLC0415 - constrained native schemas
            enum_members,
            validate_constrained_value,
        )

        if base.name in {"enum8", "enum16"}:
            enum_members(target_type)
        else:
            validate_constrained_value("", target_type)
            if (
                not profile.families <= {"string", "binary"}
                or not profile.utf8_valid
                or profile.maximum_byte_length > int(base.arguments[0])
            ):
                msg = "Selected input exceeds authoritative FixedString byte constraints."
                raise ValueError(msg)
        if not profile.native_constraints_valid:
            msg = "Selected input violates authoritative ClickHouse scalar constraints."
            raise ValueError(msg)
        return
    if (
        backend == "gp"
        and base.name in {"text", "varchar", "char", "character", "character varying"}
        and ("binary" in profile.families or profile.text_has_nul or not profile.utf8_valid)
    ):
        msg = "Selected input is incompatible with authoritative Greenplum text; use BYTEA."
        raise ValueError(msg)
    if backend == "trino" and base.name in {"varchar", "char"} and not profile.utf8_valid:
        msg = "Selected text cannot be encoded exactly as UTF-8 for Trino."
        raise ValueError(msg)
    domain = integer_domain(target_type, backend=backend)
    decimal_shape = decimal_domain(target_type)
    if domain is not None:
        if not profile.families <= {"integer", "boolean"} or (
            profile.minimum_integer is not None
            and not domain[0]
            <= profile.minimum_integer
            <= cast("int", profile.maximum_integer)
            <= domain[1]
        ):
            msg = "Selected input is incompatible with authoritative integer schema."
            raise ValueError(msg)
    elif decimal_shape is not None:
        precision, scale = decimal_shape
        if (
            not profile.families <= {"integer", "decimal"}
            or profile.integral_digits > precision - scale
            or required_fractional_scale(profile) > scale
        ):
            msg = "Selected input exceeds authoritative decimal precision or scale."
            raise ValueError(msg)
    elif base.name in {"numeric", "decimal", "number"}:
        if (
            not profile.families <= {"integer", "decimal"}
            or profile.integral_digits > _GP_NUMERIC_INTEGRAL_DIGITS
            or required_fractional_scale(profile) > _GP_NUMERIC_FRACTIONAL_DIGITS
        ):
            msg = "Selected input exceeds authoritative numeric domain."
            raise ValueError(msg)
    elif base.name in _FLOAT_TYPES:
        limit = (
            _FLOAT32_LIMIT
            if _float_width(target_type, backend) == _FLOAT32_BITS
            else _FLOAT64_LIMIT
        )
        if (
            not profile.families <= {"integer", "decimal", "float"}
            or profile.maximum_numeric_magnitude > limit
        ):
            msg = "Selected input exceeds authoritative floating-point domain."
            raise ValueError(msg)
    elif base.name == "uuid":
        if not profile.families <= {"uuid", "string"} or not profile.all_uuid_strings:
            msg = "Selected input is incompatible with authoritative UUID schema."
            raise ValueError(msg)
    elif base.name in {"bytea", "binary", "varbinary"} and not binary_profile_fits(
        profile, source_type, source_backend
    ):
        msg = "Selected input is incompatible with authoritative binary schema."
        raise ValueError(msg)
    elif is_timestamp_type(target_type):
        native_integer_ticks = backend == "ch" and base.name in {"datetime", "datetime64"}
        families = {"timestamp", "date"}
        if native_integer_ticks:
            families.update({"integer", "boolean"})
        if not profile.families <= families or (profile.aware and profile.naive):
            msg = "Selected input has ambiguous or incompatible authoritative timestamp values."
            raise ValueError(msg)
        if not allow_timestamp_precision_loss and profile.temporal_scale > timestamp_scale(
            target_type
        ):
            msg = "Selected input exceeds authoritative timestamp precision."
            raise ValueError(msg)
        if backend == "ch":
            validate_timestamp_range(profile, target_type, destination_timezone)
            if native_integer_ticks:
                validate_integer_timestamp_range(profile, base.name, timestamp_scale(target_type))
        elif backend == "trino":
            from .trino.timestamp_bounds import (  # noqa: PLC0415 - registry cycle.
                validate_timestamp_profile,
            )

            validate_timestamp_profile(
                profile, target_type, destination_timezone=destination_timezone
            )
    elif base.name in {"bool", "boolean"} and not profile.families <= {"boolean"}:
        msg = "Selected input is incompatible with authoritative boolean schema."
        raise ValueError(msg)
    elif base.name in {"date", "date32"}:
        if not profile.families <= {"date"}:
            msg = "Selected input is incompatible with authoritative date schema."
            raise ValueError(msg)
        if backend == "ch" and profile.minimum_date is not None:
            lower_date, upper_date = (
                (dt.date(1970, 1, 1), dt.date(2149, 6, 6))
                if base.name == "date"
                else (dt.date(1900, 1, 1), dt.date(2299, 12, 31))
            )
            if (
                profile.minimum_date < lower_date
                or cast("dt.date", profile.maximum_date) > upper_date
            ):
                msg = "Selected input exceeds authoritative date range."
                raise ValueError(msg)

    known = (
        domain is not None
        or decimal_shape is not None
        or is_timestamp_type(target_type)
        or base.name in _FLOAT_TYPES
        or base.name
        in {
            "numeric",
            "decimal",
            "number",
            "boolean",
            "bool",
            "date",
            "date32",
            "uuid",
            "bytea",
            "binary",
            "varbinary",
            "varchar",
            "char",
            "character",
            "character varying",
            "text",
            "string",
        }
    )
    if not known and not (
        source_type and source_backend == backend and source_type.strip() == target_type.strip()
    ):
        msg = (
            "Cannot certify values for this authoritative native type before mutation. "
            "Use an owned typed-stage validation or metadata-proven identical source type."
        )
        raise ValueError(msg)


def _date_domain(type_name: str | None, backend: str | None) -> tuple[dt.date, dt.date] | None:
    native = parse_native_type(type_name).base.name
    if native not in {"date", "date32"}:
        return None
    if native == "date32":
        return dt.date(1900, 1, 1), dt.date(2299, 12, 31)
    if backend == "ch":
        return dt.date(1970, 1, 1), dt.date(2149, 6, 6)
    return dt.date.min, dt.date.max


def _timestamp_range_fits(
    source_type: str, target_type: str, source_backend: str | None, target_backend: str
) -> bool:
    if source_backend is None and parse_native_type(source_type).base.name.startswith("datetime"):
        source_backend = "ch"
    if target_backend != "ch":
        return source_backend in {target_backend, "ch"}
    if source_backend != "ch":
        return False

    def bounds(native: str) -> tuple[int, int]:
        if parse_native_type(native).base.name == "datetime":
            return 0, (2**32 - 1) * 10**9
        scale = timestamp_scale(native)
        unit = 10 ** (9 - scale)
        # DateTime64's documented calendar domain and signed transport ticks
        # both constrain its range; scale 9 therefore ends during 2262.
        return max(-2208988800 * 10**9, -(2**63) * unit), min(
            10413791999 * 10**9, (2**63 - 1) * unit
        )

    source_range, target_range = bounds(source_type), bounds(target_type)
    return target_range[0] <= source_range[0] and source_range[1] <= target_range[1]


def declared_domain_fits(  # noqa: C901, PLR0911, PLR0912 - exact declared domain proofs
    source_type: str | None,
    target_type: str | None,
    target_backend: str,
    *,
    source_backend: str | None = None,
) -> bool:
    """Whether metadata alone proves exact transport (no adaptive scan required)."""
    source_type = canonical_native_type(source_type, source_backend)
    target_type = canonical_native_type(target_type, target_backend)
    if not source_type or not target_type:
        return False
    if (
        target_backend == "ch"
        and not _permits_null(target_type)
        and (_permits_null(source_type) or (source_backend is not None and source_backend != "ch"))
    ):
        return False
    from .nested_types import declared_nested_fits  # noqa: PLC0415 - recursive native domains

    nested_fit = declared_nested_fits(source_type, target_type, target_backend, source_backend)
    if nested_fit is not None:
        return nested_fit
    backend_fit = backend_domain_fits(source_type, target_type, target_backend, source_backend)
    if backend_fit is not None:
        return backend_fit
    source, target = parse_native_type(source_type).base, parse_native_type(target_type).base
    if source_backend == target_backend and source_type.strip() == target_type.strip():
        return True
    source_integer = integer_domain(source_type, backend=source_backend)
    target_integer = integer_domain(target_type, backend=target_backend)
    if source_integer:
        if target_integer:
            return target_integer[0] <= source_integer[0] and source_integer[1] <= target_integer[1]
        decimal = decimal_domain(target_type)
        return bool(
            decimal and decimal[0] - decimal[1] >= max(len(str(abs(v))) for v in source_integer)
        )
    source_decimal, target_decimal = decimal_domain(source_type), decimal_domain(target_type)
    if (
        source.name in {"numeric", "decimal"}
        and target.name in {"numeric", "decimal"}
        and not source.arguments
        and not target.arguments
        and target_backend == "gp"
    ):
        return True
    if source_decimal:
        return bool(
            target_decimal
            and source_decimal[1] <= target_decimal[1]
            and source_decimal[0] - source_decimal[1] <= target_decimal[0] - target_decimal[1]
        )
    if is_timestamp_type(source_type):
        from analytics_toolkit.sql.backends.source_schema import (  # noqa: PLC0415 - avoid backend registry import cycles
            type_has_timezone,
        )

        return (
            is_timestamp_type(target_type)
            and timestamp_scale(source_type) <= timestamp_scale(target_type)
            and _timestamp_range_fits(source_type, target_type, source_backend, target_backend)
            and (
                source_backend == "ch"
                or type_has_timezone((source_type or "").lower())
                == type_has_timezone(target_type.lower())
            )
        )
    if source.name in {
        "char",
        "varchar",
        "text",
        "string",
        "character varying",
    } and target.name in {"char", "varchar", "text", "string", "character varying"}:
        if (source.name == "string" and target_backend != "ch") or (
            target_backend == "gp" and source_backend != "gp"
        ):
            return False
        if target.arguments and target.arguments[0].isdigit():
            return bool(
                source.arguments
                and source.arguments[0].isdigit()
                and int(source.arguments[0]) <= int(target.arguments[0])
            )
        return True
    if (
        source.name in {"binary", "bytea", "varbinary"}
        and target.name == "string"
        and target_backend == "ch"
    ):
        return True
    source_float = _float_width(source_type, source_backend)
    target_float = _float_width(target_type, target_backend)
    if source_float is not None or target_float is not None:
        return bool(source_float and target_float and source_float <= target_float)
    source_date = _date_domain(source_type, source_backend)
    target_date = _date_domain(target_type, target_backend)
    if source_date is not None or target_date is not None:
        if source_backend in {"gp", "trino"} and target_backend in {"gp", "trino"}:
            return bool(source_date and target_date and source_backend == target_backend)
        return bool(
            source_date
            and target_date
            and target_date[0] <= source_date[0]
            and source_date[1] <= target_date[1]
        )
    binary = {"bytea", "binary", "varbinary"}
    if source.name in binary or target.name in binary:
        return bool(
            source.name in binary
            and target.name in binary
            and (
                not target.arguments
                or (
                    source.arguments
                    and source.arguments[0].isdigit()
                    and target.arguments[0].isdigit()
                    and int(source.arguments[0]) <= int(target.arguments[0])
                )
            )
        )
    families = (
        {"bool", "boolean"},
        {"uuid"},
    )
    return any(source.name in group and target.name in group for group in families)


def native_candidates(backend: str) -> list[str]:
    return {
        "gp": ["BIGINT", "NUMERIC", "TIMESTAMP", "TEXT"],
        "trino": ["BIGINT", "DECIMAL(38, scale)", "TIMESTAMP(6)", "VARCHAR"],
        "ch": [
            "Int64",
            "UInt64",
            "Int128",
            "UInt128",
            "Int256",
            "UInt256",
            "Decimal(76, scale)",
            "DateTime64(9)",
            "String",
        ],
    }[backend]


def requires_adaptive_resolution(
    source_schema: Any,
    target_backend: str,
    source_backend: str | None = None,
    authoritative_types: dict[str, str] | None = None,
) -> bool:
    targets = authoritative_types or {
        column.name: _declared_target(column.native_type, source_backend or "", target_backend)
        or ""
        for column in source_schema
    }
    return any(
        not declared_domain_fits(
            column.native_type,
            targets.get(column.name),
            target_backend,
            source_backend=source_backend,
        )
        for column in source_schema
    )
