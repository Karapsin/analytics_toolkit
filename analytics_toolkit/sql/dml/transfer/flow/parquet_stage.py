from __future__ import annotations

import gc
import shutil
import tempfile
import uuid
from collections.abc import Mapping
from typing import Any

import pandas as pd

from ....backend_adapters import get_backend_adapter
from analytics_toolkit.general import time_print
from ...load.stage import (
    STAGE_TABLE_NAME_MAX_ATTEMPTS,
    build_stage_table_name,
)
from ...table._basic_ops import table_exists
from ..runtime.models import (
    RowBatch,
    TransferConnectionRefs,
    TransferOptions,
    TransferStageState,
)

PARQUET_STAGING_IMPORT_ERROR = (
    "Parquet object-storage staging requires pyarrow, fsspec, and s3fs. "
    "Install or repair the analytics-toolkit package dependencies to enable "
    "Trino object-storage staging."
)
PARQUET_STAGE_MAX_SPOOL_BYTES = 64 * 1024 * 1024
PARQUET_STAGE_DEFAULT_MAX_ROW_GROUP_SIZE = 50_000


def create_parquet_stage_table(
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    stage_state: TransferStageState,
) -> None:
    parquet_schema = options.transfer_parquet_staging_schema or options.transfer_staging_schema
    if not parquet_schema:
        raise ValueError("transfer_staging_schema is required for Parquet staging.")
    if not options.transfer_staging_location:
        raise ValueError("transfer_staging_location is required for Parquet staging.")
    if stage_state.stage_column_types is None:
        raise ValueError(
            "Could not resolve source schema before creating a Parquet stage table. "
            "Pass table_schema or use a source query with inspectable column types."
        )

    for attempt in range(1, STAGE_TABLE_NAME_MAX_ATTEMPTS + 1):
        stage_table = build_stage_table_name(
            "trino",
            options.target_table,
            transfer_staging_schema=parquet_schema,
            transfer_staging_username=options.transfer_staging_username,
        )
        if table_exists(
            "trino",
            connection_refs.target["connection"],
            stage_table,
            connection_key=options.to_db_key,
        ):
            time_print(
                f"Stage table name collision detected for {stage_table}; "
                f"retrying with a new name ({attempt}/{STAGE_TABLE_NAME_MAX_ATTEMPTS})"
            )
            continue

        stage_external_location = build_stage_external_location(options)
        adapter = get_backend_adapter(options.to_db_backend)
        create_sql = adapter.build_parquet_stage_table_sql(
            stage_table,
            stage_state.stage_column_types,
            stage_external_location,
            query_label=options.query_label,
            ddl_properties={
                **(options.staging_ddl_properties or {}),
                **(options.parquet_ddl_properties or {}),
            },
        )
        adapter.execute_command(
            connection_refs.target["connection"],
            create_sql,
        )
        stage_state.stage_table = stage_table
        stage_state.stage_external_location = stage_external_location
        stage_state.stage_table_created = True
        return

    raise RuntimeError(
        "Could not generate a unique stage table name after "
        f"{STAGE_TABLE_NAME_MAX_ATTEMPTS} attempts."
    )


def build_create_parquet_stage_table_sql(
    stage_table: str,
    column_types: Mapping[str, str] | None,
    stage_external_location: str,
    *,
    query_label: str | None = None,
    ddl_properties: Mapping[str, Any] | None = None,
) -> str:
    return get_backend_adapter("trino").build_parquet_stage_table_sql(
        stage_table,
        column_types,
        stage_external_location,
        query_label=query_label,
        ddl_properties=ddl_properties,
    )


def build_stage_external_location(
    options: Any,
    *,
    stage_suffix: str | None = None,
) -> str:
    if not options.transfer_staging_location:
        raise ValueError("transfer_staging_location is required.")
    base_location = options.transfer_staging_location.rstrip("/")
    target_base = get_backend_adapter("trino").parquet_stage_target_table_base(
        _stage_target_table_name(options)
    )
    username = options.transfer_staging_username or "unknown"
    resolved_suffix = stage_suffix or uuid.uuid4().hex
    return (
        f"{base_location}/{target_base}/__analytics_toolkit_{username}__stage__{resolved_suffix}/"
    )


def parquet_row_group_size(options: TransferOptions) -> int:
    return max(1, min(options.batch_size, PARQUET_STAGE_DEFAULT_MAX_ROW_GROUP_SIZE))


def ensure_parquet_staging_dependencies() -> tuple[Any, Any, Any]:
    try:
        import fsspec
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise ImportError(PARQUET_STAGING_IMPORT_ERROR) from exc
    return pa, pq, fsspec


def write_batch_to_parquet_stage(
    batch: RowBatch,
    *,
    file_index: int,
    slice_index: int | None = None,
    stage_external_location: str,
    pa: Any,
    pq: Any,
    fsspec_module: Any,
    row_group_size: int,
) -> int:
    row_count = len(batch.rows)
    if row_count == 0:
        return 0

    spooled_file = tempfile.SpooledTemporaryFile(
        max_size=PARQUET_STAGE_MAX_SPOOL_BYTES,
    )
    try:
        arrow_table = row_batch_to_arrow_table(pa, batch)
        write_arrow_table_to_parquet(
            pq,
            arrow_table,
            spooled_file,
            row_group_size=row_group_size,
        )
        del arrow_table
        spooled_file.seek(0)
        file_name = (
            f"slice-{slice_index:05d}-part-{file_index:05d}.parquet"
            if slice_index is not None
            else f"part-{file_index:05d}.parquet"
        )
        remote_uri = f"{stage_external_location.rstrip('/')}/{file_name}"
        upload_spooled_file(fsspec_module, spooled_file, remote_uri)
        if _spooled_file_rolled_to_disk(spooled_file):
            gc.collect()
        return row_count
    finally:
        spooled_file.close()


def write_dataframe_to_parquet_stage(
    df: pd.DataFrame,
    *,
    stage_external_location: str,
    pa: Any,
    pq: Any,
    fsspec_module: Any,
    row_group_size: int,
    on_progress: Any | None = None,
) -> int:
    if len(df) == 0:
        return 0

    row_group_size = max(1, row_group_size)
    written_rows = 0
    for file_index, start in enumerate(range(0, len(df), row_group_size)):
        stop = min(start + row_group_size, len(df))
        chunk = df.iloc[start:stop]
        spooled_file = tempfile.SpooledTemporaryFile(
            max_size=PARQUET_STAGE_MAX_SPOOL_BYTES,
        )
        try:
            arrow_table = pa.Table.from_pandas(chunk, preserve_index=False)
            write_arrow_table_to_parquet(
                pq,
                arrow_table,
                spooled_file,
                row_group_size=row_group_size,
            )
            del arrow_table
            spooled_file.seek(0)
            remote_uri = f"{stage_external_location.rstrip('/')}/part-{file_index:05d}.parquet"
            upload_spooled_file(fsspec_module, spooled_file, remote_uri)
            written_count = len(chunk)
            written_rows += written_count
            if on_progress is not None:
                on_progress(written_count)
            if _spooled_file_rolled_to_disk(spooled_file):
                gc.collect()
        finally:
            del chunk
            spooled_file.close()
    return written_rows


def row_batch_to_arrow_table(pa: Any, batch: RowBatch) -> Any:
    column_values = {
        column_name: [row[index] for row in batch.rows]
        for index, column_name in enumerate(batch.columns)
    }
    try:
        return pa.Table.from_pydict(column_values)
    finally:
        del column_values


def write_arrow_table_to_parquet(
    pq: Any,
    arrow_table: Any,
    spooled_file: Any,
    *,
    row_group_size: int,
) -> None:
    pq.write_table(
        arrow_table,
        spooled_file,
        row_group_size=row_group_size,
    )


def upload_spooled_file(
    fsspec_module: Any,
    spooled_file: Any,
    remote_uri: str,
) -> None:
    with fsspec_module.open(remote_uri, "wb") as remote_file:
        shutil.copyfileobj(spooled_file, remote_file)


def cleanup_parquet_stage_location(
    stage_external_location: str,
    *,
    fsspec_module: Any | None = None,
) -> None:
    if fsspec_module is None:
        _pa, _pq, fsspec_module = ensure_parquet_staging_dependencies()
    fs, path = fsspec_module.core.url_to_fs(stage_external_location)
    fs.rm(path, recursive=True)


def sample_dataframe_from_batch(batch: RowBatch) -> pd.DataFrame:
    return pd.DataFrame.from_records(batch.rows[:1], columns=batch.columns)


def infer_trino_column_types_from_rows(batch: RowBatch) -> dict[str, str]:
    return get_backend_adapter("trino").infer_parquet_stage_column_types_from_rows(batch)


def _stage_target_table_name(options: Any) -> str:
    target_table = getattr(options, "target_table", None)
    if target_table is not None:
        return target_table
    destination_table = getattr(options, "destination_table", None)
    if destination_table is not None:
        return destination_table
    raise ValueError("Parquet staging options must include a target table name.")


def _spooled_file_rolled_to_disk(spooled_file: Any) -> bool:
    return bool(getattr(spooled_file, "_rolled", False))
