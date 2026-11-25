"""Quote one exact ClickHouse metadata identifier without interpreting escapes."""

from __future__ import annotations


def quote_identifier(identifier: str) -> str:
    escaped = identifier.replace("\\", "\\\\").replace("`", "``")
    return f"`{escaped}`"


def add_table_suffix(table_name: str, suffix: str) -> str:
    from analytics_toolkit.sql.core.identifiers import (  # noqa: PLC0415 -- registry initialization.
        TableIdentifier,
    )

    return TableIdentifier.parse(table_name, "ch").with_relation_suffix(suffix).render("ch")
