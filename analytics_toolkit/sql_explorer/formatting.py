"""Local, atomic SQL formatting for an Explorer editor."""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlparse
from sqlparse import tokens
from textual.document._document import Document, Selection

from analytics_toolkit import sql_format

from .statements import _DIALECTS, _has_sql_content

if TYPE_CHECKING:
    from .editor import SqlEditor


def _format_statement(statement: str, backend: str) -> str:
    if not _has_sql_content(statement):
        return statement
    # A comment after a terminator belongs to the script, not the expression
    # accepted by format_sql. Retain it outside that function's input.
    # sqlparse exposes flattened Token objects but does not annotate flatten().
    parts = list(sqlparse.parse(statement)[0].flatten())  # type: ignore[no-untyped-call]
    trailing = []
    while parts and (parts[-1].is_whitespace or parts[-1].ttype in tokens.Comment):
        trailing.append(parts.pop())
    if parts and parts[-1].ttype is tokens.Punctuation and parts[-1].value == ";":
        sql = "".join(part.value for part in parts)
        suffix = "".join(part.value for part in reversed(trailing))
    else:
        sql, suffix = statement, ""
    return sql_format.format_sql(sql, dialect=_DIALECTS.get(backend)) + suffix


def format_script(source: str, backend: str) -> str:
    """Format statements independently, retaining standalone comment blocks."""
    if not source.strip():
        return source
    statements = sqlparse.split(source)
    return "\n\n".join(_format_statement(statement, backend) for statement in statements)


def _selected_ranges(editor: SqlEditor, document: Document) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for selection in editor.cursor_selections:
        if selection.is_empty:
            continue
        start, end = sorted(document.get_index_from_location(point) for point in selection)
        if ranges and start < ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(end, ranges[-1][1]))
        else:
            ranges.append((start, end))
    return ranges or [(0, len(document.text))]


def _transformed_offset(offset: int, replacements: list[tuple[int, int, str]]) -> int:
    delta = 0
    for start, end, formatted in replacements:
        if offset < start:
            break
        if offset <= end:
            return start + delta + len(formatted)
        delta += len(formatted) - (end - start)
    return offset + delta


def format_editor(editor: SqlEditor, backend: str) -> bool:
    """Compute all replacements first and record one undoable buffer edit."""
    original = editor.text
    document = Document(original)
    ranges = _selected_ranges(editor, document)
    replacements = [
        (start, end, format_script(original[start:end], backend)) for start, end in ranges
    ]
    pieces: list[str] = []
    previous = 0
    for start, end, formatted in replacements:
        pieces.extend((original[previous:start], formatted))
        previous = end
    pieces.append(original[previous:])
    result = "".join(pieces)
    if result == original:
        return False

    active_offset = _transformed_offset(
        document.get_index_from_location(editor.selection.end), replacements
    )
    secondary_offsets = [
        _transformed_offset(document.get_index_from_location(selection.end), replacements)
        for selection in editor.cursor_selections
        if selection != editor.selection
    ]
    editor.history.checkpoint()
    editor.replace(result, (0, 0), document.end, maintain_selection_offset=False)
    editor.history.checkpoint()
    formatted_document = Document(result)
    editor._set_selections(  # noqa: SLF001 - retain the editor's multi-cursor contract.
        Selection.cursor(formatted_document.get_location_from_index(active_offset)),
        [
            Selection.cursor(formatted_document.get_location_from_index(offset))
            for offset in secondary_offsets
        ],
    )
    return True
