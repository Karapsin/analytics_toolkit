"""ClickHouse parser shared by routing, defaults and structural DDL inspection."""

from __future__ import annotations

from typing import Any

from sqlglot import exp
from sqlglot.dialects.clickhouse import ClickHouse


class ClickHouseDialect(ClickHouse):
    """Preserve integer rand(), which is distinct from randCanonical()."""

    Parser: Any = type(
        "Parser",
        (ClickHouse.parser_class,),
        {
            "FUNCTIONS": {
                **ClickHouse.parser_class.FUNCTIONS,
                "RAND": lambda args: exp.Anonymous(this="rand", expressions=args),
            },
        },
    )


def sql_string_literal(value: str) -> str:
    """Quote CH text without interpreting a metadata backslash as an escape."""
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"
