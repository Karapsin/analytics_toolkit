from __future__ import annotations

# ruff: noqa: EM101, EM102, I001, S608, TC001, TC003, TID252, TRY003

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ....backends import get_backend_adapter
from ....dml.io.read_sql import _read_backend
from ...._log_context import sql_log_context
from ..runtime.models import TransferOptions
from .stage_identity import TransferInternalColumns


@dataclass(frozen=True)
class _SliceOrdinalValidation:
    stage_table: str
    ordinal_column: str
    where_slice: str
    slice_id: int
    expected_count: int
    log_prefix: str


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
            action_name="stage-identity validation",
            phase="validate_stage_identity",
        )
        if not identity_rows:
            raise RuntimeError(
                f"Transfer stage integrity failure for {stage_table}: the created stage is empty."
            )
        if identity_rows != [(transfer_id, destination)]:
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
        action_name="ordinal validation",
        phase="validate_stage_ordinals",
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


def validate_transfer_stage_slice(  # noqa: PLR0913 -- compatibility surface
    *,
    options: TransferOptions,
    connection: Any,
    stage_table: str | Sequence[str] | None,
    internal_columns: TransferInternalColumns,
    slice_id: int,
    expected_count: int,
    streamed_count: int,
    log_prefix: str = "",
) -> None:
    """Validate one immutable keyed slice before its source stage is acknowledged."""
    if streamed_count != expected_count:
        raise RuntimeError(
            f"Transfer slice {slice_id} streamed {streamed_count} row(s); "
            f"expected {expected_count}."
        )
    if not isinstance(stage_table, str):
        if expected_count != 0:
            raise RuntimeError(f"Transfer slice {slice_id} has no target stage.")
        for candidate in list(stage_table or []):
            validate_transfer_stage_slice(
                options=options,
                connection=connection,
                stage_table=candidate,
                internal_columns=internal_columns,
                slice_id=slice_id,
                expected_count=0,
                streamed_count=0,
                log_prefix=log_prefix,
            )
        if not stage_table:
            _validate_no_stage_empty_slice(options, connection, slice_id, log_prefix)
        return

    transfer_id = options.transfer_id
    destination = options.canonical_destination_identity
    if transfer_id is None or destination is None:
        raise RuntimeError("Transfer runtime identity was not initialized.")
    adapter = get_backend_adapter(options.to_db_backend)
    transfer_column = adapter.quote_identifier(internal_columns.transfer_id)
    destination_column = adapter.quote_identifier(internal_columns.destination_table)
    slice_column = adapter.quote_identifier(internal_columns.slice_id)
    ordinal_column = adapter.quote_identifier(internal_columns.row_ordinal)
    where_slice = f"{slice_column} = {slice_id}"

    identity_rows = _rows(
        options.to_db_backend,
        connection,
        (
            f"SELECT {transfer_column}, {destination_column}, COUNT(*) "
            f"FROM {stage_table} WHERE {where_slice} "
            f"GROUP BY {transfer_column}, {destination_column}"
        ),
        action_name="keyed stage-slice identity validation",
        phase="validate_stage_slice_identity",
        log_prefix=log_prefix,
    )
    expected_identity = [] if expected_count == 0 else [(transfer_id, destination, expected_count)]
    if identity_rows != expected_identity:
        raise RuntimeError(
            f"Transfer stage identity/count failure for slice {slice_id}: "
            f"expected {expected_count} row(s)."
        )
    if expected_count == 0:
        return

    _validate_slice_ordinals(
        options,
        connection,
        _SliceOrdinalValidation(
            stage_table=stage_table,
            ordinal_column=ordinal_column,
            where_slice=where_slice,
            slice_id=slice_id,
            expected_count=expected_count,
            log_prefix=log_prefix,
        ),
    )


def _validate_slice_ordinals(
    options: TransferOptions,
    connection: Any,
    validation: _SliceOrdinalValidation,
) -> None:
    ordinal_column = validation.ordinal_column
    ordinal_rows = _rows(
        options.to_db_backend,
        connection,
        (
            f"SELECT MIN({ordinal_column}), MAX({ordinal_column}), COUNT(*), "
            f"COUNT(DISTINCT {ordinal_column}) FROM {validation.stage_table} "
            f"WHERE {validation.where_slice}"
        ),
        action_name="keyed stage-slice ordinal validation",
        phase="validate_stage_slice_ordinals",
        log_prefix=validation.log_prefix,
    )
    if len(ordinal_rows) != 1:
        raise RuntimeError(
            f"Transfer stage returned no ordinal aggregate for slice {validation.slice_id}."
        )
    minimum, maximum, count, distinct_count = ordinal_rows[0]
    actual = (
        None if minimum is None else int(minimum),
        None if maximum is None else int(maximum),
        int(count),
        int(distinct_count),
    )
    expected_ordinals = (
        1,
        validation.expected_count,
        validation.expected_count,
        validation.expected_count,
    )
    if actual != expected_ordinals:
        raise RuntimeError(
            f"Transfer stage ordinal integrity failure for slice {validation.slice_id}: "
            f"expected {expected_ordinals}, got {actual}."
        )


def _validate_no_stage_empty_slice(
    options: TransferOptions,
    connection: Any,
    slice_id: int,
    log_prefix: str,
) -> None:
    rows = _rows(
        options.to_db_backend,
        connection,
        "SELECT 0",
        action_name="empty keyed stage-slice validation",
        phase="validate_empty_stage_slice",
        log_prefix=log_prefix,
    )
    if rows != [(0,)]:
        raise RuntimeError(f"Transfer slice {slice_id} empty validation failed.")


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


def _rows(  # noqa: PLR0913 -- preserve test/import compatibility for this helper
    backend: str,
    connection: Any,
    sql: str,
    *,
    action_name: str,
    phase: str,
    log_prefix: str = "",
) -> list[tuple[Any, ...]]:
    with sql_log_context(log_prefix, suppress_sql=True):
        result = _read_backend(
            backend,
            connection,
            sql,
            print_queries=False,
            output_type="dict",
            action_name=action_name,
            phase=phase,
        )
    return [tuple(values) for values in zip(*result.columns)]
