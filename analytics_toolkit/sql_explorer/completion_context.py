"""Pure SQL cursor-to-metadata scope parsing."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from .column_completion import column_fragment, projection_context
from .completion_scope import relation_position

if TYPE_CHECKING:
    from .completion import CompletionContext


_TOKEN_RE: Final = re.compile(r'([A-Za-z0-9_."]+)$')


def parse_completion_context(  # noqa: PLR0913 -- explicit cursor, backend and completion-mode inputs.
    text: str,
    cursor_offset: int,
    *,
    backend: str,
    connection_key: str = "",
    trino_catalogs: tuple[str, ...] | None = None,
    allow_empty_column_prefix: bool = False,
) -> CompletionContext:
    """Return completion scope and the final identifier fragment to replace."""
    from .completion import (  # noqa: PLC0415 -- facade compatibility without import cycles.
        _COLUMN_KEYWORDS,
        CompletionContext,
        CompletionRequest,
        normalize_identifier_prefix,
    )

    cursor_offset = max(0, min(cursor_offset, len(text)))
    before = text[:cursor_offset]
    line_start = before.rfind("\n") + 1
    line = before[line_start:]
    token_match = _TOKEN_RE.search(line)
    raw_token = token_match.group(1) if token_match else ""
    final_prefix = raw_token.rsplit(".", 1)[-1]
    replacement_end = cursor_offset
    replacement_start = cursor_offset - len(final_prefix)
    normalized_backend = backend.casefold()

    column_start, column_end = column_fragment(text, cursor_offset, normalized_backend) or (
        replacement_start,
        replacement_end,
    )
    column_prefix = text[column_start:cursor_offset].strip('"`')
    projection = (
        projection_context(text, column_start, column_end, normalized_backend)
        if (
            (allow_empty_column_prefix and not column_prefix)
            or (not column_prefix and before.endswith("."))
            or (
                column_prefix
                and column_prefix.casefold() not in _COLUMN_KEYWORDS
                and cursor_offset > 0
                and not text[cursor_offset - 1].isspace()
            )
        )
        and (cursor_offset == len(text) or text[cursor_offset].isspace())
        else None
    )
    if projection is not None:
        final_prefix = text[column_start:cursor_offset].strip('"`')
        request = CompletionRequest(
            connection_key,
            normalized_backend,
            "column",
            normalize_identifier_prefix(final_prefix),
            context=projection,
        )
        return CompletionContext(request, column_start, column_end, final_prefix, request.prefix)

    relation = relation_position(text, cursor_offset, normalized_backend)
    if relation is None:
        request = CompletionRequest(
            connection_key,
            normalized_backend,
            "keyword",
            normalize_identifier_prefix(final_prefix),
            context=f"keyword:{line_start}",
        )
        return CompletionContext(
            request,
            replacement_start,
            replacement_end,
            final_prefix,
            request.prefix,
        )

    parts = relation.parts
    replacement_start = relation.start
    if column_start == replacement_start:
        replacement_end = column_end
    if text[replacement_start : replacement_start + 1] in {'"', "`"}:
        quote = text[replacement_start]
        if replacement_end == cursor_offset and text[cursor_offset : cursor_offset + 1] == quote:
            replacement_end += 1
    final_prefix = text[replacement_start:cursor_offset]
    catalog, schema, database = _namespace(parts, normalized_backend, trino_catalogs)

    prefix = parts[-1]
    request = CompletionRequest(
        connection_key,
        normalized_backend,
        "table",
        prefix,
        schema=schema,
        catalog=catalog,
        database=database,
        context=relation.context,
    )
    return CompletionContext(
        request,
        replacement_start,
        replacement_end,
        final_prefix,
        prefix,
        local_relations=relation.ctes,
    )


def _namespace(
    parts: tuple[str, ...], backend: str, trino_catalogs: tuple[str, ...] | None
) -> tuple[str | None, str | None, str | None]:
    from .completion import (  # noqa: PLC0415 -- retain shared facade constants.
        _CATALOG_SCHEMA_TABLE_PARTS,
        _SCHEMA_TABLE_PARTS,
    )

    catalog: str | None = None
    schema: str | None = None
    database: str | None = None
    if backend == "trino":
        if len(parts) >= _CATALOG_SCHEMA_TABLE_PARTS:
            catalog, schema = parts[0], parts[1]
        elif len(parts) == _SCHEMA_TABLE_PARTS:
            if trino_catalogs and parts[0] in trino_catalogs:
                catalog = parts[0]
            else:
                schema = parts[0]
    elif len(parts) >= _SCHEMA_TABLE_PARTS:
        schema = parts[-2]
        if backend in {"ch", "clickhouse"}:
            database = schema
    return catalog, schema, database
