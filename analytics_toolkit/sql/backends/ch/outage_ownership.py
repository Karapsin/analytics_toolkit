"""Keep CREATE candidates separate from acknowledged native resource ownership."""

from __future__ import annotations

from typing import Any

from sqlglot import exp, parse_one

from analytics_toolkit.sql.connection.errors import ClickHouseClusterTopologyError

from .outage_session import private_table
from .routing import _RoutingClickHouse


def claim_private_create(
    session: Any, statement: exp.Expr, target: tuple[str, str] | None, clusters: list[str]
) -> None:
    if not isinstance(statement, exp.Create) or target is None or not private_table(target[1]):
        return
    if statement.args.get("exists") or statement.args.get("replace"):
        return
    session.state.resources[("ch_create_candidate", session.config.connection_key, target)] = (
        tuple(dict.fromkeys(clusters)) if clusters else (None,)
    )


def remember_created_resource(resource: Any) -> None:
    """Publish only a receipt already matched to its submitted CREATE generation."""
    from analytics_toolkit.sql.execution.backend_options import (  # noqa: PLC0415 - runtime ACK cycle.
        current_backend_operation,
    )

    from .created_resources import CreatedIdentity  # noqa: PLC0415 - runtime ACK cycle.
    from .reconfigure_support import unquoted_table_name  # noqa: PLC0415

    state = current_backend_operation()
    if state is None or not isinstance(resource.certificate, CreatedIdentity):
        return
    database, table = unquoted_table_name(resource.name)
    if database is None:
        return
    target = (database, table)
    key = ("ch_owned", resource.connection_key, target)
    previous = state.resources.get(key, ())
    previous = previous if isinstance(previous, tuple) else ()
    location = _resource_location(resource)
    state.resources[key] = (
        *(record for record in previous if _resource_location(record) != location),
        resource,
    )
    if resource.scope.kind == "local":
        state.resources[("ch_owned_local", resource.connection_key, target)] = (resource,)


def _resource_location(resource: Any) -> tuple[Any, ...]:
    certificate = resource.certificate.resource
    if certificate.scope is None:
        return (certificate.local_identity,)
    return tuple(
        sorted(
            (endpoint.native, certificate.scope.identities[position])
            for position, endpoint in enumerate(certificate.scope.endpoints, 1)
        )
    )


def retire_created_resource(resource: Any) -> None:
    """Retire one acknowledged DROP without changing other scope generations."""
    from analytics_toolkit.sql.execution.backend_options import (  # noqa: PLC0415 - runtime ACK cycle.
        current_backend_operation,
    )

    from .created_resources import CreatedIdentity  # noqa: PLC0415 - runtime ACK cycle.
    from .reconfigure_support import unquoted_table_name  # noqa: PLC0415

    state = current_backend_operation()
    if state is None or not isinstance(resource.certificate, CreatedIdentity):
        return
    target = unquoted_table_name(resource.name)
    location = _resource_location(resource)
    for kind in ("ch_owned", "ch_owned_local"):
        key = (kind, resource.connection_key, target)
        records = state.resources.get(key, ())
        if not isinstance(records, tuple):
            continue
        remaining = tuple(
            record
            for record in records
            if _resource_location(record) != location
            or record.certificate.uuid != resource.certificate.uuid
        )
        if remaining:
            state.resources[key] = remaining
        else:
            state.resources.pop(key, None)


def retire_host_drop(
    session: Any, target: tuple[str, str], server_identity: tuple[Any, int]
) -> None:
    """Retire only this native endpoint's acknowledged, generation-bound DROP."""
    records = session.state.resources.get(("ch_owned", session.config.connection_key, target), ())
    if not isinstance(records, tuple):
        return
    for record in records:
        certificate = record.certificate.resource
        identity = certificate.local_identity
        if certificate.scope is None and (str(identity[0]), identity[1]) == (
            str(server_identity[0]),
            server_identity[1],
        ):
            retire_created_resource(record)


def owned_private(session: Any, target: tuple[str, str]) -> bool:
    """A stale generation cannot authorize private mutation or coverage exemptions."""
    from .created_resources import require_created_resource  # noqa: PLC0415

    records = session.state.resources.get(("ch_owned", session.config.connection_key, target), ())
    if not isinstance(records, tuple) or not records:
        return False
    for record in records:
        require_created_resource(session.client, record)
    return True


def owned_local(session: Any, target: tuple[str, str]) -> bool:
    """Select an acknowledged instance that is the actual current coordinator."""
    from .created_resources import require_created_resource  # noqa: PLC0415
    from .drop_layout import coordinator_identity  # noqa: PLC0415

    records = session.state.resources.get(("ch_owned", session.config.connection_key, target), ())
    if not isinstance(records, tuple) or not records:
        return False
    identity = coordinator_identity(session.client)
    selected = [
        record
        for record in records
        if record.certificate.resource.scope is None
        and record.certificate.resource.local_identity == identity
    ]
    for record in selected:
        require_created_resource(session.client, record)
    return bool(selected)


def assert_new_target(
    session: Any, table_name: str, *, policy: Any, cluster: str | None, only_shard: bool
) -> None:
    from .availability import bind_session_availability  # noqa: PLC0415 -- session cycle.
    from .ddl import build_ch_shard_table_name  # noqa: PLC0415 - DDL imports adapter

    if policy is None:
        from analytics_toolkit.sql.ddl.ch_policy import (  # noqa: PLC0415 - DDL imports adapter
            resolve_create_ch_policy,
        )

        policy = resolve_create_ch_policy(
            session.config,
            ch_engine=None,
            ch_cluster=cluster,
            ch_sharding_key=None,
            ch_distributed_table=None,
            ch_only_shard=only_shard,
            ch_distributed_engine_template=None,
            ch_distributed_cluster=None,
            warn_ch_cluster=False,
        )
    bind_session_availability(session, policy)
    roles = [
        (
            build_ch_shard_table_name(table_name) if policy.create_distributed_pair else table_name,
            policy.shard_on_cluster,
        )
    ]
    if policy.create_distributed_pair:
        roles.append((table_name, policy.distributed_on_cluster))
    for name, creation_cluster in roles:
        table = parse_one(name, into=exp.Table, read=_RoutingClickHouse)
        database = table.db or session.config.database
        if not database:
            msg = "A database is required to prove a target is new."
            raise ClickHouseClusterTopologyError(msg)
        if creation_cluster is None:
            objects = session.local_objects(database, table.name)
        else:
            scope = session.scope(creation_cluster)
            if session.policy == "stop":
                scope.require_available(scope.reachable, "New target preflight")
            objects = scope.objects(database, table.name)
        if objects:
            msg = (
                f"Target {table_name} is not genuinely new: {name} already exists in its "
                "creation scope. Reconcile the existing or partial table pair before loading."
            )
            raise ClickHouseClusterTopologyError(msg)
