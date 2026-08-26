from __future__ import annotations

import builtins
import importlib
import inspect
import io
import sys
import threading
import uuid
import warnings
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import analytics_toolkit.general as general_module
import pandas as pd
import pytest

attempt_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.attempt")

config_module = importlib.import_module("analytics_toolkit.sql.connection.config")

finalize_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.finalize")

transfer_stage_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.stage")

stage_identity_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.flow.stage_identity"
)

parquet_stage_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.flow.parquet_stage"
)

parquet_batches_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.flow.parquet_batches"
)

transfer_options_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.options")

transfer_concurrency_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.flow.concurrency"
)

keys_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.keys")

estimate_module = importlib.import_module("analytics_toolkit.sql.backends.source_estimate")

row_counts_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.row_counts")

progress_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.progress")

dry_run_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.dry_run")

keyed_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.keyed")

keyed_pipeline_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.flow.keyed_pipeline"
)

transfer_logging_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.logging")

staging_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.staging")

load_sql_table_module = importlib.import_module("analytics_toolkit.sql.dml.load.load_sql_table")

transfer_api_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.api")

models_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.runtime.models")

retry_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.runtime.retry")

source_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.io.source")

backends_module = importlib.import_module("analytics_toolkit.sql.backends")


class DatabaseError(Exception):
    pass


class RecordingSourceCursor:
    def __init__(self, rows: list[tuple[int]]) -> None:
        self._rows = rows
        self.description = [("id", 23, None, None, None, None)]
        self.fetch_sizes: list[int] = []
        self.executed: list[str] = []
        self.close_calls = 0

    def execute(self, query: str) -> None:
        self.executed.append(query)

    def fetchmany(self, size: int) -> list[tuple[int]]:
        self.fetch_sizes.append(size)
        batch = self._rows[:size]
        self._rows = self._rows[size:]
        return batch

    def close(self) -> None:
        self.close_calls += 1


class RecordingSourceConnection:
    def __init__(self, rows: list[tuple[int]]) -> None:
        self.cursor_obj = RecordingSourceCursor(rows)

    def cursor(self) -> RecordingSourceCursor:
        return self.cursor_obj


class StaticDbapiCursor:
    def __init__(
        self,
        connection: StaticDbapiConnection,
        rows: list[tuple[Any, ...]],
    ) -> None:
        self.connection = connection
        self._rows = rows
        self.close_calls = 0

    def execute(self, query: str) -> None:
        self.connection.executed.append(query)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def close(self) -> None:
        self.close_calls += 1


class StaticDbapiConnection:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.executed: list[str] = []
        self.rollback_calls = 0

    def cursor(self) -> StaticDbapiCursor:
        return StaticDbapiCursor(self, self.rows)

    def rollback(self) -> None:
        self.rollback_calls += 1


class StaticClickHouseResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.result_rows = rows


class StaticClickHouseClient:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.queries: list[str] = []

    def query(self, query: str) -> StaticClickHouseResult:
        self.queries.append(query)
        return StaticClickHouseResult(self.rows)


class FakeTransferConnection:
    def __init__(self, name: str) -> None:
        self.name = name
        self.close_calls = 0
        self.rollback_calls = 0

    def close(self) -> None:
        self.close_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1


class ProtocolError(Exception):
    pass


class RenderingFakeTqdm:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.total = kwargs["total"]
        self.n = 0
        self.rendered: list[str] = []

    @property
    def format_dict(self) -> dict[str, Any]:
        desc = self.kwargs["desc"]
        return {
            "n": self.n,
            "total": self.total,
            "desc": desc,
            "unit": self.kwargs["unit"],
            "elapsed": "00:00",
            "remaining": "00:02",
            "rate_fmt": "14087.46row/s",
            "postfix": "",
            "l_bar": f"{desc}:  86%|",
            "bar": "########",
        }

    def update(self, value: int) -> None:
        self.n += value
        if not self.kwargs["disable"]:
            self.rendered.append(self.kwargs["bar_format"].format(**self.format_dict))


def capture_rendering_progress_bars(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    progress_bars: list[Any] = []

    class CapturingTqdm(RenderingFakeTqdm):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            progress_bars.append(self)

    monkeypatch.setattr(attempt_module, "tqdm", CapturingTqdm)
    return progress_bars


def make_progress_options(**overrides: Any) -> Any:
    values = {
        "from_db_key": "gp",
        "from_db_backend": "gp",
        "to_db_key": "gp_sandbox",
        "to_db_backend": "gp",
        "source_sql": "select id from source_table",
        "target_table": "sandbox.target",
        "batch_size": 2,
        "progress": True,
    }
    values.update(overrides)
    return models_module.TransferOptions(**values)


def make_gp_config(
    connection_key: str,
    *,
    transfer_staging_schema: str | None = None,
) -> Any:
    return config_module.GpConfig(
        connection_key=connection_key,
        backend="gp",
        host="gp.example",
        port=5432,
        user="source_user",
        password="password",
        database="db",
        connect_timeout=30,
        keepalives=True,
        keepalives_idle=60,
        keepalives_interval=10,
        keepalives_count=3,
        sslmode=None,
        ca_certs=[],
        ssl_cert=None,
        ssl_key=None,
        transfer_staging_schema=transfer_staging_schema,
    )


def make_ch_config(connection_key: str) -> Any:
    return config_module.ChConfig(
        connection_key=connection_key,
        backend="ch",
        host="ch.example",
        port=8123,
        user="source_user",
        password="password",
        database="default",
        secure=False,
        verify_value=None,
        ca_certs=[],
        ca_certs_variable=None,
        connect_timeout=None,
        send_receive_timeout=None,
        settings=None,
        interface=None,
        query_limit=None,
        query_retries=None,
        client_name=None,
        transfer_staging_schema=None,
    )


def make_trino_config(
    connection_key: str,
    *,
    transfer_staging_schema: str | None = "object_storage.sandbox",
    s3_transfer_staging_schema: str | None = "hive.sandbox",
    s3_transfer_staging_location: str | None = "s3://bucket/tmp/analytics_toolkit_transfer",
) -> Any:
    return config_module.TrinoConfig(
        connection_key=connection_key,
        backend="trino",
        host="trino.example",
        port=8080,
        user="target_user",
        password="password",
        catalog="iceberg",
        schema="sandbox",
        auth_mode="basic",
        http_scheme="https",
        verify_value="true",
        ca_certs=[],
        insert_chunk_size=None,
        request_timeout=None,
        source=None,
        transfer_staging_schema=transfer_staging_schema,
        s3_transfer_staging_schema=s3_transfer_staging_schema,
        s3_transfer_staging_location=s3_transfer_staging_location,
        upsert_partition_drop_sql_template=(
            "ALTER TABLE {table} DROP PARTITION ({partition_column} = {partition_value})"
        ),
    )


def make_keyed_options(**overrides: Any) -> Any:
    _keys, expressions, values, slices, concurrency = keys_module.normalize_transfer_slices(
        source_sql="select id, event_date from source_table where {event_date}",
        transfer_keys="event_date",
        transfer_key_values=["2025-01-01", "2025-01-02"],
        concurrency=overrides.pop("concurrency", 1),
    )
    option_values = {
        "from_db_key": "source_db",
        "from_db_backend": "gp",
        "to_db_key": "target_db",
        "to_db_backend": "gp",
        "source_sql": "select id, event_date from source_table",
        "target_table": "sandbox.target",
        "batch_size": 2,
        "transfer_keys": ["event_date"],
        "transfer_key_expressions": expressions,
        "transfer_key_values": values,
        "transfer_slices": slices,
        "concurrency": concurrency,
    }
    option_values.update(overrides)
    return models_module.TransferOptions(**option_values)


__all__ = [
    "Any",
    "DatabaseError",
    "Decimal",
    "FakeTransferConnection",
    "Path",
    "ProtocolError",
    "RecordingSourceConnection",
    "RecordingSourceCursor",
    "RenderingFakeTqdm",
    "SimpleNamespace",
    "StaticClickHouseClient",
    "StaticClickHouseResult",
    "StaticDbapiConnection",
    "StaticDbapiCursor",
    "attempt_module",
    "backends_module",
    "builtins",
    "capture_rendering_progress_bars",
    "config_module",
    "date",
    "datetime",
    "dry_run_module",
    "estimate_module",
    "finalize_module",
    "general_module",
    "importlib",
    "inspect",
    "io",
    "keyed_module",
    "keyed_pipeline_module",
    "keys_module",
    "load_sql_table_module",
    "make_ch_config",
    "make_gp_config",
    "make_keyed_options",
    "make_progress_options",
    "make_trino_config",
    "models_module",
    "parquet_batches_module",
    "parquet_stage_module",
    "pd",
    "progress_module",
    "pytest",
    "replace",
    "retry_module",
    "row_counts_module",
    "source_module",
    "stage_identity_module",
    "staging_module",
    "sys",
    "threading",
    "transfer_api_module",
    "transfer_concurrency_module",
    "transfer_logging_module",
    "transfer_options_module",
    "transfer_stage_module",
    "uuid",
    "warnings",
]
