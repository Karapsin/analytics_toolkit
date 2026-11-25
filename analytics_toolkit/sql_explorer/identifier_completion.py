"""Metadata identity and SQL insertion are deliberately separate representations."""

from __future__ import annotations

import re

import sqlglot
from sqlglot import TokenType, exp
from sqlglot.errors import SqlglotError

from analytics_toolkit.sql.backends.ch.identifiers import (
    quote_identifier as quote_clickhouse_identifier,
)

_DIALECTS = {"gp": "postgres", "ch": "clickhouse", "clickhouse": "clickhouse", "trino": "trino"}


def quote_completion_name(name: str, backend: str) -> str:
    """Retain ordinary lowercase spelling; quote reserved or exact-case names."""
    dialect = _DIALECTS.get(backend, backend)
    simple = re.fullmatch(r"[a-z_][a-z0-9_]*", name) is not None
    try:
        tokens = sqlglot.tokenize(name, read=dialect)
        simple = (
            simple and len(tokens) == 1 and tokens[0].token_type in {TokenType.VAR, TokenType.NAME}
        )
    except SqlglotError:
        simple = False
    if not simple and backend in {"ch", "clickhouse"}:
        return quote_clickhouse_identifier(name)
    return exp.to_identifier(name, quoted=not simple).sql(dialect=dialect)


def identifier_tokens(text: str, dialect: str) -> list[sqlglot.Token]:
    """Complete an unfinished final quoted identifier solely for tokenization."""
    try:
        return sqlglot.tokenize(text, read=dialect)
    except SqlglotError:
        for quote in ('"', "`"):
            try:
                tokens = sqlglot.tokenize(text + quote, read=dialect)
            except SqlglotError:
                continue
            if (
                tokens
                and tokens[-1].token_type == TokenType.IDENTIFIER
                and tokens[-1].end == len(text)
                and text[tokens[-1].start :].startswith(quote)
            ):
                tokens[-1].end = len(text) - 1
                return tokens
        raise


def completion_identifier_parts(value: str, backend: str) -> tuple[str, ...]:
    """Decode identifier tokens without confusing literal dots with qualification."""
    try:
        tokens = identifier_tokens(value, _DIALECTS.get(backend, backend))
    except SqlglotError:
        return (value,)
    parts: list[str] = []
    expect_name = True
    for token in tokens:
        if expect_name:
            if token.token_type not in {TokenType.IDENTIFIER, TokenType.VAR, TokenType.NAME}:
                return (value,)
            parts.append(token.text)
        elif token.token_type != TokenType.DOT:
            return (value,)
        expect_name = not expect_name
    return (value,) if expect_name else tuple(parts)


def column_completion_matches(value: str, prefix: str, backend: str) -> bool:
    """Match the semantic column name and retain full qualified-prefix completion."""
    parts = completion_identifier_parts(value, backend)
    normalized = prefix.casefold()
    return any(
        candidate.casefold().startswith(normalized) for candidate in (".".join(parts), parts[-1])
    )
