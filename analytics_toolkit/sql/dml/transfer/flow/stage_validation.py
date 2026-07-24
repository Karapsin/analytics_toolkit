from __future__ import annotations

# ruff: noqa: EM101, EM102, I001, S608, TC001, TC003, TID252, TRY003

from collections.abc import Mapping, Sequence
from typing import Any

from ....backends import get_backend_adapter
from ....dml.io.read_sql import _read_backend
from ..runtime.models import TransferOptions
from .stage_identity import TransferInternalColumns


def validate_transfer_stage_identity(
    *,
    options: TransferOptions,
    connection: Any,
    stage_tables: Sequence[str],
    internal_columns: TransferInternalColumns,
    expected_slice_counts: Mapping[int, int],
) -> None:
    transfer_id = options.transfer_id
    destination = options.canonical_destination_identity
    if transfer_id is None or destination is None:
        raise RuntimeError("Transfer runtime identity was not initialized.")
    for stage_table in stage_tables:
        identity_rows = _rows(
            options.to_db_backend,
            connection,
            build_stage_identity_sql(
                options.to_db_backend,
                stage_table,
                internal_columns,
            ),
        )
        if identity_rows and identity_rows != [(transfer_id, destination)]:
            raise RuntimeError(
                f"Transfer stage integrity failure for {stage_table}: mixed or "
                "unexpected transfer/destination identity."
            )

    aggregate_sql = " UNION ALL ".join(
        f"SELECT * FROM {stage_table}" for stage_table in stage_tables
    )
    ordinal_rows = _rows(
        options.to_db_backend,
        connection,
        build_stage_ordinal_validation_sql(
            options.to_db_backend,
            aggregate_sql,
            internal_columns,
        ),
    )
    actual = {
        int(slice_id): (int(minimum), int(maximum), int(count), int(distinct_count))
        for slice_id, minimum, maximum, count, distinct_count in ordinal_rows
    }
    for slice_id, expected_count in expected_slice_counts.items():
        if expected_count == 0:
            if slice_id in actual:
                raise RuntimeError(f"Unexpected ordinal rows for empty slice {slice_id}.")
            continue
        expected = (1, expected_count, expected_count, expected_count)
        if actual.get(slice_id) != expected:
            raise RuntimeError(
                f"Transfer stage ordinal integrity failure for slice {slice_id}: "
                f"expected {expected}, got {actual.get(slice_id)}."
            )
    if set(actual) != {key for key, value in expected_slice_counts.items() if value}:
        raise RuntimeError("Transfer stage contains an unexpected slice ID.")


def build_stage_identity_sql(
    backend: str,
    stage_table: str,
    internal_columns: TransferInternalColumns,
) -> str:
    adapter = get_backend_adapter(backend)
    transfer_column = adapter.quote_identifier(internal_columns.transfer_id)
    destination_column = adapter.quote_identifier(internal_columns.destination_table)
    return (
        f"SELECT {transfer_column}, {destination_column} FROM {stage_table} "
        f"GROUP BY {transfer_column}, {destination_column}"
    )


def build_stage_ordinal_validation_sql(
    backend: str,
    aggregate_sql: str,
    internal_columns: TransferInternalColumns,
) -> str:
    adapter = get_backend_adapter(backend)
    slice_column = adapter.quote_identifier(internal_columns.slice_id)
    ordinal_column = adapter.quote_identifier(internal_columns.row_ordinal)
    return (
        f"SELECT {slice_column}, MIN({ordinal_column}), MAX({ordinal_column}), "
        f"COUNT(*), COUNT(DISTINCT {ordinal_column}) FROM ({aggregate_sql}) AS stage_rows "
        f"GROUP BY {slice_column} ORDER BY {slice_column}"
    )


def _rows(backend: str, connection: Any, sql: str) -> list[tuple[Any, ...]]:
    result = _read_backend(
        backend,
        connection,
        sql,
        print_queries=False,
        output_type="dict",
    )
    return [tuple(values) for values in zip(*result.columns)]
