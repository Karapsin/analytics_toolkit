"""Release coordinator capacity before acquiring captured drop-recovery workers."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from .drop_execution import (
    AcknowledgedDropWaitTimeoutError,
    DropExecutionOptions,
    _configuration_guard,
    _drop_position_targets,
    _record_acknowledged,
    _wait_absent,
    _wait_for_submitted_resource,
    execute_drop_recovery,
    execute_verified_drop,
    preflight_verified_drop,
    prepare_drop_recovery,
)
from .drop_layout import (
    VerifiedDropLayout,
    require_drop_configuration,
    unacknowledged_drop_positions,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from .drop_execution import DropRecoveryTarget


def _submit_bounded_resource(
    adapter: Any,
    runner: Callable[[str, Callable[[Any], Any]], Any],
    plan: VerifiedDropLayout,
    options: DropExecutionOptions,
    acknowledged: set[tuple[object, ...]],
) -> None:
    resource = plan.resources[0]
    inline = replace(
        options,
        retry_per_host_drops=False,
        defer_ack_timeout=options.retry_per_host_drops and resource.scope is not None,
    )

    def remaining(connection: Any) -> tuple[int, ...]:
        require_drop_configuration(plan)
        return unacknowledged_drop_positions(connection, resource, acknowledged)

    def submit(connection: Any) -> None:
        execute_verified_drop(adapter, connection, plan, inline, acknowledged=acknowledged)

    def wait(connection: Any) -> None:
        _wait_for_submitted_resource(adapter, connection, plan, resource, inline)

    positions = runner("resolve_target_drop_positions", remaining)
    if positions and len(positions) == len(resource.table_identities):
        runner("drop_target_pair", submit)
    else:
        if positions:
            # The full certificate remains unchanged; only endpoints whose
            # UUIDs have not already been acknowledged are submitted.
            targets = _drop_position_targets(resource, positions)
            execute_drop_recovery(
                adapter,
                None,
                plan,
                targets,
                replace(options, allow_already_absent=False),
            )
            _record_acknowledged(acknowledged, resource, positions)
        if options.wait_for_absence:
            runner("validate_target_pair_drop", wait)


def _execute_bounded_resource(
    adapter: Any,
    runner: Callable[[str, Callable[[Any], Any]], Any],
    plan: VerifiedDropLayout,
    options: DropExecutionOptions,
    acknowledged: set[tuple[object, ...]],
) -> None:
    resource = plan.resources[0]

    def prepare(connection: Any) -> tuple[DropRecoveryTarget, ...]:
        return prepare_drop_recovery(connection, resource)

    def validate(connection: Any) -> None:
        _wait_absent(connection, plan, resource, options)

    try:
        _submit_bounded_resource(adapter, runner, plan, options, acknowledged)
    except AcknowledgedDropWaitTimeoutError:
        if not options.retry_per_host_drops or resource.scope is None:
            raise
        require_drop_configuration(plan)
        targets = runner("resolve_target_drop_hosts", prepare)
        # No coordinator runner is held while host runners acquire the
        # same capacity lease, including when the shared cap is one.
        execute_drop_recovery(adapter, None, plan, targets, options)
        runner("validate_target_pair_drop", validate)


def execute_bounded_verified_drop(
    adapter: Any,
    runner: Callable[[str, Callable[[Any], Any]], Any],
    certify: Callable[[Any], VerifiedDropLayout],
    options: DropExecutionOptions,
) -> None:
    """Retain one certificate across acknowledged drops, lease-free recovery and validation."""
    plan = runner("certify_target_drop", certify)
    acknowledged: set[tuple[object, ...]] = set()

    def preflight(connection: Any) -> None:
        preflight_verified_drop(connection, plan)

    with _configuration_guard(plan):
        runner("preflight_target_drop", preflight)
        for resource in plan.resources:
            selected = VerifiedDropLayout((resource,), plan.config)
            _execute_bounded_resource(adapter, runner, selected, options, acknowledged)


__all__ = ["execute_bounded_verified_drop"]
