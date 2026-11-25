"""Resolve runtime namespace identity from the actual coordinator session."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import sqlparse

from analytics_toolkit.sql.connection.errors import ClickHouseClusterTopologyError
from analytics_toolkit.sql.execution.backend_options import (
    current_backend_operation,
    current_backend_query_label,
)
from analytics_toolkit.sql.execution.labels import apply_query_label

from .metadata_values import metadata_text
from .outage_context import internal_io
from .routing import local_sql

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


def _coordinator_identity(connection: Any) -> int:
    if getattr(connection, "_ch_outage_client", False) is True:
        connection = object.__getattribute__(connection, "_client")
    return id(connection)


def changes_current_database(sql: str) -> bool:
    """Recognize executable USE commands without inspecting literal/comment text."""
    tokenize = cast("Callable[[str], Iterable[tuple[Any, str]]]", sqlparse.lexer.tokenize)
    for kind, value in tokenize(sql):
        if kind in sqlparse.tokens.Whitespace or kind in sqlparse.tokens.Comment:
            continue
        return kind in sqlparse.tokens.Keyword and value.upper() == "USE"
    return False


def invalidate_current_database(connection: Any) -> None:
    state = current_backend_operation()
    if state is None:
        return
    identity = _coordinator_identity(connection)
    with state.lock:
        for key in list(state.resources):
            if isinstance(key, tuple) and key[:2] == ("ch_current_database", identity):
                state.resources.pop(key, None)


def resolve_current_database(connection: Any) -> str:
    """Return an exact namespace, cached only for this connection and operation.

    Runtime callers use this when no database was frozen from configuration.
    Pure SQL planning never invokes the resolver or opens a connection.
    """
    state = current_backend_operation()
    config = getattr(connection, "_config", None)
    key = ("ch_current_database", _coordinator_identity(connection), id(config))
    if state is not None:
        with state.lock:
            cached = state.resources.get(key)
        if isinstance(cached, str):
            return cached
    query = apply_query_label("SELECT currentDatabase()", current_backend_query_label())
    with internal_io(), local_sql():
        result = connection.query(query)
    rows = list(result.result_rows)
    if len(rows) != 1 or len(rows[0]) != 1:
        message = "The ClickHouse coordinator did not return an exact current database identity."
        raise ClickHouseClusterTopologyError(message)
    database = metadata_text(rows[0][0])
    if not database:
        message = "The ClickHouse coordinator returned an empty database identity."
        raise ClickHouseClusterTopologyError(message)
    if state is not None:
        with state.lock:
            state.resources[key] = database
    return database
