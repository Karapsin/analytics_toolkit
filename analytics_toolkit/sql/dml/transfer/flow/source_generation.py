"""Observe one selected source generation through owned snapshot materialization."""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.backends.stage_ownership import single_resource_policy
from analytics_toolkit.sql.execution.backend_options import current_backend_operation

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from analytics_toolkit.sql.dml.transfer.runtime.models import (
        TransferOptions,
        TransferStageState,
    )

_OBSERVER: ContextVar[Callable[[], None] | None] = ContextVar(
    "sql_transfer_source_generation_observer", default=None
)


def _source_statuses(source_alias: str) -> tuple[tuple[Any, str], ...]:
    operation = current_backend_operation()
    if operation is None:
        return ()
    with operation.lock:
        return tuple(
            (key, json.dumps(status, sort_keys=True, default=str))
            for key, status in operation.statuses.items()
            if status.get("alias", "").lower() == source_alias.lower()
        )


def source_availability_scope(source_alias: str) -> tuple[str, ...]:
    return tuple(sorted(signature for _key, signature in _source_statuses(source_alias)))


def observe_generation_scope(source_alias: str, observed: dict[Any, str]) -> bool:
    """Return false when an already selected source scope changes its coverage."""
    for key, signature in _source_statuses(source_alias):
        if key in observed and observed[key] != signature:
            return False
        observed[key] = signature
    return True


@contextmanager
def observed_source_operation() -> Iterator[None]:
    """Check around real source work without masking its primary failure."""
    selected = _OBSERVER.get()
    if selected is not None:
        selected()
    yield
    if selected is not None:
        selected()


@contextmanager
def observing_source_generation(observer: Callable[[], None]) -> Iterator[None]:
    token = _OBSERVER.set(observer)
    try:
        with observed_source_operation():
            yield
    finally:
        _OBSERVER.reset(token)


@dataclass(frozen=True)
class SourceSnapshotOperations:
    """Keep the historical staged-attempt callback seams bound at call time."""

    adapter: Callable[[str], Any]
    allocate: Callable[..., str]
    select_sql: Callable[..., str]
    create: Callable[..., Any]
    append_sql: Callable[..., str]
    materialize: Callable[..., Any]
    counts: Callable[..., dict[int, int]]
    cleanup: Callable[..., Any]
    log: Callable[..., Any]


def materialize_source_snapshot(
    options: TransferOptions,
    source_ref: dict[str, Any],
    stage_state: TransferStageState,
    operations: SourceSnapshotOperations,
) -> tuple[str, dict[int, int]]:
    staging_schema = options.source_transfer_staging_schema
    internal = stage_state.internal_columns
    if staging_schema is None or internal is None:
        message = "Source snapshot configuration is incomplete."
        raise RuntimeError(message)
    with observed_source_operation():
        snapshot_table = operations.allocate(options, source_ref)
    slices = options.transfer_slices or []
    source_sqls = (
        [(item.index, item.source_sql) for item in slices] if slices else [(0, options.source_sql)]
    )
    adapter = operations.adapter(options.from_db_backend)
    created = False
    snapshot_owned: list[str] = []
    cluster_routed = False
    post_create_sqls: tuple[str, ...] = ()

    try:
        for slice_id, source_sql in source_sqls:
            select_sql = operations.select_sql(
                backend=options.from_db_backend,
                source_sql=source_sql,
                source_columns=stage_state.source_columns,
                transfer_id=options.transfer_id or "",
                canonical_destination=options.canonical_destination_identity or "",
                slice_id=slice_id,
                internal_columns=internal,
            )
            if not created:
                with observed_source_operation():
                    snapshot_sql = operations.create(
                        backend=options.from_db_backend,
                        connection=source_ref["connection"],
                        snapshot_table=snapshot_table,
                        snapshot_select_sql=select_sql,
                        internal_columns=internal,
                        source_staging_ch_policy=getattr(options, "source_staging_ch_policy", None),
                        run_post_create_sqls=False,
                        on_resource_created=snapshot_owned.append,
                    )
                    snapshot_owned.append(snapshot_table)
                cluster_routed = snapshot_sql.populate_sql is not None
                post_create_sqls = snapshot_sql.post_create_sqls
                created = True
            else:
                with observed_source_operation():
                    operations.materialize(
                        adapter,
                        options.from_db_backend,
                        source_ref["connection"],
                        operations.append_sql(
                            backend=options.from_db_backend,
                            snapshot_table=snapshot_table,
                            source_columns=stage_state.source_columns,
                            internal_columns=internal,
                            snapshot_select_sql=select_sql,
                            cluster_routed=cluster_routed,
                        ),
                    )
        for statement in post_create_sqls:
            with observed_source_operation():
                adapter.execute_command(source_ref["connection"], statement)
        with observed_source_operation():
            counts = operations.counts(
                options, source_ref["connection"], snapshot_table, internal.slice_id
            )
    except BaseException as exc:
        if not snapshot_owned:
            exc.__dict__["analytics_toolkit_sql_source_stage_candidate"] = snapshot_table
        if snapshot_owned:
            try:
                operations.cleanup(
                    options.from_db_backend,
                    source_ref["connection"],
                    snapshot_table,
                    query_label=options.query_label,
                    ch_creation_policy=single_resource_policy(
                        getattr(options, "source_staging_ch_policy", None)
                    ),
                )
            except BaseException:  # noqa: BLE001 -- cleanup preserves the source failure
                operations.log(
                    "[slice=1/1] Failed to drop an incomplete source snapshot; "
                    "startup cleanup will retry it",
                    level="warning",
                )
        raise
    else:
        return snapshot_table, counts
