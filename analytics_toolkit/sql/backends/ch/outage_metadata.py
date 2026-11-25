"""Route bounded metadata commands through their verified selected scope."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlglot import exp, parse_one

from analytics_toolkit.sql.connection.errors import ClickHouseClusterTopologyError

from .dialect import sql_string_literal
from .routing import _LOCAL_SQL_EXECUTION, _RoutingClickHouse

if TYPE_CHECKING:
    from .outage_session import OutageSession


def metadata_scope_sql(  # noqa: C901, PLR0912 - metadata commands share one verified scope
    config: Any,
    session: OutageSession,
    statement: exp.Expr,
    sql: str,
    target: tuple[str, str] | None,
) -> str:
    from .outage_ownership import owned_local  # noqa: PLC0415 - session/ownership cycle.

    if not _LOCAL_SQL_EXECUTION.get():
        return sql
    routing = config.cluster_routing
    default = routing.cluster if routing is not None else None

    if (
        default
        and isinstance(statement, exp.Describe)
        and target
        and not owned_local(session, target)
    ):
        route = session.resolve(cluster=default, database=target[0], table=target[1])
        return (
            f"DESCRIBE TABLE cluster({sql_string_literal(session.scope(default).cluster)}, "
            f"{sql_string_literal(route.database)}, {sql_string_literal(route.table)})"
        )
    if not isinstance(statement, exp.Select):
        return sql
    for table in statement.find_all(exp.Table):
        if table.db == "system" and table.name == "clusters" and session.policy == "healthy_shards":
            where = statement.args.get("where")
            if where is None:
                continue
            for equality in where.find_all(exp.EQ):
                if equality.left.name == "cluster" and isinstance(equality.right, exp.Literal):
                    scope = session.scope(str(equality.right.this))
                    positions = ", ".join(
                        f"({host.shard}, {host.replica})" for host in scope.reachable
                    )
                    statement.where(
                        f"(shard_num, replica_num) IN ({positions})", append=True, copy=False
                    )
                    return statement.sql(dialect=_RoutingClickHouse)
        if table.db == "system" and table.name in {"tables", "columns"} and default:
            where = statement.args.get("where")
            if where is None:
                continue
            values = {
                equality.left.name: str(equality.right.this)
                for equality in where.find_all(exp.EQ)
                if isinstance(equality.right, exp.Literal)
            }
            name = values.get("table") or values.get("name")
            database = values.get("database") or config.database
            if not name or not database:
                continue
            if owned_local(session, (database, name)):
                continue
            scope = session.scope(default)
            objects = scope.objects(database, name)
            if not objects:
                continue
            session.resolve(cluster=default, database=database, table=name)
            replacement = parse_one(
                f"SELECT * FROM cluster({sql_string_literal(scope.cluster)}, "  # noqa: S608 - escaped literals or parsed SQL identifiers
                f"system, {table.name})",
                read=_RoutingClickHouse,
            ).find(exp.Table)
            if replacement is None:
                msg = "Cannot resolve metadata table function."
                raise ClickHouseClusterTopologyError(msg)
            table.replace(replacement.copy())
            statement.where(f"_shard_num = {objects[0].host.shard}", append=True, copy=False)
            return statement.sql(dialect=_RoutingClickHouse)
    return sql
