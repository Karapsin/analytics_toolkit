from __future__ import annotations

# ruff: noqa: BLE001, I001, TC002

import importlib
import datetime as dt
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.connection.get_sql_connection import get_sql_connection
from tests.integration.manifest import scenario_param
from tests.integration.support.backends import (
    BACKENDS,
    backend_alias,
    backend_enabled,
    integration_table,
    table_options,
)
from tests.integration.support.normalization import assert_exact_frame
from tests.integration.support.pressure import MemorySampler
from tests.integration.support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_stress]
ARTIFACT_DIR = Path(os.environ.get("SQL_INTEGRATION_ARTIFACT_DIR", ".integration-artifacts/stress"))
LAZY_DDL_CHURN_KEY_COUNT = 64
LAZY_DDL_CHURN_READERS = 4
LAZY_DDL_CHURN_WRITERS = 3


def _lazy_attempt_stage_tables(alias: str, backend: str, transfer_id: str) -> pd.DataFrame:
    schema = {
        "gp": "public",
        "trino": "integration_stage",
        "ch": "integration",
    }[backend]
    options: dict[str, Any] = {
        "schema": schema,
        "conditions": f"table_name LIKE '%{transfer_id}%'",
    }
    if backend == "trino":
        options["trino_catalog"] = "iceberg"
    return sql.show_tables(alias, **options)


def _lazy_ddl_transfer_options(backend: str) -> dict[str, Any]:
    options = table_options(backend)
    if backend == "ch":
        options["table_schema"] = {
            "row_id": "Int64",
            "slice_key": "Int64",
            "event_date": "Date",
            "value": "String",
        }
    return options


@pytest.mark.parametrize(
    "backend",
    [scenario_param(f"stress.transfer.lazy_keyed_ddl.{backend}", backend) for backend in BACKENDS],
)
def test_lazy_keyed_source_staging_handles_64_key_ddl_churn(
    backend: str,
    resource_registry: ResourceRegistry,
) -> None:
    """Exercise 64 per-key CTAS/DROP cycles with a seven-stage live bound."""
    if not backend_enabled(backend):
        pytest.skip("Greenplum stress coverage requires x86_64")

    source_alias = backend_alias(backend)
    target_alias = backend_alias(backend, target=True)
    cluster = "integration_cluster" if backend == "ch" else None
    source_table = resource_registry.table(
        source_alias,
        integration_table(backend, "lazy_ddl_churn_source"),
        ch_cluster=cluster,
    )
    target_table = resource_registry.table(
        target_alias,
        integration_table(backend, "lazy_ddl_churn_target"),
        ch_cluster=cluster,
    )
    expected = pd.DataFrame(
        {
            "row_id": list(range(1, LAZY_DDL_CHURN_KEY_COUNT + 1)),
            "slice_key": list(range(LAZY_DDL_CHURN_KEY_COUNT)),
            "event_date": [
                dt.date(2026, 8, 1) + dt.timedelta(days=index % 7)
                for index in range(LAZY_DDL_CHURN_KEY_COUNT)
            ],
            "value": [f"key-{index:02d}" for index in range(LAZY_DDL_CHURN_KEY_COUNT)],
        }
    )
    sql.load_df(
        source_alias,
        source_table,
        expected,
        write_mode="replace",
        **table_options(backend),
    )

    result = sql.transfer(
        source_alias,
        target_alias,
        from_table=source_table,
        to_table=target_table,
        transfer_keys="slice_key",
        transfer_key_values=list(range(LAZY_DDL_CHURN_KEY_COUNT)),
        read_concurrency=LAZY_DDL_CHURN_READERS,
        write_concurrency=LAZY_DDL_CHURN_WRITERS,
        hard_concurrency_cap=5,
        batch_size=1,
        adaptive_batch_size=False,
        target_rows_per_second=False,
        write_mode="replace",
        retry_cnt=1,
        timeout_increment=0,
        full_retry_cnt=1,
        full_timeout_increment=0,
        return_metadata=True,
        **_lazy_ddl_transfer_options(backend),
    )

    assert isinstance(result, sql.SqlOperationResult)
    assert result.rows == LAZY_DDL_CHURN_KEY_COUNT
    assert result.metadata.effective_read_concurrency == LAZY_DDL_CHURN_READERS
    assert result.metadata.effective_write_concurrency == LAZY_DDL_CHURN_WRITERS
    assert result.metadata.source_stage_count == LAZY_DDL_CHURN_KEY_COUNT
    assert result.metadata.live_source_stage_limit == (
        LAZY_DDL_CHURN_READERS + LAZY_DDL_CHURN_WRITERS
    )
    slice_counts = result.metadata.transfer_slice_counts or []
    assert len(slice_counts) == LAZY_DDL_CHURN_KEY_COUNT
    assert all((item["expected_rows"], item["streamed_rows"]) == (1, 1) for item in slice_counts)
    actual = sql.read(
        target_alias,
        (f"SELECT row_id, slice_key, event_date, value FROM {target_table} ORDER BY row_id"),
    )
    assert_exact_frame(actual, expected, date_columns=("event_date",))

    transfer_id = result.metadata.transfer_id
    assert transfer_id
    source_stages = _lazy_attempt_stage_tables(source_alias, backend, transfer_id)
    target_stages = _lazy_attempt_stage_tables(target_alias, backend, transfer_id)
    assert source_stages.empty, source_stages["table_name"].tolist()
    assert target_stages.empty, target_stages["table_name"].tolist()


@pytest.mark.parametrize(
    "backend",
    [scenario_param(f"concurrency.write.{backend}", backend) for backend in BACKENDS],
)
def test_concurrent_writers_preserve_exact_disjoint_rows(
    backend: str,
    resource_registry: ResourceRegistry,
) -> None:
    if backend == "gp" and os.environ.get("SQL_INTEGRATION_GP") != "1":
        pytest.skip("Greenplum stress coverage requires x86_64")
    alias = backend_alias(backend, target=True)
    table = resource_registry.table(alias, integration_table(backend, "concurrent_write"))
    options = table_options(backend, only_shard=backend == "ch")
    frames = [
        pd.DataFrame(
            {
                "row_id": list(range(offset, offset + 20)),
                "event_date": [pd.Timestamp("2026-08-01") + pd.Timedelta(days=worker)] * 20,
                "value": [f"writer-{worker}"] * 20,
            }
        )
        for worker, offset in enumerate((0, 100))
    ]
    row_type = "Int64" if backend == "ch" else "BIGINT"
    value_type = "TEXT" if backend == "gp" else "String" if backend == "ch" else "VARCHAR"
    sql.create_sql_table(
        alias,
        table,
        table_schema={"row_id": row_type, "event_date": "DATE", "value": value_type},
        retry_cnt=1,
        **options,
    )
    barrier = threading.Barrier(2, timeout=20)

    def append(frame: pd.DataFrame) -> int:
        barrier.wait()
        return sql.load_df(alias, table, frame, write_mode="append", retry_cnt=2, **options)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(append, frames))
    assert results == [20, 20]
    expected = pd.concat(frames, ignore_index=True).sort_values("row_id").reset_index(drop=True)
    actual = sql.read(alias, f"SELECT row_id, event_date, value FROM {table} ORDER BY row_id")
    assert_exact_frame(actual, expected, date_columns=("event_date",))
    (ARTIFACT_DIR / "concurrent-writer-results.json").write_text(
        json.dumps({"backend": backend, "results": results, "rows": len(actual)}, indent=2),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "backend",
    [scenario_param(f"concurrency.upsert.{backend}", backend) for backend in BACKENDS],
)
def test_concurrent_disjoint_upserts_preserve_exact_values(
    backend: str,
    resource_registry: ResourceRegistry,
) -> None:
    if backend == "gp" and os.environ.get("SQL_INTEGRATION_GP") != "1":
        pytest.skip("Greenplum stress coverage requires x86_64")
    alias = backend_alias(backend, target=True)
    table = resource_registry.table(alias, integration_table(backend, "concurrent_upsert"))
    options = table_options(backend, only_shard=backend == "ch")
    original = pd.DataFrame(
        {
            "row_id": [1, 2],
            "event_date": [pd.Timestamp("2026-08-01"), pd.Timestamp("2026-08-02")],
            "value": ["before-1", "before-2"],
        }
    )
    sql.load_df(alias, table, original, write_mode="replace", **options)
    updates = [
        pd.DataFrame(
            {
                "row_id": [row_id],
                "event_date": [pd.Timestamp("2026-08-01") + pd.Timedelta(days=row_id - 1)],
                "value": [f"after-{row_id}"],
            }
        )
        for row_id in (1, 2)
    ]
    barrier = threading.Barrier(2, timeout=20)

    def upsert(frame: pd.DataFrame) -> int:
        barrier.wait()
        upsert_options = dict(options)
        if backend != "gp":
            upsert_options["upsert_partition_column"] = "event_date"
        return sql.load_df(
            alias,
            table,
            frame,
            write_mode="upsert",
            key_columns=["row_id"],
            retry_cnt=2,
            **upsert_options,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(upsert, updates))
    assert results == [1, 1]
    actual = sql.read(alias, f"SELECT row_id, event_date, value FROM {table} ORDER BY row_id")
    expected = pd.concat(updates, ignore_index=True)
    assert_exact_frame(actual, expected, date_columns=("event_date",))


@pytest.mark.sql_scenario("concurrency.lock.gp")
def test_greenplum_lock_contention_recovers_with_fresh_connection(
    resource_registry: ResourceRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.environ.get("SQL_INTEGRATION_GP") != "1":
        pytest.skip("Greenplum stress coverage requires x86_64")
    table = resource_registry.table("gp_target", integration_table("gp", "lock_recovery"))
    frame = pd.DataFrame({"row_id": [1], "value": ["seed"]})
    sql.load_df("gp_target", table, frame, write_mode="replace")
    admin = get_sql_connection("gp_target")
    lock_connection = get_sql_connection("gp_target")
    resource_registry.finalizer(lock_connection.close)
    resource_registry.finalizer(admin.close)
    admin.cursor().execute("ALTER ROLE gpadmin SET lock_timeout = '1s'")
    admin.commit()
    cursor = lock_connection.cursor()
    cursor.execute(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE")

    load_module = importlib.import_module("analytics_toolkit.sql.dml.load.load_df")
    real_open = load_module.get_sql_connection
    identities: list[int] = []

    def record_connection(db_key: str) -> Any:
        connection = real_open(db_key)
        identities.append(id(connection))
        return connection

    monkeypatch.setattr(load_module, "get_sql_connection", record_connection)
    outcome: dict[str, Any] = {}

    def load() -> None:
        try:
            outcome["result"] = sql.load_df(
                "gp_target",
                table,
                pd.DataFrame({"row_id": [2], "value": ["after-lock"]}),
                write_mode="append",
                retry_cnt=3,
                timeout_increment=1,
            )
        except BaseException as exc:
            outcome["error"] = repr(exc)

    worker = threading.Thread(target=load, name="gp-lock-load")
    worker.start()
    deadline = time.monotonic() + 20
    while len(identities) < 2 and time.monotonic() < deadline:
        time.sleep(0.1)
    lock_connection.rollback()
    worker.join(30)
    admin.cursor().execute("ALTER ROLE gpadmin RESET lock_timeout")
    admin.commit()
    assert not worker.is_alive()
    assert outcome == {"result": 1}
    assert len(set(identities)) >= 2
    actual = sql.read("gp_target", f"SELECT row_id, value FROM {table} ORDER BY row_id")
    assert actual.to_dict("records") == [
        {"row_id": 1, "value": "seed"},
        {"row_id": 2, "value": "after-lock"},
    ]
    (ARTIFACT_DIR / "lock-timeline.json").write_text(
        json.dumps({"connection_identities": identities, "outcome": outcome}, indent=2),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("mode", "target_alias"),
    [
        scenario_param("stress.stream.values", "values", "ch_target"),
        scenario_param("stress.stream.parquet", "parquet", "trino_target_parquet"),
    ],
)
def test_million_row_transfer_has_bounded_batches_and_memory(
    mode: str,
    target_alias: str,
    resource_registry: ResourceRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = "ch" if mode == "values" else "trino"
    table = resource_registry.table(target_alias, integration_table(backend, f"stress_{mode}"))
    attempt_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.attempt")
    real_batches = attempt_module.iter_source_batches
    batch_sizes: list[int] = []

    def observed_batches(*args: Any, **kwargs: Any):
        for batch in real_batches(*args, **kwargs):
            batch_sizes.append(batch.row_count)
            yield batch

    monkeypatch.setattr(attempt_module, "iter_source_batches", observed_batches)
    query = (
        "WITH generated AS ("
        "SELECT ((left_value - 1) * 1000 + right_value) AS value "
        "FROM UNNEST(sequence(1, 1000)) left_side(left_value) "
        "CROSS JOIN UNNEST(sequence(1, 1000)) right_side(right_value)) "
        "SELECT value AS row_id, DATE '2026-08-01' AS event_date, "
        "CAST(value AS VARCHAR) AS value FROM generated"
    )
    options: dict[str, Any] = {
        "write_mode": "replace",
        "batch_size": 10_000,
        "retry_cnt": 1,
        "full_retry_cnt": 1,
        "adaptive_batch_size": False,
        "target_rows_per_second": False,
        "ignore_source_staging": True,
    }
    if mode == "values":
        options.update(table_options("ch", only_shard=True))
    else:
        options["partition_by"] = ["event_date"]
    artifact = ARTIFACT_DIR / f"memory-profile-{mode}.json"
    with MemorySampler(artifact) as sampler:
        transferred = sql.transfer(
            "trino_values",
            target_alias,
            query,
            table,
            **options,
        )
    assert transferred == 1_000_000
    assert len(batch_sizes) >= 100
    assert max(batch_sizes) <= 10_000
    assert sampler.growth_bytes < 512 * 1024 * 1024
    result = sql.read(
        target_alias,
        f"SELECT count(*) AS rows, min(row_id) AS low, max(row_id) AS high FROM {table}",
    )
    assert result.iloc[0].tolist() == [1_000_000, 1, 1_000_000]


@pytest.mark.sql_scenario("stress.connection.pressure")
def test_connection_pressure_fails_contextually_and_recovers() -> None:
    tasks = [
        {
            "name": f"pressure_{index}",
            "type": "read",
            "db_key": "trino_pressure",
            "query": (
                "SELECT sum(sin(left_value + right_value + multiplier)) "
                "FROM UNNEST(sequence(1, 10000)) left_side(left_value) "
                "CROSS JOIN UNNEST(sequence(1, 10000)) right_side(right_value) "
                "CROSS JOIN UNNEST(sequence(1, 5)) multipliers(multiplier)"
            ),
            "retry_cnt": 1,
        }
        for index in range(4)
    ]
    results = sql.parallel_sql(tasks, concurrency=4, fail_fast=False)
    failures = {name: value for name, value in results.items() if isinstance(value, str)}
    successes = {name: value for name, value in results.items() if not isinstance(value, str)}
    assert len(failures) >= 1
    assert len(successes) >= 1
    recovered = sql.read("trino_pressure", "SELECT 1 AS value", retry_cnt=1)
    assert int(recovered.iloc[0, 0]) == 1
    (ARTIFACT_DIR / "connection-pressure.json").write_text(
        json.dumps(
            {"failures": failures, "successes": sorted(successes), "recovered": True},
            indent=2,
        ),
        encoding="utf-8",
    )
