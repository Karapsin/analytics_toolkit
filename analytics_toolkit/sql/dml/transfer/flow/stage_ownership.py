"""Track candidate visibility separately from acknowledged stage ownership."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from analytics_toolkit.general import time_print
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.creation_resources import supports_created_resource_identity
from analytics_toolkit.sql.backends.stage_ownership import drop_owned_resource
from analytics_toolkit.sql.dml.transfer.runtime.models import TransferSliceRowCount

from .staged_keyed_logging import slice_tag as _slice_tag

if TYPE_CHECKING:
    import pandas as pd

    from analytics_toolkit.sql.ddl.creation_resources import CreatedTableResource
    from analytics_toolkit.sql.dml.transfer.runtime.models import (
        TransferConnectionRefs,
        TransferOptions,
        TransferStageState,
    )


def stage_creation_callbacks(state: Any, backend: str | None = None) -> dict[str, Any]:
    candidate: list[str] = []

    def visible(name: str) -> None:
        candidate[:] = [name]
        state.stage_table_candidates.append(name)

    def owned(name: str) -> None:
        state.owned_stage_resources.append((name, candidate[-1]))

    callbacks: dict[str, Any] = {"on_stage_candidate": visible, "on_resource_created": owned}
    if backend is not None and supports_created_resource_identity(get_backend_adapter(backend)):
        state.scoped_stage_ownership_requested = True
        if not hasattr(state, "owned_stage_scoped_resources"):
            state.owned_stage_scoped_resources = []

        def scoped(resource: CreatedTableResource) -> None:
            state.owned_stage_scoped_resources.append((resource, candidate[-1]))

        callbacks["on_scoped_resource_created"] = scoped
    return callbacks


def cleanup_acknowledged_stages(
    options: Any, state: Any, run_target: Any, *, retained_resources: frozenset[str] = frozenset()
) -> None:

    seen: set[Any] = set()
    first_error: Exception | None = None
    scoped = getattr(state, "owned_stage_scoped_resources", [])
    identity_required = supports_created_resource_identity(
        get_backend_adapter(options.to_db_backend)
    )
    if identity_required:
        scoped_names = {resource.name for resource, _stage in scoped}
        for name, _stage in state.owned_stage_resources:
            if name not in scoped_names:
                time_print(f"Retaining stage resource {name}: native ownership is unverified.")
    resources = scoped if identity_required else state.owned_stage_resources
    for resource, stage in reversed(resources):
        name = resource.name if identity_required else resource
        key = (name, resource.scope) if identity_required else name
        if key in seen or name in retained_resources:
            continue
        seen.add(key)
        try:
            run_target(
                lambda ref, resource=resource, stage=stage: drop_owned_resource(
                    get_backend_adapter(options.to_db_backend),
                    ref["connection"],
                    resource,
                    logical_stage=stage,
                    creation_policy=options.staging_ch_policy,
                )
            )
        except Exception as exc:  # noqa: BLE001 -- attempt every independently owned cleanup.
            first_error = first_error or exc
    if first_error is not None:
        raise first_error


def sync_stage_state(
    options: Any,
    stage_state: Any,
    runtime: Any,
    *,
    require_complete: bool,
) -> None:

    verified = runtime.verified
    expected_ids = {item.index for item in options.transfer_slices or []}
    if require_complete and set(verified) != expected_ids:
        message = "Not every transfer key reached a verified checkpoint."
        raise RuntimeError(message)
    stage_state.source_stage_tables = runtime.source_stage_tables
    stage_map = runtime.target_stages
    registered_stage_tables = [stage_map[index] for index in sorted(stage_map)]
    stage_tables = list(registered_stage_tables)
    stage_state.stage_tables = stage_tables
    stage_state.stage_table = registered_stage_tables[0] if registered_stage_tables else None
    stage_state.stage_table_created = bool(stage_tables)
    stage_state.expected_source_rows = sum(item.expected_rows for item in verified.values())
    stage_state.slice_counts = [
        TransferSliceRowCount(
            index=transfer_slice.index,
            label=_slice_tag(options, transfer_slice),
            expected_rows=verified[transfer_slice.index].expected_rows,
            streamed_rows=verified[transfer_slice.index].streamed_rows,
        )
        for transfer_slice in options.transfer_slices or []
        if transfer_slice.index in verified
    ]


def create_keyed_worker_stage_tables(  # noqa: PLR0913 -- injected stage construction boundary.
    *,
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    stage_state: TransferStageState,
    sample_batch: pd.DataFrame,
    column_types: dict[str, str] | None,
    create_stage: Any,
    run_token: str,
) -> list[str]:
    transfer_slices = options.transfer_slices or []
    worker_count = min(
        options.transfer_concurrency.effective_write,
        len(transfer_slices),
    )
    stage_tables: list[str] = []
    for worker_index in range(worker_count):
        stage_table = create_stage(
            connection_type=options.to_db_backend,
            connection=connection_refs.target["connection"],
            target_table=options.target_table,
            batch=sample_batch,
            column_types=column_types,
            gp_distributed_by_key=options.gp_distributed_by_key,
            connection_key=options.to_db_key,
            query_label=options.query_label,
            transfer_staging_schema=options.transfer_staging_schema,
            transfer_staging_username=options.transfer_staging_username,
            random_suffix=f"{run_token}__w{worker_index:05d}",
            destination_hash=options.destination_hash,
            ddl_properties=options.staging_ddl_properties,
            ch_creation_policy=options.staging_ch_policy,
            **stage_creation_callbacks(stage_state, options.to_db_backend),
        )
        stage_tables.append(stage_table)
        stage_state.stage_table = stage_tables[0]
        stage_state.stage_tables = list(stage_tables)
        stage_state.stage_table_created = True
    return stage_tables


def uses_keyed_worker_stages(options: TransferOptions) -> bool:
    return (
        options.transfer_slices is not None
        and options.trino_mode != "parquet"
        and options.transfer_concurrency.effective_write > 1
        and len(options.transfer_slices) > 1
    )


def cleanup_target_superseded_stages(
    options: TransferOptions,
    stage_state: TransferStageState,
    run_target: Any,
    cleanup: Any,
) -> None:
    if stage_state.internal_columns is None:
        return
    run_target(
        options,
        "cleanup_superseded_stages",
        lambda target_ref: cleanup(
            options=options,
            connection=target_ref["connection"],
            backend=options.to_db_backend,
            connection_key=options.to_db_key,
            staging_schema=options.transfer_staging_schema,
            internal_columns=stage_state.internal_columns,
        ),
    )
