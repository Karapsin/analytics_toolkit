"""Exact Greenplum destination metadata, including array element typmods/rank."""

from __future__ import annotations

from typing import Any

from analytics_toolkit.sql.backends.execution_outcome import close_cursor

_SCALAR_NAMES = {
    "int2": "SMALLINT",
    "smallint": "SMALLINT",
    "int4": "INTEGER",
    "integer": "INTEGER",
    "int8": "BIGINT",
    "bigint": "BIGINT",
    "bool": "BOOLEAN",
    "boolean": "BOOLEAN",
    "float4": "REAL",
    "real": "REAL",
    "float8": "DOUBLE PRECISION",
    "double precision": "DOUBLE PRECISION",
    "numeric": "NUMERIC",
    "decimal": "NUMERIC",
    "varchar": "VARCHAR",
    "character varying": "VARCHAR",
    "bpchar": "CHARACTER",
    "character": "CHARACTER",
    "timestamp": "TIMESTAMP",
    "timestamp without time zone": "TIMESTAMP",
    "timestamp with time zone": "TIMESTAMP WITH TIME ZONE",
    "timestamptz": "TIMESTAMP WITH TIME ZONE",
    "time": "TIME",
    "time without time zone": "TIME",
    "time with time zone": "TIME WITH TIME ZONE",
    "date": "DATE",
    "text": "TEXT",
    "bytea": "BYTEA",
    "uuid": "UUID",
    "json": "JSON",
    "jsonb": "JSONB",
    "interval": "INTERVAL",
}


def get_table_column_types(
    adapter: Any, connection: Any, table_name: str, *, connection_key: str
) -> dict[str, str]:
    from analytics_toolkit.sql.core.identifiers import (  # noqa: PLC0415 -- registry cycle.
        split_gp_table_name,
    )

    del adapter, connection_key
    schema, relation = split_gp_table_name(table_name)
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod) "
            "|| pg_catalog.repeat('[]', GREATEST(a.attndims - 1, 0)) AS formatted_type "
            "FROM pg_catalog.pg_attribute AS a "
            "JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid "
            "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = %s "
            "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum",
            (schema, relation),
        )
        return {
            str(name): canonical_catalog_type(str(native)) for name, native in cursor.fetchall()
        }
    finally:
        close_cursor(cursor, backend="gp")


def canonical_catalog_type(native: str) -> str:
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- registry cycle.
        parse_native_type,
    )

    scalar = native
    suffix = ""
    while scalar.endswith("[]"):
        scalar = scalar[:-2]
        suffix += "[]"
    parsed = parse_native_type(scalar)
    name = _SCALAR_NAMES.get(parsed.name)
    if name is None:
        return native  # Quoted, schema-qualified domains/enums retain their identity.
    arguments = f"({', '.join(parsed.arguments)})" if parsed.arguments else ""
    if name.endswith(" WITH TIME ZONE"):
        return name[: -len(" WITH TIME ZONE")] + arguments + " WITH TIME ZONE" + suffix
    return name + arguments + suffix


def format_gp_information_schema_type(
    data_type: str, udt_name: Any, numeric_precision: Any, numeric_scale: Any
) -> str:
    """Compatibility formatter; catalog lookup is required for complete array typmods."""
    normalized = data_type.lower()
    if normalized == "array":
        from sqlglot import exp  # noqa: PLC0415 -- compatibility helper only.

        element = str(udt_name or "")
        if not element.startswith("_") or len(element) == 1:
            message = "Greenplum array metadata requires its catalog element type."
            raise ValueError(message)
        element = element[1:]
        base = _SCALAR_NAMES.get(element.lower())
        if base is None:
            return str(exp.to_identifier(element, quoted=True).sql(dialect="postgres")) + "[]"
        return (
            format_gp_information_schema_type(base, element, numeric_precision, numeric_scale)
            + "[]"
        )
    if normalized == "numeric" and numeric_precision is not None:
        scale = f", {numeric_scale}" if numeric_scale is not None else ""
        return f"NUMERIC({numeric_precision}{scale})"
    return _SCALAR_NAMES.get(normalized, str(udt_name or data_type).upper())
