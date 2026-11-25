"""Preserve common properties of an actual Distributed facade during replacement."""

from __future__ import annotations

from sqlglot import exp

from .dialect import ClickHouseDialect
from .reconfigure_ddl import engine_name, parse_create_table


def preserve_facade_properties(sql: str, original_ddl: str) -> str:
    """Keep actual facade settings/comments while retaining new columns and engine."""
    original = parse_create_table(original_ddl, "existing replacement facade")
    replacement = parse_create_table(sql, "replacement facade")
    if engine_name(original).casefold() != "distributed":
        return sql
    existing = original.args.get("properties")
    properties = replacement.args.get("properties")
    if existing is not None and properties is not None:
        for property_ in existing.expressions:
            if not isinstance(property_, (exp.EngineProperty, exp.OnCluster, exp.UuidProperty)):
                properties.append("expressions", property_.copy())
    return replacement.sql(dialect=ClickHouseDialect)
