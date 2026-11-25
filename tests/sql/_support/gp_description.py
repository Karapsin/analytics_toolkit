"""Actual builtin OIDs and explicit run-owned domain catalog metadata."""

from __future__ import annotations

from analytics_toolkit.sql.backends.gp.adapter import _GP_OID_TYPES
from analytics_toolkit.sql.backends.gp.type_identity import canonical_builtin_type
from analytics_toolkit.sql.backends.type_resolution import parse_native_type

_DOMAIN_OIDS = {
    '"fixture"."short_text"': (500001, 1043),
    "fixture.short_text": (500001, 1043),
    '"fixture"."checked_text"': (500002, 25),
    "fixture.checked_text": (500002, 25),
}


def _family(native):
    array = native.endswith("[]")
    while native.endswith("[]"):
        native = native[:-2]
    normalized = canonical_builtin_type(native) or native
    return parse_native_type(normalized).name, array


def description_column(name, native):
    declaration = native.strip()
    if declaration in _DOMAIN_OIDS:
        return name, _DOMAIN_OIDS[declaration][0], None, None, None, None, None
    family = _family(declaration)
    candidates = [oid for oid, value in _GP_OID_TYPES.items() if _family(value) == family]
    if not candidates and family == ("macaddr", False):
        candidates = [829]
    assert len(candidates) == 1, f"Fixture requires actual native type metadata: {declaration}"
    arguments = parse_native_type(declaration).arguments
    precision, scale = None, None
    if family[0] == "numeric" and len(arguments) == 2:
        precision, scale = map(int, arguments)
    return name, candidates[0], None, None, precision, scale, None


def native_catalog_row(oid):
    for domain_oid, base_oid in _DOMAIN_OIDS.values():
        if domain_oid == oid:
            return "d", base_oid, 0
    if oid == 829:
        return "b", 0, 0
    message = "The fixture has no declared native source catalog identity."
    raise AssertionError(message)
