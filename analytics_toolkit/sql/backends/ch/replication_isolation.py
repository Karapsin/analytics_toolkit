"""Prove off-side replicated storage cannot write into the selected live source."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlglot import exp, parse_one

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.ddl.creation_resources import CreatedTableResource, CreationScope

from .coordinator_database import resolve_current_database
from .dialect import ClickHouseDialect
from .outage_replication import _expand
from .reconfigure_ddl import distributed_table_parts, engine_name
from .reconfigure_facade_scope import cluster_table, inspect_facade_scope, read_rows

if TYPE_CHECKING:
    from .reconfigure_facade_scope import FacadeScope
    from .reconfigure_models import ChReconfiguration

_PAIR_FIELDS = 2
_MACRO_FIELDS = 3


def _scope(connection: Any, cluster: str | None) -> FacadeScope | None:
    return (
        inspect_facade_scope(connection, cluster, coordinator_required=False)
        if cluster is not None
        else None
    )


def _positions(scope: FacadeScope | None) -> set[int]:
    return set(scope.identities) if scope is not None else {1}


def _query(scope: FacadeScope | None, table: str, *columns: str) -> exp.Select:
    position = exp.column("_shard_num") if scope else exp.Literal.number(1).as_("position")
    source = cluster_table(scope.cluster, table) if scope else exp.to_table(f"system.{table}")
    return exp.select(position, *columns).from_(source)


def _named(query: exp.Select, table: str, relation_column: str) -> exp.Select:
    database, relation = distributed_table_parts(table)
    return query.where(
        exp.column("database").eq(exp.Literal.string(database)),
        exp.column(relation_column).eq(exp.Literal.string(relation)),
    )


def _complete_values(rows: list[Any], scope: FacadeScope | None) -> dict[int, str]:
    message = "Cannot verify complete replication storage metadata before replacement."
    if any(len(row) != _PAIR_FIELDS or not isinstance(row[1], str) for row in rows):
        raise InvalidSqlInputError(message)
    try:
        values = {int(position): value for position, value in rows}
    except (TypeError, ValueError) as exc:
        raise InvalidSqlInputError(message) from exc
    if len(values) != len(rows) or set(values) != _positions(scope):
        raise InvalidSqlInputError(message)
    return values


def _actual_source_paths(connection: Any, table: str, scope: FacadeScope | None) -> set[str]:
    query = _named(_query(scope, "tables", "engine"), table, "name")
    engines = _complete_values(
        read_rows(connection, query.sql(dialect=ClickHouseDialect), text_columns=(1,)), scope
    )
    replicated = [value.casefold().startswith("replicated") for value in engines.values()]
    if not any(replicated):
        return set()
    if not all(replicated):
        message = "Source replication storage engines differ across the selected scope."
        raise InvalidSqlInputError(message)
    query = _named(_query(scope, "replicas", "zookeeper_path"), table, "table")
    paths = _complete_values(
        read_rows(connection, query.sql(dialect=ClickHouseDialect), text_columns=(1,)), scope
    )
    return {_keeper_path(path) for path in paths.values()}


def _macro_values(connection: Any, scope: FacadeScope | None) -> dict[int, dict[str, str]]:
    query = _query(scope, "macros", "macro", "substitution")
    rows = read_rows(connection, query.sql(dialect=ClickHouseDialect), text_columns=(1, 2))
    values: dict[int, dict[str, str]] = {position: {} for position in _positions(scope)}
    message = "Cannot verify replacement replication path macros on every selected host."
    for row in rows:
        if len(row) != _MACRO_FIELDS or not all(isinstance(value, str) for value in row[1:]):
            raise InvalidSqlInputError(message)
        try:
            position = int(row[0])
        except (TypeError, ValueError) as exc:
            raise InvalidSqlInputError(message) from exc
        name, value = row[1:]
        if position not in values or not name or name in values[position]:
            raise InvalidSqlInputError(message)
        values[position][name] = value
    return values


def _path_templates(
    connection: Any, scope: FacadeScope | None, create: exp.Create
) -> dict[int, str]:
    prop = create.find(exp.EngineProperty)
    if prop is None:
        message = "Replacement replication engine metadata is missing."
        raise InvalidSqlInputError(message)
    arguments = list(prop.this.expressions)
    if len(arguments) >= _PAIR_FIELDS and all(
        isinstance(argument, exp.Literal) and argument.is_string for argument in arguments[:2]
    ):
        return dict.fromkeys(_positions(scope), str(arguments[0].this))
    if any(isinstance(argument, exp.Literal) and argument.is_string for argument in arguments[:2]):
        message = "Replacement replication path and replica name must be complete static strings."
        raise InvalidSqlInputError(message)
    query = _query(scope, "server_settings", "value").where(
        exp.column("name").eq(exp.Literal.string("default_replica_path"))
    )
    return _complete_values(
        read_rows(connection, query.sql(dialect=ClickHouseDialect), text_columns=(1,)), scope
    )


def _keeper_path(value: str) -> str:
    # An auxiliary Keeper alias is not evidence of a separate coordination
    # domain. Compare actual absolute paths conservatively across all aliases.
    if not value.startswith("/"):
        _alias, separator, value = value.partition(":")
        if not separator:
            value = ""
    if (
        not value.startswith("/")
        or value == "/"
        or any(part in {"", ".", ".."} for part in value[1:].split("/"))
        or "\x00" in value
    ):
        message = "Cannot verify a canonical replacement replication path."
        raise InvalidSqlInputError(message)
    return value


def _seed_uuid(connection: Any, sql: str, table: str, cluster: str | None, key: str | None) -> str:
    # Submitted-generation ownership cycle.
    from .created_resources import prepare_created_table  # noqa: PLC0415

    scope = CreationScope("cluster", cluster=cluster) if cluster is not None else CreationScope()
    resource = CreatedTableResource(table, "ch", scope=scope, connection_key=key)
    return prepare_created_table(connection, sql, resource)[0]


def _uuid(create: exp.Create) -> str | None:
    prop = create.find(exp.UuidProperty)
    if prop is None:
        return None
    try:
        value = UUID(prop.this.name)
    except (TypeError, ValueError, AttributeError) as exc:
        message = "Cannot verify the submitted replacement UUID for replication isolation."
        raise InvalidSqlInputError(message) from exc
    if not value.int:
        message = "Replacement UUID cannot identify an independent replication generation."
        raise InvalidSqlInputError(message)
    return str(value)


def certify_replication_isolation(
    connection: Any,
    source_table: str,
    source_cluster: str | None,
    candidate_sql: str,
    *,
    connection_key: str | None = None,
) -> str:
    """Return the exact prospective CREATE after proving every desired path is distinct."""
    create = parse_one(candidate_sql, read=ClickHouseDialect)
    if not isinstance(create, exp.Create) or not engine_name(create).casefold().startswith(
        "replicated"
    ):
        return candidate_sql
    target = create.this.this
    if not isinstance(target, exp.Table) or not isinstance(target.this, exp.Identifier):
        message = "Replication isolation requires a static physical replacement identity."
        raise InvalidSqlInputError(message)
    database = target.db or resolve_current_database(connection)
    relation = target.name
    table = exp.Table(this=target.this.copy(), db=exp.to_identifier(database)).sql(
        dialect=ClickHouseDialect
    )
    cluster_prop = create.find(exp.OnCluster)
    cluster = cluster_prop.this.name if cluster_prop is not None else None
    source_scope = _scope(connection, source_cluster)
    target_scope = source_scope if cluster == source_cluster else _scope(connection, cluster)
    source_paths = _actual_source_paths(connection, source_table, source_scope)
    templates = _path_templates(connection, target_scope, create)
    macros = (
        _macro_values(connection, target_scope)
        if any("{" in path for path in templates.values())
        else {}
    )
    if any("{uuid}" in path for path in templates.values()) and _uuid(create) is None:
        candidate_sql = _seed_uuid(connection, candidate_sql, table, cluster, connection_key)
        create = cast("exp.Create", parse_one(candidate_sql, read=ClickHouseDialect))
    identity = _uuid(create)
    desired = set()
    for position, template in templates.items():
        values = dict(macros.get(position, {}))
        values.setdefault("database", database)
        values.setdefault("table", relation)
        if identity is not None:
            if "uuid" in values and values["uuid"] != identity:
                message = "A server macro shadows the submitted replacement UUID."
                raise InvalidSqlInputError(message)
            values.setdefault("uuid", identity)
        desired.add(_keeper_path(_expand(template, values)))
    if desired & source_paths:
        message = (
            "Replacement replication storage overlaps the live source. Use a distinct resolved "
            "Keeper path or an independently submitted UUID generation before copying."
        )
        raise InvalidSqlInputError(message)
    return candidate_sql


def certify_reconfiguration_replication(
    connection: Any, change: ChReconfiguration, key: str
) -> None:
    """Bind planned physical replacement SQL to the already inspected original storage."""
    if not any(statement.phase == "create_replacement" for statement in change.plan.statements):
        return
    source_ddl = change.before_ddl["shard" if change.source_pair else "table"]
    source = parse_one(source_ddl, read=ClickHouseDialect).this.this
    source_table = source.sql(dialect=ClickHouseDialect)
    source_cluster = change.source_cluster_resolved if change.source_pair else None
    change.plan.statements = [
        replace(
            statement,
            sql=certify_replication_isolation(
                connection, source_table, source_cluster, statement.sql, connection_key=key
            ),
        )
        if statement.phase == "create_replacement"
        else statement
        for statement in change.plan.statements
    ]
