"""SQL formatting and conservative SQL-to-CTE rewriting helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

import sqlparse
from sqlglot import Dialect, exp, parse
from sqlglot.errors import ErrorLevel, SqlglotError
from sqlglot.tokens import Tokenizer

_SUPPORTED_DIALECTS = {"postgres", "trino", "clickhouse"}
_SUPPORTED_GROUP_ORDER_FORMATS = {"expressions", "ordinal"}
_SUPPORTED_KEYWORD_CASES = {"upper", "lower", "capitalize"}
_SUPPORTED_WHERE_ANCHORS = {"1=1", "true", "first_condition", "preserve"}
_SUPPORTED_REWRITE_STRATEGIES = {"auto"}
_CTE_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ORDINAL_RE = re.compile(r"^[0-9]+$")
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


@dataclass(frozen=True)
class _GpTempTable:
    name: str
    query: exp.Query


@dataclass(frozen=True)
class _SelectOrdinalMapping:
    expression_positions: dict[str, int]
    alias_positions: dict[str, int]


@dataclass(frozen=True)
class _ClauseCompactionTarget:
    clause: str
    items_sql: str


def format_sql(
    sql: str,
    *,
    dialect: str | None = None,
    leading_commas: bool = False,
    where_anchor: str = "1=1",
    group_by_format: str = "ordinal",
    order_by_format: str = "ordinal",
    keyword_case: str = "lower",
    indent: int = 4,
    cte_blank_lines: int = 1,
    union_blank_lines: int = 1,
) -> str:
    """Format exactly one SQL statement without opening a database connection."""

    normalized_dialect = _validate_dialect(dialect)
    normalized_keyword_case = _validate_keyword_case(keyword_case)
    normalized_indent = _validate_indent(indent)
    normalized_cte_blank_lines = _validate_cte_blank_lines(cte_blank_lines)
    normalized_union_blank_lines = _validate_union_blank_lines(union_blank_lines)
    normalized_where_anchor = _validate_where_anchor(where_anchor)
    normalized_group_by_format = _validate_group_order_format(
        group_by_format,
        label="group_by_format",
    )
    normalized_order_by_format = _validate_group_order_format(
        order_by_format,
        label="order_by_format",
    )
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
        where_anchor=normalized_where_anchor,
        group_by_format=normalized_group_by_format,
        order_by_format=normalized_order_by_format,
        keyword_case=normalized_keyword_case,
        indent=normalized_indent,
        cte_blank_lines=normalized_cte_blank_lines,
        union_blank_lines=normalized_union_blank_lines,
        operation="format_sql",
    )
    return _with_semicolon_policy(rendered, statement.has_trailing_semicolon)


def rewrite_with_ctes(
    sql: str,
    *,
    dialect: str | None = None,
    strategy: str = "auto",
    cte_prefix: str = "cte",
    group_by_format: str = "ordinal",
    order_by_format: str = "ordinal",
    keyword_case: str = "lower",
    indent: int = 4,
    cte_blank_lines: int = 1,
    union_blank_lines: int = 1,
) -> str:
    """Rewrite derived-table SELECT subqueries into named CTEs."""

    normalized_dialect = _validate_dialect(dialect)
    normalized_keyword_case = _validate_keyword_case(keyword_case)
    normalized_indent = _validate_indent(indent)
    normalized_cte_blank_lines = _validate_cte_blank_lines(cte_blank_lines)
    normalized_union_blank_lines = _validate_union_blank_lines(union_blank_lines)
    normalized_group_by_format = _validate_group_order_format(
        group_by_format,
        label="group_by_format",
    )
    normalized_order_by_format = _validate_group_order_format(
        order_by_format,
        label="order_by_format",
    )
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
        where_anchor=None,
        group_by_format=normalized_group_by_format,
        order_by_format=normalized_order_by_format,
        keyword_case=normalized_keyword_case,
        indent=normalized_indent,
        cte_blank_lines=normalized_cte_blank_lines,
        union_blank_lines=normalized_union_blank_lines,
        operation="rewrite_with_ctes",
    )
    _parse_expression(
        rendered,
        dialect=normalized_dialect,
        operation="rewrite_with_ctes",
    )
    return _with_semicolon_policy(rendered, statement.has_trailing_semicolon)


def gp_rewrite_to_temp_tables(
    sql: str,
    *,
    dialect: str | None = "postgres",
    temp_prefix: str = "tmp",
    group_by_format: str = "ordinal",
    order_by_format: str = "ordinal",
    keyword_case: str = "lower",
    indent: int = 4,
    cte_blank_lines: int = 1,
    union_blank_lines: int = 1,
) -> str:
    """Rewrite SELECT CTEs and subqueries into Greenplum temp-table SQL."""

    normalized_dialect = _validate_dialect(dialect)
    normalized_keyword_case = _validate_keyword_case(keyword_case)
    normalized_indent = _validate_indent(indent)
    normalized_cte_blank_lines = _validate_cte_blank_lines(cte_blank_lines)
    normalized_union_blank_lines = _validate_union_blank_lines(union_blank_lines)
    normalized_group_by_format = _validate_group_order_format(
        group_by_format,
        label="group_by_format",
    )
    normalized_order_by_format = _validate_group_order_format(
        order_by_format,
        label="order_by_format",
    )
    _validate_temp_prefix(temp_prefix)
    statement = _split_one_statement(sql, operation="gp_rewrite_to_temp_tables")
    expression = _parse_expression(
        statement.sql,
        dialect=normalized_dialect,
        operation="gp_rewrite_to_temp_tables",
    )
    if not isinstance(expression, exp.Select):
        raise ValueError("gp_rewrite_to_temp_tables expects a SELECT statement.")

    planner = _GpTempTablePlanner(
        dialect=normalized_dialect,
        temp_prefix=temp_prefix,
        group_by_format=normalized_group_by_format,
        order_by_format=normalized_order_by_format,
        keyword_case=normalized_keyword_case,
        indent=normalized_indent,
        cte_blank_lines=normalized_cte_blank_lines,
        union_blank_lines=normalized_union_blank_lines,
    )
    planner.rewrite_select(expression)
    planner.validate_complete_rewrite(expression)
    if not planner.temp_tables:
        raise ValueError(
            "gp_rewrite_to_temp_tables could not find CTEs or SELECT subqueries "
            "to materialize."
        )

    rendered_final = _render_expression(
        expression,
        dialect=normalized_dialect,
        leading_commas=False,
        where_anchor=None,
        group_by_format=normalized_group_by_format,
        order_by_format=normalized_order_by_format,
        keyword_case=normalized_keyword_case,
        indent=normalized_indent,
        cte_blank_lines=normalized_cte_blank_lines,
        union_blank_lines=normalized_union_blank_lines,
        operation="gp_rewrite_to_temp_tables",
    )
    _parse_expression(
        rendered_final,
        dialect=normalized_dialect,
        operation="gp_rewrite_to_temp_tables",
    )

    blocks = planner.render_temp_table_blocks(expression)
    final_sql = _with_semicolon_policy(
        rendered_final,
        statement.has_trailing_semicolon,
    )
    return "\n\n".join([*blocks, final_sql])


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


def _validate_cte_blank_lines(cte_blank_lines: int) -> int:
    return _validate_blank_lines(cte_blank_lines, label="cte_blank_lines")


def _validate_union_blank_lines(union_blank_lines: int) -> int:
    return _validate_blank_lines(union_blank_lines, label="union_blank_lines")


def _validate_blank_lines(value: int, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer.")
    return value


def _validate_where_anchor(where_anchor: str) -> str:
    normalized = where_anchor.strip().lower()
    if normalized not in _SUPPORTED_WHERE_ANCHORS:
        supported = ", ".join(sorted(_SUPPORTED_WHERE_ANCHORS))
        raise ValueError(f"where_anchor must be one of: {supported}.")
    return normalized


def _validate_group_order_format(value: str, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string.")
    normalized = value.strip().lower()
    if normalized not in _SUPPORTED_GROUP_ORDER_FORMATS:
        supported = ", ".join(sorted(_SUPPORTED_GROUP_ORDER_FORMATS))
        raise ValueError(f"{label} must be one of: {supported}.")
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


def _validate_temp_prefix(temp_prefix: str) -> None:
    if not temp_prefix or not _CTE_PREFIX_RE.match(temp_prefix):
        raise ValueError(
            "temp_prefix must be a non-empty unquoted SQL identifier prefix."
        )


def _validate_temp_table_name(name: str, *, label: str) -> None:
    if not name or not _CTE_PREFIX_RE.match(name):
        raise ValueError(f"{label} must be a non-empty unquoted SQL identifier.")


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
    where_anchor: str | None,
    group_by_format: str,
    order_by_format: str,
    keyword_case: str,
    indent: int,
    cte_blank_lines: int,
    union_blank_lines: int,
    operation: str,
) -> str:
    expression_to_render, compaction_targets = _prepare_group_order_rendering(
        expression,
        dialect=dialect,
        group_by_format=group_by_format,
        order_by_format=order_by_format,
    )
    try:
        rendered = expression_to_render.sql(
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
    if where_anchor in {"1=1", "true"}:
        rendered = _normalize_where_anchor_layout(rendered, where_anchor)
    if compaction_targets:
        rendered = _compact_targeted_clause_layout(rendered, compaction_targets)
    rendered = _normalize_join_condition_layout(rendered)
    rendered = _normalize_cte_separator_layout(rendered, cte_blank_lines)
    rendered = _normalize_union_separator_layout(rendered, union_blank_lines)
    rendered = _compact_single_star_select_layout(rendered)
    return _apply_keyword_case(rendered, keyword_case, dialect=dialect)


def _prepare_group_order_rendering(
    expression: exp.Expression,
    *,
    dialect: str | None,
    group_by_format: str,
    order_by_format: str,
) -> tuple[exp.Expression, list[_ClauseCompactionTarget]]:
    if group_by_format == "expressions" and order_by_format == "expressions":
        return expression, []

    expression_copy = expression.copy()
    for select in expression_copy.find_all(exp.Select):
        mapping = _select_ordinal_mapping(select, dialect=dialect)
        if group_by_format == "ordinal":
            _replace_group_by_items(select, mapping=mapping, dialect=dialect)
        if order_by_format == "ordinal":
            _replace_order_by_items(select, mapping=mapping, dialect=dialect)

    return expression_copy, _clause_compaction_targets(
        expression_copy,
        dialect=dialect,
        group_by_format=group_by_format,
        order_by_format=order_by_format,
    )


def _select_ordinal_mapping(
    select: exp.Select,
    *,
    dialect: str | None,
) -> _SelectOrdinalMapping:
    expression_positions: dict[str, int] = {}
    alias_candidates: dict[str, int | None] = {}
    for position, projection in enumerate(select.expressions, start=1):
        expression = (
            projection.this if isinstance(projection, exp.Alias) else projection
        )
        expression_key = _expression_match_key(expression, dialect=dialect)
        if expression_key not in expression_positions:
            expression_positions[expression_key] = position

        alias_key = _projection_alias_key(projection)
        if alias_key is None:
            continue
        if alias_key in alias_candidates and alias_candidates[alias_key] != position:
            alias_candidates[alias_key] = None
        else:
            alias_candidates[alias_key] = position

    return _SelectOrdinalMapping(
        expression_positions=expression_positions,
        alias_positions={
            alias_key: position
            for alias_key, position in alias_candidates.items()
            if position is not None
        },
    )


def _projection_alias_key(projection: exp.Expression) -> str | None:
    if not isinstance(projection, exp.Alias):
        return None
    alias = projection.alias
    if not alias:
        return None
    return alias.casefold()


def _expression_match_key(
    expression: exp.Expression,
    *,
    dialect: str | None,
) -> str:
    try:
        return expression.sql(
            dialect=dialect,
            unsupported_level=ErrorLevel.RAISE,
        )
    except SqlglotError as exc:
        raise ValueError(
            f"Could not render SQL expression for matching: {exc}"
        ) from exc


def _replace_group_by_items(
    select: exp.Select,
    *,
    mapping: _SelectOrdinalMapping,
    dialect: str | None,
) -> None:
    group = select.args.get("group")
    if group is None:
        return

    replaced_expressions: list[exp.Expression] = []
    for expression in group.expressions:
        position = _select_position_for_clause_expression(
            expression,
            mapping=mapping,
            dialect=dialect,
        )
        replaced_expressions.append(
            exp.Literal.number(position) if position is not None else expression
        )
    group.set("expressions", replaced_expressions)


def _replace_order_by_items(
    select: exp.Select,
    *,
    mapping: _SelectOrdinalMapping,
    dialect: str | None,
) -> None:
    order = select.args.get("order")
    if order is None:
        return

    for order_item in order.expressions:
        if isinstance(order_item, exp.Ordered):
            expression = order_item.this
            position = _select_position_for_clause_expression(
                expression,
                mapping=mapping,
                dialect=dialect,
            )
            if position is not None:
                order_item.set("this", exp.Literal.number(position))
            continue

        position = _select_position_for_clause_expression(
            order_item,
            mapping=mapping,
            dialect=dialect,
        )
        if position is not None:
            order_item.replace(exp.Literal.number(position))


def _select_position_for_clause_expression(
    expression: exp.Expression,
    *,
    mapping: _SelectOrdinalMapping,
    dialect: str | None,
) -> int | None:
    if _is_numeric_ordinal(expression):
        return None

    expression_key = _expression_match_key(expression, dialect=dialect)
    if expression_key in mapping.expression_positions:
        return mapping.expression_positions[expression_key]

    alias_key = _bare_identifier_key(expression)
    if alias_key is None:
        return None
    return mapping.alias_positions.get(alias_key)


def _is_numeric_ordinal(expression: exp.Expression) -> bool:
    return (
        isinstance(expression, exp.Literal)
        and not expression.is_string
        and _ORDINAL_RE.match(str(expression.this)) is not None
    )


def _bare_identifier_key(expression: exp.Expression) -> str | None:
    if isinstance(expression, exp.Column) and not expression.table:
        return expression.name.casefold()
    if isinstance(expression, exp.Identifier):
        return str(expression.this).casefold()
    return None


def _clause_compaction_targets(
    expression: exp.Expression,
    *,
    dialect: str | None,
    group_by_format: str,
    order_by_format: str,
) -> list[_ClauseCompactionTarget]:
    targets: list[_ClauseCompactionTarget] = []
    for select in expression.find_all(exp.Select):
        group = select.args.get("group")
        if group_by_format == "ordinal" and group is not None:
            targets.append(
                _ClauseCompactionTarget(
                    clause="GROUP BY",
                    items_sql=_render_clause_items(group.expressions, dialect=dialect),
                )
            )

        order = select.args.get("order")
        if order_by_format == "ordinal" and order is not None:
            targets.append(
                _ClauseCompactionTarget(
                    clause="ORDER BY",
                    items_sql=_render_clause_items(order.expressions, dialect=dialect),
                )
            )
    return targets


def _render_clause_items(
    expressions: list[exp.Expression],
    *,
    dialect: str | None,
) -> str:
    return ", ".join(
        expression.sql(dialect=dialect, unsupported_level=ErrorLevel.RAISE)
        for expression in expressions
    )


def _compact_targeted_clause_layout(
    sql: str,
    targets: list[_ClauseCompactionTarget],
) -> str:
    remaining: dict[tuple[str, str], int] = {}
    for target in targets:
        key = (target.clause, _normalize_compact_clause_sql(target.items_sql))
        remaining[key] = remaining.get(key, 0) + 1

    lines = sql.splitlines()
    compacted_lines: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        clause = _compactable_clause(line)
        if clause is None:
            compacted_lines.append(line)
            index += 1
            continue

        clause_indent = len(line) - len(line.lstrip(" "))
        item_lines: list[str] = []
        next_index = index + 1
        while next_index < len(lines):
            next_line = lines[next_index]
            if not next_line.strip():
                break
            next_indent = len(next_line) - len(next_line.lstrip(" "))
            if next_indent <= clause_indent:
                break
            item_lines.append(next_line)
            next_index += 1

        if not item_lines:
            compacted_lines.append(line)
            index += 1
            continue

        items_sql = _compact_clause_item_lines(item_lines)
        target_key = (clause, _normalize_compact_clause_sql(items_sql))
        if remaining.get(target_key, 0) <= 0:
            compacted_lines.append(line)
            index += 1
            continue

        remaining[target_key] -= 1
        prefix = line[:clause_indent]
        compacted_lines.append(f"{prefix}{clause} {items_sql}")
        index = next_index

    return "\n".join(compacted_lines)


def _compactable_clause(line: str) -> str | None:
    stripped = line.strip().upper()
    if stripped in {"GROUP BY", "ORDER BY"}:
        return stripped
    return None


def _compact_clause_item_lines(lines: list[str]) -> str:
    items: list[str] = []
    current = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(","):
            if current:
                items.append(current.strip())
            current = stripped[1:].strip()
        elif current:
            current = f"{current} {stripped}"
        else:
            current = stripped

        if current.endswith(","):
            items.append(current[:-1].strip())
            current = ""

    if current:
        items.append(current.strip())
    return ", ".join(item for item in items if item)


def _normalize_compact_clause_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip())


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


def _from_arg_name() -> str:
    if "from_" in exp.Select.arg_types:
        return "from_"
    return "from"


class _GpTempTablePlanner:
    def __init__(
        self,
        *,
        dialect: str | None,
        temp_prefix: str,
        group_by_format: str,
        order_by_format: str,
        keyword_case: str,
        indent: int,
        cte_blank_lines: int,
        union_blank_lines: int,
    ) -> None:
        self.dialect = dialect
        self.temp_prefix = temp_prefix
        self.group_by_format = group_by_format
        self.order_by_format = order_by_format
        self.keyword_case = keyword_case
        self.indent = indent
        self.cte_blank_lines = cte_blank_lines
        self.union_blank_lines = union_blank_lines
        self.temp_tables: list[_GpTempTable] = []
        self._used_temp_names: set[str] = set()
        self._next_generated_index = 1

    def rewrite_select(self, select: exp.Select) -> None:
        self._rewrite_query(select)

    def _rewrite_query(self, query: exp.Query) -> None:
        self._materialize_with_clause(query)
        if isinstance(query, exp.SetOperation):
            self._rewrite_set_operand(query.this)
            self._rewrite_set_operand(query.expression)
            self._rewrite_expression_children(
                cast("exp.Expression", query),
                skipped_args={"this", "expression"},
            )
            return
        self._rewrite_expression_children(cast("exp.Expression", query))

    def _rewrite_set_operand(self, operand: exp.Expression) -> None:
        if isinstance(operand, exp.Subquery) and isinstance(operand.this, exp.Query):
            self._rewrite_query(operand.this)
            return
        if isinstance(operand, exp.Query):
            self._rewrite_query(operand)
            return
        self._rewrite_child_expression(operand)

    def validate_complete_rewrite(self, expression: exp.Expression) -> None:
        temp_names = self._temp_name_keys()
        for select in expression.find_all(exp.Select):
            if select.args.get(_with_arg_name()) is not None:
                raise ValueError(
                    "gp_rewrite_to_temp_tables could not remove every WITH clause."
                )
            if id(select) != id(expression) and not _is_temp_reference_select(
                select,
                temp_names=temp_names,
            ):
                raise ValueError(
                    "gp_rewrite_to_temp_tables could not confidently rewrite all "
                    "nested SELECT queries."
                )
        for temp_table in self.temp_tables:
            self._assert_query_is_uncorrelated(temp_table.query)

    def render_temp_table_blocks(self, final_expression: exp.Select) -> list[str]:
        consumer_expressions = [
            cast("exp.Expression", temp_table.query) for temp_table in self.temp_tables
        ] + [cast("exp.Expression", final_expression)]
        blocks: list[str] = []
        for temp_table in self.temp_tables:
            distributed_columns = self._distributed_columns(
                temp_table.name,
                consumer_expressions=consumer_expressions,
            )
            blocks.append(
                self._render_temp_table_block(
                    temp_table,
                    distributed_columns=distributed_columns,
                )
            )
        return blocks

    def _materialize_with_clause(self, query: exp.Query) -> None:
        with_arg = _with_arg_name()
        with_expression = query.args.get(with_arg)
        if with_expression is None:
            return
        if with_expression.args.get("recursive"):
            raise ValueError(
                "gp_rewrite_to_temp_tables does not support recursive CTEs."
            )

        for cte in list(with_expression.expressions):
            if not isinstance(cte, exp.CTE) or not isinstance(cte.this, exp.Query):
                raise ValueError(
                    "gp_rewrite_to_temp_tables only supports SELECT CTEs."
                )
            alias = cte.args.get("alias")
            if _table_alias_has_columns(alias):
                raise ValueError(
                    "gp_rewrite_to_temp_tables does not support CTE column aliases."
                )
            name = cte.alias_or_name
            self._reserve_temp_name(name, label="CTE alias")
            cte_query = cte.this.copy()
            self._rewrite_query(cte_query)
            self.temp_tables.append(_GpTempTable(name=name, query=cte_query))

        query.set(with_arg, None)

    def _rewrite_expression_children(
        self,
        expression: exp.Expression,
        *,
        skipped_args: set[str] | None = None,
    ) -> None:
        skipped_args = skipped_args or set()
        for key, value in list(expression.args.items()):
            if key in skipped_args:
                continue
            if isinstance(expression, exp.Query) and key == _with_arg_name():
                continue
            if isinstance(value, list):
                for child in list(value):
                    if isinstance(child, exp.Expression):
                        self._rewrite_child_expression(child)
                continue
            if isinstance(value, exp.Expression):
                self._rewrite_child_expression(value)

    def _rewrite_child_expression(self, child: exp.Expression) -> None:
        if isinstance(child, exp.Subquery) and isinstance(child.this, exp.Query):
            self._materialize_subquery(child)
            return
        if isinstance(child, exp.Exists) and isinstance(child.this, exp.Query):
            self._materialize_exists(child)
            return
        if isinstance(child, exp.Query):
            self._materialize_direct_query(child)
            return
        self._rewrite_expression_children(child)

    def _materialize_subquery(self, subquery: exp.Subquery) -> None:
        query = subquery.this.copy()
        is_derived = isinstance(subquery.parent, (exp.From, exp.Join))
        if is_derived:
            alias = subquery.args.get("alias")
            if _table_alias_has_columns(alias):
                raise ValueError(
                    "gp_rewrite_to_temp_tables does not support derived-table "
                    "column aliases."
                )
            name = subquery.alias_or_name
            self._reserve_temp_name(name, label="derived-table alias")
            self._rewrite_query(query)
            self.temp_tables.append(_GpTempTable(name=name, query=query))
            subquery.replace(exp.Table(this=exp.to_identifier(name)))
            return

        name = self._next_generated_temp_name()
        self._rewrite_query(query)
        self.temp_tables.append(_GpTempTable(name=name, query=query))
        subquery.set("this", _temp_reference_select(name))

    def _materialize_exists(self, exists: exp.Exists) -> None:
        query = exists.this.copy()
        name = self._next_generated_temp_name()
        self._rewrite_query(query)
        self.temp_tables.append(_GpTempTable(name=name, query=query))
        exists.set("this", _temp_reference_select(name))

    def _materialize_direct_query(self, child_query: exp.Query) -> None:
        query = child_query.copy()
        name = self._next_generated_temp_name()
        self._rewrite_query(query)
        self.temp_tables.append(_GpTempTable(name=name, query=query))
        child_query.replace(_temp_reference_select(name))

    def _reserve_temp_name(self, name: str, *, label: str) -> None:
        _validate_temp_table_name(name, label=label)
        key = name.casefold()
        if key in self._used_temp_names:
            raise ValueError(
                "gp_rewrite_to_temp_tables found duplicate temp table name "
                f"{name!r}."
            )
        self._used_temp_names.add(key)

    def _next_generated_temp_name(self) -> str:
        while True:
            name = f"{self.temp_prefix}_{self._next_generated_index}"
            self._next_generated_index += 1
            if name.casefold() not in self._used_temp_names:
                self._reserve_temp_name(name, label="generated temp table name")
                return name

    def _temp_name_keys(self) -> set[str]:
        return {temp_table.name.casefold() for temp_table in self.temp_tables}

    def _assert_query_is_uncorrelated(self, query: exp.Query) -> None:
        for select in query.find_all(exp.Select):
            local_names = _local_select_source_names(select)
            for column in select.find_all(exp.Column):
                qualifier = column.table
                if not qualifier:
                    continue
                if qualifier.casefold() not in local_names:
                    raise ValueError(
                        "gp_rewrite_to_temp_tables does not support correlated "
                        "subqueries."
                    )

    def _distributed_columns(
        self,
        temp_name: str,
        *,
        consumer_expressions: list[exp.Expression],
    ) -> list[str]:
        columns: list[str] = []
        seen: set[str] = set()
        for consumer_expression in consumer_expressions:
            consumer_names = _temp_consumer_names(
                consumer_expression,
                temp_name=temp_name,
            )
            if not consumer_names:
                continue
            for join in consumer_expression.find_all(exp.Join):
                on_expression = join.args.get("on")
                if on_expression is None:
                    continue
                for condition in _flatten_and(on_expression):
                    if not isinstance(condition, exp.EQ):
                        continue
                    column_name = _join_column_for_temp(
                        condition,
                        consumer_names=consumer_names,
                        dialect=self.dialect,
                    )
                    if column_name is None:
                        continue
                    key = column_name.casefold()
                    if key not in seen:
                        seen.add(key)
                        columns.append(column_name)
        return columns

    def _render_temp_table_block(
        self,
        temp_table: _GpTempTable,
        *,
        distributed_columns: list[str],
    ) -> str:
        rendered_query = _render_expression(
            cast("exp.Expression", temp_table.query),
            dialect=self.dialect,
            leading_commas=False,
            where_anchor=None,
            group_by_format=self.group_by_format,
            order_by_format=self.order_by_format,
            keyword_case=self.keyword_case,
            indent=self.indent,
            cte_blank_lines=self.cte_blank_lines,
            union_blank_lines=self.union_blank_lines,
            operation="gp_rewrite_to_temp_tables",
        )
        indented_query = _indent_sql(rendered_query, self.indent)
        if distributed_columns:
            distribution = (
                f"{self._keyword('distributed')} {self._keyword('by')} "
                f"({', '.join(distributed_columns)})"
            )
        else:
            distribution = (
                f"{self._keyword('distributed')} {self._keyword('randomly')}"
            )
        return "\n".join(
            [
                (
                    f"{self._keyword('drop')} {self._keyword('table')} "
                    f"{self._keyword('if')} {self._keyword('exists')} "
                    f"{temp_table.name};"
                ),
                (
                    f"{self._keyword('create')} {self._keyword('temporary')} "
                    f"{self._keyword('table')} {temp_table.name} "
                    f"{self._keyword('as')} ("
                ),
                indented_query,
                f") {distribution};",
                f"{self._keyword('analyze')} {temp_table.name};",
            ]
        )

    def _keyword(self, keyword: str) -> str:
        if self.keyword_case == "upper":
            return keyword.upper()
        if self.keyword_case == "capitalize":
            return keyword.capitalize()
        return keyword.lower()


def _table_alias_has_columns(alias: exp.Expression | None) -> bool:
    return isinstance(alias, exp.TableAlias) and bool(alias.args.get("columns"))


def _temp_reference_select(name: str) -> exp.Select:
    return exp.select("*").from_(name)


def _is_temp_reference_select(select: exp.Select, *, temp_names: set[str]) -> bool:
    if select.args.get(_with_arg_name()) is not None:
        return False
    if select.args.get("joins"):
        return False
    unsupported_args = {
        "where",
        "group",
        "having",
        "qualify",
        "order",
        "limit",
        "offset",
        "sample",
    }
    if any(select.args.get(arg_name) is not None for arg_name in unsupported_args):
        return False
    expressions = select.expressions
    if len(expressions) != 1 or not isinstance(expressions[0], exp.Star):
        return False
    from_expression = select.args.get(_from_arg_name())
    if from_expression is None or not isinstance(from_expression.this, exp.Table):
        return False
    return _table_name_key(from_expression.this) in temp_names


def _local_select_source_names(select: exp.Select) -> set[str]:
    names: set[str] = set()
    from_expression = select.args.get(_from_arg_name())
    if from_expression is not None:
        names.update(_relation_source_names(from_expression.this))
    for join in select.args.get("joins") or []:
        names.update(_relation_source_names(join.this))
    return names


def _relation_source_names(relation: exp.Expression | None) -> set[str]:
    if relation is None:
        return set()
    if isinstance(relation, exp.Table):
        return _table_reference_names(relation)
    if isinstance(relation, exp.Subquery):
        alias = relation.alias_or_name
        return {alias.casefold()} if alias else set()
    return set()


def _temp_consumer_names(
    expression: exp.Expression,
    *,
    temp_name: str,
) -> set[str]:
    names: set[str] = set()
    temp_key = temp_name.casefold()
    for table in expression.find_all(exp.Table):
        if _table_name_key(table) != temp_key:
            continue
        names.update(_table_reference_names(table))
    return names


def _table_reference_names(table: exp.Table) -> set[str]:
    names = {_table_name_key(table)}
    alias = table.alias
    if alias:
        names.add(alias.casefold())
    return names


def _table_name_key(table: exp.Table) -> str:
    return table.name.casefold()


def _join_column_for_temp(
    condition: exp.EQ,
    *,
    consumer_names: set[str],
    dialect: str | None,
) -> str | None:
    left_column = _column_name_for_temp(
        condition.this,
        consumer_names=consumer_names,
        dialect=dialect,
    )
    right_column = _column_name_for_temp(
        condition.expression,
        consumer_names=consumer_names,
        dialect=dialect,
    )
    if left_column and not right_column:
        return left_column
    if right_column and not left_column:
        return right_column
    return None


def _column_name_for_temp(
    expression: exp.Expression,
    *,
    consumer_names: set[str],
    dialect: str | None,
) -> str | None:
    if not isinstance(expression, exp.Column):
        return None
    if not expression.table or expression.table.casefold() not in consumer_names:
        return None
    column_expression = expression.this
    try:
        return column_expression.sql(dialect=dialect)
    except SqlglotError as exc:
        raise ValueError(
            "gp_rewrite_to_temp_tables could not render distribution column: "
            f"{exc}"
        ) from exc


def _indent_sql(sql: str, indent: int) -> str:
    prefix = " " * indent
    return "\n".join(f"{prefix}{line}" for line in sql.splitlines())


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


def _normalize_join_condition_layout(sql: str) -> str:
    normalized_lines: list[str] = []
    for line in sql.splitlines():
        stripped = line.lstrip(" ")
        previous_line = _previous_non_empty_line(
            normalized_lines,
            len(normalized_lines),
        )

        if _starts_join_condition(stripped):
            if previous_line is None or not _is_join_line(previous_line.strip()):
                normalized_lines.append(line)
                continue

            join_prefix = previous_line[
                : len(previous_line) - len(previous_line.lstrip(" "))
            ]
            condition_prefix = f"{join_prefix}  "
            and_prefix = f"{join_prefix} "
            for condition_line in _split_join_condition_line(stripped):
                prefix = (
                    and_prefix
                    if _starts_join_and_condition(condition_line)
                    else condition_prefix
                )
                normalized_lines.append(f"{prefix}{condition_line}")
            continue

        if not _starts_join_and_condition(stripped):
            normalized_lines.append(line)
            continue
        if previous_line is None:
            normalized_lines.append(line)
            continue
        previous_stripped = previous_line.lstrip(" ")
        if not (
            _starts_join_condition(previous_stripped)
            or _starts_join_and_condition(previous_stripped)
        ):
            normalized_lines.append(line)
            continue

        previous_prefix = previous_line[
            : len(previous_line) - len(previous_line.lstrip(" "))
        ]
        if _starts_join_condition(previous_stripped):
            previous_prefix = previous_prefix[:-1]
        normalized_lines.append(f"{previous_prefix}{stripped}")
    return "\n".join(normalized_lines)


def _normalize_cte_separator_layout(sql: str, cte_blank_lines: int) -> str:
    normalized_lines: list[str] = []
    for line in sql.splitlines():
        separator = re.match(
            r"^(?P<indent>\s*)\),\s+(?P<next_cte>.+\bAS\s+\()$",
            line,
            flags=re.IGNORECASE,
        )
        if separator is None:
            normalized_lines.append(line)
            continue

        indent_prefix = separator.group("indent")
        normalized_lines.append(f"{indent_prefix}),")
        normalized_lines.extend("" for _ in range(cte_blank_lines))
        normalized_lines.append(f"{indent_prefix}{separator.group('next_cte')}")
    return "\n".join(normalized_lines)


def _normalize_union_separator_layout(sql: str, union_blank_lines: int) -> str:
    normalized_lines: list[str] = []
    for line in sql.splitlines():
        if line.strip().upper() not in {"UNION", "UNION ALL"}:
            normalized_lines.append(line)
            continue

        while normalized_lines and not normalized_lines[-1].strip():
            normalized_lines.pop()
        normalized_lines.extend("" for _ in range(union_blank_lines))
        normalized_lines.append(line)
    return "\n".join(normalized_lines)


def _compact_single_star_select_layout(sql: str) -> str:
    lines = sql.splitlines()
    compacted_lines: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if index + 1 >= len(lines) or not _is_single_star_select_line(line):
            compacted_lines.append(line)
            index += 1
            continue

        star_line = lines[index + 1]
        stripped_star = star_line.strip()
        if not _is_single_star_projection(stripped_star):
            compacted_lines.append(line)
            index += 1
            continue

        prefix = line[: len(line) - len(line.lstrip(" "))]
        compacted_lines.append(f"{prefix}{line.strip()} {stripped_star}")
        index += 2
    return "\n".join(compacted_lines)


def _is_single_star_select_line(line: str) -> bool:
    stripped = line.strip().upper()
    return stripped in {"SELECT", "SELECT DISTINCT"}


def _is_single_star_projection(projection_sql: str) -> bool:
    return (
        projection_sql == "*"
        or re.match(r"^[^\s,()]+\.\*$", projection_sql) is not None
    )


def _split_join_condition_line(stripped_line: str) -> list[str]:
    if not stripped_line.upper().startswith("ON "):
        return [stripped_line]

    condition_text = stripped_line[len("ON ") :].strip()
    if not condition_text:
        return [stripped_line]

    conditions = _split_top_level_and_conditions(condition_text)
    if len(conditions) <= 1:
        return [stripped_line]
    return [
        f"{stripped_line[:2]} {conditions[0]}",
        *(f"AND {condition}" for condition in conditions[1:]),
    ]


def _starts_join_condition(stripped_line: str) -> bool:
    upper_line = stripped_line.upper()
    return upper_line == "ON" or upper_line.startswith("ON ")


def _starts_join_and_condition(stripped_line: str) -> bool:
    upper_line = stripped_line.upper()
    return upper_line == "AND" or upper_line.startswith("AND ")


def _previous_non_empty_line(lines: list[str], index: int) -> str | None:
    previous_index = index - 1
    while previous_index >= 0:
        if lines[previous_index].strip():
            return lines[previous_index]
        previous_index -= 1
    return None


def _is_join_line(stripped_line: str) -> bool:
    return re.match(
        r"^(?:"
        r"(?:LEFT|RIGHT|FULL)(?:\s+OUTER)?|"
        r"INNER|CROSS|SEMI|ANTI|ASOF|NATURAL"
        r")?\s*JOIN\b",
        stripped_line,
        flags=re.IGNORECASE,
    ) is not None


def _normalize_where_anchor_layout(sql: str, where_anchor: str) -> str:
    anchor_sql = "1 = 1" if where_anchor == "1=1" else "TRUE"
    display_anchor = "1=1" if where_anchor == "1=1" else "TRUE"
    lines = sql.splitlines()
    normalized_lines: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.strip().upper() != "WHERE" or index + 1 >= len(lines):
            normalized_lines.append(line)
            index += 1
            continue

        condition_line = lines[index + 1]
        condition_text = condition_line.strip()
        if condition_text != anchor_sql and not condition_text.startswith(
            f"{anchor_sql} AND "
        ):
            normalized_lines.append(line)
            index += 1
            continue

        prefix = line[: len(line) - len(line.lstrip(" "))]
        and_prefix = " " * (len(prefix) + len("WHERE "))
        normalized_lines.append(f"{prefix}WHERE {display_anchor}")
        for condition in _split_anchor_and_conditions(condition_text, anchor_sql):
            normalized_lines.append(f"{and_prefix}AND {condition}")
        index += 2
    return "\n".join(normalized_lines)


def _split_anchor_and_conditions(condition_text: str, anchor_sql: str) -> list[str]:
    remaining = condition_text[len(anchor_sql) :].strip()
    if not remaining:
        return []
    if not remaining.startswith("AND "):
        return [remaining]
    return _split_top_level_and_conditions(remaining[len("AND ") :])


def _split_top_level_and_conditions(condition_text: str) -> list[str]:
    conditions: list[str] = []
    start = 0
    depth = 0
    quote_end: str | None = None
    index = 0
    while index < len(condition_text):
        character = condition_text[index]
        if quote_end is not None:
            if character == quote_end:
                if quote_end == "'" and _is_escaped_single_quote(
                    condition_text,
                    index,
                ):
                    index += 2
                    continue
                quote_end = None
            index += 1
            continue

        if character in {"'", '"', "`"}:
            quote_end = character
            index += 1
            continue
        if character == "[":
            quote_end = "]"
            index += 1
            continue
        if character == "(":
            depth += 1
        elif character == ")" and depth > 0:
            depth -= 1
        elif depth == 0 and condition_text[index : index + 5].upper() == " AND ":
            conditions.append(condition_text[start:index].strip())
            index += 5
            start = index
            continue
        index += 1

    conditions.append(condition_text[start:].strip())
    return [condition for condition in conditions if condition]


def _is_escaped_single_quote(text: str, index: int) -> bool:
    return index + 1 < len(text) and text[index + 1] == "'"


__all__ = ["format_sql", "gp_rewrite_to_temp_tables", "rewrite_with_ctes"]
