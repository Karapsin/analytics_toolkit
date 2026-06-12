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


@dataclass(frozen=True)
class _GpTempTable:
    name: str
    query: exp.Select


def format_sql(
    sql: str,
    *,
    dialect: str | None = None,
    leading_commas: bool = False,
    where_anchor: str = "1=1",
    keyword_case: str = "lower",
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
        where_anchor=normalized_where_anchor,
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
    keyword_case: str = "lower",
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
        where_anchor=None,
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


def gp_rewrite_to_temp_tables(
    sql: str,
    *,
    dialect: str | None = "postgres",
    temp_prefix: str = "tmp",
    keyword_case: str = "lower",
    indent: int = 4,
) -> str:
    """Rewrite SELECT CTEs and subqueries into Greenplum temp-table SQL."""

    normalized_dialect = _validate_dialect(dialect)
    normalized_keyword_case = _validate_keyword_case(keyword_case)
    normalized_indent = _validate_indent(indent)
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
        keyword_case=normalized_keyword_case,
        indent=normalized_indent,
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
        keyword_case=normalized_keyword_case,
        indent=normalized_indent,
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
    if where_anchor in {"1=1", "true"}:
        rendered = _normalize_where_anchor_layout(rendered, where_anchor)
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
        keyword_case: str,
        indent: int,
    ) -> None:
        self.dialect = dialect
        self.temp_prefix = temp_prefix
        self.keyword_case = keyword_case
        self.indent = indent
        self.temp_tables: list[_GpTempTable] = []
        self._used_temp_names: set[str] = set()
        self._next_generated_index = 1

    def rewrite_select(self, select: exp.Select) -> None:
        self._materialize_with_clause(select)
        self._rewrite_expression_children(select)

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
        for subquery in expression.find_all(exp.Subquery):
            if isinstance(subquery.this, exp.Select) and not _is_temp_reference_select(
                subquery.this,
                temp_names=temp_names,
            ):
                raise ValueError(
                    "gp_rewrite_to_temp_tables could not confidently rewrite all "
                    "SELECT subqueries."
                )
        for exists in expression.find_all(exp.Exists):
            if isinstance(exists.this, exp.Select) and not _is_temp_reference_select(
                exists.this,
                temp_names=temp_names,
            ):
                raise ValueError(
                    "gp_rewrite_to_temp_tables could not confidently rewrite all "
                    "predicate SELECT subqueries."
                )
        for temp_table in self.temp_tables:
            self._assert_select_is_uncorrelated(temp_table.query)

    def render_temp_table_blocks(self, final_expression: exp.Select) -> list[str]:
        consumer_expressions = [
            temp_table.query for temp_table in self.temp_tables
        ] + [final_expression]
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

    def _materialize_with_clause(self, select: exp.Select) -> None:
        with_arg = _with_arg_name()
        with_expression = select.args.get(with_arg)
        if with_expression is None:
            return
        if with_expression.args.get("recursive"):
            raise ValueError(
                "gp_rewrite_to_temp_tables does not support recursive CTEs."
            )

        for cte in list(with_expression.expressions):
            if not isinstance(cte, exp.CTE) or not isinstance(cte.this, exp.Select):
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
            query = cte.this.copy()
            self.rewrite_select(query)
            self.temp_tables.append(_GpTempTable(name=name, query=query))

        select.set(with_arg, None)

    def _rewrite_expression_children(self, expression: exp.Expression) -> None:
        for key, value in list(expression.args.items()):
            if isinstance(expression, exp.Select) and key == _with_arg_name():
                continue
            if isinstance(value, list):
                for child in list(value):
                    if isinstance(child, exp.Expression):
                        self._rewrite_child_expression(child)
                continue
            if isinstance(value, exp.Expression):
                self._rewrite_child_expression(value)

    def _rewrite_child_expression(self, child: exp.Expression) -> None:
        if isinstance(child, exp.Subquery) and isinstance(child.this, exp.Select):
            self._materialize_subquery(child)
            return
        if isinstance(child, exp.Exists) and isinstance(child.this, exp.Select):
            self._materialize_exists(child)
            return
        if isinstance(child, exp.Select):
            self._materialize_direct_select(child)
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
            self.rewrite_select(query)
            self.temp_tables.append(_GpTempTable(name=name, query=query))
            subquery.replace(exp.Table(this=exp.to_identifier(name)))
            return

        name = self._next_generated_temp_name()
        self.rewrite_select(query)
        self.temp_tables.append(_GpTempTable(name=name, query=query))
        subquery.set("this", _temp_reference_select(name))

    def _materialize_exists(self, exists: exp.Exists) -> None:
        query = exists.this.copy()
        name = self._next_generated_temp_name()
        self.rewrite_select(query)
        self.temp_tables.append(_GpTempTable(name=name, query=query))
        exists.set("this", _temp_reference_select(name))

    def _materialize_direct_select(self, select: exp.Select) -> None:
        query = select.copy()
        name = self._next_generated_temp_name()
        self.rewrite_select(query)
        self.temp_tables.append(_GpTempTable(name=name, query=query))
        select.replace(_temp_reference_select(name))

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

    def _assert_select_is_uncorrelated(self, select: exp.Select) -> None:
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
            temp_table.query,
            dialect=self.dialect,
            leading_commas=False,
            where_anchor=None,
            keyword_case=self.keyword_case,
            indent=self.indent,
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
                "",
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
