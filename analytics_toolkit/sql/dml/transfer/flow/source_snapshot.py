from __future__ import annotations

# ruff: noqa: I001, PLR0913, S608, TC001, TID252

from dataclasses import dataclass
from typing import Any, Sequence

from ....backends import get_backend_adapter
from ....backends.transfer_stage import (
    build_append_source_snapshot_sql,
    build_cluster_routed_source_snapshot_sqls,
    build_source_snapshot_populate_sql,
    build_source_snapshot_sqls,
    execute_transfer_materialization,
    is_cluster_routed_source_snapshot,
)
from .range_scheduler import OrdinalRange
from .stage_identity import TransferInternalColumns

_MISSING_ROUTED_POLICY = "Cluster-routed ClickHouse source staging policy is missing."


@dataclass(frozen=True)
class SourceSnapshotSql:
    create_sql: str
    populate_sql: str | None
    post_create_sqls: tuple[str, ...]


def build_snapshot_select_sql(
    *,
    backend: str,
    source_sql: str,
    source_columns: Sequence[str],
    transfer_id: str,
    canonical_destination: str,
    slice_id: int,
    internal_columns: TransferInternalColumns,
) -> str:
    del transfer_id, canonical_destination
    adapter = get_backend_adapter(backend)
    projected = [f"source_rows.{adapter.quote_identifier(column)}" for column in source_columns]
    slice_column, ordinal_column = (
        adapter.quote_identifier(column) for column in internal_columns.paging_names()
    )
    projected.extend(
        [
            f"{slice_id} AS {slice_column}",
            (f"row_number() OVER (PARTITION BY {slice_id}) AS {ordinal_column}"),
        ]
    )
    stripped_source = adapter.strip_query_semicolon(source_sql)
    return f"SELECT {', '.join(projected)} FROM ({stripped_source}) AS source_rows"


def build_source_snapshot_sql(
    *,
    backend: str,
    snapshot_table: str,
    snapshot_select_sql: str,
    internal_columns: TransferInternalColumns,
    cluster_routed: bool = False,
) -> SourceSnapshotSql:
    adapter = get_backend_adapter(backend)
    slice_column = adapter.quote_identifier(internal_columns.slice_id)
    ordinal_column = adapter.quote_identifier(internal_columns.row_ordinal)
    builder = build_source_snapshot_sqls
    if cluster_routed:
        builder = build_cluster_routed_source_snapshot_sqls
    create_sql, post_create_sqls = builder(
        backend,
        snapshot_table,
        snapshot_select_sql,
        slice_column,
        ordinal_column,
    )
    populate_sql = build_source_snapshot_populate_sql(
        backend,
        snapshot_table,
        snapshot_select_sql,
        cluster_routed=cluster_routed,
    )
    return SourceSnapshotSql(
        create_sql=create_sql,
        populate_sql=populate_sql,
        post_create_sqls=post_create_sqls,
    )


def execute_source_snapshot_materialization(
    *,
    backend: str,
    connection: Any,
    snapshot_table: str,
    snapshot_select_sql: str,
    internal_columns: TransferInternalColumns,
    source_staging_ch_policy: Any = None,
    run_post_create_sqls: bool = True,
) -> SourceSnapshotSql:
    adapter = get_backend_adapter(backend)
    cluster_routed = is_cluster_routed_source_snapshot(backend, connection)
    snapshot_sql = build_source_snapshot_sql(
        backend=backend,
        snapshot_table=snapshot_table,
        snapshot_select_sql=snapshot_select_sql,
        internal_columns=internal_columns,
        cluster_routed=cluster_routed,
    )
    execute_transfer_materialization(
        adapter,
        backend,
        connection,
        snapshot_sql.create_sql,
    )
    if snapshot_sql.populate_sql is not None:
        if source_staging_ch_policy is None:
            raise RuntimeError(_MISSING_ROUTED_POLICY)
        adapter.after_create_table(
            connection,
            snapshot_table,
            ch_only_shard=True,
            ch_creation_policy=source_staging_ch_policy,
        )
        execute_transfer_materialization(
            adapter,
            backend,
            connection,
            snapshot_sql.populate_sql,
        )
    if run_post_create_sqls:
        for sql in snapshot_sql.post_create_sqls:
            adapter.execute_command(connection, sql)
    return snapshot_sql


def build_append_snapshot_slice_sql(
    *,
    backend: str,
    snapshot_table: str,
    source_columns: Sequence[str],
    internal_columns: TransferInternalColumns,
    snapshot_select_sql: str,
    cluster_routed: bool = False,
) -> str:
    adapter = get_backend_adapter(backend)
    columns = [*source_columns, *internal_columns.paging_names()]
    column_sql = ", ".join(adapter.quote_identifier(column) for column in columns)
    return build_append_source_snapshot_sql(
        backend,
        snapshot_table,
        column_sql,
        snapshot_select_sql,
        cluster_routed=cluster_routed,
    )


def build_snapshot_range_sql(
    *,
    backend: str,
    snapshot_table: str,
    source_columns: Sequence[str],
    internal_columns: TransferInternalColumns,
    transfer_id: str,
    canonical_destination: str,
    ordinal_range: OrdinalRange,
) -> str:
    del transfer_id, canonical_destination
    adapter = get_backend_adapter(backend)
    row_limit = ordinal_range.stop_ordinal - ordinal_range.start_ordinal
    columns = list(source_columns)
    projected = ", ".join(adapter.quote_identifier(column) for column in columns)
    slice_column, ordinal_column = (
        adapter.quote_identifier(column) for column in internal_columns.paging_names()
    )
    return (
        f"SELECT {projected} FROM {snapshot_table} WHERE "
        f"{slice_column} = {ordinal_range.slice_id} AND "
        f"{ordinal_column} >= {ordinal_range.start_ordinal} AND "
        f"{ordinal_column} < {ordinal_range.stop_ordinal} "
        f"ORDER BY {ordinal_column} LIMIT {row_limit}"
    )
