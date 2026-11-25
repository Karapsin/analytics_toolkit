"""Evaluate the bounded GROUP BY and HAVING SQL used by outage regressions."""

from __future__ import annotations

from typing import Any

from analytics_toolkit.sql.backends.ch.routing import _RoutingClickHouse
from sqlglot import exp, parse_one


def _accepts_group(having: Any, values: Any) -> bool:
    if having is None:
        return True
    condition = having.this
    assert isinstance(condition, exp.GT)
    assert isinstance(condition.this, exp.Count)
    assert isinstance(condition.expression, exp.Literal)
    return len(values) > int(condition.expression.this)


def count_result(statement: Any, rows: Any, obj: Any, result: Any) -> Any:
    group = statement.args.get("group")
    if group is None:
        return result([(len(rows),)])
    assert obj is not None
    columns = list(parse_one(obj.ddl, read=_RoutingClickHouse).find_all(exp.ColumnDef))
    fields = [column.name for column in columns]
    types = {column.name: column.args["kind"].sql(dialect="clickhouse") for column in columns}
    expressions = [
        item.this if isinstance(item, exp.Alias) else item for item in statement.expressions
    ]
    assert all(isinstance(item, exp.Column) for item in group.expressions)
    grouped_fields = [item.name for item in group.expressions]
    indexes = [fields.index(name) for name in grouped_fields]
    groups: dict[tuple[Any, ...], list[Any]] = {}
    for row in rows:
        groups.setdefault(tuple(row[index] for index in indexes), []).append(row)
    projected = []
    having = statement.args.get("having")
    for key, values in groups.items():
        if not _accepts_group(having, values):
            continue
        record = dict(zip(grouped_fields, key))
        aggregate = []
        for item in expressions:
            if isinstance(item, exp.Column):
                aggregate.append(record[item.name])
            elif isinstance(item, exp.Literal):
                aggregate.append(int(item.this))
            else:
                assert isinstance(item, exp.Count)
                if isinstance(item.this, exp.Column):
                    index = fields.index(item.this.name)
                    aggregate.append(sum(row[index] is not None for row in values))
                else:
                    aggregate.append(len(values))
        projected.append(tuple(aggregate))
    limit = statement.args.get("limit")
    if limit is not None:
        projected = projected[: int(limit.expression.this)]
    return result(
        projected,
        [item.alias_or_name for item in statement.expressions],
        [types[item.name] if isinstance(item, exp.Column) else "UInt64" for item in expressions],
    )
