"""Opt-in exact decoders for internal source cursors."""

from __future__ import annotations

from typing import Any


def prepare_exact_source_cursor(backend: str, connection: Any, cursor: Any) -> None:
    """Register backend codecs before an internal source query can decode rows."""
    if backend == "gp":
        from .gp.source_codecs import (  # noqa: PLC0415 - keep optional GP codecs backend-local.
            prepare_exact_source_cursor as prepare_greenplum_cursor,
        )

        prepare_greenplum_cursor(connection, cursor)


def execute_exact_source_query(  # noqa: PLR0913 -- preserve the source cursor/metadata protocol.
    backend: str,
    connection: Any,
    cursor: Any,
    query: str,
    *,
    source_columns: Any = None,
    source_description: Any = None,
) -> Any:
    """Execute internal source data once using its backend's exact transport."""
    prepare_exact_source_cursor(backend, connection, cursor)
    if backend == "gp":
        from .gp.source_float_wire import (  # noqa: PLC0415 -- optional backend transport.
            execute_exact_source_query as execute_greenplum_query,
        )

        return execute_greenplum_query(
            connection,
            cursor,
            query,
            source_columns=source_columns,
            source_description=source_description,
        )
    cursor.execute(query)
    return cursor


class _ExactSourceCursor:
    def __init__(self, backend: str, connection: Any, cursor: Any, columns: Any):
        self._backend = backend
        self._connection = connection
        self._cursor = cursor
        self._columns = columns

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    def execute(self, query: str, parameters: Any = None) -> None:
        if parameters is not None:
            message = "Exact internal source execution requires an already rendered SELECT."
            raise ValueError(message)
        self._cursor = execute_exact_source_query(
            self._backend, self._connection, self._cursor, query, source_columns=self._columns
        )


class _ExactSourceConnection:
    def __init__(self, backend: str, connection: Any, columns: Any):
        self._backend = backend
        self._connection = connection
        self._columns = columns

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        return _ExactSourceCursor(
            self._backend,
            self._connection,
            self._connection.cursor(*args, **kwargs),
            self._columns,
        )


def exact_source_connection(backend: str, connection: Any, *, source_columns: Any = None) -> Any:
    """Opt a single internal eager reader into exact codecs without altering public reads."""
    return (
        _ExactSourceConnection(backend, connection, source_columns)
        if backend == "gp"
        else connection
    )
