"""Disposable backend checks for partition and keyed-transfer literal values."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import analytics_toolkit.general as general_module
import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql.integration._support.backends import backend_enabled, integration_table
from tests.sql.integration._support.identity import query_label

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]


def _label(purpose: str) -> str:
    return query_label(
        os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8]),
        os.environ.get("SQL_INTEGRATION_TEST_ID", "manual"),
        purpose,
    )


@pytest.mark.sql_scenario("audit.upsert.trino_string_partitions")
def test_trino_upsert_treats_marker_shaped_partitions_as_stored_strings(
    resource_registry: ResourceRegistry,
    integration_connections: dict[str, dict[str, object]],
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    alias = "trino_target_values"
    integration_connections[alias] = {
        **integration_connections[alias],
        "upsert_partition_drop_sql_template": (
            "DELETE FROM {table} WHERE {partition_column} IS NOT DISTINCT FROM {partition_value}"
        ),
    }
    general_module.set_connections_path(write_sql_connections(integration_connections))
    table = resource_registry.table(alias, integration_table("trino", "literal_partitions"))
    partitions = ["<part>", "<affected partition value>", r"prefix\n\path'quoted"]
    initial = pd.DataFrame(
        {
            "row_id": [1, 1, 1, 2, 9, 9],
            "partition_name": [*partitions, "<part>", "ordinary", None],
            "payload": ["stale"] * 3
            + ["preserved-key", "preserved-partition", "preserved-null-partition"],
        }
    )
    schema = {"row_id": "BIGINT", "partition_name": "VARCHAR", "payload": "VARCHAR"}
    assert sql.load_df(
        alias,
        table,
        initial,
        write_mode="replace",
        partition_by=["partition_name"],
        table_schema=schema,
        retry_cnt=1,
        query_label=_label("string-partition-seed"),
    ) == len(initial)
    update = pd.DataFrame(
        {
            "row_id": [1] * len(partitions),
            "partition_name": partitions,
            "payload": [f"replacement-{index}" for index in range(len(partitions))],
        }
    )
    assert sql.load_df(
        alias,
        table,
        update,
        write_mode="upsert",
        key_columns="row_id",
        upsert_partition_column="partition_name",
        retry_cnt=1,
        query_label=_label("string-partition-upsert"),
    ) == len(update)
    actual = sql.read(
        alias,
        f"SELECT row_id, partition_name, payload FROM {table}",
        retry_cnt=1,
        query_label=_label("string-partition-check"),
    )
    expected = set(update.itertuples(index=False, name=None))
    expected.update(
        {
            (2, "<part>", "preserved-key"),
            (9, "ordinary", "preserved-partition"),
            (9, None, "preserved-null-partition"),
        }
    )
    assert set(actual.itertuples(index=False, name=None)) == expected
    assert len(actual) == len(initial)


@pytest.mark.sql_scenario("audit.transfer.gp_aware_key")
def test_greenplum_keyed_transfer_selects_the_aware_instant_in_a_utc_session(
    monkeypatch: pytest.MonkeyPatch,
    resource_registry: ResourceRegistry,
) -> None:
    if not backend_enabled("gp"):
        pytest.skip("Greenplum requires x86_64")
    # libpq applies this test-local startup option to every connection and retry.
    # No shared database or role setting is changed.
    monkeypatch.setenv("PGOPTIONS", "-c timezone=UTC")
    source_alias, target_alias = "gp_source", "gp_target"
    assert (
        sql.read(
            source_alias,
            "SELECT current_setting('TimeZone')",
            output_type="scalar",
            retry_cnt=1,
            query_label=_label("aware-key-session"),
        )
        == "UTC"
    )
    source = resource_registry.table(source_alias, integration_table("gp", "aware_key_source"))
    target = resource_registry.table(target_alias, integration_table("gp", "aware_key_target"))
    utc_instant = datetime(2025, 1, 1, tzinfo=timezone.utc)
    frame = pd.DataFrame(
        {
            "row_id": [1, 2],
            "event_ts": [utc_instant, utc_instant + timedelta(hours=3)],
            "payload": ["intended-instant", "wrong-wall-clock"],
        }
    )
    schema = {"row_id": "BIGINT", "event_ts": "TIMESTAMPTZ", "payload": "TEXT"}
    assert (
        sql.load_df(
            source_alias,
            source,
            frame,
            write_mode="replace",
            table_schema=schema,
            gp_distributed_by_key="row_id",
            retry_cnt=1,
            query_label=_label("aware-key-seed"),
        )
        == 2
    )
    aware_key = datetime(2025, 1, 1, 3, tzinfo=timezone(timedelta(hours=3)))
    assert (
        sql.transfer(
            source_alias,
            target_alias,
            from_table=source,
            to_table=target,
            write_mode="replace",
            transfer_keys="event_ts",
            transfer_key_values=[aware_key],
            ignore_source_staging=True,
            table_schema=schema,
            gp_distributed_by_key="row_id",
            concurrency=1,
            retry_cnt=1,
            full_retry_cnt=1,
            query_label=_label("aware-key-transfer"),
        )
        == 1
    )
    actual = sql.read(
        target_alias,
        f"SELECT row_id, event_ts, payload FROM {target}",
        retry_cnt=1,
        query_label=_label("aware-key-check"),
    )
    assert list(actual[["row_id", "payload"]].itertuples(index=False, name=None)) == [
        (1, "intended-instant")
    ]
    assert actual.iloc[0]["event_ts"] == utc_instant == aware_key
