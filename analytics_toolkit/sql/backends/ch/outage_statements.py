"""Statement details that the ClickHouse SQLGlot dialect does not model."""

from __future__ import annotations

from typing import Any, cast

import sqlparse
from sqlglot import exp, parse_one

from .routing import _RoutingClickHouse


def exchange_or_rename(sql: str) -> tuple[exp.Command, list[str]] | None:
    first = next(
        (
            value.upper()
            for kind, value in cast("Any", sqlparse.lexer.tokenize)(sql)
            if kind not in sqlparse.tokens.Whitespace and kind not in sqlparse.tokens.Comment
        ),
        "",
    )
    if first not in {"EXCHANGE", "RENAME"}:
        return None
    statements = sqlparse.parse(sql)
    if len(statements) != 1:
        return None
    tokens = [
        token
        for token in statements[0].flatten()
        if not token.is_whitespace and token.ttype not in sqlparse.tokens.Comment
    ]
    clusters = []
    for i, token in enumerate(tokens[:-2]):
        if token.normalized == "ON" and tokens[i + 1].normalized == "CLUSTER":
            clusters.append(tokens[i + 2].value)
    tables = []
    for token in statements[0].tokens:
        if token.normalized == "ON":
            break
        identifiers = (
            list(token.get_identifiers())
            if isinstance(token, sqlparse.sql.IdentifierList)
            else [token]
            if isinstance(token, sqlparse.sql.Identifier)
            else []
        )
        tables.extend(
            parse_one(identifier.value, into=exp.Table, read=_RoutingClickHouse)
            for identifier in identifiers
        )
    return exp.Command(this=tokens[0].normalized, expressions=tables), clusters
