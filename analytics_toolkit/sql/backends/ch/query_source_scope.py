"""Prove whether replacement DDL can mutate a query's bound sources."""

from __future__ import annotations

from typing import Any

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import Scope, traverse_scope

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

from .cluster_functions import cluster_arguments
from .dialect import ClickHouseDialect
from .reconfigure_support import query_scalar, unquoted_table_name

_GENERATORS = frozenset({"numbers", "numbers_mt", "zeros", "zeros_mt", "generaterandom"})


def query_overlaps_layout(query: str, layout: Any, connection: Any, database: str | None) -> bool:
    default_database = database or str(query_scalar(connection, "SELECT currentDatabase()"))
    identities = {
        (db or default_database, name)
        for db, name in (
            unquoted_table_name(layout.logical_table),
            unquoted_table_name(layout.physical_table),
        )
    }
    try:
        statement = parse_one(query, read=ClickHouseDialect)
        for scope in traverse_scope(statement) or []:
            for _node, source in scope.selected_sources.values():
                if isinstance(source, Scope):
                    continue
                identity = _source_identity(source, default_database)
                if identity in identities:
                    return True
    except SqlglotError as exc:
        message = "Cannot verify replacement query source bindings before target mutation."
        raise InvalidSqlInputError(message) from exc
    return False


def _source_identity(source: exp.Expression, database: str) -> tuple[str, str] | None:
    if (
        isinstance(source, exp.Table)
        and isinstance(source.this, exp.Identifier)
        and not source.catalog
    ):
        return source.db or database, source.name
    if isinstance(source, exp.Table) and isinstance(source.this, exp.Anonymous):
        name = source.this.name.lower()
        if name in {"cluster", "clusterallreplicas"}:
            _cluster, db, table, _trailing = cluster_arguments(source.this, database)
            return db, table
        if name in _GENERATORS:
            return None
    message = "Replacement query sources must have verifiable static table identities."
    raise InvalidSqlInputError(message)
