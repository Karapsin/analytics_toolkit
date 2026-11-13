"""Disposable checks for physical and facade properties during reconfiguration."""

from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit import sql
from sqlglot import exp, parse_one

from tests.sql.integration._support.backends import integration_table
from tests.sql.integration._support.identity import query_label

if TYPE_CHECKING:
    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]


def _label(purpose: str) -> str:
    return query_label(
        os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8]),
        os.environ.get("SQL_INTEGRATION_TEST_ID", "manual"),
        purpose,
    )


def _columns(ddl: str) -> dict[str, exp.ColumnDef]:
    create = parse_one(ddl, read="clickhouse")
    assert isinstance(create, exp.Create)
    schema = create.this
    assert isinstance(schema, exp.Schema)
    return {
        column.name: column for column in schema.expressions if isinstance(column, exp.ColumnDef)
    }


def _has_column_ttl(column: exp.ColumnDef) -> bool:
    return any(
        isinstance(constraint.args["kind"], exp.MergeTreeTTL)
        for constraint in column.args.get("constraints", [])
    )


def _common_constraints(column: exp.ColumnDef) -> list[str]:
    return [
        constraint.sql(dialect="clickhouse")
        for constraint in column.args.get("constraints", [])
        if not isinstance(constraint.args["kind"], exp.MergeTreeTTL)
    ]


@pytest.mark.sql_scenario("audit.clickhouse.column_ttl_reconfigure")
def test_local_to_pair_keeps_column_ttl_only_on_physical_storage(
    resource_registry: ResourceRegistry,
) -> None:
    alias, cluster = "ch", "integration_cluster"
    table = resource_registry.table(
        alias, integration_table("ch", "column_ttl"), ch_cluster=cluster
    )
    shard = resource_registry.table(alias, f"{table}_shard", ch_cluster=cluster)
    sql.execute(
        alias,
        f"CREATE TABLE {table} ("
        "id UInt64 DEFAULT 7 COMMENT 'id TTL stays text', "
        "ts DateTime('UTC') DEFAULT toDateTime('2099-01-01 00:00:00', 'UTC') "
        "TTL ts + INTERVAL 1 DAY COMMENT 'ts comment', "
        "payload String) ENGINE=MergeTree ORDER BY id",
        retry_cnt=1,
        query_label=_label("column-ttl-create"),
    )
    sql.execute(
        alias,
        f"INSERT INTO {table} (id, payload) VALUES (1, 'first'), (2, 'second')",
        retry_cnt=1,
        query_label=_label("column-ttl-seed"),
    )
    original_columns = _columns(sql.extract_ddl(alias, table))
    assert _has_column_ttl(original_columns["ts"])
    result = sql.ch_reconfigure_table(
        alias,
        table,
        ch_distributed_table=True,
        ch_engine="MergeTree",
        ch_shard_on_cluster=cluster,
        ch_distributed_on_cluster=cluster,
        ch_distributed_cluster=cluster,
        retry_cnt=1,
        return_metadata=True,
        query_label=_label("column-ttl-reconfigure"),
    )
    assert isinstance(result, sql.SqlOperationResult)
    assert result.data["strategy"] == "local_to_pair"
    assert result.data["row_count_validated"] is True
    assert result.data["cleanup_complete"] is True
    facade_ddl, shard_ddl = sql.extract_ddl(alias, table), sql.extract_ddl(alias, shard)
    assert "Distributed(" in facade_ddl
    facade_columns, shard_columns = _columns(facade_ddl), _columns(shard_ddl)
    assert _has_column_ttl(shard_columns["ts"])
    assert shard_columns["ts"] == original_columns["ts"]
    assert all(not _has_column_ttl(column) for column in facade_columns.values())
    for columns in (facade_columns, shard_columns):
        assert set(columns) == set(original_columns)
        for name, original in original_columns.items():
            assert columns[name].args["kind"] == original.args["kind"]
            assert _common_constraints(columns[name]) == _common_constraints(original)
    for actual_table in (table, shard):
        actual = sql.read(
            alias,
            f"SELECT id, ts, payload FROM {actual_table} ORDER BY id",
            retry_cnt=1,
            query_label=_label("column-ttl-check"),
        )
        assert list(actual.itertuples(index=False, name=None)) == [
            (1, pd.Timestamp("2099-01-01", tz="UTC"), "first"),
            (2, pd.Timestamp("2099-01-01", tz="UTC"), "second"),
        ]
    inspected_plan = sql.ch_reconfigure_table(
        alias,
        table,
        ch_distributed_table=False,
        ch_shard_on_cluster=cluster,
        ch_distributed_on_cluster=cluster,
        dry_run=True,
        retry_cnt=1,
        query_label=_label("column-ttl-inspect-pair"),
    )
    assert isinstance(inspected_plan, sql.SqlPlan)
    assert inspected_plan.options["strategy"] == "pair_to_local"
