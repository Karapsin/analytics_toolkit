"""Bind acknowledged owned CREATEs to their submitted native generation."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlglot import exp, parse_one

from analytics_toolkit.sql.connection.config import ChConfig, get_connection_config
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.ddl.creation_resources import (
    CreatedResourceChangedError,
    CreatedTableResource,
    CreationScope,
    configuration_digest,
    current_scoped_creation_resource,
    record_created_table,
    require_resource_configuration,
    resource_retention_message,
)

from .dialect import ClickHouseDialect
from .outage_context import internal_io

if TYPE_CHECKING:
    from collections.abc import Iterator

    from .drop_layout import VerifiedDropLayout, VerifiedDropResource
    from .outage_topology import Host


@dataclass(frozen=True, repr=False)
class SubmittedGeneration:
    uuid: UUID


@dataclass(frozen=True, repr=False)
class CreatedIdentity:
    uuid: UUID
    resource: VerifiedDropResource


def _submitted_generation(create: exp.Create) -> SubmittedGeneration | None:
    explicit = create.find(exp.UuidProperty)
    if explicit is None:
        return None
    try:
        identity = UUID(explicit.this.name)
    except (TypeError, ValueError, AttributeError):
        return None
    return SubmittedGeneration(identity) if identity.int else None


def _configuration(resource: CreatedTableResource) -> ChConfig:
    if resource.connection_key is None:
        raise CreatedResourceChangedError(resource_retention_message(resource))
    config = get_connection_config(resource.connection_key)
    if not isinstance(config, ChConfig):
        raise CreatedResourceChangedError(resource_retention_message(resource))
    return config


def _atomic_database(connection: Any, resource: CreatedTableResource, database: str) -> bool:
    # Optional generation proof must not turn supported healthy-host creation
    # into an all-host operation. Its actual host submissions provide their own
    # UUID seeds; an unproved nominal submission remains retained, never adopted.
    # ACK ownership and adapter initialization cycle.
    from .drop_layout import _database_engines  # noqa: PLC0415

    # ACK ownership and adapter initialization cycle.
    from .reconfigure_facade_scope import inspect_facade_scope  # noqa: PLC0415

    try:
        scope = (
            inspect_facade_scope(connection, resource.scope.cluster, coordinator_required=False)
            if resource.scope.kind == "cluster" and resource.scope.cluster is not None
            else None
        )
        engines = _database_engines(connection, database, scope)
    except Exception:  # noqa: BLE001 - failed optional proof retains unsupported resources.
        return False
    expected = set(scope.identities) if scope is not None else {1}
    return set(engines) == expected and all(
        value.casefold() == "atomic" for value in engines.values()
    )


def prepare_coordinator_mirror(connection: Any, sql: str) -> tuple[str, bool]:
    """A verified-absent local mirror uses plain CREATE, preserving collision safety."""
    # ACK ownership and adapter initialization cycle.
    from .reconfigure_facade_scope import read_rows  # noqa: PLC0415

    create = parse_one(sql, read=ClickHouseDialect)
    if not isinstance(create, exp.Create) or not create.args.get("exists"):
        return sql, False
    target = create.this.this if isinstance(create.this, exp.Schema) else create.this
    if not isinstance(target, exp.Table) or not isinstance(target.this, exp.Identifier):
        message = "Cannot verify the coordinator mirror's exact table identity."
        raise CreatedResourceChangedError(message)
    rows = read_rows(connection, f"EXISTS TABLE {target.sql(dialect=ClickHouseDialect)}")
    if len(rows) != 1 or len(rows[0]) != 1 or rows[0][0] not in (0, 1):
        message = "Cannot verify coordinator mirror absence before plain CREATE."
        raise CreatedResourceChangedError(message)
    if rows[0][0]:
        return sql, False
    create.set("exists", value=False)
    return create.sql(dialect=ClickHouseDialect), True


def prepare_created_table(
    connection: Any, sql: str, resource: CreatedTableResource
) -> tuple[str, CreatedTableResource]:
    """Seed plain owned Atomic CREATEs without changing public DDL spelling APIs."""
    # ACK ownership and adapter initialization cycle.
    from .coordinator_database import resolve_current_database  # noqa: PLC0415

    create = parse_one(sql, read=ClickHouseDialect)
    if (
        not isinstance(create, exp.Create)
        or str(create.args.get("kind", "")).upper() != "TABLE"
        or create.args.get("exists")
        or create.args.get("replace")
    ):
        return sql, resource
    target = create.this.this if isinstance(create.this, exp.Schema) else create.this
    if not isinstance(target, exp.Table) or not isinstance(target.this, exp.Identifier):
        return sql, resource
    properties = create.args.get("properties")
    cluster = create.find(exp.OnCluster)
    if cluster is not None:
        if not isinstance(cluster.this, (exp.Identifier, exp.Literal)):
            return sql, replace(resource, certificate=None)
        resource = replace(resource, scope=CreationScope("cluster", cluster=cluster.this.name))
    if create.find(exp.UuidProperty) is not None:
        return sql, replace(resource, certificate=_submitted_generation(create))
    database = target.db or resolve_current_database(connection)
    with internal_io():
        if not _atomic_database(connection, resource, database):
            return sql, resource
    identity = uuid4()
    if properties is None:
        properties = exp.Properties(expressions=[])
        create.set("properties", properties)
    properties.append("expressions", exp.UuidProperty(this=exp.Literal.string(str(identity))))
    return create.sql(dialect=ClickHouseDialect), replace(
        resource, certificate=SubmittedGeneration(identity)
    )


def capture_created_resource(
    connection: Any, resource: CreatedTableResource
) -> CreatedTableResource:
    """A successful lookup cannot adopt a homonym with a different CREATE UUID."""
    from .drop_layout import (  # noqa: PLC0415 - ACK ownership and adapter initialization cycle.
        ResourceInspection,
        _qualified_table,
        inspect_resource,
    )

    submitted = resource.certificate
    if not isinstance(submitted, SubmittedGeneration):
        return replace(resource, certificate=None)
    cluster = resource.scope.cluster if resource.scope.kind == "cluster" else None
    try:
        _configuration(resource)
        name = _qualified_table(connection, resource.name, None)
        with internal_io():
            current = inspect_resource(connection, name, cluster, "created", ResourceInspection())
        if current is None or any(
            identity != submitted.uuid for _, identity in current.table_identities
        ):
            return replace(resource, name=name, certificate=None)
    except Exception:  # noqa: BLE001 - failed post-ACK capture never replays or adopts a homonym.
        # CREATE already acknowledged. Failed optional capture cannot replay it;
        # the receipt remains visible but is deliberately not disposable.
        return replace(resource, certificate=None)
    captured = replace(resource, name=name, certificate=CreatedIdentity(submitted.uuid, current))
    # ACK ownership and adapter initialization cycle.
    from .outage_ownership import remember_created_resource  # noqa: PLC0415

    remember_created_resource(captured)
    return captured


def report_host_created(
    connection: Any, sql: str, host: Host, *, connection_key: str | None = None
) -> bool:
    """Publish one actual host acknowledgement before readiness or the next host."""
    create = parse_one(sql, read=ClickHouseDialect)
    if (
        not isinstance(create, exp.Create)
        or create.args.get("exists")
        or create.args.get("replace")
    ):
        return False
    requested = current_scoped_creation_resource()
    if requested is None:
        if connection_key is None:
            return False
        target = create.this.this if isinstance(create.this, exp.Schema) else create.this
        if not isinstance(target, exp.Table):
            return False
        requested = CreatedTableResource(
            target.sql(dialect=ClickHouseDialect),
            "ch",
            connection_key=connection_key,
            configuration_digest=configuration_digest(connection_key),
        )
    resource = replace(
        requested,
        scope=CreationScope("host", endpoint=(host.name, host.port)),
        certificate=_submitted_generation(create),
    )
    return record_created_table(capture_created_resource(connection, resource))


@contextmanager
def _cleanup_connection(connection: Any, resource: CreatedTableResource) -> Iterator[Any]:
    # ACK ownership and adapter initialization cycle.
    from .drop_layout import coordinator_identity  # noqa: PLC0415

    certificate = resource.certificate
    if resource.scope.kind != "host" or (
        isinstance(certificate, CreatedIdentity)
        and coordinator_identity(connection) == certificate.resource.local_identity
    ):
        yield connection
        return
    endpoint = resource.scope.endpoint
    if endpoint is None:
        raise CreatedResourceChangedError(resource_retention_message(resource))
    # ACK ownership and adapter initialization cycle.
    from analytics_toolkit.sql.connection.get_sql_connection import (  # noqa: PLC0415
        get_ch_connection_for_host,
    )

    # ACK ownership and adapter initialization cycle.
    from analytics_toolkit.sql.dml.transfer.runtime.retry import (  # noqa: PLC0415
        close_connection_ref,
    )

    config = _configuration(resource)
    with internal_io():
        worker = get_ch_connection_for_host(
            config.connection_key,
            endpoint[0],
            port=endpoint[1] if config.driver == "native" else None,
        )
    try:
        yield worker
    finally:
        close_connection_ref({"connection": worker}, config.connection_key, "owned-resource")


def require_created_resource(connection: Any, resource: CreatedTableResource) -> None:
    """Validate a completed creation receipt without preflighting a mutation."""
    # ACK ownership and adapter initialization cycle.
    from .drop_layout import require_drop_resource  # noqa: PLC0415

    require_resource_configuration(resource)
    certificate = resource.certificate
    if not isinstance(certificate, CreatedIdentity):
        raise CreatedResourceChangedError(resource_retention_message(resource))
    try:
        with _cleanup_connection(connection, resource) as worker:
            # The metadata comparison is shared; its preflight remains a read
            # so a data-write retry cannot be mistaken for structural replay.
            require_drop_resource(worker, certificate.resource, sqls=("SELECT 1",))
    except CreatedResourceChangedError:
        raise
    except InvalidSqlInputError as exc:
        raise CreatedResourceChangedError(resource_retention_message(resource)) from exc


def drop_created_resource(adapter: Any, connection: Any, resource: CreatedTableResource) -> None:
    """Revalidate the original generation and execute only its actual scope."""
    # ACK ownership and adapter initialization cycle.
    from .drop_execution import DropExecutionOptions, execute_verified_drop  # noqa: PLC0415
    from .drop_layout import (  # noqa: PLC0415 - ACK ownership and adapter initialization cycle.
        VerifiedDropLayout,
        remaining_drop_positions,
        require_drop_resource,
    )

    # ACK ownership and adapter initialization cycle.
    from .routing import local_sql  # noqa: PLC0415

    require_resource_configuration(resource)
    certificate = resource.certificate
    if not isinstance(certificate, CreatedIdentity):
        raise CreatedResourceChangedError(resource_retention_message(resource))
    with _cleanup_connection(connection, resource) as selected, local_sql(selected):
        try:
            if not remaining_drop_positions(selected, certificate.resource, sqls=("SELECT 1",)):
                return
            require_drop_resource(selected, certificate.resource)
        except InvalidSqlInputError as exc:
            raise CreatedResourceChangedError(resource_retention_message(resource)) from exc
        execute_verified_drop(
            adapter,
            selected,
            VerifiedDropLayout((certificate.resource,), _configuration(resource)),
            DropExecutionOptions(if_exists=True, query_label=None, created_resource=resource),
        )
        # ACK ownership and adapter initialization cycle.
        from .outage_ownership import retire_created_resource  # noqa: PLC0415

        retire_created_resource(resource)


def require_created_drop_plan(plan: VerifiedDropLayout, resource: CreatedTableResource) -> None:
    """Authorize only the immutable native scope acknowledged by this CREATE."""
    require_resource_configuration(resource)
    certificate = resource.certificate
    if (
        not isinstance(certificate, CreatedIdentity)
        or plan.resources != (certificate.resource,)
        or plan.config != _configuration(resource)
    ):
        raise CreatedResourceChangedError(resource_retention_message(resource))
