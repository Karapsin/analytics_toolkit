"""Metadata proofs for injective native map-key conversions."""

from __future__ import annotations


def map_key_conversion_is_injective(
    source_type: str, target_type: str, backend: str, source_backend: str | None
) -> bool:
    from .nested_types import nested_descriptor  # noqa: PLC0415 - recursive native structure
    from .type_resolution import (  # noqa: PLC0415 - native domains
        declared_domain_fits,
        parse_native_type,
    )

    if not declared_domain_fits(source_type, target_type, backend, source_backend=source_backend):
        return False
    if source_backend == backend and source_type.strip() == target_type.strip():
        return True
    source_kind, source_fields = nested_descriptor(source_type)
    target_kind, target_fields = nested_descriptor(target_type)
    if source_kind or target_kind:
        same_shape = source_kind == target_kind or {source_kind, target_kind} <= {"row", "tuple"}
        return bool(
            same_shape
            and source_kind in {"array", "row", "tuple"}
            and len(source_fields) == len(target_fields)
            and all(
                map_key_conversion_is_injective(left[1], right[1], backend, source_backend)
                for left, right in zip(source_fields, target_fields)
            )
        )
    source = parse_native_type(source_type).base.name
    target = parse_native_type(target_type).base.name
    char = {"char", "character"}
    # VARCHAR values can differ only in trailing blanks. CHAR identity merges
    # them even when its declared width covers the source's entire value domain.
    if backend == "trino" and target in char and source not in char:
        return False
    if target == "binary" and source != "binary":
        return False
    left_family = _key_family(source_type, source_backend)
    right_family = _key_family(target_type, backend)
    return left_family is not None and left_family == right_family


def _key_family(native_type: str, backend: str | None) -> str | None:
    from .type_resolution import (  # noqa: PLC0415 - exact primitive native domains
        _float_width,
        decimal_domain,
        integer_domain,
        is_timestamp_type,
        parse_native_type,
    )

    if (
        integer_domain(native_type, backend=backend) is not None
        or decimal_domain(native_type) is not None
    ):
        return "exact numeric"
    if _float_width(native_type, backend) is not None:
        return "floating"
    if is_timestamp_type(native_type):
        return "timestamp"
    name = parse_native_type(native_type).base.name
    for family, names in {
        "text": {"char", "character", "varchar", "character varying", "text", "string"},
        "binary": {"binary", "varbinary", "bytea"},
        "boolean": {"bool", "boolean"},
        "date": {"date", "date32"},
        "uuid": {"uuid"},
    }.items():
        if name in names:
            return family
    return None
