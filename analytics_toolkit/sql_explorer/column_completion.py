"""Resolve SELECT and WHERE columns without executing the editor's SQL."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, cast

import sqlglot
from sqlglot import TokenType, exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import Scope, traverse_scope

from .completion_scope import DIALECTS, marked_statement

if TYPE_CHECKING:
    from collections.abc import Callable

_MARKER = "__explorer_cursor_column__"
_DIALECTS = DIALECTS


def column_fragment(text: str, cursor: int, backend: str) -> tuple[int, int] | None:
    """Find complete quoted identifiers and suffixes to replace at the cursor."""
    try:
        tokens = sqlglot.tokenize(text, read=_DIALECTS.get(backend, backend))
    except SqlglotError:
        return None
    return next(
        (
            (token.start, token.end + 1)
            for token in tokens
            if token.start < cursor <= token.end + 1
            and (
                token.token_type == TokenType.IDENTIFIER
                or text[token.start : token.end + 1].isidentifier()
            )
        ),
        None,
    )


def projection_context(text: str, start: int, end: int, backend: str) -> str | None:
    """Return a stable parseable scope with the editable fragment replaced."""
    dialect = _DIALECTS.get(backend, backend)
    candidate = marked_statement(text, start, end, _MARKER)
    try:
        statements = sqlglot.parse(candidate, read=dialect)
        for statement in statements:
            if statement is None:
                continue
            for column in statement.find_all(exp.Column):
                if column.name != _MARKER:
                    continue
                select = column.find_ancestor(exp.Select)
                if select is None:
                    continue
                # Resolve only expressions belonging to this SELECT's projection
                # or WHERE, never aliases or an enclosing query's sources.
                node: exp.Expression = column
                while node.parent is not select and node.parent is not None:
                    node = cast("exp.Expression", node.parent)
                if node not in select.expressions and node is not select.args.get("where"):
                    continue
                if not (select.args.get("from_") or select.args.get("from")):
                    continue
                return statement.sql(dialect=dialect)
    except (SqlglotError, ValueError):
        return None
    return None


def projection_suggestions(
    statement: str,
    backend: str,
    columns_for_table: Callable[[str], tuple[str, ...]],
) -> tuple[str, ...]:
    """Resolve aliases, CTE output names and stars in the cursor's scope."""
    dialect = _DIALECTS.get(backend, backend)
    root = normalize_identifiers(sqlglot.parse_one(statement, read=dialect), dialect=dialect)
    scopes = traverse_scope(root)
    target = next(
        (scope for scope in scopes if any(c.name == _MARKER for c in scope.columns)),
        None,
    )
    if target is None:
        return ()
    marker = next(c for c in target.columns if c.name == _MARKER)

    def quoted(name: str) -> str:
        needs_quotes = not name.isidentifier() or name != name.lower()
        return exp.to_identifier(name, quoted=needs_quotes).sql(dialect=dialect)

    available = {
        alias: _source_columns(source, frozenset(), dialect, columns_for_table)
        for alias, (_, source) in target.selected_sources.items()
        if not marker.table or alias == marker.table
    }
    counts = Counter(name for names in available.values() for name in names)
    suggestions = {
        f"{quoted(alias)}.{quoted(name)}" if not marker.table and counts[name] > 1 else quoted(name)
        for alias, names in available.items()
        for name in names
    }
    return tuple(sorted(suggestions, key=str.casefold))


def _source_columns(
    source: exp.Expression | Scope,
    visiting: frozenset[int],
    dialect: str,
    columns_for_table: Callable[[str], tuple[str, ...]],
) -> tuple[str, ...]:
    if isinstance(source, exp.Table):
        table = source.copy()
        table.set("alias", None)
        return columns_for_table(table.sql(dialect=dialect))
    if not isinstance(source, Scope) or id(source) in visiting:
        return ()
    if source.outer_columns:
        return tuple(source.outer_columns)
    visiting = visiting | {id(source)}
    expression = source.expression
    if isinstance(expression, exp.SetOperation):
        # SQLGlot 30.20 renamed union_scopes to cover all set operations.
        children = getattr(source, "set_operation_scopes", getattr(source, "union_scopes", ()))
        return (
            _source_columns(children[0], visiting, dialect, columns_for_table) if children else ()
        )
    names: list[str] = []
    for projection in expression.expressions:
        if projection.is_star:
            qualifier = projection.table if isinstance(projection, exp.Column) else ""
            for alias, (_, nested) in source.selected_sources.items():
                if not qualifier or alias == qualifier:
                    names.extend(_source_columns(nested, visiting, dialect, columns_for_table))
        elif (
            isinstance(projection, (exp.Column, exp.Alias)) and projection.alias_or_name != _MARKER
        ):
            names.append(projection.alias_or_name)
    return tuple(dict.fromkeys(names))
