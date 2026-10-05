"""Cursor-local SQL scopes for completion, without executing editor SQL."""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import TokenType, exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import traverse_scope
from sqlparse import tokens

from analytics_toolkit._sql_statements import sql_tokens

from .editor_actions import code_context

DIALECTS = {"gp": "postgres", "ch": "clickhouse", "trino": "trino"}
_RELATION_MARKER = "__explorer_cursor_relation__"
_RELATION_CLAUSES = {TokenType.FROM, TokenType.JOIN, TokenType.UPDATE, TokenType.INTO}


def marked_statement(text: str, start: int, end: int, marker: str) -> str:
    """Isolate the current statement, preserving semicolons in comments/strings."""
    left, right, offset = 0, len(text), 0
    for token in sql_tokens(text):
        if token.ttype in tokens.Punctuation and token.value == ";":
            if offset >= start:
                right = offset
                break
            left = offset + 1
        offset += len(token.value)
    return text[left:start] + marker + text[end:right]


@dataclass(frozen=True)
class RelationPosition:
    start: int
    parts: tuple[str, ...]
    context: str
    ctes: tuple[str, ...] = ()


def relation_position(text: str, cursor: int, backend: str) -> RelationPosition | None:
    """Recognize relation positions across whitespace and comments using SQL tokens."""
    if not code_context(text, cursor):
        return None
    dialect = DIALECTS.get(backend, backend)
    try:
        parsed = sqlglot.tokenize(text[:cursor], read=dialect)
    except SqlglotError:
        return None
    parts: list[str] = []
    start = cursor
    if parsed and parsed[-1].end + 1 == cursor:
        final = parsed[-1]
        if final.token_type == TokenType.DOT:
            parts.append("")
            parsed.pop()
        else:
            identifier = _pop_identifier(parsed, text)
            if identifier is None:
                return None
            name, start = identifier
            parts.append(name)
            if parsed and parsed[-1].token_type == TokenType.DOT:
                parsed.pop()
            else:
                return _relation_position(text, (start, cursor), parts, parsed, dialect)
        while parsed:
            identifier = _pop_identifier(parsed, text)
            if identifier is None:
                return None
            parts.insert(0, identifier[0])
            if not parsed or parsed[-1].token_type != TokenType.DOT:
                break
            parsed.pop()
    return _relation_position(text, (start, cursor), parts or [""], parsed, dialect)


def _pop_identifier(parsed: list[sqlglot.Token], text: str) -> tuple[str, int] | None:
    token = parsed.pop()
    if token.token_type == TokenType.IDENTIFIER:
        return token.text, token.start
    if token.token_type == TokenType.STRING or not ("_" + token.text).isidentifier():
        return None
    start = token.start
    # PostgreSQL/Trino tokenize digit-leading prefixes as adjacent NUMBER/VAR
    # tokens. Keep these editable metadata names intact, as ClickHouse does.
    while parsed and parsed[-1].end + 1 == start:
        previous = parsed[-1]
        if previous.token_type != TokenType.NUMBER or not ("_" + previous.text).isidentifier():
            break
        start = parsed.pop().start
    return text[start : token.end + 1], start


def _relation_position(
    text: str,
    bounds: tuple[int, int],
    parts: list[str],
    parsed: list[sqlglot.Token],
    dialect: str,
) -> RelationPosition | None:
    if not parsed or parsed[-1].token_type not in _RELATION_CLAUSES:
        return None
    start, cursor = bounds
    clause = parsed[-1]
    ctes = (
        _visible_ctes(text, start, cursor, dialect)
        if len(parts) == 1 and clause.token_type in {TokenType.FROM, TokenType.JOIN}
        else ()
    )
    return RelationPosition(start, tuple(parts), f"{clause.text.lower()}:{clause.start}", ctes)


def _visible_ctes(text: str, start: int, end: int, dialect: str) -> tuple[str, ...]:
    candidate = marked_statement(text, start, end, _RELATION_MARKER)
    try:
        root = sqlglot.parse_one(candidate, read=dialect)
        for scope in traverse_scope(root):
            if any(table.name == _RELATION_MARKER for table in scope.tables):
                names = []
                for name, source in scope.cte_sources.items():
                    cte = source.expression.find_ancestor(exp.CTE)
                    identifier = (
                        cte.args["alias"].this if cte is not None else exp.to_identifier(name)
                    )
                    names.append(identifier.sql(dialect=dialect))
                return tuple(sorted(set(names), key=str.casefold))
    except (SqlglotError, ValueError):
        return ()
    return ()
