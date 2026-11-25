"""Bind partition rebuilds to one complete native target and physical selector."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from sqlglot import exp

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.core.identifiers import TableIdentifier
from analytics_toolkit.sql.execution.backend_options import require_complete_operation
from analytics_toolkit.sql.execution.upsert_state import current_upsert_state

from .dialect import ClickHouseDialect
from .outage_context import internal_io
from .routing import local_sql

if TYPE_CHECKING:
    from .drop_layout import VerifiedDropResource
    from .layout import ExistingClickHouseLayout
    from .reconfigure_physical import PhysicalStorage
    from .upsert_partitions import PartitionSelector


_TABLE_IDENTITY_PARTS = 2
_REPLICA_SNAPSHOT_FIELDS = 5


@dataclass(frozen=True)
class UpsertTarget:
    layout: ExistingClickHouseLayout
    logical: VerifiedDropResource
    physical: VerifiedDropResource
    storage: PhysicalStorage
    selector: PartitionSelector


def _parts(table: str, database: str = "") -> tuple[str, str]:
    parts = TableIdentifier.parse(table, "ch").parts
    if len(parts) == 1 and database:
        parts = (database, *parts)
    if len(parts) != _TABLE_IDENTITY_PARTS:
        message = "Partition upsert requires a complete native database/table identity."
        raise InvalidSqlInputError(message)
    return parts[0], parts[1]


def bound_upsert_target(connection: object | None = None) -> UpsertTarget | None:
    state = current_upsert_state(connection)
    return cast("UpsertTarget | None", None if state is None else state.get("ch_upsert_target"))


def require_upsert_target(connection: Any, table: str) -> UpsertTarget | None:
    from .drop_layout import require_drop_resource  # noqa: PLC0415 -- native DDL registry cycle.

    target = bound_upsert_target(connection)
    if target is None:
        return None
    logical = _parts(target.layout.logical_table)
    if _parts(table, logical[0]) not in {logical, _parts(target.layout.physical_table)}:
        message = "Prepared partition payload belongs to a different ClickHouse target."
        raise InvalidSqlInputError(message)
    with internal_io(), local_sql(connection):
        for resource in (target.logical, target.physical):
            require_drop_resource(connection, resource, sqls=("SELECT 1",))
    if target.storage.replicated and target.layout.shard_on_cluster is not None:
        _require_logical_group(connection, target)
    return target


def _require_logical_group(connection: Any, target: UpsertTarget) -> None:
    certify = getattr(connection, "outage_certify_logical_storage", None)
    if not callable(certify):
        message = "Cannot certify the live Keeper group for replicated partition preservation."
        raise InvalidSqlInputError(message)
    database, table = _parts(target.layout.physical_table)
    certify(target.layout.shard_on_cluster, database, table)


def _resource(connection: Any, table: str, cluster: str | None, role: str) -> VerifiedDropResource:
    from .drop_layout import (  # noqa: PLC0415 -- native DDL imports backend registry helpers.
        ResourceInspection,
        inspect_resource,
    )

    resource = inspect_resource(
        connection,
        table,
        cluster,
        role,
        ResourceInspection(sqls=("SELECT 1",)),
    )
    if resource is None or any(
        not identity.int for _position, identity in resource.table_identities
    ):
        message = "Cannot certify the native table generation before a partition upsert."
        raise InvalidSqlInputError(message)
    return resource


def _physical_reference(table: str, cluster: str | None) -> str:
    if cluster is None:
        return table
    database, relation = _parts(table)
    return exp.Anonymous(
        this="clusterAllReplicas",
        expressions=[exp.Literal.string(value) for value in (cluster, database, relation)],
    ).sql(dialect=ClickHouseDialect)


def prepare_upsert_target(
    adapter: Any, connection: Any, table: str, *, partition_column: str | None
) -> None:
    require_complete_operation(
        "Partition upsert preservation requires complete ClickHouse coverage."
    )
    from .layout import inspect_existing_layout  # noqa: PLC0415 -- backend registry cycle.
    from .reconfigure_physical import (  # noqa: PLC0415 -- DDL imports backend registry helpers.
        ClusterPhysicalStorage,
        _storage,
        certify_physical_table,
    )
    from .upsert_partitions import (  # noqa: PLC0415 -- partition DDL imports backend helpers.
        build_partition_selector,
    )

    state = current_upsert_state(connection)
    if state is None:
        message = "Partition upsert certification requires a run-owned target binding."
        raise InvalidSqlInputError(message)
    if require_upsert_target(connection, table) is not None:
        return
    if partition_column is None:
        message = "A logical partition column is required before rebuilding physical partitions."
        raise InvalidSqlInputError(message)
    with internal_io(), local_sql(connection):
        layout = inspect_existing_layout(connection, table)
        logical = _resource(connection, layout.logical_table, None, "upsert facade")
        physical = _resource(
            connection, layout.physical_table, layout.shard_on_cluster, "upsert physical"
        )
        storage = _storage(connection, physical.ddl, layout.shard_on_cluster)
        certify_physical_table(connection, storage, storage.table)
        if isinstance(storage, ClusterPhysicalStorage) and storage.independent_replicas:
            message = "Partition upsert cannot publish a complete independent-replica snapshot."
            raise InvalidSqlInputError(message)
        facade_types = adapter.get_writable_table_column_types(
            connection, layout.logical_table, connection_key="ch"
        )
        physical_types = adapter.get_writable_table_column_types(
            connection,
            _physical_reference(layout.physical_table, layout.shard_on_cluster),
            connection_key="ch",
        )
        if not facade_types or facade_types != physical_types:
            message = "Facade writable columns do not cover the complete physical storage schema."
            raise InvalidSqlInputError(message)
        if partition_column not in facade_types:
            message = "The logical partition column is absent from the writable target schema."
            raise InvalidSqlInputError(message)
        selector = build_partition_selector(physical.ddl, layout.physical_table, physical_types)
    state["ch_upsert_target"] = UpsertTarget(layout, logical, physical, storage, selector)
    require_upsert_target(connection, table)


def prepare_preservation_snapshot(connection: Any, target: UpsertTarget, sql: str) -> None:
    from .reconfigure_readiness import (  # noqa: PLC0415 -- DDL imports backend registry helpers.
        synchronize_verified_replicas,
    )

    state = current_upsert_state(connection)
    if state is None or state.get("ch_upsert_snapshot_ready"):
        return
    tree = ClickHouseDialect().parse(sql)[0]
    if (
        not isinstance(tree, exp.Insert)
        or tree.expression is None
        or not any(table.alias == "target_dst" for table in tree.expression.find_all(exp.Table))
    ):
        return
    if target.storage.replicated:
        if target.layout.shard_on_cluster is not None:
            _require_logical_group(connection, target)
        synchronize_verified_replicas(connection, target.storage)
        _require_empty_replica_queues(connection, target)
        require_upsert_target(connection, target.layout.logical_table)
    state["ch_upsert_snapshot_ready"] = True


def _require_empty_replica_queues(connection: Any, target: UpsertTarget) -> None:
    from .reconfigure_facade_scope import (  # noqa: PLC0415 -- native DDL/backend registry cycle.
        cluster_table,
        read_rows,
    )

    cluster = target.layout.shard_on_cluster
    database, table = _parts(target.layout.physical_table)
    source = (
        cluster_table(cluster, "replicas")
        if cluster is not None
        else exp.to_table("system.replicas")
    )
    position = exp.column("_shard_num") if cluster is not None else exp.Literal.number(1)
    query = exp.select(
        position, "queue_size", "inserts_in_queue", "is_readonly", "is_session_expired"
    ).from_(source)
    query = query.where(exp.column("database").eq(exp.Literal.string(database))).where(
        exp.column("table").eq(exp.Literal.string(table))
    )
    rows = read_rows(connection, query.sql(dialect=ClickHouseDialect))
    expected = set(target.physical.scope.identities) if target.physical.scope else {1}
    try:
        positions = {int(row[0]) for row in rows}
        valid = (
            len(rows) == len(expected)
            and positions == expected
            and all(
                len(row) == _REPLICA_SNAPSHOT_FIELDS and all(int(value) == 0 for value in row[1:])
                for row in rows
            )
        )
    except (ValueError, TypeError, IndexError):
        valid = False
    if not valid:
        message = "Replicated target snapshot is incomplete after synchronization."
        raise InvalidSqlInputError(message)
