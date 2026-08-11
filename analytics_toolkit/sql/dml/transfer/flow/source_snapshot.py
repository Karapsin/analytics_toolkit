from __future__ import annotations

# ruff: noqa: I001, PLR0913, S608, TC001, TID252

from dataclasses import dataclass
from typing import Sequence

from ....backends import get_backend_adapter
from ....backends.transfer_stage import build_source_snapshot_sqls
from .range_scheduler import OrdinalRange
from .stage_identity import TransferInternalColumns


@dataclass(frozen=True)
class SourceSnapshotSql:
    create_sql: str
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
) -> SourceSnapshotSql:
    adapter = get_backend_adapter(backend)
    slice_column = adapter.quote_identifier(internal_columns.slice_id)
    ordinal_column = adapter.quote_identifier(internal_columns.row_ordinal)
    create_sql, post_create_sqls = build_source_snapshot_sqls(
        backend,
        snapshot_table,
        snapshot_select_sql,
        slice_column,
        ordinal_column,
    )
    return SourceSnapshotSql(create_sql=create_sql, post_create_sqls=post_create_sqls)


def build_append_snapshot_slice_sql(
    *,
    backend: str,
    snapshot_table: str,
    source_columns: Sequence[str],
    internal_columns: TransferInternalColumns,
    snapshot_select_sql: str,
) -> str:
    adapter = get_backend_adapter(backend)
    columns = [*source_columns, *internal_columns.paging_names()]
    column_sql = ", ".join(adapter.quote_identifier(column) for column in columns)
    return f"INSERT INTO {snapshot_table} ({column_sql}) {snapshot_select_sql}"


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
