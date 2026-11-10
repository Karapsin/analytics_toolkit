"""Substitute executable placeholders once, preserving SQL literal spans."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping

from sqlparse import tokens
from sqlparse.lexer import tokenize

_COMMENT_MARKERS = re.compile(r"/\*|\*/")


def _block_comment_end(sql: str, start: int) -> int:
    depth = 0
    for marker in _COMMENT_MARKERS.finditer(sql, start):
        depth += 1 if marker.group() == "/*" else -1
        if depth == 0:
            return marker.end()
    return len(sql)


def _sql_spans(sql: str) -> Iterator[tuple[bool, str]]:
    """Keep nested block comments opaque, restarting the lexer after each one.

    sqlparse's lexer ends a block comment at the first closing marker. Quotes
    inside the rest of a nested comment must not influence subsequent SQL.
    """
    sql_tokens = cast("Callable[[str], Iterable[tuple[Any, str]]]", tokenize)
    cursor = 0
    while cursor < len(sql):
        start = cursor
        for kind, text in sql_tokens(sql[cursor:]):
            if sql.startswith("/*", start):
                cursor = _block_comment_end(sql, start)
                yield True, sql[start:cursor]
                break
            protected = (
                kind in tokens.Comment
                or kind in tokens.Literal
                or (kind in tokens.Name and text.startswith(("`", '"', "[")))
            )
            yield protected, text
            start += len(text)
        else:
            return


def replace_predicate_placeholders(sql: str, replacements: Mapping[str, str]) -> str:
    if not replacements:
        return sql
    pattern = re.compile(r"\{(" + "|".join(re.escape(key) for key in replacements) + r")\}")
    found: set[str] = set()
    result: list[str] = []
    executable: list[str] = []

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        found.add(name)
        return replacements[name]

    def flush() -> None:
        result.append(pattern.sub(substitute, "".join(executable)))
        executable.clear()

    for protected, text in _sql_spans(sql):
        if protected:
            flush()
            result.append(text)
        else:
            executable.append(text)
    flush()
    for name in replacements:
        if name not in found:
            message = (
                "transfer_keys requires an executable predicate placeholder per key in from_sql.\n"
                f"Missing placeholder: {{{name}}}\n\n"
                "Place it in a WHERE predicate outside strings, identifiers, and comments. "
                "For an f-string, escape the placeholder braces."
            )
            raise ValueError(message)
    return "".join(result)
