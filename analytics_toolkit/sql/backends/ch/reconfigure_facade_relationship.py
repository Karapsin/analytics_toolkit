"""Match existing facades to the verified source storage, using each server's metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from sqlglot import exp

from analytics_toolkit.sql.connection.errors import (
    ClickHouseClusterTopologyError,
    InvalidSqlInputError,
)

from .cluster_functions import cluster_arguments
from .dialect import ClickHouseDialect
from .metadata import _is_current_database, extract_clickhouse_macro_name
from .reconfigure_ddl import (
    comparable_create_sql,
    distributed_table_parts,
    parse_create_table,
    retarget_create,
)
from .reconfigure_facade_scope import cluster_table, read_rows

if TYPE_CHECKING:
    from .reconfigure_facade_scope import FacadeScope

_MIN_ENGINE_ARGS = 3
_MACRO_FIELDS = 3
_CLUSTER_FIELDS = 5
_MAX_PORT = 65535


@dataclass(frozen=True)
class FacadeDefinition:
    cluster: str
    database: str
    table: str
    signature: str


@dataclass(frozen=True)
class FacadeSource:
    ddl: str
    cluster: str


def facade_definition(ddl: str, table: str) -> FacadeDefinition:
    message = "Facade destination contains an unowned logical table definition."
    create = parse_create_table(ddl, table)
    expected_database, expected_table = distributed_table_parts(table)
    declared = create.this.this
    if (
        not isinstance(declared, exp.Table)
        or not isinstance(declared.this, exp.Identifier)
        or declared.catalog
        or declared.name != expected_table
        or (declared.db or expected_database) != expected_database
    ):
        raise InvalidSqlInputError(message)
    normalized = retarget_create(create, table, None)
    properties = normalized.args.get("properties")
    engine_property = (
        next(
            (item for item in properties.expressions if isinstance(item, exp.EngineProperty)),
            None,
        )
        if isinstance(properties, exp.Properties)
        else None
    )
    engine = engine_property.this if engine_property is not None else None
    if (
        not isinstance(engine, exp.Anonymous)
        or engine.name.casefold() != "distributed"
        or len(engine.expressions) < _MIN_ENGINE_ARGS
    ):
        raise InvalidSqlInputError(message)
    arguments = list(engine.expressions)
    database_argument = arguments[1]
    if _is_current_database(database_argument):
        arguments[1] = exp.Literal.string(expected_database)
        engine.set("expressions", arguments)
    try:
        cluster, database, relation, trailing = cluster_arguments(engine, None)
    except ClickHouseClusterTopologyError as exc:
        raise InvalidSqlInputError(message) from exc
    engine.set(
        "expressions",
        [
            exp.Literal.string("source-storage"),
            exp.Literal.string("source-database"),
            exp.Literal.string("source-table"),
            *trailing,
        ],
    )
    return FacadeDefinition(cluster, database, relation, comparable_create_sql(normalized))


def _scope_macros(
    connection: Any, scope: FacadeScope, definitions: dict[int, FacadeDefinition]
) -> dict[int, dict[str, str]]:
    names = {
        macro
        for definition in definitions.values()
        if (macro := extract_clickhouse_macro_name(definition.cluster)) is not None
    }
    if not names:
        return {}
    query = (
        exp.select("_shard_num", "macro", "substitution")
        .from_(cluster_table(scope.cluster, "macros"))
        .where(exp.column("macro").isin(*(exp.Literal.string(name) for name in sorted(names))))
    )
    rows = read_rows(connection, query.sql(dialect=ClickHouseDialect))
    message = "Cannot verify the source facade's routing macros."
    result: dict[int, dict[str, str]] = {}
    try:
        for row in rows:
            position, name, substitution = _macro_row(row, scope)
            values = result.setdefault(position, {})
            _check_new_macro(name, values)
            values[name] = substitution
    except (IndexError, TypeError, ValueError) as exc:
        raise InvalidSqlInputError(message) from exc
    return result


def _macro_row(row: Any, scope: FacadeScope) -> tuple[int, str, str]:
    message = "Incomplete facade routing macro metadata."
    if len(row) != _MACRO_FIELDS:
        raise ValueError(message)
    position, name, substitution = int(row[0]), str(row[1]), str(row[2])
    if position not in scope.identities or not name or not substitution:
        raise ValueError(message)
    return position, name, substitution


def _check_new_macro(name: str, values: dict[str, str]) -> None:
    if name in values:
        message = "Duplicate facade routing macro metadata."
        raise ValueError(message)


def _resolved_names(
    definitions: dict[int, FacadeDefinition], macros: dict[int, dict[str, str]]
) -> dict[int, str]:
    resolved: dict[int, str] = {}
    for position, definition in definitions.items():
        macro = extract_clickhouse_macro_name(definition.cluster)
        name = macros.get(position, {}).get(macro) if macro is not None else definition.cluster
        if not name:
            message = "Cannot verify the source facade's routing macros."
            raise InvalidSqlInputError(message)
        resolved[position] = name
    return resolved


def _routing_endpoints(
    connection: Any, scope: FacadeScope, names: set[str]
) -> dict[int, dict[str, set[tuple[str, int]]]]:
    query = (
        exp.select("_shard_num", "cluster", "host_name", "host_address", "port")
        .from_(cluster_table(scope.cluster, "clusters"))
        .where(exp.column("cluster").isin(*(exp.Literal.string(name) for name in sorted(names))))
    )
    rows = read_rows(connection, query.sql(dialect=ClickHouseDialect))
    message = "Cannot verify the source facade's routing endpoints."
    result: dict[int, dict[str, set[tuple[str, int]]]] = {}
    try:
        for row in rows:
            position, cluster, endpoint = _routing_row(row, scope)
            result.setdefault(position, {}).setdefault(cluster, set()).add(endpoint)
    except (IndexError, TypeError, ValueError) as exc:
        raise InvalidSqlInputError(message) from exc
    return result


def _routing_row(row: Any, scope: FacadeScope) -> tuple[int, str, tuple[str, int]]:
    message = "Incomplete facade routing endpoint metadata."
    if len(row) != _CLUSTER_FIELDS:
        raise ValueError(message)
    position, cluster = int(row[0]), str(row[1])
    address, port = str(row[3] or row[2] or ""), int(row[4])
    if position not in scope.identities or not cluster or not address or not 0 < port <= _MAX_PORT:
        raise ValueError(message)
    return position, cluster, (address, port)


def verify_facade_relationships(
    connection: Any,
    table: str,
    scope: FacadeScope,
    occupied: dict[int, str],
    source_layout: FacadeSource,
) -> None:
    source = facade_definition(source_layout.ddl, table)
    definitions = {position: facade_definition(ddl, table) for position, ddl in occupied.items()}
    if any(
        (definition.database, definition.table, definition.signature)
        != (source.database, source.table, source.signature)
        for definition in definitions.values()
    ):
        message = "Facade destination contains an unowned logical table definition."
        raise InvalidSqlInputError(message)
    macros = _scope_macros(connection, scope, definitions)
    names = _resolved_names(definitions, macros)
    coordinator = cast("int", scope.coordinator)
    endpoints = _routing_endpoints(connection, scope, {*names.values(), source_layout.cluster})
    expected = endpoints.get(coordinator, {}).get(source_layout.cluster)
    if not expected or any(
        endpoints.get(position, {}).get(name) != expected for position, name in names.items()
    ):
        message = "Facade destination routes to unowned source storage endpoints."
        raise InvalidSqlInputError(message)


__all__ = ["FacadeSource", "facade_definition", "verify_facade_relationships"]
