"""Model the native ordinal snapshot projection over the selected SQL rows."""

from __future__ import annotations

from sqlglot import exp


def is_zero_limit_query(tree):
    limit = tree.args.get("limit") if isinstance(tree, exp.Select) else None
    return (
        limit is not None
        and isinstance(limit.expression, exp.Literal)
        and not limit.expression.is_string
        and limit.expression.this == "0"
    )


def expand_snapshot_stars(database, tree):
    if not isinstance(tree, exp.Select) or len(tree.expressions) < 2:
        return tree
    if not any(isinstance(item, exp.Star) for item in tree.expressions):
        return tree
    source = tree.args["from_"].this
    names = (
        database.query_names(source)
        if isinstance(source, exp.Subquery)
        else list(database.table(source).schema)
    )
    selected = tree.copy()
    selected.set(
        "expressions",
        [
            column
            for item in selected.expressions
            for column in (
                [exp.column(name) for name in names] if isinstance(item, exp.Star) else [item]
            )
        ],
    )
    return selected


def ordinal_snapshot_rows(database, tree, outer):
    if not isinstance(tree, exp.Select):
        return None
    positions = []
    for position, projection in enumerate(tree.expressions):
        window = projection.unalias()
        if not isinstance(window, exp.Window):
            continue
        assert isinstance(window.this, exp.RowNumber)
        assert window.args.get("order") is None
        assert all(
            isinstance(item, exp.Literal) and item.this == "0"
            for item in window.args.get("partition_by", [])
        )
        positions.append(position)
    if not positions:
        return None
    selected = tree.copy().transform(
        lambda node: exp.Literal.number(0) if isinstance(node, exp.Window) else node
    )
    rows, names = database.select(selected, outer)
    return [
        tuple(ordinal if position in positions else value for position, value in enumerate(row))
        for ordinal, row in enumerate(rows, 1)
    ], names
