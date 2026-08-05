from __future__ import annotations

# ruff: noqa: I001, PT018, TC002

import importlib
import threading
import uuid
from collections import Counter
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit import sql
from tests.integration.manifest import scenario_param
from tests.integration.support.backends import (
    BACKENDS,
    backend_alias,
    backend_enabled,
    canonical_frame,
    canonical_schema,
    canonical_type_tokens,
    integration_table,
    table_options,
)
from tests.integration.support.normalization import assert_exact_frame, schema_contains
from tests.integration.support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]


def _register_table(
    registry: ResourceRegistry,
    backend: str,
    alias: str,
    purpose: str,
) -> str:
    return registry.table(
        alias,
        integration_table(backend, purpose),
    )


def _assert_canonical(actual, expected) -> None:
    assert_exact_frame(
        actual,
        expected,
        json_columns=("json_value",),
        decimal_columns=("decimal_value",),
        date_columns=("event_date",),
    )
    assert actual.isna().sum()["all_null_text"] == len(actual)


def _quote_column(backend: str, column: str) -> str:
    quote = "`" if backend == "ch" else '"'
    return f"{quote}{column}{quote}"


def _canonical_projection(backend: str) -> str:
    return ", ".join(_quote_column(backend, column) for column in canonical_frame().columns)


def _read_canonical(alias: str, table: str, backend: str):
    return sql.read(
        alias,
        f"SELECT {_canonical_projection(backend)} FROM {table} "
        f"ORDER BY {_quote_column(backend, 'row_id')}",
    )


def _assert_canonical_table(
    alias: str,
    table: str,
    backend: str,
    expected: pd.DataFrame,
) -> None:
    actual = _read_canonical(alias, table, backend)
    _assert_canonical(actual, expected)
    info = sql.table_info(alias, table, include_row_count=True)
    assert info.exists and info.row_count == len(expected)
    assert set(info.columns) == set(canonical_frame().columns)
    schema_contains(info.columns, canonical_type_tokens(backend))


def _seed_frame(*, upsert: bool = False) -> pd.DataFrame:
    frame = canonical_frame()
    if not upsert:
        seed = frame.iloc[[3, 4]].copy()
        seed["row_id"] = [101, 102]
        seed["event_date"] = [
            pd.Timestamp("2026-03-01").date(),
            pd.Timestamp("2026-03-02").date(),
        ]
        seed["uuid_value"] = [uuid.UUID(int=101), uuid.UUID(int=102)]
        return seed.reset_index(drop=True)

    seed = frame.iloc[[0, 4]].copy()
    seed.loc[seed.index[0], ["flag", "signed_value", "float_value", "unicode_text"]] = [
        False,
        -999,
        -8.5,
        "stale-value",
    ]
    seed.loc[seed.index[0], "decimal_value"] = Decimal("7.7777")
    seed.loc[seed.index[0], "json_value"] = '{"stale":true}'
    seed.loc[seed.index[0], "uuid_value"] = uuid.UUID(int=999)
    seed.loc[seed.index[1], "row_id"] = 105
    seed.loc[seed.index[1], "event_date"] = pd.Timestamp("2026-03-05").date()
    seed.loc[seed.index[1], "nullable_ts"] = None
    seed.loc[seed.index[1], "uuid_value"] = uuid.UUID(int=105)
    return seed.reset_index(drop=True)


def _expected_for_mode(frame: pd.DataFrame, seed: pd.DataFrame, write_mode: str) -> pd.DataFrame:
    if write_mode == "append":
        expected = pd.concat([frame, seed], ignore_index=True)
    elif write_mode == "upsert":
        expected = pd.concat([frame, seed.iloc[[1]]], ignore_index=True)
    else:
        expected = frame.copy()
    return expected.sort_values("row_id").reset_index(drop=True)


@pytest.mark.parametrize(
    "backend",
    [scenario_param(f"types.roundtrip.{backend}", backend) for backend in BACKENDS],
)
def test_explicit_type_roundtrip(
    backend: str,
    resource_registry: ResourceRegistry,
) -> None:
    if not backend_enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    alias = backend_alias(backend, target=True)
    table = _register_table(resource_registry, backend, alias, "type_roundtrip")
    frame = canonical_frame()
    inserted = sql.load_df(
        alias,
        table,
        frame,
        write_mode="replace",
        table_schema=canonical_schema(backend),
        **table_options(backend, only_shard=backend == "ch"),
    )
    assert inserted == len(frame)
    _assert_canonical_table(alias, table, backend, frame)
    ddl = sql.extract_ddl(alias, table)
    assert "CREATE" in ddl.upper()
    assert "decimal" in ddl.lower() or "numeric" in ddl.lower()


@pytest.mark.parametrize(
    "backend",
    [scenario_param(f"types.inferred.{backend}", backend) for backend in BACKENDS],
)
def test_inferred_portable_subset_roundtrip(
    backend: str,
    resource_registry: ResourceRegistry,
) -> None:
    if not backend_enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    alias = backend_alias(backend, target=True)
    table = _register_table(resource_registry, backend, alias, "type_inferred")
    expected = canonical_frame()[
        ["row_id", "flag", "signed_value", "float_value", "unicode_text", "uuid_value"]
    ].copy()
    # Keep inference portable: nullable/all-null and backend-native temporal,
    # decimal, and JSON types are exercised by the explicit-schema case.
    expected = expected.iloc[:2].copy()
    expected["flag"] = expected["flag"].astype(bool)
    if backend == "gp":
        options = {"gp_distributed_by_key": "row_id"}
    elif backend == "ch":
        options = {
            "order_by": "row_id",
            "ch_engine": "MergeTree",
            "ch_shard_on_cluster": "integration_cluster",
            "ch_distributed_on_cluster": "integration_cluster",
            "ch_distributed_cluster": "integration_cluster",
            "ch_only_shard": True,
        }
    else:
        options = {}
    inserted = sql.load_df(
        alias,
        table,
        expected,
        write_mode="replace",
        retry_cnt=1,
        **options,
    )
    assert inserted == len(expected)
    actual = sql.read(alias, f"SELECT * FROM {table} ORDER BY row_id")
    assert_exact_frame(actual, expected)
    info = sql.table_info(alias, table)
    schema_contains(info.columns, {"uuid_value": ("uuid",)})


@pytest.mark.parametrize(
    ("source", "target"),
    [
        scenario_param(f"types.transfer.{source}.{target}", source, target)
        for source in BACKENDS
        for target in BACKENDS
    ],
)
def test_cross_backend_exact_type_transfer(
    source: str,
    target: str,
    resource_registry: ResourceRegistry,
) -> None:
    if not backend_enabled(source) or not backend_enabled(target):
        pytest.skip("Greenplum requires x86_64")
    source_alias = backend_alias(source)
    target_alias = backend_alias(target, target=True)
    source_table = _register_table(resource_registry, source, source_alias, "type_source")
    frame = canonical_frame()
    sql.load_df(
        source_alias,
        source_table,
        frame,
        write_mode="replace",
        table_schema=canonical_schema(source),
        retry_cnt=1,
        **table_options(source, only_shard=source == "ch"),
    )
    for write_mode in ("append", "replace", "truncate_insert", "upsert"):
        target_table = _register_table(
            resource_registry,
            target,
            target_alias,
            f"type_target_{write_mode}",
        )
        seed = _seed_frame(upsert=write_mode == "upsert")
        target_options = table_options(
            target,
            only_shard=target == "ch" and write_mode == "upsert",
        )
        sql.load_df(
            target_alias,
            target_table,
            seed,
            write_mode="replace",
            table_schema=canonical_schema(target),
            retry_cnt=1,
            **target_options,
        )
        mode_options: dict[str, Any] = {}
        if write_mode == "upsert":
            mode_options["key_columns"] = ["row_id"]
            if target != "gp":
                mode_options["upsert_partition_column"] = "event_date"
        transferred = sql.transfer(
            source_alias,
            target_alias,
            from_table=source_table,
            to_table=target_table,
            write_mode=write_mode,
            concurrency=3,
            batch_size=2,
            adaptive_batch_size=False,
            target_rows_per_second=False,
            table_schema=canonical_schema(target),
            retry_cnt=1,
            **target_options,
            **mode_options,
        )
        assert transferred == len(frame)
        expected = _expected_for_mode(frame, seed, write_mode)
        _assert_canonical_table(target_alias, target_table, target, expected)


@pytest.mark.sql_scenario("types.transfer.parquet.ch.trino")
def test_rich_type_parquet_transfer(
    resource_registry: ResourceRegistry,
) -> None:
    source_alias = backend_alias("ch")
    target_alias = "trino_target_parquet"
    source_table = _register_table(
        resource_registry,
        "ch",
        source_alias,
        "parquet_type_source",
    )
    target_table = _register_table(
        resource_registry,
        "trino",
        target_alias,
        "parquet_type_target",
    )
    frame = canonical_frame()
    sql.load_df(
        source_alias,
        source_table,
        frame,
        write_mode="replace",
        table_schema=canonical_schema("ch"),
        retry_cnt=1,
        **table_options("ch", only_shard=True),
    )
    transferred = sql.transfer(
        source_alias,
        target_alias,
        from_table=source_table,
        to_table=target_table,
        write_mode="replace",
        trino_mode="parquet",
        concurrency=3,
        batch_size=2,
        adaptive_batch_size=False,
        target_rows_per_second=False,
        table_schema=canonical_schema("trino"),
        retry_cnt=1,
        **table_options("trino"),
    )
    assert transferred == len(frame)
    _assert_canonical_table(target_alias, target_table, "trino", frame)


@pytest.mark.sql_scenario("types.transfer.retry.trino.ch")
def test_staged_source_full_retry_rematerializes_completed_ranges(  # noqa: PLR0915
    resource_registry: ResourceRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged_module = importlib.import_module(
        "analytics_toolkit.sql.dml.transfer.flow.staged_attempt"
    )
    attempt_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.attempt")
    scheduler_module = importlib.import_module(
        "analytics_toolkit.sql.dml.transfer.flow.range_scheduler"
    )
    real_read = staged_module._read_snapshot_range
    real_complete = scheduler_module.AdaptiveRangeScheduler.complete
    real_attempt = attempt_module.run_staged_source_transfer_attempt
    parent = (0, 3, 5)
    lock = threading.Lock()
    completion_gate = threading.Event()
    read_counts: Counter[tuple[int, int, int]] = Counter()
    reads_by_attempt: dict[int, list[tuple[int, int, int]]] = {}
    completed: list[tuple[int, int, int]] = []
    completed_before_failure: list[tuple[int, int, int]] = []
    fault_count = 0
    attempt_count = 0

    def key(claimed: Any) -> tuple[int, int, int]:
        return (claimed.slice_id, claimed.start_ordinal, claimed.stop_ordinal)

    def faulted_read(*args: Any, **kwargs: Any) -> Any:
        nonlocal fault_count
        claimed = args[-1]
        interval = key(claimed)
        with lock:
            read_counts[interval] += 1
            reads_by_attempt.setdefault(attempt_count, []).append(interval)
            should_fail = interval == parent and fault_count == 0
        if should_fail:
            assert completion_gate.wait(30), "no ordinal range completed before injected failure"
            with lock:
                fault_count += 1
                completed_before_failure.append(completed[0])
            raise RuntimeError
        return real_read(*args, **kwargs)

    def recording_complete(self: Any, worker_id: int, claimed: Any) -> None:
        real_complete(self, worker_id, claimed)
        interval = key(claimed)
        with lock:
            completed.append(interval)
        if interval != parent:
            completion_gate.set()

    def recording_attempt(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempt_count
        attempt_count += 1
        return real_attempt(*args, **kwargs)

    monkeypatch.setattr(staged_module, "_read_snapshot_range", faulted_read)
    monkeypatch.setattr(
        scheduler_module.AdaptiveRangeScheduler,
        "complete",
        recording_complete,
    )
    monkeypatch.setattr(attempt_module, "run_staged_source_transfer_attempt", recording_attempt)

    source_alias = backend_alias("trino")
    target_alias = backend_alias("ch", target=True)
    source_table = _register_table(
        resource_registry,
        "trino",
        source_alias,
        "retry_type_source",
    )
    target_table = _register_table(
        resource_registry,
        "ch",
        target_alias,
        "retry_type_target",
    )
    frame = canonical_frame()
    sql.load_df(
        source_alias,
        source_table,
        frame,
        write_mode="replace",
        table_schema=canonical_schema("trino"),
        retry_cnt=1,
        **table_options("trino"),
    )
    transferred = sql.transfer(
        source_alias,
        target_alias,
        from_table=source_table,
        to_table=target_table,
        write_mode="replace",
        concurrency=3,
        batch_size=2,
        min_batch_size=1,
        adaptive_batch_size=False,
        target_rows_per_second=False,
        full_retry_cnt=2,
        full_timeout_increment=0,
        table_schema=canonical_schema("ch"),
        retry_cnt=1,
        **table_options("ch", only_shard=True),
    )
    assert transferred == len(frame)
    assert fault_count == 1 and read_counts[parent] == 2
    assert completed.count(parent) == 1
    assert len(completed_before_failure) == 1
    completed_first = completed_before_failure[0]
    assert read_counts[completed_first] == 2
    assert completed.count(completed_first) == 2
    assert attempt_count == 2
    expected_ranges = {
        (0, start, min(len(frame) + 1, start + 2)) for start in range(1, len(frame) + 1, 2)
    }
    assert set(reads_by_attempt) == {1, 2}
    assert set(reads_by_attempt[1]) <= expected_ranges
    assert set(reads_by_attempt[2]) == expected_ranges
    assert completed_first in reads_by_attempt[1]
    assert completed_first in reads_by_attempt[2]
    _assert_canonical_table(target_alias, target_table, "ch", frame)


@pytest.mark.sql_scenario("types.transfer.staged_small.ch.gp")
def test_staged_ch_to_gp_transfer_caps_workers_for_one_batch(
    resource_registry: ResourceRegistry,
) -> None:
    if not backend_enabled("gp"):
        pytest.skip("Greenplum requires x86_64")
    source_alias = backend_alias("ch")
    target_alias = backend_alias("gp", target=True)
    source_table = _register_table(
        resource_registry,
        "ch",
        source_alias,
        "staged_small_source",
    )
    target_table = _register_table(
        resource_registry,
        "gp",
        target_alias,
        "staged_small_target",
    )
    frame = canonical_frame().iloc[:2].copy()
    sql.load_df(
        source_alias,
        source_table,
        frame,
        write_mode="replace",
        table_schema=canonical_schema("ch"),
        retry_cnt=1,
        **table_options("ch", only_shard=True),
    )

    transferred = sql.transfer(
        source_alias,
        target_alias,
        from_table=source_table,
        to_table=target_table,
        write_mode="replace",
        concurrency=3,
        batch_size=100,
        adaptive_batch_size=False,
        target_rows_per_second=False,
        table_schema=canonical_schema("gp"),
        retry_cnt=1,
        **table_options("gp"),
    )

    assert transferred == len(frame)
    _assert_canonical_table(target_alias, target_table, "gp", frame)


@pytest.mark.sql_scenario("types.transfer.staged_refresh.ch.gp")
def test_staged_ch_to_gp_consolidation_replaces_closed_coordinator(
    resource_registry: ResourceRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not backend_enabled("gp"):
        pytest.skip("Greenplum requires x86_64")
    staged_module = importlib.import_module(
        "analytics_toolkit.sql.dml.transfer.flow.staged_attempt"
    )
    real_consolidate = staged_module._consolidate_worker_stages
    coordinator_closed = False

    def close_before_consolidation(*args: Any, **kwargs: Any) -> Any:
        nonlocal coordinator_closed
        target_ref = args[1]
        target_ref["connection"].close()
        coordinator_closed = True
        return real_consolidate(*args, **kwargs)

    monkeypatch.setattr(
        staged_module,
        "_consolidate_worker_stages",
        close_before_consolidation,
    )
    source_alias = backend_alias("ch")
    target_alias = backend_alias("gp", target=True)
    source_table = _register_table(
        resource_registry,
        "ch",
        source_alias,
        "staged_refresh_source",
    )
    target_table = _register_table(
        resource_registry,
        "gp",
        target_alias,
        "staged_refresh_target",
    )
    frame = canonical_frame()
    sql.load_df(
        source_alias,
        source_table,
        frame,
        write_mode="replace",
        table_schema=canonical_schema("ch"),
        retry_cnt=1,
        **table_options("ch", only_shard=True),
    )

    transferred = sql.transfer(
        source_alias,
        target_alias,
        from_table=source_table,
        to_table=target_table,
        write_mode="replace",
        concurrency=3,
        batch_size=2,
        adaptive_batch_size=False,
        target_rows_per_second=False,
        table_schema=canonical_schema("gp"),
        retry_cnt=1,
        **table_options("gp"),
    )

    assert coordinator_closed
    assert transferred == len(frame)
    _assert_canonical_table(target_alias, target_table, "gp", frame)
