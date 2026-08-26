from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import source_count, transfer_stage
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.dml.load import stage as load_stage
from analytics_toolkit.sql.dml.transfer import schema as transfer_schema
from analytics_toolkit.sql.dml.transfer.flow import (
    api,
    attempt,
    dry_run,
    parquet_batches,
    parquet_stage,
    stage_validation,
    staged_attempt,
    staged_keyed_pipeline,
    superseded,
)
from analytics_toolkit.sql.dml.transfer.flow.range_scheduler import (
    AdaptiveRangeScheduler,
    OrdinalRange,
)
from analytics_toolkit.sql.dml.transfer.flow.source_snapshot import (
    build_snapshot_range_sql,
    build_snapshot_select_sql,
    build_source_snapshot_sql,
)
from analytics_toolkit.sql.dml.transfer.flow.stage_identity import (
    assert_transfer_identity,
    resolve_destination_identity,
    resolve_internal_columns,
)
from analytics_toolkit.sql.dml.transfer.flow.superseded import (
    cleanup_superseded_transfer_stages,
)
from analytics_toolkit.sql.dml.transfer.runtime.models import (
    RowBatch,
    TransferConcurrency,
    TransferOptions,
    TransferSlice,
    TransferStageState,
)


def _staged_options(**overrides: Any) -> TransferOptions:
    values: dict[str, Any] = {
        "from_db_key": "source",
        "from_db_backend": "gp",
        "to_db_key": "target",
        "to_db_backend": "gp",
        "source_sql": "SELECT id FROM source",
        "target_table": "public.target",
        "transfer_id": "a" * 32,
        "canonical_destination_identity": "public.target",
        "destination_hash": "0123456789abcdef",
        "source_transfer_staging_schema": "source_stage",
        "transfer_staging_schema": "target_stage",
        "batch_size": 2,
        "min_batch_size": 1,
        "max_batch_size": 4,
        "adaptive_batch_size": False,
        "full_retry_cnt": 2,
    }
    values.update(overrides)
    return TransferOptions(**values)


__all__ = [
    "AdaptiveRangeScheduler",
    "Any",
    "OrdinalRange",
    "RowBatch",
    "SimpleNamespace",
    "SourceColumn",
    "TransferConcurrency",
    "TransferOptions",
    "TransferSlice",
    "TransferStageState",
    "_staged_options",
    "api",
    "assert_transfer_identity",
    "attempt",
    "build_snapshot_range_sql",
    "build_snapshot_select_sql",
    "build_source_snapshot_sql",
    "cleanup_superseded_transfer_stages",
    "dry_run",
    "load_stage",
    "parquet_batches",
    "parquet_stage",
    "pd",
    "pytest",
    "resolve_destination_identity",
    "resolve_internal_columns",
    "source_count",
    "stage_validation",
    "staged_attempt",
    "staged_keyed_pipeline",
    "superseded",
    "threading",
    "transfer_schema",
    "transfer_stage",
]
