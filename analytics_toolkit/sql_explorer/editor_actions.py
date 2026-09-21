"""Pure coordinate and paired-edit helpers for the SQL editor."""

from __future__ import annotations

from sqlparse import tokens

from analytics_toolkit._sql_statements import sql_tokens

PAIRS = {"(": ")", "[": "]", "{": "}", "'": "'", '"': '"', "`": "`"}


def code_context(text: str, offset: int) -> bool:
    """Whether insertion is outside a SQL comment or quoted token."""
    cursor = 0
    # Complete unfinished lexical constructs solely for token classification.
    # This also recognizes an unclosed comment/string around an end-of-buffer caret.
    for token in sql_tokens(text + "\n*/\n'\n\"\n`"):
        kind, value = token.ttype, token.value
        end = cursor + len(value)
        if cursor < offset < end:
            return (
                kind not in tokens.Comment
                and kind not in tokens.Literal.String
                and not value.startswith(('"', "`"))
            )
        cursor = end
    return True


def completion_text(suggestion: str, suffix: str) -> str:
    if suggestion.endswith((".", " ")) or (
        suffix and (suffix[0].isspace() or suffix[0] in ",);.([]}")
    ):
        return suggestion
    return suggestion + " "


def row_coordinate(value: str, line_count: int) -> int:
    row = 1 if value == "S" else line_count if value == "E" else int(value)
    if not 1 <= row <= line_count:
        message = f"Row must be between 1 and {line_count}."
        raise ValueError(message)
    return row - 1


def column_coordinate(value: str, line: str) -> int:
    if value == "S":
        return 0
    if value == "E":
        return len(line)
    column = int(value)
    if column < 1:
        message = "Column must be a positive integer."
        raise ValueError(message)
    return min(column - 1, len(line))


def positive_count(value: str) -> int:
    count = int(value)
    if count < 1:
        message = "Count must be a positive integer."
        raise ValueError(message)
    return count


def cursor_edit(
    text: str, bounds: tuple[int, int], insert: str, action: str
) -> tuple[int, int, str, int]:
    """Plan one replacement and the caret offset within its inserted text."""
    start, end = bounds
    value, caret = insert, len(insert)
    if start != end:
        if action == "paired" and insert in PAIRS:
            value = insert + text[start:end] + PAIRS[insert]
            caret = len(value)
        return start, end, value, caret
    if action == "backspace":
        start = max(0, start - 1)
        if text[start:end] in PAIRS and text[end : end + 1] == PAIRS[text[start:end]]:
            end += 1
    elif action == "delete":
        end = min(len(text), end + 1)
    elif action == "paired":
        if insert in PAIRS.values() and text[end : end + 1] == insert:
            end += 1
        elif insert in PAIRS and code_context(text, start):
            value, caret = insert + PAIRS[insert], 1
    return start, end, value, caret
