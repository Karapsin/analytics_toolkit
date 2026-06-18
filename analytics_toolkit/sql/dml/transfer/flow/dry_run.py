from __future__ import annotations

from sqlglot import exp, parse_one

from ....core.identifiers import sqlglot_dialect
from ...load.stage import build_stage_table_name
from .parquet_stage import build_stage_external_location
from ..runtime.models import TransferOptions


def dry_run_stage_table_names(options: TransferOptions) -> list[str]:
    worker_count = dry_run_worker_stage_count(options)
    if worker_count == 1:
        return [dry_run_stage_table_name(options)]
    return [
        dry_run_stage_table_name(options, worker_index=worker_index)
        for worker_index in range(worker_count)
    ]


def dry_run_worker_stage_count(options: TransferOptions) -> int:
    if (
        options.transfer_slices is None
        or options.trino_mode == "parquet"
        or options.concurrency <= 1
    ):
        return 1
    return min(options.concurrency, len(options.transfer_slices))


def dry_run_stage_table_name(
    options: TransferOptions,
    *,
    worker_index: int | None = None,
) -> str:
    suffix = "dryrun" if worker_index is None else f"dryrun__w{worker_index:05d}"
    try:
        return build_stage_table_name(
            options.to_db_backend,
            options.target_table,
            transfer_staging_schema=options.transfer_staging_schema,
            transfer_staging_username=options.transfer_staging_username,
            random_suffix=suffix,
        )
    except Exception:
        return f"{options.target_table}__stage__{suffix}"


def source_batches_label(
    options: TransferOptions,
    worker_index: int | None = None,
) -> str:
    if options.transfer_slices is None:
        return "source batches"
    if worker_index is None or dry_run_worker_stage_count(options) == 1:
        return "shared keyed source slice batches"
    worker_count = dry_run_worker_stage_count(options)
    slice_indexes = [
        transfer_slice.index
        for transfer_slice in options.transfer_slices[worker_index::worker_count]
    ]
    return (
        f"worker {worker_index} streamed keyed source slice batches "
        f"{slice_indexes}"
    )


def dry_run_stage_external_location(options: TransferOptions) -> str | None:
    if not options.transfer_staging_location:
        return None
    try:
        return build_stage_external_location(options, stage_suffix="dryrun")
    except Exception:
        return options.transfer_staging_location.rstrip("/") + "/__stage__dryrun/"


def resolve_dry_run_upsert_columns(options: TransferOptions) -> list[str] | None:
    if options.table_schema is not None:
        return list(options.table_schema)
    return infer_source_select_columns(
        options.source_sql,
        source_backend=options.from_db_backend,
    )


def infer_source_select_columns(
    source_sql: str,
    *,
    source_backend: str,
) -> list[str] | None:
    try:
        expression = parse_one(
            source_sql.strip().rstrip(";"),
            read=sqlglot_dialect(source_backend),
        )
    except Exception:
        return None

    if not isinstance(expression, exp.Select):
        return None

    columns: list[str] = []
    for projection in expression.expressions:
        if isinstance(projection, exp.Star) or projection.find(exp.Star) is not None:
            return None
        column_name = projection.alias_or_name
        if not column_name or column_name == "*":
            return None
        columns.append(str(column_name))
    return columns or None
