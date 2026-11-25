"""Compatibility imports for shared value profiles and backend resolution."""

from analytics_toolkit.sql.backends.type_resolution import (
    ColumnProfile,
    NativeType,
    ResolvedColumn,
    decimal_domain,
    declared_domain_fits,
    integer_domain,
    is_timestamp_type,
    native_candidates,
    parse_native_type,
    requires_adaptive_resolution,
    resolve_column_type,
    resolve_profile,
    timestamp_scale,
    validate_profile,
)

__all__ = [
    "ColumnProfile",
    "NativeType",
    "ResolvedColumn",
    "decimal_domain",
    "declared_domain_fits",
    "integer_domain",
    "is_timestamp_type",
    "native_candidates",
    "parse_native_type",
    "requires_adaptive_resolution",
    "resolve_column_type",
    "resolve_profile",
    "timestamp_scale",
    "validate_profile",
]
