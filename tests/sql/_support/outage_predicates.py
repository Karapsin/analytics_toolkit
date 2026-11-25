"""Apply native keyed equality predicates before fake projection and counting."""

from __future__ import annotations

from typing import Any

from analytics_toolkit.sql.backends.ch.routing import _RoutingClickHouse
from sqlglot import exp, parse_one


def filter_rows(statement: Any, rows: Any, obj: Any) -> Any:
    where = statement.args.get("where")
    if where is None:
        where = next(
            (
                query.args["where"]
                for query in statement.find_all(exp.Select)
                if query.args.get("where") is not None
            ),
            None,
        )
    if where is None or obj is None:
        return rows
    condition = where.this
    while isinstance(condition, exp.Paren):
        condition = condition.this
    if not isinstance(condition, exp.EQ):
        return rows
    column, value = condition.this, condition.expression
    while isinstance(column, exp.Paren):
        column = column.this
    if not isinstance(column, exp.Column) or not isinstance(value, exp.Literal):
        return rows
    columns = list(parse_one(obj.ddl, read=_RoutingClickHouse).find_all(exp.ColumnDef))
    index = [item.name for item in columns].index(column.name)
    expected = value.this if value.is_string else int(value.this)
    return [row for row in rows if row[index] == expected]
