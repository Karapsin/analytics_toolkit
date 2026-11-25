"""Static identity parsing for supported ClickHouse cluster table functions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlglot import exp
from sqlglot.errors import SqlglotError

from analytics_toolkit.sql.connection.errors import ClickHouseClusterTopologyError

from .dialect import ClickHouseDialect

if TYPE_CHECKING:
    from collections.abc import Callable

_SEPARATE_COMPONENT_ARGS = 3
_QUALIFIED_NAME_ARGS = 2


def function_insert_target(statement: exp.Expr) -> tuple[str, str, str, str] | None:
    """Resolve a static insertion function while preserving compatibility shape."""
    target = statement.this.this if isinstance(statement.this, exp.Schema) else statement.this
    function = target.this if isinstance(target, exp.Table) else target
    if not isinstance(function, exp.Anonymous):
        return None
    if function.name.lower() not in {"cluster", "clusterallreplicas"}:
        message = "Outage policies require a named or cluster-function insertion target."
        raise ClickHouseClusterTopologyError(message)
    cluster, database, relation, trailing = cluster_arguments(function, None)
    key = trailing[0].sql(dialect=ClickHouseDialect) if trailing else "rand()"
    return cluster, database, relation, key


def insert_coverage_intent(statement: exp.Expression, *, automatic_routing: bool) -> str | None:
    """Preserve an unrouted caller's explicit physical-replica write scope."""
    target = statement.this
    if isinstance(target, exp.Schema):
        target = target.this
    if isinstance(target, exp.Table):
        target = target.this
    return (
        "physical_hosts"
        if not automatic_routing
        and isinstance(target, exp.Anonymous)
        and target.name.lower() == "clusterallreplicas"
        else None
    )


def static_name(value: exp.Expression) -> str | None:
    if isinstance(value, exp.Column) and not value.table and not value.db:
        value = value.this
    if isinstance(value, exp.Identifier) or (isinstance(value, exp.Literal) and value.is_string):
        return str(value.this) or None
    return None


def cluster_arguments(
    function: exp.Anonymous,
    default_database: str | None,
    *,
    database_resolver: Callable[[], str] | None = None,
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
        if isinstance(value, exp.Literal) and value.is_string:
            text = str(value.this)
        else:
            identifier = value.copy()
            for node in identifier.walk():
                node.comments = None
            text = identifier.sql(dialect=ClickHouseDialect)
        try:
            parsed = ClickHouseDialect().parse_into(exp.Table, text)
            table = parsed[0] if len(parsed) == 1 else None
        except SqlglotError as exc:
            message = "Cluster source identity must be static."
            raise ClickHouseClusterTopologyError(message) from exc
        if (
            isinstance(table, exp.Table)
            and isinstance(table.this, exp.Identifier)
            and not table.catalog
            and not any(
                value for key, value in table.args.items() if key not in {"this", "db", "catalog"}
            )
            and not any(node.comments for node in table.walk())
        ):
            database = table.db or (
                database_resolver() if database_resolver is not None else default_database
            )
            relation = table.name
        trailing = []
    else:
        trailing = []
    if not cluster or not database or not relation:
        message = "Cluster source arguments must be static identifiers."
        raise ClickHouseClusterTopologyError(message)
    return cluster, database, relation, trailing


def canonicalize_cluster_functions(
    statement: exp.Expression,
    default_database: str | None,
    *,
    database_resolver: Callable[[], str] | None = None,
) -> bool:
    """Expand static two-argument sources while retaining the caller's replica intent."""
    changed = False
    functions = [table.this for table in statement.find_all(exp.Table)]
    if isinstance(statement, exp.Insert):
        target = statement.this
        if isinstance(target, exp.Schema):
            target = target.this
        # INSERT INTO FUNCTION uses an Anonymous target rather than Table.
        if isinstance(target, exp.Anonymous):
            functions.append(target)
    for function in functions:
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
        cluster, database, relation, trailing = cluster_arguments(
            function, default_database, database_resolver=database_resolver
        )
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
