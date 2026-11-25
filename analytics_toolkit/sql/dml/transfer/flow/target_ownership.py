"""Track and clean only target generations acknowledged by this transfer."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from analytics_toolkit.general import time_print
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.stage_ownership import drop_owned_resource

if TYPE_CHECKING:
    from collections.abc import Callable

    from analytics_toolkit.sql.ddl.creation_resources import CreatedTableResource
    from analytics_toolkit.sql.dml.transfer.runtime.models import (
        TransferOptions,
        TransferStageState,
    )


def target_creation_callbacks(state: TransferStageState) -> dict[str, Any]:
    def acknowledged(name: str) -> None:
        if name not in state.acknowledged_target_names:
            state.acknowledged_target_names.append(name)
        state.target_created_by_operation = state.target_existed_at_start is False

    def scoped(resource: CreatedTableResource) -> None:
        if resource not in state.owned_target_resources:
            state.owned_target_resources.append(resource)
        acknowledged(resource.name)

    return {
        "on_resource_created": acknowledged,
        "on_scoped_resource_created": scoped,
    }


def cleanup_owned_transfer_target(
    options: TransferOptions,
    state: TransferStageState,
    run_target: Callable[[Any], Any],
) -> None:
    if not state.target_created_by_operation or state.target_existed_at_start is not False:
        return
    resources: list[str | CreatedTableResource] = (
        list(state.owned_target_resources)
        if options.to_db_backend in {"gp", "ch"}
        else list(state.acknowledged_target_names)
    )
    retained: list[str | CreatedTableResource] = options.input_resolution.setdefault(
        "retained_target_cleanup", []
    )
    first_error: Exception | None = None
    for resource in reversed(resources):
        if resource in retained:
            continue
        try:
            run_target(
                lambda ref, resource=resource: drop_owned_resource(
                    get_backend_adapter(options.to_db_backend),
                    ref["connection"],
                    resource,
                    logical_stage=options.target_table,
                    creation_policy=options.regular_ch_policy,
                )
            )
        except Exception as error:  # noqa: BLE001 -- attempt every distinct owned resource.
            retained.append(resource)
            first_error = first_error or error
            continue
        state.owned_target_resources[:] = [
            owned for owned in state.owned_target_resources if owned != resource
        ]
    if first_error is not None:
        raise first_error
    if resources and not any(resource in retained for resource in resources):
        state.acknowledged_target_names.clear()
        state.target_exists = False
        state.target_created_by_operation = False


def cleanup_failed_transfer_target(
    options: TransferOptions,
    state: TransferStageState,
    run_target: Callable[[Any], Any],
    primary: Exception,
) -> None:
    """Retain the original write failure when verified cleanup cannot complete."""
    try:
        cleanup_owned_transfer_target(options, state, run_target)
    except Exception as error:  # noqa: BLE001 -- cleanup must preserve the primary failure.
        message = f"Target cleanup retained {options.target_table}: {type(error).__name__}."
        primary.__dict__["analytics_toolkit_sql_cleanup_error"] = error
        primary.__dict__["analytics_toolkit_sql_retained_target_resources"] = tuple(
            options.input_resolution.get("retained_target_cleanup", ())
        )
        primary.__dict__["analytics_toolkit_sql_retry_safe"] = False
        if hasattr(primary, "add_note"):
            primary.add_note(message)
        time_print(message)
