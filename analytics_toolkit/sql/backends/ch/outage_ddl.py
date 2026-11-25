"""Host-local, identity-stable creation without deferred cluster-wide work."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlglot import exp, parse_one

from analytics_toolkit.sql.backends.utils import sql_string_literal
from analytics_toolkit.sql.connection.errors import (
    ClickHouseClusterTopologyError,
    ClickHouseClusterUnavailableError,
)
from analytics_toolkit.sql.execution.backend_options import current_backend_query_label
from analytics_toolkit.sql.execution.labels import apply_query_label

from .availability import record_optional_availability, warn_optional_availability
from .creation_outcomes import record_skipped_optional_facade
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
    result.set("exists", value=bool(statement.args.get("exists")))
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
    normalized.set("exists", value=False)
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
        text_columns=(1,),
    )
    return {int(row[0]): str(row[1]) for row in rows if len(row) >= _DATABASE_FIELDS}


def _execute_local(scope: ClusterScope, host: Host, sql: str) -> None:
    sql = apply_query_label(sql, current_backend_query_label())
    with host_connection(scope, host) as connection:
        connection.command(sql)
        from .created_resources import report_host_created  # noqa: PLC0415 - adapter cycle.

        report_host_created(connection, sql, host, connection_key=scope.config.connection_key)


def _generation(statement: exp.Create) -> str | None:
    explicit = statement.find(exp.UuidProperty)
    if explicit is None:
        return None
    try:
        identity = UUID(explicit.this.name)
    except (TypeError, ValueError, AttributeError) as exc:
        msg = "Healthy-only creation requires a valid submitted table UUID."
        raise ClickHouseClusterTopologyError(msg) from exc
    if not identity.int:
        msg = "Healthy-only creation requires a nonzero submitted table UUID."
        raise ClickHouseClusterTopologyError(msg)
    return str(identity)


def _canonical_healthy_create(
    statement: exp.Create,
    existing: list[TableObject],
    full_name: str,
    *,
    retained: tuple[bool, str],
    atomic: bool,
) -> exp.Create:
    owned, retained_sql = retained
    if owned:
        parsed = parse_one(retained_sql, read=_RoutingClickHouse)
        if not isinstance(parsed, exp.Create):
            msg = "Retained creation schema is not a CREATE statement."
            raise ClickHouseClusterTopologyError(msg)
        identity = _generation(parsed)
        if existing and (identity is None or any(item.uuid != identity for item in existing)):
            msg = f"Cannot verify the retained creation generation of {full_name}."
            raise ClickHouseClusterTopologyError(msg)
        if identity is None and atomic:
            identity = str(uuid4())
        canonical = local_create(parsed, uuid=identity)
    elif existing:
        canonical = local_create(
            statement, uuid=existing[0].uuid if existing[0].uuid.strip("0-") else None
        )
    else:
        common_uuid = _generation(statement) or str(uuid4()) if atomic else None
        canonical = local_create(statement, uuid=common_uuid)
    return canonical


def _skip_optional_deployment(scope: ClusterScope, table: str) -> bool:
    if not scope.availability_optional:
        return False
    warn_optional_availability(scope, "create table")
    record_optional_availability(scope, table)
    return not scope.reachable


def _record_create_coverage(scope: ClusterScope, table: str, coverage_intent: str) -> None:
    if scope.availability_optional:
        record_optional_availability(scope, table)
        return
    coverage = scope.record(scope.reachable, table, coverage_intent=coverage_intent)
    if not coverage["complete"]:
        warn_degraded(scope.state, scope.config, coverage, "create table")


def create_on_healthy_hosts(  # noqa: C901 - keep the safety decision and submission in one boundary
    scope: ClusterScope, statement: exp.Create, *, coverage_intent: str = "physical_hosts"
) -> bool:
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
    if _skip_optional_deployment(scope, full_name):
        record_skipped_optional_facade(scope, database, table)
        return False
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
    canonical = _canonical_healthy_create(
        statement,
        existing,
        full_name,
        retained=(owned, scope.state.resources[key] if owned else ""),
        atomic=all(engines[host.position] == "Atomic" for host in scope.reachable),
    )
    canonical.set("exists", value=bool(statement.args.get("exists")))
    validate_replica_creation(scope, canonical, database, table, existing)
    sql = canonical.sql(dialect="clickhouse")
    scope.state.resources[key] = sql
    _record_create_coverage(scope, full_name, coverage_intent)
    present = {item.host.position for item in existing}
    for host in scope.reachable:
        if host.position not in present:
            _execute_local(scope, host, sql)
    objects = scope.objects(database, table)
    if {item.host.position for item in objects} != {host.position for host in scope.reachable}:
        msg = f"Creation of {full_name} was not verified on selected hosts."
        raise ClickHouseClusterUnavailableError(msg)
    _record_create_coverage(scope, full_name, coverage_intent)
    # A retained definition records identity, not whether a disposable table
    # still exists. A recreated table must be populated again before counting.
    return not existing


def provision_missing(
    scope: ClusterScope,
    database: str,
    table: str,
    objects: list[TableObject],
    *,
    coverage_intent: str | None = None,
) -> list[TableObject]:
    present = {item.host.position for item in objects}
    missing = [host for host in scope.reachable if host.position not in present]
    if not missing or not objects:
        return objects
    statement = parse_one(objects[0].create_sql, read=_RoutingClickHouse)
    if not isinstance(statement, exp.Create):
        msg = "Cannot reconstruct the existing table schema safely."
        raise ClickHouseClusterTopologyError(msg)
    intent = coverage_intent or (
        "logical_shards"
        if objects[0].engine.lower().startswith(("replicated", "shared"))
        else "physical_hosts"
    )
    coverage = scope.record(
        [item.host for item in objects], f"{database}.{table}", coverage_intent=intent
    )
    if not coverage["complete"]:
        warn_degraded(
            scope.state,
            scope.config,
            coverage,
            "create empty tables on recovered hosts (no historical rows copied)",
        )
    missing_create = local_create(
        statement, uuid=objects[0].uuid if objects[0].uuid.strip("0-") else None
    )
    missing_create.set("exists", value=True)
    create_on_healthy_hosts(
        scope,
        missing_create,
        coverage_intent=intent,
    )
    return scope.objects(database, table)
