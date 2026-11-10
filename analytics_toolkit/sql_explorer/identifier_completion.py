"""Metadata identity and SQL insertion are deliberately separate representations."""

from __future__ import annotations

import re

import sqlglot
from sqlglot import TokenType, exp
from sqlglot.errors import SqlglotError

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
