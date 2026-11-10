"""Native text/binary choices preserve physical bytes and Python value identity."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .value_profiles import ColumnProfile


def resolve_text_family(
    profile: ColumnProfile, backend: str, source_type: str | None, source_backend: str | None
) -> str | None:
    from .type_resolution import parse_native_type  # noqa: PLC0415 - native grammar cycle

    families = profile.families
    binary_type = {"gp": "BYTEA", "trino": "VARBINARY", "ch": "String"}[backend]
    if families == {"binary"}:
        return binary_type
    physical_bytes = source_backend == "ch" and parse_native_type(source_type).base.name in {
        "string",
        "fixedstring",
    }
    if families == {"string", "binary"}:
        return binary_type if physical_bytes else None
    if backend == "gp" and not profile.utf8_valid:
        msg = "Selected text cannot be encoded exactly for Greenplum."
        raise ValueError(msg)
    if backend == "gp" and profile.text_has_nul:
        return binary_type
    return {"gp": "TEXT", "trino": "VARCHAR", "ch": "String"}[backend]


def binary_profile_fits(
    profile: ColumnProfile, source_type: str | None, source_backend: str | None
) -> bool:
    from .type_resolution import parse_native_type  # noqa: PLC0415 - native grammar cycle

    physical_bytes = source_backend == "ch" and parse_native_type(source_type).base.name in {
        "string",
        "fixedstring",
    }
    return profile.families <= {"binary"} or bool(
        physical_bytes and profile.utf8_valid and profile.families <= {"string", "binary"}
    )
