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


def _parse_insert(self: Any, *args: Any, **kwargs: Any) -> Any:
    previous = getattr(self, "_atk_insert_target", False)
    self._atk_insert_target = True
    try:
        return super(ClickHouseDialect.parser_class, self)._parse_insert(*args, **kwargs)
    finally:
        self._atk_insert_target = previous


def _parse_table(
    self: Any,
    schema: bool = False,  # noqa: FBT001, FBT002 - Match SQLGlot's positional parser interface.
    *args: Any,
    **kwargs: Any,
) -> Any:
    if schema:
        self._atk_insert_target = False
    return super(ClickHouseDialect.parser_class, self)._parse_table(schema, *args, **kwargs)


def _parse_insert_function(self: Any, *args: Any, **kwargs: Any) -> Any:
    function_target = (
        getattr(self, "_atk_insert_target", False) and self._prev.token_type == _TOKEN_TYPE.FUNCTION
    )
    if function_target:
        # Consume the context before parsing arguments, which may themselves
        # contain functions. Only this direct INSERT FUNCTION target owns the
        # optional target-column list following its closing parenthesis.
        self._atk_insert_target = False
    function = super(ClickHouseDialect.parser_class, self)._parse_function(*args, **kwargs)
    return self._parse_schema(this=function) if function_target else function


def _identifier_sql(self: Any, expression: exp.Identifier) -> str:
    # Supported SQLGlot generators differ in whether identifier escapes are
    # applied natively. Retain their quoting and normalization decisions while
    # supplying the missing escape on older generator interfaces only.
    escapes = getattr(self, "_identifier_escaped_sequences", {})
    if "\\" in expression.name and "\\" not in escapes:
        expression = expression.copy()
        expression.set("this", expression.name.replace("\\", "\\\\"))
    return str(super(ClickHouseDialect.generator_class, self).identifier_sql(expression))


class ClickHouseDialect(ClickHouse):
    """Preserve integer rand(), which is distinct from randCanonical()."""

    Parser: Any = type(
        "Parser",
        (ClickHouse.parser_class,),
        {
            "_parse_insert": _parse_insert,
            "_parse_table": _parse_table,
            "_parse_function": _parse_insert_function,
            "FUNC_TOKENS": ClickHouse.parser_class.FUNC_TOKENS | {_TOKEN_TYPE.DATABASE},
            "STRUCT_TYPE_TOKENS": ClickHouse.parser_class.STRUCT_TYPE_TOKENS - {_TOKEN_TYPE.OBJECT},
            "NESTED_TYPE_TOKENS": ClickHouse.parser_class.NESTED_TYPE_TOKENS - {_TOKEN_TYPE.OBJECT},
            "TYPE_CONVERTERS": {
                **ClickHouse.parser_class.TYPE_CONVERTERS,
                exp.DataType.Type.OBJECT: _legacy_object_type,
            },
            "FUNCTIONS": {
                **ClickHouse.parser_class.FUNCTIONS,
                "DATABASE": lambda args: exp.Anonymous(this="database", expressions=args),
                "RAND": lambda args: exp.Anonymous(this="rand", expressions=args),
            },
        },
    )
    Generator: Any = type(
        "Generator",
        (ClickHouse.generator_class,),
        {
            "identifier_sql": _identifier_sql,
            "TYPE_MAPPING": {
                **ClickHouse.generator_class.TYPE_MAPPING,
                exp.DataType.Type.DATE: "Date",
            },
        },
    )


def sql_string_literal(value: str) -> str:
    """Quote CH text without interpreting a metadata backslash as an escape."""
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"
