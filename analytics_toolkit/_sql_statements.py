"""Token-aware SQL boundaries shared by execution and local formatting."""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlparse
from sqlparse import tokens

if TYPE_CHECKING:
    from sqlparse.sql import Token


def sql_tokens(source: str) -> list[Token]:
    # sqlparse's flatten method has no annotations in the supported releases.
    return [token for statement in sqlparse.parse(source) for token in statement.flatten()]  # type: ignore[no-untyped-call]


def terminal_parts(source: str) -> tuple[str, str, bool]:
    """Separate a terminal delimiter and trailing trivia without touching literals."""
    parts = sql_tokens(source)
    suffix: list[str] = []
    while parts and (parts[-1].is_whitespace or parts[-1].ttype in tokens.Comment):
        suffix.append(parts.pop().value)
    terminated = bool(parts and parts[-1].ttype is tokens.Punctuation and parts[-1].value == ";")
    if terminated:
        parts.pop()
    return "".join(part.value for part in parts).rstrip(), "".join(reversed(suffix)), terminated


def has_sql_content(source: str) -> bool:
    return any(
        not token.is_whitespace
        and token.ttype not in tokens.Comment
        and not (token.ttype is tokens.Punctuation and token.value == ";")
        for token in sql_tokens(source)
    )


def split_statements(source: str) -> list[str]:
    """Keep comments with SQL, excluding comment-only fragments from its count."""
    result: list[str] = []
    leading: list[str] = []
    for fragment in sqlparse.split(source):
        if has_sql_content(fragment):
            body, suffix, _ = terminal_parts(fragment)
            result.append("\n".join([*leading, body + suffix]).strip())
            leading.clear()
        else:
            trivia = "".join(
                token.value
                for token in sql_tokens(fragment)
                if token.ttype is not tokens.Punctuation or token.value != ";"
            ).strip()
            if trivia and result:
                result[-1] += "\n" + trivia
            elif trivia:
                leading.append(trivia)
    return result


def join_statements(statements: tuple[str, ...] | list[str]) -> str:
    # A separator on its own line cannot become part of a final -- comment.
    pieces = []
    for statement in statements[:-1]:
        last = sql_tokens(statement)[-1]
        pieces.append(statement + ("\n;\n" if last.ttype in tokens.Comment.Single else ";\n"))
    return "".join(pieces) + (statements[-1] if statements else "")
