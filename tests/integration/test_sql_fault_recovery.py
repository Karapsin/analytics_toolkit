from __future__ import annotations

# ruff: noqa: BLE001, C901, EM101, I001, PLR0915, PT018, TC002, TRY003, UP037

import importlib
import json
import os
import threading
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit import sql
from tests.integration.manifest import scenario_param
from tests.integration.support.backends import (
    BACKENDS,
    backend_alias,
    backend_enabled,
    integration_table,
    table_options,
)
from tests.integration.support.faults import FaultController, FaultGate
from tests.integration.support.normalization import assert_exact_frame
from tests.integration.support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_fault]
SERVICES = {"gp": "greenplum", "trino": "trino", "ch": "clickhouse"}


class _OperationWorker:
    def __init__(self, function: Any) -> None:
        self.function = function
        self.result: Any = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, name="fault-operation-worker")

    def start(self) -> "_OperationWorker":
        self.thread.start()
        return self

    def _run(self) -> None:
        try:
            self.result = self.function()
        except BaseException as exc:
            self.error = exc

    def cancel(self) -> None:
        return

    def join(self, timeout: float = 120) -> None:
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise TimeoutError("fault operation worker did not terminate")


def _fault_group(name: str) -> None:
    if os.environ.get("SQL_INTEGRATION_FAULT_GROUP") not in {None, name}:
        pytest.skip(f"{name} fault group not selected")


@pytest.mark.parametrize(
    "backend",
    [scenario_param(f"fault.finalize.{backend}", backend) for backend in BACKENDS],
)
def test_service_failure_during_upsert_finalization_retries_from_fresh_connections(
    backend: str,
    fault_controller: FaultController,
    resource_registry: ResourceRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fault_group("database")
    if not backend_enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    alias = backend_alias(backend, target=True)
    table = resource_registry.table(
        alias,
        integration_table(backend, "fault_finalize"),
    )
    original = pd.DataFrame(
        {
            "row_id": [1, 2],
            "event_date": [pd.Timestamp("2026-07-01"), pd.Timestamp("2026-07-02")],
            "value": ["original", "unchanged"],
        }
    )
    update = pd.DataFrame(
        {
            "row_id": [2, 3],
            "event_date": [pd.Timestamp("2026-07-02"), pd.Timestamp("2026-07-03")],
            "value": ["updated", "new"],
        }
    )
    options = table_options(backend, only_shard=backend == "ch")
    sql.load_df(alias, table, original, write_mode="replace", **options)

    module = importlib.import_module("analytics_toolkit.sql.dml.load.load_df")
    gate = FaultGate(phase="finalize_target", hold_exception=True, timeout=90)
    monkeypatch.setattr(module, "upsert_stage_table", gate.wrap(module.upsert_stage_table))
    opened_connections: list[Any] = []
    real_open = module.get_sql_connection

    def record_connection(db_key: str) -> Any:
        connection = real_open(db_key)
        opened_connections.append(connection)
        return connection

    monkeypatch.setattr(module, "get_sql_connection", record_connection)
    upsert_options = dict(options)
    upsert_options["key_columns"] = ["row_id"]
    if backend != "gp":
        upsert_options["upsert_partition_column"] = "event_date"

    worker = resource_registry.worker(
        _OperationWorker(
            lambda: sql.load_df(
                alias,
                table,
                update,
                write_mode="upsert",
                retry_cnt=2,
                timeout_increment=3,
                **upsert_options,
            )
        ).start()
    )
    gate.wait()
    fault_controller.stop(SERVICES[backend])
    gate.open()
    gate.wait_for_failure()
    fault_controller.restart(SERVICES[backend])
    fault_controller.wait_healthy(SERVICES[backend])
    gate.resume_after_failure()
    worker.join()
    assert gate.exception is not None
    assert len(opened_connections) >= 2
    assert len({id(connection) for connection in opened_connections}) == len(opened_connections)

    if backend == "ch":
        assert worker.error is not None
        assert getattr(worker.error, "analytics_toolkit_sql_retry_safe", True) is False
        expected = original
    else:
        assert worker.error is None, repr(worker.error)
        assert worker.result == len(update)
        expected = pd.concat([original.iloc[:1], update], ignore_index=True)
    actual = sql.read(alias, f"SELECT row_id, event_date, value FROM {table} ORDER BY row_id")
    assert_exact_frame(actual, expected)
    artifact_dir = Path(os.environ["SQL_INTEGRATION_ARTIFACT_DIR"])
    (artifact_dir / "operation-retry-timeline.json").write_text(
        json.dumps(gate.timeline, indent=2), encoding="utf-8"
    )
    (artifact_dir / "connection-identities.json").write_text(
        json.dumps([id(connection) for connection in opened_connections], indent=2),
        encoding="utf-8",
    )


@pytest.mark.sql_scenario("fault.load.minio_before_upload")
def test_minio_unavailable_before_parquet_upload_reports_primary_failure(
    fault_controller: FaultController,
    resource_registry: ResourceRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fault_group("staging")
    parquet_module = importlib.import_module(
        "analytics_toolkit.sql.dml.transfer.flow.parquet_stage"
    )
    load_module = importlib.import_module("analytics_toolkit.sql.dml.load.load_df")
    real_upload = parquet_module.upload_spooled_file
    real_cleanup = load_module.cleanup_parquet_stage_location
    upload_threads: list[threading.Thread] = []
    upload_errors: list[BaseException] = []
    deferred_cleanup: list[str] = []
    upload_reached = threading.Event()
    upload_release = threading.Event()

    def bounded_upload(*args: Any, **kwargs: Any) -> None:
        upload_reached.set()
        if not upload_release.wait(30):
            raise TimeoutError("MinIO upload fault gate was not released")

        def upload() -> None:
            try:
                real_upload(*args, **kwargs)
            except BaseException as exc:
                upload_errors.append(exc)

        thread = threading.Thread(target=upload, name="bounded-minio-upload", daemon=True)
        upload_threads.append(thread)
        thread.start()
        thread.join(5)
        if thread.is_alive():
            raise TimeoutError("MinIO upload remained unavailable for five seconds")
        if upload_errors:
            raise upload_errors[-1]

    def defer_cleanup(location: str) -> None:
        deferred_cleanup.append(location)
        raise RuntimeError("MinIO cleanup deferred until service recovery")

    monkeypatch.setattr(parquet_module, "upload_spooled_file", bounded_upload)
    monkeypatch.setattr(load_module, "cleanup_parquet_stage_location", defer_cleanup)
    table = resource_registry.table(
        "trino_target_parquet",
        integration_table("trino", "fault_minio"),
    )
    frame = pd.DataFrame(
        {
            "row_id": [1, 2],
            "event_date": [pd.Timestamp("2026-07-01").date(), pd.Timestamp("2026-07-02").date()],
            "value": ["one", "two"],
        }
    )
    sql.create_sql_table(
        "trino_target_parquet",
        table,
        table_schema={"row_id": "BIGINT", "event_date": "DATE", "value": "VARCHAR"},
        partition_by=["event_date"],
        retry_cnt=1,
    )
    worker = resource_registry.worker(
        _OperationWorker(
            lambda: sql.load_df(
                "trino_target_parquet",
                table,
                frame,
                write_mode="append",
                retry_cnt=1,
                partition_by=["event_date"],
            )
        ).start()
    )
    stopped = False
    try:
        assert upload_reached.wait(30), "Parquet upload checkpoint was not reached"
        fault_controller.stop("minio")
        stopped = True
        upload_release.set()
        worker.join(30)
        assert worker.error is not None
        assert "minio" in repr(worker.error).lower() or "endpoint" in repr(worker.error).lower()
    finally:
        upload_release.set()
        if stopped:
            fault_controller.restart("minio")
            fault_controller.wait_healthy("minio")
        for thread in upload_threads:
            thread.join(30)
            assert not thread.is_alive(), "bounded MinIO upload worker did not terminate"
        for location in deferred_cleanup:
            real_cleanup(location)
    info = sql.table_info("trino_target_parquet", table, include_row_count=True)
    assert info.exists and info.row_count == 0
