"""Execute certified ClickHouse drops without widening any resource's scope."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.dml.transfer.runtime.retry import close_connection_ref
from analytics_toolkit.sql.execution.backend_options import (
    current_backend_operation,
    require_complete_operation,
)
from analytics_toolkit.sql.execution.cancellation import (
    raise_if_cancelled,
    wait_or_raise_if_cancelled,
)
from analytics_toolkit.sql.execution.labels import apply_query_label

from .drop_layout import (
    VerifiedDropLayout,
    VerifiedDropResource,
    coordinator_identity,
    drop_position_identity,
    remaining_drop_positions,
    require_drop_configuration,
    require_drop_resource,
    require_drop_scope,
    unacknowledged_drop_positions,
)
from .outage_context import internal_io
from .reconfigure_ddl import engine_name, parse_create_table
from .reconfigure_execution import cluster_clause
from .reconfigure_facade_scope import read_rows
from .reconfigure_support import unquoted_table_name
from .recovery_endpoints import RecoveryEndpoint
from .routing import local_sql

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


@dataclass(frozen=True)
class DropExecutionOptions:
    if_exists: bool = True
    wait_for_absence: bool = False
    timeout_seconds: int = 300
    poll_interval_seconds: float = 1
    retry_per_host_drops: bool = True
    query_label: str | None = None
    per_host_connection_factory: Callable[[str], Any] | None = None
    host_connection_runner: Callable[[str, Callable[[Any], Any]], Any] | None = None
    per_host_drop_workers: int = 5
    allow_already_absent: bool = True
    defer_ack_timeout: bool = False


@dataclass(frozen=True)
class DropRecoveryTarget:
    host: RecoveryEndpoint
    resource: VerifiedDropResource
    coordinator: bool


class AcknowledgedDropWaitTimeoutError(TimeoutError):
    """The submitted drop was acknowledged, but its absence wait expired."""


def resource_drop_sql(
    resource: VerifiedDropResource, *, if_exists: bool, query_label: str | None
) -> str:
    if resource.if_exists is not None:
        if_exists = resource.if_exists
    prefix = "DROP TABLE IF EXISTS" if if_exists else "DROP TABLE"
    return apply_query_label(
        f"{prefix} {resource.table}{cluster_clause(resource.cluster)}", query_label
    )


@contextmanager
def _configuration_guard(plan: VerifiedDropLayout) -> Iterator[None]:
    state = current_backend_operation()
    key = ("verified_drop", id(plan))

    def guard(alias: str, config: Any) -> None:
        if alias == plan.config.connection_key and config != plan.config:
            message = "ClickHouse configuration changed during the verified drop."
            raise InvalidSqlInputError(message)

    if state is not None:
        with state.lock:
            state.resources.setdefault("connection_configuration_guards", {})[key] = guard
    try:
        yield
    finally:
        if state is not None:
            with state.lock:
                state.resources.get("connection_configuration_guards", {}).pop(key, None)


def _require_http_endpoints(plan: VerifiedDropLayout) -> None:
    if plan.config.driver == "native":
        return
    ports: dict[str, set[int]] = {}
    for resource in plan.resources:
        if resource.scope is not None:
            for endpoint in resource.scope.endpoints:
                ports.setdefault(endpoint.native[0], set()).add(endpoint.native[1])
    if any(len(values) > 1 for values in ports.values()):
        message = (
            "HTTP drop recovery cannot distinguish multiple native instances on one host; "
            "use independently addressable HTTP endpoints or the native driver before dropping."
        )
        raise InvalidSqlInputError(message)


def _absent(connection: Any, resource: VerifiedDropResource) -> bool:
    require_drop_scope(connection, resource)
    if resource.scope is not None:
        return not remaining_drop_positions(connection, resource)
    rows = read_rows(connection, f"EXISTS TABLE {resource.table}")
    if len(rows) != 1 or len(rows[0]) != 1 or rows[0][0] not in (0, 1):
        message = "Cannot verify the local ClickHouse drop absence result."
        raise InvalidSqlInputError(message)
    if not rows[0][0]:
        return True
    remaining_drop_positions(connection, resource)
    return False


def _wait_absent(
    connection: Any,
    plan: VerifiedDropLayout,
    resource: VerifiedDropResource,
    options: DropExecutionOptions,
) -> None:
    deadline = time.monotonic() + options.timeout_seconds
    while True:
        raise_if_cancelled()
        require_drop_configuration(plan)
        if _absent(connection, resource):
            return
        if time.monotonic() >= deadline:
            positions = remaining_drop_positions(connection, resource)
            engine = engine_name(parse_create_table(resource.ddl, resource.table))
            database, relation = unquoted_table_name(resource.table)
            if database is None:
                message = "Drop diagnostics require the certified qualified table identity."
                raise InvalidSqlInputError(message)
            display = f"{database}.{relation}"
            hosts = (
                [
                    f"{resource.scope.membership[position - 1][2]} "
                    f"({resource.scope.endpoints[position - 1].native[0]}:"
                    f"{resource.scope.endpoints[position - 1].native[1]}): {display} ({engine})"
                    for position in positions
                ]
                if resource.scope is not None
                else [f"coordinator: {display} ({engine})"]
            )
            message = (
                f"ClickHouse {resource.role} resource {resource.table} remains visible in "
                f"its verified drop scope after {options.timeout_seconds} second(s). "
                f"Leftover table(s): {'; '.join(hosts)}. Direct local drops on affected "
                "hosts may be attempted with ch_retry_per_host_drops=True."
            )
            raise TimeoutError(message)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            wait_or_raise_if_cancelled(min(options.poll_interval_seconds, remaining))


def _execute_one(
    adapter: Any, connection: Any, resource: VerifiedDropResource, options: DropExecutionOptions
) -> None:
    with local_sql(connection):
        adapter.execute_commands(
            connection,
            [
                resource_drop_sql(
                    resource, if_exists=options.if_exists, query_label=options.query_label
                )
            ],
        )


def _recover_on_worker(
    adapter: Any,
    worker: Any,
    plan: VerifiedDropLayout,
    resource: VerifiedDropResource,
    options: DropExecutionOptions,
) -> None:
    require_drop_configuration(plan)
    if coordinator_identity(worker) != resource.local_identity:
        message = "Cannot verify the selected ClickHouse drop recovery endpoint identity."
        raise InvalidSqlInputError(message)
    if options.allow_already_absent and _absent(worker, resource):
        return
    require_drop_resource(worker, resource)
    _execute_one(adapter, worker, resource, options)


def prepare_drop_recovery(
    connection: Any, resource: VerifiedDropResource
) -> tuple[DropRecoveryTarget, ...]:
    """Capture remaining exact targets while the coordinator lease is available."""
    scope = resource.scope
    if scope is None:
        return ()
    remaining = remaining_drop_positions(connection, resource)
    return _drop_position_targets(resource, remaining)


def _drop_position_targets(
    resource: VerifiedDropResource, positions: tuple[int, ...]
) -> tuple[DropRecoveryTarget, ...]:
    scope = resource.scope
    if scope is None:
        return ()
    identities = dict(resource.table_identities)
    return tuple(
        DropRecoveryTarget(
            RecoveryEndpoint(
                scope.endpoints[position - 1].native[0], scope.endpoints[position - 1].native[1]
            ),
            replace(
                resource,
                cluster=None,
                scope=None,
                local_identity=scope.identities[position],
                table_identities=((1, identities[position]),),
            ),
            position == scope.coordinator,
        )
        for position in positions
    )


def execute_drop_recovery(
    adapter: Any,
    connection: Any | None,
    plan: VerifiedDropLayout,
    targets: tuple[DropRecoveryTarget, ...],
    options: DropExecutionOptions,
) -> None:
    """Execute captured recovery targets without requiring a held coordinator lease."""
    from analytics_toolkit.sql.connection.get_sql_connection import (  # noqa: PLC0415 - connection factory cycle
        get_ch_connection_for_host,
    )

    def recover(target: DropRecoveryTarget) -> None:
        raise_if_cancelled()
        require_drop_configuration(plan)
        worker = None
        host, local = target.host, target.resource
        if options.host_connection_runner is not None and (
            connection is None or not target.coordinator
        ):

            def recover_on_host(worker: Any) -> None:
                _recover_on_worker(adapter, worker, plan, local, options)

            options.host_connection_runner(host, recover_on_host)
            return
        try:
            if target.coordinator and connection is not None:
                worker = connection
            else:
                with internal_io():
                    worker = (
                        options.per_host_connection_factory(host)
                        if options.per_host_connection_factory is not None
                        else get_ch_connection_for_host(plan.config.connection_key, host)
                    )
            _recover_on_worker(adapter, worker, plan, local, options)
        finally:
            if worker is not None and worker is not connection:
                close_connection_ref({"connection": worker}, host.identity, "host")

    if not targets:
        return
    with ThreadPoolExecutor(max_workers=min(options.per_host_drop_workers, len(targets))) as pool:
        futures = [pool.submit(copy_context().run, recover, target) for target in targets]
        failures = [failure for future in futures if (failure := future.exception()) is not None]
    if failures:
        raise failures[0]


def preflight_verified_drop(
    connection: Any,
    plan: VerifiedDropLayout,
    *,
    acknowledged: set[tuple[object, ...]] | None = None,
) -> None:
    require_complete_operation("Dropping existing ClickHouse resources requires complete coverage.")
    require_drop_configuration(plan)
    _require_http_endpoints(plan)
    for resource in plan.resources:
        if acknowledged is None:
            require_drop_resource(connection, resource)
        else:
            unacknowledged_drop_positions(connection, resource, acknowledged)


def _record_acknowledged(
    acknowledged: set[tuple[object, ...]],
    resource: VerifiedDropResource,
    positions: tuple[int, ...],
) -> None:
    acknowledged.update(drop_position_identity(resource, position) for position in positions)


def _wait_for_submitted_resource(
    adapter: Any,
    connection: Any,
    plan: VerifiedDropLayout,
    resource: VerifiedDropResource,
    options: DropExecutionOptions,
) -> None:
    try:
        _wait_absent(connection, plan, resource, options)
    except TimeoutError as error:
        if not options.retry_per_host_drops or resource.scope is None:
            if options.defer_ack_timeout:
                raise AcknowledgedDropWaitTimeoutError(*error.args) from error
            raise
        targets = prepare_drop_recovery(connection, resource)
        execute_drop_recovery(adapter, connection, plan, targets, options)
        _wait_absent(connection, plan, resource, options)


def execute_verified_drop(
    adapter: Any,
    connection: Any,
    plan: VerifiedDropLayout,
    options: DropExecutionOptions,
    *,
    acknowledged: set[tuple[object, ...]] | None = None,
) -> None:
    """Preflight every selected resource before the first destructive submission."""
    completed: set[tuple[object, ...]] = set() if acknowledged is None else acknowledged
    with _configuration_guard(plan):
        preflight_verified_drop(connection, plan, acknowledged=completed)
        for resource in plan.resources:
            raise_if_cancelled()
            require_drop_configuration(plan)
            positions = unacknowledged_drop_positions(connection, resource, completed)
            if positions:
                if len(positions) == len(resource.table_identities):
                    _execute_one(adapter, connection, resource, options)
                else:
                    targets = _drop_position_targets(resource, positions)
                    execute_drop_recovery(
                        adapter,
                        connection,
                        plan,
                        targets,
                        replace(options, allow_already_absent=False),
                    )
                _record_acknowledged(completed, resource, positions)
            if options.wait_for_absence:
                _wait_for_submitted_resource(adapter, connection, plan, resource, options)


__all__ = [
    "DropExecutionOptions",
    "DropRecoveryTarget",
    "execute_drop_recovery",
    "execute_verified_drop",
    "preflight_verified_drop",
    "prepare_drop_recovery",
    "resource_drop_sql",
]
