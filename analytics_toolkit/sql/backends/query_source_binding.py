"""Conservative source-binding proof for destructive query creation."""

from __future__ import annotations

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import Scope, traverse_scope

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.core.identifiers import TableIdentifier, sqlglot_dialect


def query_may_read_target(query: str, target: str, backend: str) -> bool:
    """Unknown namespace bindings freeze conservatively; CTEs keep their scope."""
    dialect = sqlglot_dialect(backend)
    target_ast = normalize_identifiers(
        parse_one(target, read=dialect, into=exp.Table), dialect=dialect
    )
    destination = TableIdentifier.parse(target_ast.sql(dialect=dialect), backend)
    try:
        statement = normalize_identifiers(parse_one(query, read=dialect), dialect=dialect)
        for scope in traverse_scope(statement) or []:
            for _node, source in scope.selected_sources.values():
                if isinstance(source, Scope):
                    continue
                if not isinstance(source, exp.Table) or not isinstance(source.this, exp.Identifier):
                    # Table functions may hide a target reference. A complete
                    # frozen read remains safe without inventing a binding.
                    return True
                identity = TableIdentifier.parse(
                    source.sql(dialect=sqlglot_dialect(backend)), backend
                )
                overlap = min(len(identity.parts), len(destination.parts))
                if identity.parts[-overlap:] == destination.parts[-overlap:]:
                    return True
    except SqlglotError as exc:
        message = "Cannot verify replacement query bindings before target mutation."
        raise InvalidSqlInputError(message) from exc
    return False
