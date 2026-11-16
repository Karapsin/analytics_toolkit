"""Certify owned native GP assignment stages without guessing custom type domains."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from numbers import Integral
from typing import TYPE_CHECKING, Any, Literal

from analytics_toolkit.sql.backends.execution_outcome import close_cursor
from analytics_toolkit.sql.backends.insert_constraints import read_staged_null_counts
from analytics_toolkit.sql.execution.cancellation import raise_if_cancelled
from analytics_toolkit.sql.execution.labels import apply_query_label

from .type_identity import canonical_builtin_type

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from analytics_toolkit.sql.core.replay_spool import FrozenInputIdentity

_OWNERSHIP_ATTRIBUTES = (
    "analytics_toolkit_sql_stage_candidate",
    "analytics_toolkit_sql_acknowledged_resources",
)
_DETERMINISTIC_SQLSTATE_CLASSES = {"0A", "21", "22", "23", "3D", "3F", "42", "44", "P0"}
_SQLSTATE_LENGTH = 5
_MAX_CATALOG_STEPS = 64
_ATTRIBUTE_FIELDS = 3
_TYPE_FIELDS = 4


@dataclass(frozen=True)
class _CatalogType:
    domain_base: int
    element: int
    formatted: str
    qualified: str


class NativeAssignmentError(ValueError):
    """A safe diagnostic retaining replay classification, never a server value."""

    def __init__(
        self,
        message: str,
        *,
        sqlstate: str | None = None,
        retry_safe: bool = False,
    ) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate
        self.analytics_toolkit_sql_retry_safe = retry_safe


@dataclass(frozen=True)
class NativeAssignmentCertificate:
    """Facts about one completely populated, caller-owned native assignment stage."""

    stage_table: str
    column_types: tuple[tuple[str, str], ...]
    row_count: int
    key_columns: tuple[str, ...]
    null_columns: tuple[str, ...]
    input_identity: FrozenInputIdentity | None = None


def requires_native_assignment(native_type: str) -> bool:
    """Leave nonlocal types to server assignment, retaining their exact SQL identity."""
    return canonical_builtin_type(native_type) is None


def requires_keyed_append_assignment(
    backend: str,
    write_mode: str,
    key_columns: Sequence[str] | None,
    requested_schema: Mapping[str, str] | None,
    final_schema: Mapping[str, str] | None = None,
) -> bool:
    """Bind explicit-to-final GP key equality to actual native assignment."""
    if backend != "gp" or write_mode != "append" or not key_columns:
        return False
    if final_schema is None:
        # Offline plans cannot inspect the existing destination's native types.
        return True
    if requested_schema is None:
        return False
    return any(
        (canonical_builtin_type(native) or native)
        != (canonical_builtin_type(final_schema[name]) or final_schema[name])
        for name, native in requested_schema.items()
    )


def assignment_parameter_types(
    adapter: Any,
    connection: Any,
    stage_table: str,
    parameter_schema: Mapping[str, str],
    *,
    query_label: str | None = None,
) -> dict[str, str]:
    """Type unknown ARRAY inputs without applying domain sizing in an explicit cast.

    Read identities from the acknowledged physical stage. The final assignment
    retains its raw declared type; only the writer's array parameter cast uses
    this unbounded base type, so domain constraints and typmods remain implicit.
    """
    result = dict(parameter_schema)
    columns = tuple(
        name
        for name, native in result.items()
        if native.strip().endswith("[]") and requires_native_assignment(native)
    )
    if not columns:
        return result
    with native_assignment_errors("validation"):
        attributes = _stage_array_attributes(
            adapter, connection, stage_table, columns, query_label=query_label
        )
        cache: dict[int, _CatalogType] = {}
        for name in columns:
            oid, dimensions = attributes[name]
            result[name] = _unbounded_array_type(
                connection, oid, dimensions, cache, query_label=query_label
            )
        raise_if_cancelled()
    return result


def _stage_array_attributes(
    adapter: Any,
    connection: Any,
    stage_table: str,
    columns: Sequence[str],
    *,
    query_label: str | None,
) -> dict[str, tuple[int, int]]:
    from analytics_toolkit.sql.core.identifiers import (  # noqa: PLC0415 -- backend registry cycle.
        split_gp_table_name,
    )

    del adapter
    raise_if_cancelled()
    schema, relation = split_gp_table_name(stage_table)
    query = apply_query_label(
        "SELECT a.attname, a.atttypid, a.attndims "
        "FROM pg_catalog.pg_attribute AS a "
        "JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid "
        "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s AND a.attname = ANY(%s) "
        "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum",
        query_label,
    )
    cursor = connection.cursor()
    try:
        cursor.execute(query, (schema, relation, list(columns)))
        rows = cursor.fetchall()
    finally:
        close_cursor(cursor, backend="gp")
    attributes: dict[str, tuple[int, int]] = {}
    for row in rows:
        if (
            len(row) != _ATTRIBUTE_FIELDS
            or row[0] not in columns
            or row[0] in attributes
            or not _catalog_integer(row[1], minimum=1)
            or not _catalog_integer(row[2])
        ):
            raise _catalog_error()
        attributes[row[0]] = (int(row[1]), int(row[2]))
    if set(attributes) != set(columns):
        raise _catalog_error()
    return attributes


def _unbounded_array_type(
    connection: Any,
    oid: int,
    dimensions: int,
    cache: dict[int, _CatalogType],
    *,
    query_label: str | None,
) -> str:
    visited: set[int] = set()
    arrays = 0
    for _step in range(_MAX_CATALOG_STEPS):
        raise_if_cancelled()
        if oid in visited:
            raise _catalog_error()
        visited.add(oid)
        if oid not in cache:
            cache[oid] = _read_catalog_type(connection, oid, query_label=query_label)
        native = cache[oid]
        if native.domain_base:
            oid = native.domain_base
        elif native.element:
            arrays += 1
            oid = native.element
        elif arrays:
            return native.qualified + "[]" * max(dimensions, arrays)
        else:
            raise _catalog_error()
    raise _catalog_error()


def _read_catalog_type(connection: Any, oid: int, *, query_label: str | None) -> _CatalogType:
    query = apply_query_label(
        "SELECT t.typbasetype, "
        "CASE WHEN t.typinput = 'pg_catalog.array_in'::pg_catalog.regproc "
        "THEN t.typelem ELSE 0 END, pg_catalog.format_type(t.oid, -1), "
        "pg_catalog.quote_ident(n.nspname) || '.' || pg_catalog.quote_ident(t.typname) "
        "FROM pg_catalog.pg_type AS t "
        "JOIN pg_catalog.pg_namespace AS n ON n.oid = t.typnamespace WHERE t.oid = %s",
        query_label,
    )
    cursor = connection.cursor()
    try:
        cursor.execute(query, (oid,))
        row = cursor.fetchone()
    finally:
        close_cursor(cursor, backend="gp")
    if (
        row is None
        or len(row) != _TYPE_FIELDS
        or not _catalog_integer(row[0])
        or not _catalog_integer(row[1])
        or (row[0] and row[1])
        or not all(isinstance(native, str) and native.strip() for native in row[2:])
    ):
        raise _catalog_error()
    return _CatalogType(int(row[0]), int(row[1]), row[2], row[3])


def _catalog_integer(value: Any, *, minimum: int = 0) -> bool:
    return isinstance(value, Integral) and not isinstance(value, bool) and int(value) >= minimum


def _catalog_error() -> NativeAssignmentError:
    return NativeAssignmentError(
        "Cannot certify an unbounded native array parameter type from the owned stage catalog."
    )


@contextmanager
def native_assignment_errors(
    phase: Literal["creation", "population", "validation"],
    *,
    non_retryable_predicate: Callable[[Exception], bool] | None = None,
) -> Iterator[None]:
    """Hide server row values while retaining transient replay and resource evidence."""
    try:
        yield
    except NativeAssignmentError:
        raise
    except Exception as exc:  # noqa: BLE001 -- redact driver diagnostics containing private values.
        sqlstate = _sqlstate(exc)
        deterministic = (
            getattr(exc, "analytics_toolkit_sql_retry_safe", True) is False
            or isinstance(exc, (TypeError, ValueError))
            or (sqlstate is not None and sqlstate[:2] in _DETERMINISTIC_SQLSTATE_CLASSES)
            or (non_retryable_predicate is not None and non_retryable_predicate(exc))
        )
        message = f"Greenplum native assignment {phase} failed"
        if sqlstate is not None:
            message += f" (SQLSTATE {sqlstate})"
        message += ". The live destination has not been certified for publication."
        if sqlstate == "42501":
            message += (
                " Check CREATE permission for the configured owned staging schema "
                "and USAGE permission for the selected native types."
            )
        error = NativeAssignmentError(message, sqlstate=sqlstate, retry_safe=not deterministic)
        for attribute in _OWNERSHIP_ATTRIBUTES:
            if hasattr(exc, attribute):
                setattr(error, attribute, getattr(exc, attribute))
        raise error from None


def _sqlstate(error: Exception) -> str | None:
    raw = getattr(error, "pgcode", None) or getattr(error, "sqlstate", None)
    if not isinstance(raw, str):
        return None
    code = raw.strip().upper()
    return code if len(code) == _SQLSTATE_LENGTH and code.isascii() and code.isalnum() else None


def certify_assignment_checkpoint(  # noqa: PLR0913 -- private certificate binds schema, scope and payload.
    adapter: Any,
    connection: Any,
    stage_table: str,
    column_types: Mapping[str, str],
    *,
    expected_rows: int,
    key_columns: Sequence[str] | None = None,
    input_identity: FrozenInputIdentity | None = None,
    query_label: str | None = None,
) -> NativeAssignmentCertificate:
    """Check actual stored values after every native assignment INSERT was acknowledged.

    The caller owns stage creation, complete population and identity binding. No
    casts or data adaptation happen here: counting a cast expression cannot prove
    it was evaluated. Publication must reuse this typed payload without rebinding.
    """
    with native_assignment_errors("validation"):
        raise_if_cancelled()
        schema = tuple(column_types.items())
        types = dict(schema)
        if (
            not isinstance(expected_rows, Integral)
            or isinstance(expected_rows, bool)
            or expected_rows < 0
        ):
            message = "Cannot certify a native assignment checkpoint without an exact input count."
            raise NativeAssignmentError(message)
        keys = tuple(dict.fromkeys(key_columns or ()))
        if any(name not in types for name in keys):
            message = "Native assignment key columns are missing from the authoritative schema."
            raise NativeAssignmentError(message)
        row_count, null_columns = read_staged_null_counts(
            adapter,
            connection,
            stage_table,
            {name: (name, None) for name in types},
            query_label=query_label,
        )
        if row_count != expected_rows:
            message = (
                "Native assignment checkpoint row count differs from the complete selected input."
            )
            raise NativeAssignmentError(message)
        if set(keys).intersection(null_columns):
            message = "Native assignment key columns contain SQL NULL after native adaptation."
            raise NativeAssignmentError(message)
        if keys:
            _require_unique_keys(adapter, connection, stage_table, keys, query_label=query_label)
        raise_if_cancelled()
        return NativeAssignmentCertificate(
            stage_table=stage_table,
            column_types=schema,
            row_count=row_count,
            key_columns=keys,
            null_columns=null_columns,
            input_identity=input_identity,
        )


def _require_unique_keys(
    adapter: Any,
    connection: Any,
    stage_table: str,
    keys: Sequence[str],
    *,
    query_label: str | None,
) -> None:
    raise_if_cancelled()
    query = apply_query_label(
        adapter.build_stage_duplicate_keys_sql(stage_table, keys), query_label
    )
    cursor = connection.cursor()
    try:
        cursor.execute(query)
        duplicate = cursor.fetchone() is not None
    finally:
        close_cursor(cursor, backend="gp")
    if duplicate:
        message = "Duplicate native key values found in the complete native assignment checkpoint."
        raise NativeAssignmentError(message)
