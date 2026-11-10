"""Static identity parsing for supported ClickHouse cluster table functions."""

from __future__ import annotations

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError

from analytics_toolkit.sql.connection.errors import ClickHouseClusterTopologyError

from .dialect import ClickHouseDialect

_SEPARATE_COMPONENT_ARGS = 3
_QUALIFIED_NAME_ARGS = 2


def static_name(value: exp.Expression) -> str | None:
    if isinstance(value, exp.Column) and not value.table and not value.db:
        value = value.this
    if isinstance(value, exp.Identifier) or (isinstance(value, exp.Literal) and value.is_string):
        return str(value.this) or None
    return None


def cluster_arguments(
    function: exp.Anonymous, default_database: str | None
) -> tuple[str, str, str, list[exp.Expression]]:
    arguments = list(function.expressions)
    cluster = static_name(arguments[0]) if arguments else None
    database = relation = None
    if len(arguments) >= _SEPARATE_COMPONENT_ARGS:
        database, relation = static_name(arguments[1]), static_name(arguments[2])
        trailing = arguments[3:]
    elif len(arguments) == _QUALIFIED_NAME_ARGS:
        value = arguments[1]
        if isinstance(value, exp.Literal) and not value.is_string:
            message = "Cluster source identity must be static identifiers or text."
            raise ClickHouseClusterTopologyError(message)
        text = (
            str(value.this)
            if isinstance(value, exp.Literal) and value.is_string
            else value.sql(dialect="clickhouse")
        )
        try:
            table = parse_one(text, into=exp.Table, read=ClickHouseDialect)
        except SqlglotError as exc:
            message = "Cluster source identity must be static."
            raise ClickHouseClusterTopologyError(message) from exc
        if (
            isinstance(table, exp.Table)
            and isinstance(table.this, exp.Identifier)
            and not table.catalog
        ):
            database, relation = table.db or default_database, table.name
        trailing = []
    else:
        trailing = []
    if not cluster or not database or not relation:
        message = "Cluster source arguments must be static identifiers."
        raise ClickHouseClusterTopologyError(message)
    return cluster, database, relation, trailing


def canonicalize_cluster_functions(statement: exp.Expression, default_database: str | None) -> bool:
    """Expand static two-argument sources while retaining the caller's replica intent."""
    changed = False
    for table in statement.find_all(exp.Table):
        function = table.this
        if (
            not isinstance(function, exp.Anonymous)
            or function.name.lower()
            not in {
                "cluster",
                "clusterallreplicas",
            }
            or len(function.expressions) != _QUALIFIED_NAME_ARGS
        ):
            continue
        cluster, database, relation, trailing = cluster_arguments(function, default_database)
        function.set(
            "expressions",
            [
                exp.Literal.string(cluster),
                exp.Literal.string(database),
                exp.Literal.string(relation),
                *trailing,
            ],
        )
        changed = True
    return changed


def cluster_names(statement: exp.Expr) -> list[str]:
    names = [str(node.this.this) for node in statement.find_all(exp.OnCluster)]
    for table in statement.find_all(exp.Table):
        function = table.this
        if (
            isinstance(function, exp.Anonymous)
            and function.name.lower()
            in {
                "cluster",
                "clusterallreplicas",
            }
            and function.expressions
        ):
            arguments = function.expressions
            if len(arguments) == _QUALIFIED_NAME_ARGS:
                cluster = static_name(arguments[0])
                if cluster is None:
                    msg = "Outage policies require static cluster arguments."
                    raise ClickHouseClusterTopologyError(msg)
                names.append(cluster)
            else:
                cluster, _database, _table, _trailing = cluster_arguments(function, None)
                names.append(cluster)
    return list(dict.fromkeys(names))
