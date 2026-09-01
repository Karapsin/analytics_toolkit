from __future__ import annotations

# ruff: noqa: BLE001, C901, EM101, EM102, I001, PLC0415, PLR0915, PT011, PT018, TRY003, UP037

import importlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import requests
from analytics_toolkit import sql
from tests.sql.integration.manifest import scenario_param
from tests.sql.integration._support.backends import (
    BACKENDS,
    backend_alias,
    backend_enabled,
    integration_table,
    table_options,
)
from tests.sql.integration._support.faults import FaultController, FaultGate
from tests.sql.integration._support.normalization import assert_exact_frame
from tests.sql.integration._support.resources import ResourceRegistry

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


def _wait_keycloak_route(timeout: float = 30) -> None:
    url = "https://127.0.0.1:18445/realms/integration/.well-known/openid-configuration"
    ca_file = Path(os.environ["SQL_INTEGRATION_CERTS"]) / "ca.crt"
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            if requests.get(url, verify=ca_file, timeout=2).status_code == 200:
                return
        except requests.RequestException as exc:
            last_error = exc
        time.sleep(0.25)
    raise TimeoutError(f"Keycloak TLS route did not recover: {last_error!r}")


@pytest.mark.parametrize(
    "backend",
    [scenario_param(f"fault.batch.{backend}", backend) for backend in BACKENDS],
)
def test_target_restart_after_first_transfer_batch_is_exact_or_contextually_ambiguous(
    backend: str,
    fault_controller: FaultController,
    resource_registry: ResourceRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fault_group("database")
    if not backend_enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    alias = backend_alias(backend, target=True)
    table = resource_registry.table(alias, integration_table(backend, "fault_first_batch"))
    original = pd.DataFrame(
        {
            "row_id": [100],
            "event_date": [pd.Timestamp("2026-07-10")],
            "value": ["original"],
        }
    )
    options = table_options(backend, only_shard=backend == "ch")
    sql.load_df(alias, table, original, write_mode="replace", **options)
    attempt_module = importlib.import_module(
        "analytics_toolkit.sql.dml.transfer.flow.staged_attempt"
    )
    gate = FaultGate(phase="second_stage_batch", trigger_call=2, timeout=90, hold_exception=True)
    monkeypatch.setattr(
        attempt_module,
        "insert_rows_batch",
        gate.wrap(attempt_module.insert_rows_batch),
    )
    transfer_options = dict(options)
    if backend != "gp":
        transfer_options["partition_by"] = ["event_date"]
    worker = resource_registry.worker(
        _OperationWorker(
            lambda: sql.transfer(
                "trino_values",
                alias,
                (
                    "SELECT value AS row_id, DATE '2026-07-01' AS event_date, "
                    "CAST(value AS VARCHAR) AS value FROM UNNEST(sequence(1, 6)) t(value)"
                ),
                table,
                write_mode="append",
                batch_size=1,
                retry_cnt=1,
                full_retry_cnt=2,
                timeout_increment=1,
                adaptive_batch_size=False,
                target_rows_per_second=False,
                **transfer_options,
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
    result = sql.read(alias, f"SELECT row_id, value FROM {table} ORDER BY row_id")
    if worker.error is not None:
        assert backend in {"trino", "ch"}
        rendered = repr(worker.error).lower()
        assert "ambiguous" in rendered or "unsafe" in rendered
        assert result.to_dict("records") == [{"row_id": 100, "value": "original"}]
    else:
        assert worker.result == 6
        assert result["row_id"].tolist() == [1, 2, 3, 4, 5, 6, 100]
        assert result["row_id"].nunique() == 7


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
    sql.create_table(
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


@pytest.mark.sql_scenario("fault.staging.minio_after_first_object")
def test_minio_unavailable_after_first_parquet_object_cleans_attempt(
    fault_controller: FaultController,
    resource_registry: ResourceRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fault_group("staging")
    parquet_module = importlib.import_module(
        "analytics_toolkit.sql.dml.transfer.flow.parquet_stage"
    )
    attempt_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.attempt")

    def initialize_object_stage(*, options: Any, stage_state: Any, **_kwargs: Any) -> None:
        suffix = f"{options.transfer_id}__w00000"
        stage_state.stage_table = f"hive.default.fault_{options.transfer_id}"
        stage_state.stage_external_location = parquet_module.build_stage_external_location(
            options,
            stage_suffix=suffix,
        )
        stage_state.stage_table_created = True

    monkeypatch.setattr(
        attempt_module,
        "create_parquet_stage_table",
        initialize_object_stage,
    )
    gate = FaultGate(
        phase="second_parquet_upload",
        trigger_call=2,
        timeout=90,
        hold_exception=True,
    )
    monkeypatch.setattr(
        parquet_module,
        "upload_spooled_file",
        gate.wrap(parquet_module.upload_spooled_file),
    )
    table = resource_registry.table(
        "trino_target_parquet",
        integration_table("trino", "fault_second_object"),
    )
    worker = resource_registry.worker(
        _OperationWorker(
            lambda: sql.transfer(
                "trino_values",
                "trino_target_parquet",
                (
                    "SELECT value AS row_id, DATE '2026-07-01' AS event_date, "
                    "CAST(value AS VARCHAR) AS value FROM UNNEST(sequence(1, 6)) t(value)"
                ),
                table,
                write_mode="replace",
                batch_size=2,
                partition_by=["event_date"],
                retry_cnt=1,
                full_retry_cnt=1,
                adaptive_batch_size=False,
                target_rows_per_second=False,
                ignore_source_staging=True,
            )
        ).start()
    )
    gate.wait()
    fault_controller.stop("minio")
    gate.open()
    gate.wait_for_failure()
    fault_controller.restart("minio")
    fault_controller.wait_healthy("minio")
    gate.resume_after_failure()
    worker.join(30)
    assert gate.calls == 2
    assert worker.error is not None
    assert not sql.table_info("trino_target_parquet", table).exists


@pytest.mark.sql_scenario("fault.staging.hive_registration")
def test_hive_metastore_failure_during_external_registration_preserves_target(
    fault_controller: FaultController,
    resource_registry: ResourceRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fault_group("staging")
    attempt_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.attempt")
    gate = FaultGate(
        phase="external_stage_registration",
        timeout=90,
        hold_exception=True,
    )
    monkeypatch.setattr(
        attempt_module,
        "create_parquet_stage_table",
        gate.wrap(attempt_module.create_parquet_stage_table),
    )
    table = resource_registry.table(
        "trino_target_values",
        integration_table("trino", "fault_hive_registration"),
    )
    original = pd.DataFrame(
        {"row_id": [100], "event_date": [pd.Timestamp("2026-07-01")], "value": ["original"]}
    )
    sql.load_df(
        "trino_target_values", table, original, write_mode="replace", partition_by=["event_date"]
    )
    worker = resource_registry.worker(
        _OperationWorker(
            lambda: sql.transfer(
                "trino_values",
                "trino_target_parquet",
                "SELECT 1 AS row_id, DATE '2026-07-02' AS event_date, 'new' AS value",
                table,
                write_mode="replace",
                batch_size=1,
                partition_by=["event_date"],
                retry_cnt=1,
                full_retry_cnt=1,
                adaptive_batch_size=False,
                target_rows_per_second=False,
                ignore_source_staging=True,
            )
        ).start()
    )
    gate.wait()
    fault_controller.stop("hive-metastore")
    gate.open()
    gate.wait_for_failure()
    fault_controller.restart("hive-metastore")
    fault_controller.wait_healthy("hive-metastore")
    gate.resume_after_failure()
    worker.join(30)
    assert worker.error is not None
    actual = sql.read(
        "trino_target_values",
        f"SELECT row_id, event_date, value FROM {table} ORDER BY row_id",
    )
    assert_exact_frame(actual, original)


@pytest.mark.sql_scenario("fault.staging.catalog_finalization")
def test_iceberg_catalog_failure_during_finalization_preserves_exact_target(
    fault_controller: FaultController,
    resource_registry: ResourceRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fault_group("staging")
    attempt_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.attempt")
    gate = FaultGate(phase="catalog_finalization", timeout=90)
    monkeypatch.setattr(
        attempt_module,
        "finalize_loaded_stage",
        gate.wrap(attempt_module.finalize_loaded_stage),
    )
    table = resource_registry.table(
        "trino_target_values",
        integration_table("trino", "fault_catalog_finalization"),
    )
    original = pd.DataFrame(
        {"row_id": [100], "event_date": [pd.Timestamp("2026-07-01")], "value": ["original"]}
    )
    sql.load_df(
        "trino_target_values", table, original, write_mode="replace", partition_by=["event_date"]
    )
    worker = resource_registry.worker(
        _OperationWorker(
            lambda: sql.transfer(
                "trino_values",
                "trino_target_values",
                "SELECT 1 AS row_id, DATE '2026-07-02' AS event_date, 'replacement' AS value",
                table,
                write_mode="replace",
                batch_size=1,
                partition_by=["event_date"],
                retry_cnt=1,
                full_retry_cnt=1,
                adaptive_batch_size=False,
                target_rows_per_second=False,
                ignore_source_staging=True,
            )
        ).start()
    )
    gate.wait()
    fault_controller.stop("iceberg-catalog-db")
    gate.open()
    worker.join(60)
    fault_controller.restart("iceberg-catalog-db")
    fault_controller.wait_healthy("iceberg-catalog-db")
    assert worker.error is not None
    actual = sql.read(
        "trino_target_values",
        f"SELECT row_id, event_date, value FROM {table} ORDER BY row_id",
    )
    assert_exact_frame(actual, original)


@pytest.mark.sql_scenario("fault.staging.cleanup_secondary")
def test_cleanup_failure_is_reported_without_masking_primary_failure(
    resource_registry: ResourceRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _fault_group("staging")
    attempt_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.attempt")
    real_cleanup = attempt_module.cleanup_stage

    def fail_finalization(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError("injected primary finalization failure")

    def fail_after_cleanup(*args: Any, **kwargs: Any) -> None:
        real_cleanup(*args, **kwargs)
        raise RuntimeError("injected secondary cleanup failure")

    monkeypatch.setattr(attempt_module, "finalize_loaded_stage", fail_finalization)
    monkeypatch.setattr(attempt_module, "cleanup_stage", fail_after_cleanup)
    table = resource_registry.table(
        "trino_target_values",
        integration_table("trino", "fault_cleanup_secondary"),
    )
    with pytest.raises(Exception, match="primary finalization failure"):
        sql.transfer(
            "trino_values",
            "trino_target_values",
            "SELECT 1 AS row_id, DATE '2026-07-02' AS event_date, 'replacement' AS value",
            table,
            write_mode="replace",
            batch_size=1,
            partition_by=["event_date"],
            retry_cnt=1,
            full_retry_cnt=1,
            adaptive_batch_size=False,
            target_rows_per_second=False,
            ignore_source_staging=True,
        )
    captured = capsys.readouterr().out
    assert "Cleanup failed while handling transfer error" in captured
    assert "secondary cleanup failure" in captured
    assert not sql.table_info("trino_target_values", table).exists


@pytest.mark.sql_scenario("fault.authentication.keycloak_discovery")
def test_keycloak_unavailable_during_oauth_start_fails_without_secret_leak(
    fault_controller: FaultController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fault_group("authentication")
    from tests.sql.integration.auth.negative import _install_browser_oauth

    alias = "trino_oauth_discovery_tls"
    _install_browser_oauth(monkeypatch, alias=alias)
    fault_controller.stop("keycloak")
    try:
        with pytest.raises(Exception) as exc_info:
            sql.read(alias, "SELECT 1", retry_cnt=1)
    finally:
        fault_controller.restart("keycloak")
        fault_controller.wait_healthy("keycloak")
        _wait_keycloak_route()
    rendered = repr(exc_info.value)
    assert "integration-oauth-secret" not in rendered
    assert "access_token" not in rendered.lower()


@pytest.mark.sql_scenario("fault.authentication.keycloak_token_exchange")
def test_keycloak_pauses_after_login_before_token_exchange(
    fault_controller: FaultController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fault_group("authentication")
    from tests.sql.integration.auth.negative import _install_browser_oauth

    paused = False

    def stop_before_callback() -> None:
        nonlocal paused
        if not paused:
            fault_controller.pause("keycloak")
            paused = True

    alias = "trino_oauth_exchange_tls"
    callbacks, _ = _install_browser_oauth(
        monkeypatch,
        alias=alias,
        before_callback=stop_before_callback,
    )
    error: BaseException | None = None
    try:
        try:
            sql.read(alias, "SELECT 1", retry_cnt=1)
        except BaseException as exc:
            error = exc
    finally:
        if paused:
            fault_controller.unpause("keycloak")
            fault_controller.wait_healthy("keycloak")
            _wait_keycloak_route()
    rendered = repr(error)
    assert paused, {"error": rendered, "callbacks": callbacks}
    assert error is not None
    assert "integration-oauth-secret" not in rendered
    assert "access_token" not in rendered.lower()
