"""SQL formatting and conservative SQL-to-CTE rewriting helpers."""

from __future__ import annotations

from dataclasses import dataclass
import re

import sqlparse
from sqlglot import Dialect, exp, parse
from sqlglot.errors import ErrorLevel, SqlglotError
from sqlglot.tokens import Tokenizer


_SUPPORTED_DIALECTS = {"postgres", "trino", "clickhouse"}
_SUPPORTED_KEYWORD_CASES = {"upper", "lower", "capitalize"}
_SUPPORTED_WHERE_ANCHORS = {"1=1", "true", "first_condition", "preserve"}
_SUPPORTED_REWRITE_STRATEGIES = {"auto"}
_CTE_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CASE_PRESERVED_TOKEN_TYPES = {
    "BIT_STRING",
    "BYTE_STRING",
    "HEX_STRING",
    "IDENTIFIER",
    "NATIONAL_STRING",
    "NUMBER",
    "PARAMETER",
    "PLACEHOLDER",
    "RAW_STRING",
    "STRING",
    "UNICODE_STRING",
    "VAR",
}


@dataclass(frozen=True)
class _SingleStatement:
    sql: str
    has_trailing_semicolon: bool


def format_sql(
    sql: str,
    *,
    dialect: str | None = None,
    leading_commas: bool = False,
    where_anchor: str = "1=1",
    keyword_case: str = "upper",
    indent: int = 4,
) -> str:
    """Format exactly one SQL statement without opening a database connection."""

    normalized_dialect = _validate_dialect(dialect)
    normalized_keyword_case = _validate_keyword_case(keyword_case)
    normalized_indent = _validate_indent(indent)
    normalized_where_anchor = _validate_where_anchor(where_anchor)
    statement = _split_one_statement(sql, operation="format_sql")
    expression = _parse_expression(
        statement.sql,
        dialect=normalized_dialect,
        operation="format_sql",
    )

    if normalized_where_anchor != "preserve":
        _normalize_where_clauses(expression, normalized_where_anchor)

    rendered = _render_expression(
        expression,
        dialect=normalized_dialect,
        leading_commas=leading_commas,
        keyword_case=normalized_keyword_case,
        indent=normalized_indent,
        operation="format_sql",
    )
    return _with_semicolon_policy(rendered, statement.has_trailing_semicolon)


def rewrite_with_ctes(
    sql: str,
    *,
    dialect: str | None = None,
    strategy: str = "auto",
    cte_prefix: str = "cte",
    keyword_case: str = "upper",
    indent: int = 4,
) -> str:
    """Rewrite derived-table SELECT subqueries into named CTEs."""

    normalized_dialect = _validate_dialect(dialect)
    normalized_keyword_case = _validate_keyword_case(keyword_case)
    normalized_indent = _validate_indent(indent)
    _validate_rewrite_strategy(strategy)
    _validate_cte_prefix(cte_prefix)
    statement = _split_one_statement(sql, operation="rewrite_with_ctes")
    expression = _parse_expression(
        statement.sql,
        dialect=normalized_dialect,
        operation="rewrite_with_ctes",
    )
    if not isinstance(expression, exp.Select):
        raise ValueError("rewrite_with_ctes expects a SELECT statement.")

    ctes = _extract_supported_ctes(expression, cte_prefix=cte_prefix)
    _attach_ctes(expression, ctes)

    rendered = _render_expression(
        expression,
        dialect=normalized_dialect,
        leading_commas=False,
        keyword_case=normalized_keyword_case,
        indent=normalized_indent,
        operation="rewrite_with_ctes",
    )
    _parse_expression(
        rendered,
        dialect=normalized_dialect,
        operation="rewrite_with_ctes",
    )
    return _with_semicolon_policy(rendered, statement.has_trailing_semicolon)


def _validate_dialect(dialect: str | None) -> str | None:
    if dialect is None:
        return None
    normalized = dialect.strip().lower()
    if normalized not in _SUPPORTED_DIALECTS:
        supported = ", ".join(sorted(_SUPPORTED_DIALECTS))
        raise ValueError(f"dialect must be one of: {supported}, or None.")
    return normalized


def _validate_keyword_case(keyword_case: str) -> str:
    normalized = keyword_case.strip().lower()
    if normalized not in _SUPPORTED_KEYWORD_CASES:
        supported = ", ".join(sorted(_SUPPORTED_KEYWORD_CASES))
        raise ValueError(f"keyword_case must be one of: {supported}.")
    return normalized


def _validate_indent(indent: int) -> int:
    if not isinstance(indent, int) or isinstance(indent, bool) or indent < 1:
        raise ValueError("indent must be a positive integer.")
    return indent


def _validate_where_anchor(where_anchor: str) -> str:
    normalized = where_anchor.strip().lower()
    if normalized not in _SUPPORTED_WHERE_ANCHORS:
        supported = ", ".join(sorted(_SUPPORTED_WHERE_ANCHORS))
        raise ValueError(f"where_anchor must be one of: {supported}.")
    return normalized


def _validate_rewrite_strategy(strategy: str) -> None:
    normalized = strategy.strip().lower()
    if normalized not in _SUPPORTED_REWRITE_STRATEGIES:
        supported = ", ".join(sorted(_SUPPORTED_REWRITE_STRATEGIES))
        raise ValueError(f"strategy must be one of: {supported}.")


def _validate_cte_prefix(cte_prefix: str) -> None:
    if not cte_prefix or not _CTE_PREFIX_RE.match(cte_prefix):
        raise ValueError(
            "cte_prefix must be a non-empty unquoted SQL identifier prefix."
        )


def _split_one_statement(sql: str, *, operation: str) -> _SingleStatement:
    if not isinstance(sql, str):
        raise ValueError(f"{operation} expects sql to be a string.")
    raw_sql = sql.strip()
    if not raw_sql:
        raise ValueError(f"{operation} expects a non-empty SQL statement.")

    statements = [
        statement.strip()
        for statement in sqlparse.split(raw_sql)
        if statement.strip().strip(";").strip()
    ]
    if len(statements) != 1:
        raise ValueError(f"{operation} expects exactly one SQL statement.")

    statement = statements[0]
    has_trailing_semicolon = statement.rstrip().endswith(";")
    return _SingleStatement(
        sql=statement.rstrip().rstrip(";").rstrip(),
        has_trailing_semicolon=has_trailing_semicolon,
    )


def _parse_expression(sql: str, *, dialect: str | None, operation: str) -> exp.Expression:
    try:
        expressions = parse(sql, read=dialect)
    except SqlglotError as exc:
        raise ValueError(f"{operation} could not parse SQL: {exc}") from exc
    if len(expressions) != 1 or expressions[0] is None:
        raise ValueError(f"{operation} expects exactly one SQL statement.")
    return expressions[0]


def _render_expression(
    expression: exp.Expression,
    *,
    dialect: str | None,
    leading_commas: bool,
    keyword_case: str,
    indent: int,
    operation: str,
) -> str:
    try:
        rendered = expression.sql(
            dialect=dialect,
            pretty=True,
            pad=indent,
            indent=indent,
            leading_comma=leading_commas,
            unsupported_level=ErrorLevel.RAISE,
        )
    except SqlglotError as exc:
        raise ValueError(f"{operation} could not render SQL: {exc}") from exc
    if leading_commas:
        rendered = _normalize_leading_comma_indentation(rendered, indent)
    return _apply_keyword_case(rendered, keyword_case, dialect=dialect)


def _with_semicolon_policy(sql: str, has_trailing_semicolon: bool) -> str:
    if has_trailing_semicolon:
        return f"{sql};"
    return sql


def _normalize_where_clauses(expression: exp.Expression, where_anchor: str) -> None:
    for where in list(expression.find_all(exp.Where)):
        condition = where.this
        if condition is None:
            continue
        conditions = _flatten_and(condition)
        real_conditions = [
            child
            for child in conditions
            if not _is_artificial_anchor_condition(child)
        ]
        if where_anchor == "first_condition":
            if real_conditions:
                where.set("this", _combine_conditions(real_conditions))
            continue

        anchor = _anchor_expression(where_anchor)
        if real_conditions:
            where.set("this", _combine_conditions([anchor, *real_conditions]))
        else:
            where.set("this", anchor)


def _flatten_and(condition: exp.Expression) -> list[exp.Expression]:
    if isinstance(condition, exp.And):
        return [
            *_flatten_and(condition.this),
            *_flatten_and(condition.expression),
        ]
    return [condition]


def _combine_conditions(conditions: list[exp.Expression]) -> exp.Expression:
    copied_conditions = [condition.copy() for condition in conditions]
    return exp.and_(*copied_conditions)


def _anchor_expression(where_anchor: str) -> exp.Expression:
    if where_anchor == "true":
        return exp.true()
    return exp.EQ(
        this=exp.Literal.number(1),
        expression=exp.Literal.number(1),
    )


def _is_artificial_anchor_condition(condition: exp.Expression) -> bool:
    if isinstance(condition, exp.Boolean):
        return bool(condition.this) is True
    if isinstance(condition, exp.EQ):
        return _is_number_one(condition.this) and _is_number_one(condition.expression)
    return False


def _is_number_one(expression: exp.Expression) -> bool:
    return (
        isinstance(expression, exp.Literal)
        and not expression.is_string
        and str(expression.this) == "1"
    )


def _extract_supported_ctes(
    expression: exp.Select,
    *,
    cte_prefix: str,
) -> list[exp.CTE]:
    select_subqueries = [
        node
        for node in expression.find_all(exp.Subquery)
        if isinstance(node, exp.Subquery) and isinstance(node.this, exp.Select)
    ]
    if not select_subqueries:
        raise ValueError(
            "rewrite_with_ctes could not find nested SELECT subqueries to extract."
        )

    eligible = [
        subquery
        for subquery in select_subqueries
        if _is_supported_derived_subquery(subquery)
    ]
    if len(eligible) != len(select_subqueries):
        raise ValueError(
            "rewrite_with_ctes only supports SELECT subqueries in FROM or JOIN."
        )
    _reject_nested_eligible_subqueries(eligible)

    used_cte_names = _existing_cte_names(expression)
    next_index = 1
    ctes: list[exp.CTE] = []
    for subquery in eligible:
        cte_name, next_index = _next_cte_name(
            cte_prefix,
            used_names=used_cte_names,
            start_index=next_index,
        )
        ctes.append(
            exp.CTE(
                this=subquery.this.copy(),
                alias=exp.TableAlias(this=exp.to_identifier(cte_name)),
            )
        )
        table = exp.Table(this=exp.to_identifier(cte_name))
        alias = subquery.args.get("alias")
        if alias is not None:
            table.set("alias", alias.copy())
        subquery.replace(table)

    if any(
        isinstance(node, exp.Subquery) and isinstance(node.this, exp.Select)
        for node in expression.find_all(exp.Subquery)
    ):
        raise ValueError(
            "rewrite_with_ctes could not confidently rewrite all SELECT subqueries."
        )
    return ctes


def _is_supported_derived_subquery(subquery: exp.Subquery) -> bool:
    if not isinstance(subquery.parent, (exp.From, exp.Join)):
        return False
    if subquery.args.get("sample") is not None:
        return False
    if subquery.args.get("pivots") is not None:
        return False
    return True


def _reject_nested_eligible_subqueries(subqueries: list[exp.Subquery]) -> None:
    subquery_ids = {id(subquery) for subquery in subqueries}
    for subquery in subqueries:
        parent = subquery.parent
        while parent is not None:
            if id(parent) in subquery_ids:
                raise ValueError(
                    "rewrite_with_ctes does not support nested derived subqueries."
                )
            parent = parent.parent


def _existing_cte_names(expression: exp.Expression) -> set[str]:
    with_expression = expression.args.get(_with_arg_name())
    if with_expression is None:
        return set()
    return {
        cte.alias_or_name
        for cte in with_expression.expressions
        if cte.alias_or_name
    }


def _next_cte_name(
    cte_prefix: str,
    *,
    used_names: set[str],
    start_index: int,
) -> tuple[str, int]:
    index = start_index
    while True:
        name = f"{cte_prefix}_{index}"
        index += 1
        if name not in used_names:
            used_names.add(name)
            return name, index


def _attach_ctes(expression: exp.Select, ctes: list[exp.CTE]) -> None:
    with_arg = _with_arg_name()
    with_expression = expression.args.get(with_arg)
    if with_expression is None:
        expression.set(with_arg, exp.With(expressions=ctes))
        return
    for cte in ctes:
        with_expression.append("expressions", cte)


def _with_arg_name() -> str:
    if "with_" in exp.Select.arg_types:
        return "with_"
    return "with"


def _apply_keyword_case(sql: str, keyword_case: str, *, dialect: str | None) -> str:
    if keyword_case == "upper":
        return sql

    tokenizer_class = (
        Dialect.get_or_raise(dialect).tokenizer_class
        if dialect is not None
        else Tokenizer
    )
    parts: list[str] = []
    cursor = 0
    for token in tokenizer_class().tokenize(sql):
        parts.append(sql[cursor : token.start])
        token_text = sql[token.start : token.end + 1]
        if _should_case_token(token):
            token_text = _case_token_text(token_text, keyword_case)
        parts.append(token_text)
        cursor = token.end + 1
    parts.append(sql[cursor:])
    return "".join(parts)


def _should_case_token(token: object) -> bool:
    token_type = getattr(token, "token_type", None)
    token_type_name = getattr(token_type, "name", "")
    token_text = getattr(token, "text", "")
    return (
        token_type_name not in _CASE_PRESERVED_TOKEN_TYPES
        and any(character.isalpha() for character in token_text)
    )


def _case_token_text(token_text: str, keyword_case: str) -> str:
    if keyword_case == "lower":
        return token_text.lower()
    return token_text.capitalize()


def _normalize_leading_comma_indentation(sql: str, indent: int) -> str:
    lines = sql.splitlines()
    for index, line in enumerate(lines[:-1]):
        if line.strip().upper() != "SELECT":
            continue
        next_index = index + 1
        next_line = lines[next_index]
        if next_line.lstrip().startswith(","):
            continue
        depth = len(line) - len(line.lstrip(" "))
        expected_prefix = " " * (depth + indent)
        doubled_prefix = " " * (depth + (indent * 2))
        if next_line.startswith(doubled_prefix):
            lines[next_index] = expected_prefix + next_line[len(doubled_prefix) :]
    return "\n".join(lines)


__all__ = ["format_sql", "rewrite_with_ctes"]
