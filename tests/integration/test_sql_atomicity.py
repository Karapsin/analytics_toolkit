from __future__ import annotations

# ruff: noqa: EM101, I001, TC002, TRY003

import importlib

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
from tests.integration.support.normalization import assert_exact_frame
from tests.integration.support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]


def _seeded_target(
    backend: str,
    registry: ResourceRegistry,
) -> tuple[str, str, pd.DataFrame]:
    alias = backend_alias(backend, target=True)
    table = registry.table(
        alias,
        integration_table(backend, "atomic_target"),
    )
    original = pd.DataFrame(
        {
            "row_id": [100, 200],
            "event_date": [pd.Timestamp("2026-05-01"), pd.Timestamp("2026-05-02")],
            "value": ["original-a", "original-b"],
        }
    )
    sql.load_df(
        alias,
        table,
        original,
        write_mode="replace",
        **table_options(backend, only_shard=backend == "ch"),
    )
    return alias, table, original


def _read(alias: str, table: str) -> pd.DataFrame:
    return sql.read(alias, f"SELECT row_id, event_date, value FROM {table} ORDER BY row_id")


def _upsert_options(backend: str) -> dict[str, object]:
    options = table_options(backend, only_shard=backend == "ch")
    options["key_columns"] = ["row_id"]
    if backend != "gp":
        options["upsert_partition_column"] = "event_date"
    return options


@pytest.mark.parametrize(
    "backend",
    [scenario_param(f"atomicity.load.{backend}", backend) for backend in BACKENDS],
)
def test_validation_failures_preserve_existing_target(
    backend: str,
    resource_registry: ResourceRegistry,
) -> None:
    if not backend_enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    alias, table, original = _seeded_target(backend, resource_registry)
    duplicate = pd.DataFrame(
        {
            "row_id": [300, 300],
            "event_date": [pd.Timestamp("2026-05-03")] * 2,
            "value": ["duplicate-a", "duplicate-b"],
        }
    )
    with pytest.raises(ValueError, match="Duplicate key"):
        sql.load_df(
            alias,
            table,
            duplicate,
            write_mode="upsert",
            retry_cnt=1,
            **_upsert_options(backend),
        )
    assert_exact_frame(_read(alias, table), original)

    null_key = duplicate.iloc[:1].copy()
    null_key.loc[null_key.index[0], "row_id"] = None
    with pytest.raises(ValueError, match="Null key"):
        sql.load_df(
            alias,
            table,
            null_key,
            write_mode="upsert",
            retry_cnt=1,
            **_upsert_options(backend),
        )
    assert_exact_frame(_read(alias, table), original)

    with pytest.raises(ValueError, match="not found"):
        sql.load_df(
            alias,
            table,
            duplicate.rename(columns={"row_id": "wrong_key"}),
            write_mode="upsert",
            retry_cnt=1,
            **_upsert_options(backend),
        )
    assert_exact_frame(_read(alias, table), original)


@pytest.mark.parametrize(
    "backend",
    [scenario_param(f"atomicity.finalize.{backend}", backend) for backend in BACKENDS],
)
def test_failure_before_upsert_finalization_preserves_target(
    backend: str,
    resource_registry: ResourceRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not backend_enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    alias, table, original = _seeded_target(backend, resource_registry)
    update = pd.DataFrame(
        {
            "row_id": [200, 300],
            "event_date": [pd.Timestamp("2026-05-02"), pd.Timestamp("2026-05-03")],
            "value": ["changed", "new"],
        }
    )
    load_module = importlib.import_module("analytics_toolkit.sql.dml.load.load_df")

    def fail_finalization(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("injected failure before finalization")

    monkeypatch.setattr(load_module, "upsert_stage_table", fail_finalization)
    with pytest.raises(Exception, match="injected failure before finalization"):
        sql.load_df(
            alias,
            table,
            update,
            write_mode="upsert",
            retry_cnt=1,
            **_upsert_options(backend),
        )
    assert_exact_frame(_read(alias, table), original)


@pytest.mark.parametrize(
    "backend",
    [scenario_param(f"atomicity.append.{backend}", backend) for backend in BACKENDS],
)
def test_append_key_overlap_preserves_existing_target(
    backend: str,
    resource_registry: ResourceRegistry,
) -> None:
    if not backend_enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    alias, table, original = _seeded_target(backend, resource_registry)
    overlap = pd.DataFrame(
        {
            "row_id": [200, 300],
            "event_date": [pd.Timestamp("2026-05-02"), pd.Timestamp("2026-05-03")],
            "value": ["overlap", "new"],
        }
    )
    options = table_options(backend, only_shard=backend == "ch")
    with pytest.raises(ValueError, match=r"(?i)overlap|key"):
        sql.load_df(
            alias,
            table,
            overlap,
            write_mode="append",
            key_columns=["row_id"],
            retry_cnt=1,
            **options,
        )
    assert_exact_frame(_read(alias, table), original)


@pytest.mark.parametrize(
    "backend",
    [scenario_param(f"atomicity.created_target.{backend}", backend) for backend in BACKENDS],
)
def test_target_created_only_for_failed_load_is_removed(
    backend: str,
    resource_registry: ResourceRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not backend_enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    alias = backend_alias(backend, target=True)
    table = resource_registry.table(alias, integration_table(backend, "failed_created_target"))
    load_module = importlib.import_module("analytics_toolkit.sql.dml.load.load_df")

    def fail_insert(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("injected insert after target creation")

    monkeypatch.setattr(load_module, "insert_table_batch", fail_insert)
    with pytest.raises(Exception, match="injected insert after target creation"):
        sql.load_df(
            alias,
            table,
            pd.DataFrame(
                {
                    "row_id": [1],
                    "event_date": [pd.Timestamp("2026-05-01")],
                    "value": ["never-loaded"],
                }
            ),
            write_mode="append",
            retry_cnt=1,
            **table_options(backend, only_shard=backend == "ch"),
        )
    assert not sql.table_info(alias, table).exists


@pytest.mark.sql_scenario("atomicity.transfer.row_count")
def test_transfer_row_count_mismatch_preserves_existing_target(
    resource_registry: ResourceRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = resource_registry.table("ch_source", integration_table("ch", "count_source"))
    target = resource_registry.table(
        "trino_target_values",
        integration_table("trino", "count_target"),
    )
    source_frame = pd.DataFrame(
        {
            "row_id": [1, 2],
            "event_date": [pd.Timestamp("2026-05-01"), pd.Timestamp("2026-05-02")],
            "value": ["one", "two"],
        }
    )
    original = pd.DataFrame(
        {
            "row_id": [100],
            "event_date": [pd.Timestamp("2026-05-10")],
            "value": ["original"],
        }
    )
    sql.load_df(
        "ch_source",
        source,
        source_frame,
        write_mode="replace",
        **table_options("ch", only_shard=True),
    )
    sql.load_df("trino_target_values", target, original, write_mode="replace")
    staged_attempt_module = importlib.import_module(
        "analytics_toolkit.sql.dml.transfer.flow.staged_attempt"
    )

    def mismatch(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("injected loaded-stage row-count mismatch")

    monkeypatch.setattr(staged_attempt_module, "validate_loaded_stage_row_count", mismatch)
    with pytest.raises(Exception, match="row-count mismatch"):
        sql.transfer(
            "ch_source",
            "trino_target_values",
            f"SELECT row_id, event_date, value FROM {source}",
            target,
            write_mode="replace",
            batch_size=1,
            retry_cnt=1,
            full_retry_cnt=1,
            adaptive_batch_size=False,
            target_rows_per_second=False,
        )
    actual = sql.read(
        "trino_target_values",
        f"SELECT row_id, event_date, value FROM {target} ORDER BY row_id",
    )
    assert_exact_frame(actual, original)
