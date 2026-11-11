"""Disposable acceptance cases for the SQL correctness audit contracts."""

from __future__ import annotations

import os
import uuid
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.metadata import CH_DATABASES_QUERY, build_ch_completion_query
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

from tests.sql.integration._support.backends import (
    BACKENDS,
    backend_alias,
    backend_enabled,
    integration_table,
    table_options,
)
from tests.sql.integration._support.identity import query_label
from tests.sql.integration.manifest import scenario_param

if TYPE_CHECKING:
    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]


def _label(purpose: str) -> str:
    return query_label(
        os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8]),
        os.environ.get("SQL_INTEGRATION_TEST_ID", "manual"),
        purpose,
    )


@pytest.mark.parametrize(
    "alias",
    [
        scenario_param("audit.exact_values.http", "ch"),
        scenario_param("audit.exact_values.native", "ch_native"),
    ],
)
def test_clickhouse_exact_values_roundtrip(alias: str, resource_registry: ResourceRegistry) -> None:
    table = resource_registry.table(
        alias, integration_table("ch", "audit_exact"), ch_cluster="integration_cluster"
    )
    exact_decimal = Decimal("12345678901234567890.123456789")
    precise_ts = pd.Timestamp("1969-12-31 23:59:59.123456789", tz="UTC")
    frame = pd.DataFrame(
        {
            "row_id": [1, 2],
            "nullable_integer": pd.Series([None, 2**53 + 1], dtype=object),
            "unsigned_integer": pd.Series([2**64 - 1, 0], dtype=object),
            "amount": pd.Series([exact_decimal, None], dtype=object),
            "event_date": pd.Series([date(1900, 1, 1), date(2026, 1, 1)], dtype=object),
            "event_ts": pd.Series([precise_ts, None], dtype=object),
            "items": pd.Series([[2**53 + 1, 2**63 - 1], []], dtype=object),
        }
    )
    assert (
        sql.load_df(
            alias,
            table,
            frame,
            write_mode="replace",
            retry_cnt=1,
            table_schema={
                "row_id": "Int64",
                "nullable_integer": "Nullable(Int64)",
                "unsigned_integer": "UInt64",
                "amount": "Nullable(Decimal(38,9))",
                "event_date": "Date32",
                "event_ts": "Nullable(DateTime64(9, 'UTC'))",
                "items": "Array(Int64)",
            },
            ch_only_shard=True,
            ch_shard_on_cluster="integration_cluster",
            order_by="row_id",
            query_label=_label("exact-load"),
        )
        == 2
    )
    actual = sql.read(
        alias, f"SELECT * FROM {table} ORDER BY row_id", query_label=_label("exact-read")
    )
    assert actual.iloc[1]["nullable_integer"] == 2**53 + 1
    assert actual.iloc[0]["unsigned_integer"] == 2**64 - 1
    assert actual.iloc[0]["amount"] == exact_decimal
    assert pd.Timestamp(actual.iloc[0]["event_date"]).date() == date(1900, 1, 1)
    assert actual.iloc[0]["event_ts"] == precise_ts
    assert list(actual.iloc[0]["items"]) == [2**53 + 1, 2**63 - 1]
    duplicate_labels = sql.read(
        alias,
        "SELECT tuple('data', 'String') AS duplicate, toUInt64(18446744073709551615) AS duplicate",
        query_label=_label("duplicate-labels"),
    )
    assert list(duplicate_labels.columns) == ["duplicate", "duplicate"]
    assert duplicate_labels.iloc[0, 0] == ("data", "String")
    assert duplicate_labels.iloc[0, 1] == 2**64 - 1


@pytest.mark.parametrize(
    "backend", [scenario_param(f"audit.upsert.{backend}", backend) for backend in BACKENDS]
)
def test_complete_reordered_upserts_use_partition_scoped_keys(
    backend: str, resource_registry: ResourceRegistry
) -> None:
    if not backend_enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    alias = backend_alias(backend, target=True)
    table = resource_registry.table(
        alias,
        integration_table(backend, "audit_upsert"),
        ch_cluster="integration_cluster" if backend == "ch" else None,
    )
    options = table_options(backend, only_shard=backend == "ch")
    initial = pd.DataFrame(
        {
            "row_id": [1, 1, 2, 9],
            "event_date": [date(2026, month, 1) for month in (1, 2, 2, 3)],
            "left_value": ["jan", "feb", "preserved", "other-partition"],
            "right_value": ["j", "f", "p", "o"],
        }
    )
    sql.load_df(
        alias, table, initial, write_mode="replace", query_label=_label("upsert-seed"), **options
    )
    update = pd.DataFrame(
        {
            "right_value": ["new-f", "new-a"],
            "left_value": ["changed", "april"],
            "event_date": [date(2026, 2, 1), date(2026, 4, 1)],
            "row_id": [1, 1],
        }
    )
    with pytest.raises(ValueError, match="writable columns"):
        sql.load_df(
            alias,
            table,
            update.drop(columns="right_value"),
            write_mode="upsert",
            key_columns="row_id",
            upsert_partition_column="event_date",
            retry_cnt=1,
            query_label=_label("upsert-incomplete"),
            **options,
        )
    unchanged = sql.read(
        alias,
        f"SELECT left_value, right_value FROM {table} ORDER BY event_date, row_id",
        query_label=_label("upsert-unchanged"),
    )
    assert list(unchanged.itertuples(index=False, name=None)) == list(
        initial[["left_value", "right_value"]].itertuples(index=False, name=None)
    )
    assert (
        sql.load_df(
            alias,
            table,
            update,
            write_mode="upsert",
            key_columns="row_id",
            upsert_partition_column="event_date",
            retry_cnt=1,
            query_label=_label("upsert-complete"),
            **options,
        )
        == 2
    )
    rows = sql.read(
        alias,
        f"SELECT row_id, left_value, right_value FROM {table} ORDER BY event_date, row_id",
        query_label=_label("upsert-check"),
    )
    assert list(rows.itertuples(index=False, name=None)) == [
        (1, "jan", "j"),
        (1, "changed", "new-f"),
        (2, "preserved", "p"),
        (9, "other-partition", "o"),
        (1, "april", "new-a"),
    ]


@pytest.mark.sql_scenario("audit.clickhouse.local_creation")
def test_clickhouse_query_creation_respects_local_layout_and_existing_target(
    resource_registry: ResourceRegistry,
) -> None:
    alias = "ch"
    target = resource_registry.table(alias, integration_table("ch", "audit_local"))
    companion = resource_registry.table(alias, f"{target}_shard")
    sql.execute(
        alias,
        f"CREATE TABLE {companion} (row_id UInt64) ENGINE=MergeTree ORDER BY row_id",
        query_label=_label("companion-create"),
    )
    sql.execute(
        alias,
        f"INSERT INTO {companion} SELECT number FROM numbers(100)",
        query_label=_label("companion-seed"),
    )
    options = {"ch_distributed_table": False, "order_by": "row_id", "retry_cnt": 1}
    sql.create_table(
        alias,
        target,
        sql="SELECT toUInt64(1) AS row_id",
        insert_data=True,
        query_label=_label("local-create"),
        **options,
    )
    with pytest.raises(InvalidSqlInputError, match="already exists"):
        sql.create_table(
            alias,
            target,
            sql="SELECT toUInt64(2) AS row_id",
            insert_data=True,
            query_label=_label("local-existing"),
            **options,
        )
    sql.create_table(
        alias,
        target,
        sql="SELECT toUInt64(2) AS row_id",
        insert_data=True,
        if_not_exists=True,
        query_label=_label("local-skip"),
        **options,
    )
    assert sql.read(
        alias, f"SELECT row_id FROM {target}", output_type="list", query_label=_label("local-check")
    ) == [1]
    sql.create_table(
        alias,
        target,
        sql="SELECT toUInt64(3) AS row_id",
        insert_data=True,
        ch_replace_table=True,
        query_label=_label("local-replace"),
        **options,
    )
    assert sql.read(
        alias, f"SELECT row_id FROM {target}", output_type="list", query_label=_label("local-check")
    ) == [3]
    assert (
        sql.read(
            alias,
            f"SELECT count() FROM {companion}",
            output_type="scalar",
            query_label=_label("companion-check"),
        )
        == 100
    )
    with pytest.raises(sql.ClickHouseClusterTopologyError, match=r"(?i)companion|already exists"):
        sql.ch_reconfigure_table(
            alias,
            target,
            ch_distributed_table=True,
            ch_shard_on_cluster="integration_cluster",
            ch_distributed_on_cluster="integration_cluster",
            ch_distributed_cluster="integration_cluster",
            retry_cnt=1,
            query_label=_label("companion-collision"),
        )
    assert (
        sql.read(
            alias,
            f"SELECT count() FROM {companion}",
            output_type="scalar",
            query_label=_label("companion-check"),
        )
        == 100
    )


@pytest.mark.sql_scenario("audit.clickhouse.routing_metadata")
def test_clickhouse_routing_bindings_and_current_database_metadata(
    resource_registry: ResourceRegistry,
) -> None:
    alias = "ch_routed"
    for query in (
        "SELECT one.dummy FROM system.one",
        "SELECT system.one.* FROM system.one",
        "SELECT o.dummy FROM system.one AS o",
        "SELECT dummy FROM cluster('integration_cluster', 'system.one')",
    ):
        result = sql.read(alias, query, query_label=_label("routing-binding"), retry_cnt=1)
        assert result.shape[1] == 1
        assert set(result.iloc[:, 0]) == {0}
    databases = sql.read(
        alias,
        CH_DATABASES_QUERY,
        return_metadata=True,
        query_label=_label("metadata-databases"),
    )
    names = databases.data.iloc[:, 0].tolist()
    assert databases.metadata.data_complete is not False
    assert "system" in names
    current = sql.read(
        alias,
        "SELECT currentDatabase()",
        output_type="scalar",
        query_label=_label("metadata-current"),
    )
    unqualified = sql.read(
        alias,
        build_ch_completion_query(None, "numbers"),
        output_type="list",
        query_label=_label("metadata-unqualified"),
    )
    system = sql.read(
        alias,
        build_ch_completion_query("system", "numbers"),
        output_type="list",
        query_label=_label("metadata-qualified"),
    )
    assert "numbers" in system
    if current != "system":
        assert "numbers" not in unqualified
    _assert_same_named_cross_database_join(resource_registry)


def _assert_same_named_cross_database_join(resource_registry: ResourceRegistry) -> None:
    left = integration_table("ch", "audit_bound_events")
    right = f"default.{left.partition('.')[2]}"
    for table, values in (
        (left, pd.DataFrame({"id": [1, 2], "left_value": [10, 20]})),
        (right, pd.DataFrame({"id": [1, 2], "right_value": [100, 200]})),
    ):
        resource_registry.table("ch", table, ch_cluster="integration_cluster")
        assert (
            sql.load_df(
                "ch_routed",
                table,
                values,
                write_mode="replace",
                ch_engine="MergeTree",
                ch_only_shard=True,
                ch_distributed_table=False,
                ch_shard_on_cluster="integration_cluster",
                order_by="id",
                retry_cnt=1,
                query_label=_label("same-name-binding-seed"),
            )
            == 2
        )
    expected = [
        {"id": 1, "left_value": 10, "right_value": 100},
        {"id": 2, "left_value": 20, "right_value": 200},
    ]
    for alias in ("ch_routed", "ch_routed_native"):
        for projection in (
            f"{left}.id, {left}.left_value, {right}.right_value",
            f"{left}.*, {right}.right_value",
        ):
            result = sql.read(
                alias,
                f"SELECT {projection} FROM {left} GLOBAL INNER JOIN {right} "
                f"ON {left}.id = {right}.id ORDER BY {left}.id",
                retry_cnt=1,
                query_label=_label("same-name-binding-read"),
            )
            assert result.to_dict("records") == expected
