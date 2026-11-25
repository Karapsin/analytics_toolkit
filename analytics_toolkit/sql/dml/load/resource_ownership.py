"""Keep acknowledged load resources bound to their actual creation scopes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from analytics_toolkit.general import time_print
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.creation_resources import supports_created_resource_identity
from analytics_toolkit.sql.backends.stage_ownership import drop_owned_resource
from analytics_toolkit.sql.ddl.creation_resources import resource_retention_message

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from analytics_toolkit.sql.ddl.creation_resources import CreatedTableResource

    from .models import LoadOptions, LoadState


def scoped_creation_kwargs(
    options: LoadOptions, state: LoadState, *, target: bool = False
) -> dict[str, Any]:
    """Request physical acknowledgements only from backends that certify identity."""
    if not supports_created_resource_identity(get_backend_adapter(options.connection_backend)):
        return {}
    if target:
        state.scoped_target_ownership_requested = True
    else:
        state.scoped_stage_ownership_requested = True

    def record(resource: CreatedTableResource) -> None:
        resources = (
            state.owned_target_scoped_resources if target else state.owned_stage_scoped_resources
        )
        resources.append(resource)
        options.destination_context.setdefault("native_assignment_owned_resources", []).append(
            resource.name
        )
        if target:
            state.target_created_by_operation = True

    return {"on_scoped_resource_created": record}


def resolve_stage_creation_policy(backend: str, connection_key: str | None, policy: Any) -> Any:
    if policy is not None or not get_backend_adapter(backend).supports_distributed_table_targets():
        return policy
    from analytics_toolkit.sql.connection.config import get_connection_config  # noqa: PLC0415
    from analytics_toolkit.sql.ddl.ch_policy import resolve_create_ch_policy  # noqa: PLC0415

    return resolve_create_ch_policy(
        get_connection_config(connection_key or backend),
        ddl_scope="staging",
        ch_engine=None,
        ch_cluster=None,
        ch_sharding_key=None,
        ch_distributed_table=None,
        ch_only_shard=False,
        ch_distributed_engine_template=None,
        ch_distributed_cluster=None,
        ch_shard_on_cluster=None,
        ch_distributed_on_cluster=None,
        warn_ch_cluster=False,
    )


def cluster_scoped_creation(policy: Any) -> bool:
    return policy is not None and (
        getattr(policy, "shard_on_cluster", None) is not None
        or (
            getattr(policy, "create_distributed_pair", False)
            and getattr(policy, "distributed_on_cluster", None) is not None
        )
    )


def cleanup_owned_load_stages(
    options: LoadOptions, state: LoadState, run_action: Any, *, dropper: Any = drop_owned_resource
) -> None:
    """Never widen a physical acknowledgement to a nominal policy cluster."""
    scoped_names = {resource.name for resource in state.owned_stage_scoped_resources}
    seen: set[Any] = set()
    for resource in reversed(state.owned_stage_scoped_resources):
        key = (resource.name, resource.scope)
        if key in seen:
            continue
        seen.add(key)
        _drop_resource(options, state, (resource, resource.name, None), run_action, dropper=dropper)
    for legacy_resource, logical_stage, policy in reversed(state.owned_stage_resources):
        if state.scoped_stage_ownership_requested:
            continue
        if legacy_resource in scoped_names or legacy_resource in seen:
            continue
        seen.add(legacy_resource)
        _drop_resource(
            options, state, (legacy_resource, logical_stage, policy), run_action, dropper=dropper
        )


def cleanup_created_load_target(
    options: LoadOptions,
    state: LoadState,
    run_action: Any,
    legacy_cleanup: Callable[[], None],
) -> None:
    resources: Sequence[str | CreatedTableResource] = (
        state.owned_target_scoped_resources
        if state.scoped_target_ownership_requested
        else state.owned_target_resources
    )
    if not resources:
        if not state.scoped_target_ownership_requested:
            legacy_cleanup()
        return
    seen: set[Any] = set()
    for resource in reversed(resources):
        key = (resource.name, resource.scope) if not isinstance(resource, str) else resource
        if key in seen:
            continue
        seen.add(key)
        _drop_resource(options, state, (resource, options.destination_table, None), run_action)


def attach_resource_diagnostics(error: BaseException, state: LoadState) -> None:
    """Report retained resources without masking or replacing the primary error."""
    resources = [*state.owned_stage_scoped_resources, *state.owned_target_scoped_resources]
    if resources:
        error.__dict__["analytics_toolkit_sql_acknowledged_scoped_resources"] = tuple(
            (item.name, item.scope.kind, item.scope.cluster, item.scope.endpoint)
            for item in resources
        )
    if state.retained_resource_messages:
        error.__dict__["analytics_toolkit_sql_retained_resources"] = tuple(
            state.retained_resource_messages
        )
    if state.uncertain_stage_resources:
        error.__dict__["analytics_toolkit_sql_uncertain_stage_resources"] = tuple(
            state.uncertain_stage_resources
        )


def _drop_resource(
    options: LoadOptions,
    state: LoadState,
    ownership: tuple[str | CreatedTableResource, str, Any],
    run_action: Any,
    *,
    dropper: Any = drop_owned_resource,
) -> None:
    resource, logical_stage, policy = ownership
    try:
        run_action(
            options,
            "cleanup_stage",
            lambda ref: dropper(
                get_backend_adapter(options.connection_backend),
                ref["connection"],
                resource,
                logical_stage=logical_stage,
                creation_policy=policy,
            ),
        )
    except Exception as error:  # noqa: BLE001 - retain the original operation failure.
        message = (
            f"Retaining acknowledged load resource {resource}; cleanup failed."
            if isinstance(resource, str)
            else resource_retention_message(resource)
        )
        message += f" Cleanup failed ({type(error).__name__})."
        state.retained_resource_messages.append(message)
        time_print(message)
