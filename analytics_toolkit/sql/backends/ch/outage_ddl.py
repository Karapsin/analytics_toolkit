"""Host-local, identity-stable creation without deferred cluster-wide work."""

from __future__ import annotations

from uuid import uuid4

from sqlglot import exp, parse_one

from analytics_toolkit.sql.backends.utils import sql_string_literal
from analytics_toolkit.sql.connection.errors import (
    ClickHouseClusterTopologyError,
    ClickHouseClusterUnavailableError,
)
from analytics_toolkit.sql.execution.backend_options import current_backend_query_label
from analytics_toolkit.sql.execution.labels import apply_query_label

from .outage_hosts import host_connection
from .outage_replication import validate_replica_creation
from .outage_topology import ClusterScope, Host, TableObject, query_rows, warn_degraded
from .routing import _RoutingClickHouse

_DATABASE_FIELDS = 2


def create_target(statement: exp.Create, database: str | None) -> tuple[str, str]:
    target = statement.this.this if isinstance(statement.this, exp.Schema) else statement.this
    if not isinstance(target, exp.Table) or not isinstance(target.this, exp.Identifier):
        msg = "Healthy-only creation requires a named table."
        raise ClickHouseClusterTopologyError(msg)
    db = target.db or database
    if not db:
        msg = "A database is required for healthy-only creation."
        raise ClickHouseClusterTopologyError(msg)
    return db, target.name


def local_create(statement: exp.Create, *, uuid: str | None = None) -> exp.Create:
    result = statement.copy()
    result.set("exists", value=True)
    result.set("replace", value=False)
    properties = result.args.get("properties")
    if properties is None:
        properties = exp.Properties(expressions=[])
        result.set("properties", properties)
    properties.set(
        "expressions",
        [
            item
            for item in properties.expressions
            if not isinstance(item, (exp.OnCluster, exp.UuidProperty))
        ],
    )
    if uuid is not None:
        properties.append("expressions", exp.UuidProperty(this=exp.Literal.string(uuid)))
    return result


def _column_signature(statement: exp.Create) -> tuple[str, ...]:
    target = statement.this
    if not isinstance(target, exp.Schema):
        msg = "Creation requires explicit column metadata."
        raise ClickHouseClusterTopologyError(msg)
    columns = []
    for column in target.expressions:
        if isinstance(column, exp.ColumnDef):
            normalized = column.copy()
            normalized.set("this", exp.to_identifier(column.name))
            columns.append(normalized.sql(dialect="clickhouse"))
    return tuple(columns)


def copy_table_definition(statement: exp.Create, source: TableObject) -> exp.Create:
    original = parse_one(source.create_sql, read=_RoutingClickHouse)
    if not isinstance(original, exp.Create) or not isinstance(original.this, exp.Schema):
        msg = "Schema-only creation requires a verifiable CREATE TABLE source definition."
        raise ClickHouseClusterTopologyError(msg)
    converted = statement.copy()
    target = converted.this.this if isinstance(converted.this, exp.Schema) else converted.this
    converted.set(
        "this",
        exp.Schema(this=target, expressions=[item.copy() for item in original.this.expressions]),
    )
    inherited = local_create(original).args["properties"].expressions
    explicit = converted.args.get("properties")
    overrides = explicit.expressions if explicit is not None else []
    overridden = {type(item) for item in overrides}
    converted.set(
        "properties",
        exp.Properties(
            expressions=[item for item in inherited if type(item) not in overridden] + overrides
        ),
    )
    converted.set("expression", None)
    return converted


def _definition_signature(statement: exp.Create) -> str:
    normalized = local_create(statement)
    target = normalized.this.this if isinstance(normalized.this, exp.Schema) else normalized.this
    target.set("db", None)
    engine = normalized.find(exp.EngineProperty)
    if engine is not None and isinstance(engine.this, exp.Identifier):
        engine.set("this", exp.Anonymous(this=engine.this.name, expressions=[]))
    for node in normalized.walk():
        node.comments = None
        if isinstance(node, exp.Identifier):
            node.set("quoted", value=False)
    return normalized.sql(dialect="clickhouse")


def _database_engines(scope: ClusterScope, database: str) -> dict[int, str]:
    rows = query_rows(
        scope.client,
        "SELECT _shard_num, engine "  # noqa: S608 - escaped literals or parsed SQL identifiers
        f"FROM clusterAllReplicas({sql_string_literal(scope.cluster)}, system, databases) "
        f"WHERE name = {sql_string_literal(database)}",
        settings=scope.probe_settings,
    )
    return {int(row[0]): str(row[1]) for row in rows if len(row) >= _DATABASE_FIELDS}


def _execute_local(scope: ClusterScope, host: Host, sql: str) -> None:
    sql = apply_query_label(sql, current_backend_query_label())
    with host_connection(scope, host) as connection:
        connection.command(sql)


def create_on_healthy_hosts(scope: ClusterScope, statement: exp.Create) -> bool:  # noqa: C901, PLR0912 - keep the safety decision and submission in one boundary
    database, table = create_target(statement, scope.config.database)
    full_name = f"{database}.{table}"
    if statement.args.get("kind") != "TABLE" or statement.args.get("replace"):
        msg = "Ad hoc creation supports new tables, not replacement or views."
        raise ClickHouseClusterUnavailableError(msg)
    if statement.args.get("expression") is not None:
        msg = (
            "Healthy-only CREATE requires explicit columns; create the schema before INSERT SELECT."
        )
        raise ClickHouseClusterTopologyError(msg)
    existing = scope.objects(database, table)
    key = ("ch_create", scope.config.connection_key, scope.cluster, full_name)
    owned = key in scope.state.resources
    if existing and not statement.args.get("exists") and not owned:
        msg = f"Table {full_name} already exists."
        raise ClickHouseClusterTopologyError(msg)
    engines = _database_engines(scope, database)
    for host in scope.reachable:
        if engines.get(host.position) not in {"Atomic", "Ordinary"}:
            msg = (
                f"Healthy-only creation in {database} requires local database DDL "
                "(Atomic or Ordinary); replicated/shared or missing database metadata is unsafe."
            )
            raise ClickHouseClusterTopologyError(msg)
    expected_columns = _column_signature(statement)
    if existing:
        parsed = parse_one(existing[0].create_sql, read=_RoutingClickHouse)
        if (
            not isinstance(parsed, exp.Create)
            or _column_signature(parsed) != expected_columns
            or _definition_signature(parsed) != _definition_signature(statement)
        ):
            msg = f"Existing schema conflicts with creation of {full_name}."
            raise ClickHouseClusterTopologyError(msg)
        canonical = local_create(
            parsed, uuid=existing[0].uuid if existing[0].uuid.strip("0-") else None
        )
    elif owned:
        parsed = parse_one(scope.state.resources[key], read=_RoutingClickHouse)
        if not isinstance(parsed, exp.Create):
            msg = "Retained creation schema is not a CREATE statement."
            raise ClickHouseClusterTopologyError(msg)
        canonical = parsed
    else:
        common_uuid = (
            str(uuid4())
            if all(engines[host.position] == "Atomic" for host in scope.reachable)
            else None
        )
        canonical = local_create(statement, uuid=common_uuid)
    validate_replica_creation(scope, canonical, database, table, existing)
    sql = canonical.sql(dialect="clickhouse")
    scope.state.resources[key] = sql
    if len(scope.reachable) != len(scope.hosts):
        warn_degraded(
            scope.state, scope.config, scope.record(scope.reachable, full_name), "create table"
        )
    for host in scope.reachable:
        _execute_local(scope, host, sql)
    objects = scope.objects(database, table)
    if {item.host.position for item in objects} != {host.position for host in scope.reachable}:
        msg = f"Creation of {full_name} was not verified on selected hosts."
        raise ClickHouseClusterUnavailableError(msg)
    scope.record([item.host for item in objects], full_name)
    return not existing and not owned


def provision_missing(
    scope: ClusterScope, database: str, table: str, objects: list[TableObject]
) -> list[TableObject]:
    present = {item.host.position for item in objects}
    missing = [host for host in scope.reachable if host.position not in present]
    if not missing or not objects:
        return objects
    statement = parse_one(objects[0].create_sql, read=_RoutingClickHouse)
    if not isinstance(statement, exp.Create):
        msg = "Cannot reconstruct the existing table schema safely."
        raise ClickHouseClusterTopologyError(msg)
    warn_degraded(
        scope.state,
        scope.config,
        scope.record([item.host for item in objects], f"{database}.{table}"),
        "create empty tables on recovered hosts (no historical rows copied)",
    )
    create_on_healthy_hosts(
        scope,
        local_create(statement, uuid=objects[0].uuid if objects[0].uuid.strip("0-") else None),
    )
    return scope.objects(database, table)
