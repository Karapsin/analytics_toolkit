from __future__ import annotations

# ruff: noqa: I001, PT018, TC002

import pytest
from analytics_toolkit import sql
from tests.integration.manifest import scenario_param
from tests.integration.support.backends import (
    BACKENDS,
    backend_alias,
    backend_enabled,
    canonical_frame,
    canonical_schema,
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
    actual = sql.read(alias, f"SELECT * FROM {table} ORDER BY row_id")
    _assert_canonical(actual, frame)

    info = sql.table_info(alias, table, include_row_count=True)
    assert info.exists and info.row_count == len(frame)
    expected_tokens = {
        "row_id": ("bigint", "int64"),
        "decimal_value": ("decimal(18,4)", "numeric(18,4)"),
        "uuid_value": ("uuid",),
    }
    schema_contains(info.columns, expected_tokens)
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
        ["row_id", "flag", "signed_value", "float_value", "unicode_text"]
    ].copy()
    # Keep inference portable: nullable/all-null and backend-native temporal,
    # decimal, UUID, and JSON types are exercised by the explicit-schema case.
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
    target_table = _register_table(resource_registry, target, target_alias, "type_target")
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
    transferred = sql.transfer(
        source_alias,
        target_alias,
        from_table=source_table,
        to_table=target_table,
        write_mode="replace",
        batch_size=2,
        adaptive_batch_size=False,
        target_rows_per_second=False,
        table_schema=canonical_schema(target),
        **table_options(target, only_shard=target == "ch"),
        retry_cnt=1,
    )
    assert transferred == len(frame)
    actual = sql.read(target_alias, f"SELECT * FROM {target_table} ORDER BY row_id")
    _assert_canonical(actual, frame)
