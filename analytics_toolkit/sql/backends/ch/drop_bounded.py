"""Release coordinator capacity before acquiring captured drop-recovery workers."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from .drop_execution import (
    DropExecutionOptions,
    _configuration_guard,
    _wait_absent,
    execute_drop_recovery,
    execute_verified_drop,
    preflight_verified_drop,
    prepare_drop_recovery,
)
from .drop_layout import VerifiedDropLayout, require_drop_configuration

if TYPE_CHECKING:
    from collections.abc import Callable

    from .drop_execution import DropRecoveryTarget
    from .drop_layout import VerifiedDropResource


def execute_bounded_verified_drop(
    adapter: Any,
    runner: Callable[[str, Callable[[Any], Any]], Any],
    certify: Callable[[Any], VerifiedDropLayout],
    options: DropExecutionOptions,
) -> None:
    """Retain one certificate across acknowledged drops, lease-free recovery and validation."""
    plan = runner("certify_target_drop", certify)
    inline = replace(options, retry_per_host_drops=False)

    def preflight(connection: Any) -> None:
        preflight_verified_drop(connection, plan)

    with _configuration_guard(plan):
        runner("preflight_target_drop", preflight)
        for resource in plan.resources:
            selected = VerifiedDropLayout((resource,), plan.config)

            def submit(connection: Any, *, current: VerifiedDropLayout = selected) -> None:
                execute_verified_drop(adapter, connection, current, inline)

            def prepare(
                connection: Any, *, current: VerifiedDropResource = resource
            ) -> tuple[DropRecoveryTarget, ...]:
                return prepare_drop_recovery(connection, current)

            def validate(connection: Any, *, current: VerifiedDropResource = resource) -> None:
                _wait_absent(connection, plan, current, options)

            try:
                runner("drop_target_pair", submit)
            except TimeoutError:
                if not options.retry_per_host_drops or resource.scope is None:
                    raise
                require_drop_configuration(plan)
                targets = runner("resolve_target_drop_hosts", prepare)
                # No coordinator runner is held while host runners acquire the
                # same capacity lease, including when the shared cap is one.
                execute_drop_recovery(adapter, None, plan, targets, options)
                runner("validate_target_pair_drop", validate)


__all__ = ["execute_bounded_verified_drop"]
