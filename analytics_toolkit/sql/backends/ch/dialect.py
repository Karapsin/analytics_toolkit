"""ClickHouse parser shared by routing, defaults and structural DDL inspection."""

from __future__ import annotations

from typing import Any

import sqlglot.tokens as sqlglot_tokens
from sqlglot import exp
from sqlglot.dialects.clickhouse import ClickHouse

_TOKEN_TYPE: Any = vars(sqlglot_tokens)["TokenType"]


def _legacy_object_type(native: exp.DataType) -> exp.DataType:
    parameters = native.expressions
    parameter = parameters[0] if len(parameters) == 1 else None
    if (
        not isinstance(parameter, exp.DataTypeParam)
        or parameter.args.get("expression") is not None
        or not isinstance(parameter.this, exp.Literal)
        or not parameter.this.is_string
        or parameter.this.this != "json"
    ):
        message = "ClickHouse Object requires the single static 'json' format."
        raise ValueError(message)
    return exp.DataType(
        this=exp.DataType.Type.USERDEFINED,
        kind=exp.Var(this="Object"),
        expressions=parameters,
    )


class ClickHouseDialect(ClickHouse):
    """Preserve integer rand(), which is distinct from randCanonical()."""

    Parser: Any = type(
        "Parser",
        (ClickHouse.parser_class,),
        {
            "STRUCT_TYPE_TOKENS": ClickHouse.parser_class.STRUCT_TYPE_TOKENS - {_TOKEN_TYPE.OBJECT},
            "NESTED_TYPE_TOKENS": ClickHouse.parser_class.NESTED_TYPE_TOKENS - {_TOKEN_TYPE.OBJECT},
            "TYPE_CONVERTERS": {
                **ClickHouse.parser_class.TYPE_CONVERTERS,
                exp.DataType.Type.OBJECT: _legacy_object_type,
            },
            "FUNCTIONS": {
                **ClickHouse.parser_class.FUNCTIONS,
                "RAND": lambda args: exp.Anonymous(this="rand", expressions=args),
            },
        },
    )
    Generator: Any = type(
        "Generator",
        (ClickHouse.generator_class,),
        {
            "TYPE_MAPPING": {
                **ClickHouse.generator_class.TYPE_MAPPING,
                exp.DataType.Type.DATE: "Date",
            },
        },
    )


def sql_string_literal(value: str) -> str:
    """Quote CH text without interpreting a metadata backslash as an escape."""
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"
