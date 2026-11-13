"""Resolve only the Greenplum built-ins already supported by native transport.

The returned declaration is for internal validation and conversion. Callers keep
the original schema spelling for DDL, metadata and SQL parameter type context.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import TYPE_CHECKING

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError, TokenError

if TYPE_CHECKING:
    from analytics_toolkit.sql.backends.value_profiles import ColumnProfile

_CATALOG_TYPES = {
    "int2": "SMALLINT",
    "int4": "INTEGER",
    "int8": "BIGINT",
    "bool": "BOOLEAN",
    "float4": "REAL",
    "float8": "DOUBLE PRECISION",
    "numeric": "NUMERIC",
    "bpchar": "CHARACTER",
    "varchar": "VARCHAR",
    "text": "TEXT",
    "date": "DATE",
    "timestamp": "TIMESTAMP",
    "timestamptz": "TIMESTAMP WITH TIME ZONE",
    "time": "TIME",
    "timetz": "TIME WITH TIME ZONE",
    "interval": "INTERVAL",
    "bytea": "BYTEA",
    "uuid": "UUID",
    "json": "JSON",
    "jsonb": "JSONB",
    "char": '"char"',
}
_CATALOG_ARRAY_TYPES = {
    f"_{name}": f"{'VARCHAR' if name == 'bpchar' else native}[]"
    for name, native in _CATALOG_TYPES.items()
}
_SQL_ALIASES = {
    **_CATALOG_TYPES,
    **_CATALOG_ARRAY_TYPES,
    "smallint": "SMALLINT",
    "integer": "INTEGER",
    "int": "INTEGER",
    "bigint": "BIGINT",
    "boolean": "BOOLEAN",
    "real": "REAL",
    "double precision": "DOUBLE PRECISION",
    "float": "FLOAT",
    "decimal": "NUMERIC",
    "dec": "NUMERIC",
    "char": "CHARACTER",
    "character": "CHARACTER",
    "character varying": "VARCHAR",
    "char varying": "VARCHAR",
    "nchar": "CHARACTER",
    "nchar varying": "VARCHAR",
    "national char": "CHARACTER",
    "national character": "CHARACTER",
    "national char varying": "VARCHAR",
    "national character varying": "VARCHAR",
}
_FIXED_SQL_CHARACTERS = {"char", "character", "nchar", "national char", "national character"}
_UNSIGNED_SQL_PARAMETERS = _FIXED_SQL_CHARACTERS | {
    "varchar",
    "char varying",
    "character varying",
    "nchar varying",
    "national char varying",
    "national character varying",
    "float",
    "time",
    "timestamp",
    "interval",
}
_IDENTIFIER = r'(?:"(?:[^"]|"")*"|[A-Za-z_][\w$]*)'
_DECLARATION = re.compile(
    rf"\s*(?P<name>NATIONAL\s+(?:CHARACTER|CHAR)(?:\s+VARYING)?|"
    rf"{_IDENTIFIER}(?:\s*\.\s*{_IDENTIFIER})?"
    r"(?:\s+(?:PRECISION|VARYING))?)"
    r"\s*(?P<parameters>\([^()]*\))?"
    r"\s*(?P<zone>(?:WITH|WITHOUT)\s+TIME\s+ZONE)?"
    r"\s*(?P<fields>(?:YEAR|MONTH|DAY|HOUR|MINUTE|SECOND)"
    r"(?:\s+TO\s+(?:YEAR|MONTH|DAY|HOUR|MINUTE|SECOND))?)?"
    r"\s*(?P<field_parameters>\([^()]*\))?"
    r"\s*(?P<arrays>(?:\[\s*\]\s*)*)",
    flags=re.IGNORECASE | re.ASCII,
)
_INTERVAL_FIELDS = {
    "YEAR",
    "MONTH",
    "DAY",
    "HOUR",
    "MINUTE",
    "SECOND",
    "YEAR TO MONTH",
    "DAY TO HOUR",
    "DAY TO MINUTE",
    "DAY TO SECOND",
    "HOUR TO MINUTE",
    "HOUR TO SECOND",
    "MINUTE TO SECOND",
}
_NUMERIC_MAX_PRECISION = 1000
_FLOAT_MAX_PRECISION = 53
_FLOAT_SINGLE_PRECISION = 24
_NUMERIC_PARAMETER_COUNT = 2
_MAX_CHARACTER_LENGTH = 10 * 1024 * 1024


def _catalog_name(name: str) -> str | None:
    try:
        table = parse_one(name, read="postgres", into=exp.Table)
    except (ParseError, TokenError):
        return None
    # The complete declaration grammar limits this parse to one or two names;
    # parse_one(into=Table) rejects the remaining SQL alias syntax.
    parts = [table.args["db"], table.this] if table.args.get("db") is not None else [table.this]
    names = [part.name if part.args.get("quoted") else part.name.lower() for part in parts]
    if table.args.get("db") is not None and names[0] != "pg_catalog":
        return None
    return str(names[-1])


def _identity(name: str) -> tuple[str, str | None] | None:
    unquoted = '"' not in name and "." not in name
    normalized = " ".join(name.lower().split())
    if unquoted:
        native = _SQL_ALIASES.get(normalized)
        return (native, normalized) if native else None
    catalog_name = _catalog_name(name)
    native = _CATALOG_TYPES.get(catalog_name or "") or _CATALOG_ARRAY_TYPES.get(catalog_name or "")
    return (native, None) if native else None


def _parameters(raw: str | None) -> tuple[int, ...]:
    if raw is None:
        return ()
    values = raw[1:-1].split(",")
    if not all(re.fullmatch(r"\s*[+-]?[0-9]+\s*", value, flags=re.ASCII) for value in values):
        message = "Greenplum built-in type parameters must be integer literals."
        raise ValueError(message)
    return tuple(int(value) for value in values)


def _invalid_parameters() -> None:
    message = "Invalid parameters for a recognized Greenplum built-in type."
    raise ValueError(message)


def _numeric_domain(parameters: tuple[int, ...]) -> str:
    if not parameters:
        return "NUMERIC"
    if len(parameters) not in {1, _NUMERIC_PARAMETER_COUNT}:
        _invalid_parameters()
    precision, scale = (
        parameters if len(parameters) == _NUMERIC_PARAMETER_COUNT else (parameters[0], 0)
    )
    if not 1 <= precision <= _NUMERIC_MAX_PRECISION or not 0 <= scale <= precision:
        _invalid_parameters()
    return f"NUMERIC({precision}, {scale})"


def _character_domain(native: str, alias: str | None, parameters: tuple[int, ...]) -> str:
    if not parameters:
        if alias in _FIXED_SQL_CHARACTERS:
            return "CHARACTER(1)"
        # Catalog bpchar without a typmod is unbounded. Use the same string
        # input domain without reintroducing SQL CHAR's default length on a
        # second internal canonicalization; the caller keeps raw BPCHAR DDL.
        return "VARCHAR"
    if len(parameters) != 1 or not 1 <= parameters[0] <= _MAX_CHARACTER_LENGTH:
        _invalid_parameters()
    return f"{native}({parameters[0]})"


def _float_domain(parameters: tuple[int, ...]) -> str:
    if len(parameters) > 1 or (parameters and not 1 <= parameters[0] <= _FLOAT_MAX_PRECISION):
        _invalid_parameters()
    return "REAL" if parameters and parameters[0] <= _FLOAT_SINGLE_PRECISION else "DOUBLE PRECISION"


def _temporal_domain(native: str, parameters: tuple[int, ...]) -> str:
    if len(parameters) > 1:
        _invalid_parameters()
    if not parameters:
        return native
    precision = f"({parameters[0]})"
    if native.endswith(" WITH TIME ZONE"):
        return native[: -len(" WITH TIME ZONE")] + precision + " WITH TIME ZONE"
    return native + precision


def _scalar(native: str, alias: str | None, parameters: tuple[int, ...]) -> str:
    if native == "NUMERIC":
        return _numeric_domain(parameters)
    if native in {"CHARACTER", "VARCHAR"}:
        return _character_domain(native, alias, parameters)
    if native == "FLOAT":
        return _float_domain(parameters)
    if native.startswith(("TIME", "INTERVAL")):
        return _temporal_domain(native, parameters)
    if parameters:
        _invalid_parameters()
    return native


def _modifiers(
    match: re.Match[str], native: str, alias: str | None, parameters: tuple[int, ...]
) -> tuple[str, tuple[int, ...]]:
    zone = " ".join((match["zone"] or "").upper().split())
    fields = " ".join((match["fields"] or "").upper().split())
    field_parameters = _parameters(match["field_parameters"])
    if zone:
        if alias not in {"time", "timestamp"} or fields or field_parameters:
            _invalid_parameters()
        if zone == "WITH TIME ZONE":
            native += " WITH TIME ZONE"
    if fields:
        if (
            alias != "interval"
            or fields not in _INTERVAL_FIELDS
            or (parameters and field_parameters)
        ):
            _invalid_parameters()
        if field_parameters and (not fields.endswith("SECOND") or len(field_parameters) != 1):
            _invalid_parameters()
        if parameters and not fields.endswith("SECOND"):
            return f"INTERVAL({parameters[0]}) {fields}", ()
        native += " " + fields
        parameters = parameters or field_parameters
    elif field_parameters:
        _invalid_parameters()
    return native, parameters


def _validate_sql_literals(alias: str | None, match: re.Match[str]) -> None:
    if alias not in _UNSIGNED_SQL_PARAMETERS:
        return
    for key in ("parameters", "field_parameters"):
        raw = match[key]
        if raw is not None and not re.fullmatch(r"\(\s*[0-9]+\s*\)", raw, flags=re.ASCII):
            _invalid_parameters()


@lru_cache(maxsize=1024)
def canonical_builtin_type(raw: str) -> str | None:
    """Return a supported built-in domain, never a custom type's guessed identity."""
    match = _DECLARATION.fullmatch(raw)
    if match is None:
        return None
    identity = _identity(match["name"])
    if identity is None:
        return None
    native, alias = identity
    _validate_sql_literals(alias, match)
    parameters = _parameters(match["parameters"])
    if parameters and native in {"INTERVAL", "INTERVAL[]"} and alias != "interval":
        # Generic catalog INTERVAL typmods start with a field bitmask, whereas
        # SQL INTERVAL(p) injects FULL_RANGE before its fractional precision.
        # Do not invent precision semantics for the internal mask interface.
        return None
    if native.endswith("[]"):
        if any(match[key] for key in ("zone", "fields", "field_parameters", "arrays")):
            _invalid_parameters()
        return _scalar(native[:-2], None, parameters) + "[]"
    native, parameters = _modifiers(match, native, alias, parameters)
    canonical = _scalar(native, alias, parameters)
    return canonical + "[]" * match["arrays"].count("[")


def validate_builtin_profile(profile: ColumnProfile, canonical_type: str) -> bool:
    """Validate the catalog's internal byte character separately from SQL CHAR."""
    if canonical_type != '"char"':
        return False
    if (
        not profile.families <= {"string"}
        or not profile.utf8_valid
        or profile.maximum_byte_length > 1
        or profile.text_has_nul
    ):
        message = "Selected input is incompatible with Greenplum's internal byte character."
        raise ValueError(message)
    return True
