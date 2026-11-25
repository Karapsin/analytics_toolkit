"""Project native floats to send-format bytes outside an unchanged source query."""

from __future__ import annotations

from typing import Any, NoReturn

from analytics_toolkit.sql.backends.execution_outcome import close_cursor
from analytics_toolkit.sql.backends.source_schema import description_value, strip_query_semicolon
from analytics_toolkit.sql.execution.labels import apply_query_label

from .source_codecs import _ARRAY_ELEMENTS
from .source_float_binary import SourceFloatWireError, decode_float, decode_float_array
from .type_identity import canonical_builtin_type

_FLOATS = {700: 4, 701: 8}
_CATALOG_FIELD_COUNT = 3
_FLOAT_ARRAYS = {1021: (4, 700), 1022: (8, 701)}
_NONFLOAT_OIDS = {
    16,
    17,
    18,
    19,
    20,
    21,
    23,
    25,
    26,
    114,
    650,
    869,
    1042,
    1043,
    1082,
    1083,
    1114,
    1184,
    1186,
    1266,
    1700,
    2950,
    3802,
} | (set(_ARRAY_ELEMENTS) - set(_FLOAT_ARRAYS))


def _fail(message: str) -> NoReturn:
    raise SourceFloatWireError(message)


def _native_float_kind(connection: Any, oid: int, seen: frozenset[int] = frozenset()) -> Any:
    if oid in _FLOATS:
        return _FLOATS[oid], None
    if oid in _FLOAT_ARRAYS:
        return _FLOAT_ARRAYS[oid]
    if oid in _NONFLOAT_OIDS:
        return None
    if oid in seen:
        _fail("Cannot resolve the native source float domain.")
    return _catalog_float_kind(connection, oid, seen | {oid})


def _catalog_type(connection: Any, oid: int) -> Any:
    cursor = connection.cursor()
    try:
        cursor.execute(
            apply_query_label(
                "SELECT t.typtype, t.typbasetype, t.typelem "
                "FROM pg_catalog.pg_type t WHERE t.oid = %s",
                None,
            ),
            (oid,),
        )
        row = cursor.fetchone()
    finally:
        close_cursor(cursor, backend="gp")
    if row is None or len(row) != _CATALOG_FIELD_COUNT:
        _fail("Cannot resolve the native source type for exact float transport.")
    return row


def _catalog_float_kind(connection: Any, oid: int, trail: frozenset[int]) -> Any:
    kind, base_oid, element_oid = _catalog_type(connection, oid)
    if kind == "d" and base_oid:
        return _native_float_kind(connection, int(base_oid), trail)
    if element_oid:
        element = _native_float_kind(connection, int(element_oid), trail)
        if element is not None:
            if element[1] is not None:
                _fail("Cannot certify a nested native source float array.")
            return element[0], int(element_oid)
    if kind == "c":
        _fail("Cannot certify native floats in an unresolved composite source.")
    return None


def _proven_nonfloat(columns: Any) -> bool:
    if not columns:
        return False
    for column in columns:
        native = canonical_builtin_type(column.native_type or "")
        if native is None:
            return False
        while native.endswith("[]"):
            native = native[:-2]
        if native in {"REAL", "DOUBLE PRECISION"}:
            return False
    return True


class ExactFloatCursor:
    """Keep the original positional DBAPI metadata while decoding bytea cells."""

    def __init__(self, cursor: Any, description: Any, kinds: list[Any]):
        self._cursor = cursor
        self.description = description
        self._kinds = kinds

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    def _row(self, row: Any) -> Any:
        if row is None:
            return None
        if len(row) != len(self._kinds):
            _fail("Native float source result has an unexpected column count.")
        return tuple(
            value
            if kind is None
            else decode_float(value, kind[0])
            if kind[1] is None
            else decode_float_array(value, kind[0], kind[1])
            for value, kind in zip(row, self._kinds)
        )

    def fetchmany(self, size: int | None = None) -> list[Any]:
        rows = self._cursor.fetchmany() if size is None else self._cursor.fetchmany(size)
        return [self._row(row) for row in rows]

    def fetchone(self) -> Any:
        return self._row(self._cursor.fetchone())

    def fetchall(self) -> list[Any]:
        return [self._row(row) for row in self._cursor.fetchall()]

    def __iter__(self) -> Any:
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row


def execute_exact_source_query(
    connection: Any,
    cursor: Any,
    query: str,
    *,
    source_columns: Any = None,
    source_description: Any = None,
) -> Any:
    """Execute selected data once; metadata probing never consumes source values."""
    if source_description is None and _proven_nonfloat(source_columns):
        cursor.execute(query)
        return cursor
    description = source_description or cursor.description
    if description is None or not description:
        cursor.execute(
            f"SELECT * FROM (\n{strip_query_semicolon(query)}\n) AS source_schema_probe WHERE 1 = 0"  # noqa: S608 -- original SELECT, no substituted values.
        )
        description = cursor.description
    if description is None:
        _fail("Exact source transport requires complete native column metadata.")
    kinds = [
        _native_float_kind(connection, int(description_value(column, "type_code", 1)))
        for column in description
    ]
    if not any(kind is not None for kind in kinds):
        cursor.execute(query)
        return cursor
    aliases = [f'"_atk_{index}"' for index in range(len(description))]
    projections = []
    for column, alias, kind in zip(description, aliases, kinds):
        expression = f'"_atk_source".{alias}'
        if kind is not None:
            function = "array_send" if kind[1] is not None else f"float{kind[0]}send"
            expression = f"pg_catalog.{function}({expression})"
        name = str(description_value(column, "name", 0)).replace('"', '""')
        projections.append(f'{expression} AS "{name}"')
    projected = (
        f"SELECT {', '.join(projections)} FROM (\n{strip_query_semicolon(query)}\n) "  # noqa: S608 -- original SELECT and quoted metadata identifiers.
        f'AS "_atk_source"({", ".join(aliases)})'
    )
    cursor.execute(projected)
    return ExactFloatCursor(cursor, description, kinds)
