"""Greenplum native domains which foreign finite schemas cannot certify."""

from __future__ import annotations

from .type_identity import canonical_builtin_type


def declared_numeric_domain_fits(source_type: str, target_backend: str) -> bool | None:
    """NUMERIC admits NaN even when its precision and scale are constrained.

    Foreign decimal precision alone therefore cannot prove complete source
    coverage. A selected-value profile must decide its exact native or text
    representation before the destination is created or populated.
    """
    source = canonical_builtin_type(source_type)
    if (
        target_backend != "gp"
        and source is not None
        and (source == "NUMERIC" or source.startswith("NUMERIC("))
    ):
        return False
    return None
