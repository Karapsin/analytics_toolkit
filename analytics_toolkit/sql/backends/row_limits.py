"""Conservative outer result limits shared by read APIs and Explorer previews."""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlglot
from sqlglot import exp
from sqlparse import tokens

from analytics_toolkit._sql_statements import sql_tokens, terminal_parts
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

if TYPE_CHECKING:
    from sqlparse.sql import Token


_LIMIT_CLAUSES = {"LIMIT", "FETCH"}
_FOLLOWING_CLAUSES = {"OFFSET", "FOR", "SETTINGS"}


def validate_row_limit(row_limit: int | None) -> None:
    if row_limit is not None and (
        isinstance(row_limit, bool) or not isinstance(row_limit, int) or row_limit <= 0
    ):
        message = "row_limit must be a positive integer or None."
        raise InvalidSqlInputError(message)


def apply_row_limit(
    statement: str, row_limit: int | None, *, dialect: str | None
) -> tuple[str, bool]:
    """Return SQL and whether its outer result is known to be capped.

    Unsupported commands and uncertain limits pass through without a warning.
    Preserve the original SQL text except for the outer limit insertion or count.
    """
    validate_row_limit(row_limit)
    if row_limit is None:
        return statement, False
    significant = [
        token
        for token in sql_tokens(statement)
        if not token.is_whitespace and token.ttype not in tokens.Comment
    ]
    if not significant or significant[0].normalized.upper() not in {"SELECT", "WITH", "VALUES"}:
        return statement, False
    body, suffix, terminated = terminal_parts(statement)
    try:
        tree = sqlglot.parse_one(body, read=dialect)
        if (
            not isinstance(tree, (exp.Select, exp.SetOperation, exp.Values))
            or tree.args.get("into")
            or tree.args.get("format")
        ):
            return statement, False
        limited = _limited_body(body, tree, row_limit)
        if limited is None or sqlglot.parse_one(limited, read=dialect) != tree:
            return statement, False
    except (ValueError, sqlglot.errors.SqlglotError):
        return statement, False
    return limited + (";" if terminated else "") + suffix, True


def _limited_body(body: str, tree: exp.Expression, row_limit: int) -> str | None:
    outer_tokens = _outer_tokens(body)
    limit = tree.args.get("limit")
    if limit is not None:
        count = _literal_limit(limit)
        if count is None:
            return None
        if int(count.this) <= row_limit:
            return body
        # Replace only the count token, preserving comments, offsets and clauses.
        limit_start = next(
            (
                start
                for start, _, token in outer_tokens
                if token.ttype in tokens.Keyword and token.normalized in _LIMIT_CLAUSES
            ),
            len(body),
        )
        following = _following_clause_start(outer_tokens, len(body), after=limit_start, tree=tree)
        counts = [
            (start, end)
            for start, end, token in outer_tokens
            if limit_start < start < following
            and token.ttype in tokens.Literal.Number.Integer
            and token.value == str(count.this)
        ]
        if not counts:
            return None
        start, end = counts[-1]  # LIMIT offset, count uses the last numeric token.
        count.set("this", str(row_limit))
        return body[:start] + str(row_limit) + body[end:]
    position = _following_clause_start(outer_tokens, len(body), tree=tree)
    tree.set("limit", exp.Limit(expression=exp.Literal.number(row_limit)))
    return body[:position] + f"\nLIMIT {row_limit}\n" + body[position:]


def _literal_limit(limit: exp.Expression) -> exp.Literal | None:
    options = limit.args.get("limit_options")
    if limit.expressions or (
        options is not None and (options.args.get("with_ties") or options.args.get("percent"))
    ):
        return None
    count = limit.args.get("count" if isinstance(limit, exp.Fetch) else "expression")
    return count if isinstance(count, exp.Literal) and count.is_int else None


def _outer_tokens(source: str) -> list[tuple[int, int, Token]]:
    result = []
    depth = 0
    position = 0
    for token in sql_tokens(source):
        end = position + len(token.value)
        if token.ttype is tokens.Punctuation and token.value == "(":
            depth += 1
        elif token.ttype is tokens.Punctuation and token.value == ")":
            depth -= 1
        elif depth == 0:
            result.append((position, end, token))
        position = end
    return result


def _following_clause_start(
    outer_tokens: list[tuple[int, int, Token]],
    default: int,
    *,
    after: int = -1,
    tree: exp.Expression,
) -> int:
    return next(
        (
            start
            for start, _, token in outer_tokens
            if start > after
            and (
                token.ttype in tokens.Keyword
                or (tree.args.get("settings") and token.value.upper() == "SETTINGS")
            )
            and token.normalized.upper() in _FOLLOWING_CLAUSES
        ),
        default,
    )
