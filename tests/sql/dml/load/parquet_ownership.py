"""Owned and uncertain Hive load stages have distinct cleanup obligations."""

from __future__ import annotations

import pandas as pd
import pytest
from analytics_toolkit import sql
from sqlglot import parse_one

from tests.sql._support.parquet_upsert import parquet_upsert_target
from tests.sql._support.trino_upsert_payload import INCOMING, SCHEMA, SEED


def _load():
    return sql.load_df(
        "trino",
        "sandbox.events",
        pd.DataFrame([INCOMING]),
        table_schema=SCHEMA,
        write_mode="upsert",
        key_columns="id",
        upsert_partition_column="month",
        retry_cnt=1,
        timeout_increment=0,
    )


def test_public_parquet_upsert_cleans_both_acknowledged_tables_and_arrow_files(
    monkeypatch, write_sql_connections
):
    database = parquet_upsert_target(monkeypatch, write_sql_connections)

    assert _load() == 1

    assert sorted(database.target.rows, key=lambda row: row["id"]) == [INCOMING, *SEED[1:]]
    assert database.target.schema == SCHEMA
    assert len(database.arrow_writes) == 1
    assert len(database.created_schemas) == 2
    assert set(database.tables) == {database.target_key}
    assert database.files == {}
    assert len(database.cleaned_locations) == 1


def test_public_parquet_lost_create_ack_reports_the_original_uncertain_stage(
    monkeypatch, write_sql_connections
):
    database = parquet_upsert_target(monkeypatch, write_sql_connections)
    failure = ConnectionError("The Hive CREATE was committed but its ACK was lost.")
    database.lose_create_ack = failure

    with pytest.raises(ConnectionError) as raised:
        _load()

    assert raised.value is failure
    assert database.target.rows == SEED
    assert database.live_mutations == []
    assert len(database.created_schemas) == 1
    identity = database.created_schemas[0][0]
    retained = (
        parse_one(
            "SELECT * FROM " + raised.value.analytics_toolkit_sql_stage_candidate, read="trino"
        )
        .args["from_"]
        .this
    )
    assert database.identity(retained) == identity
    assert raised.value.analytics_toolkit_sql_stage_external_location == database.location(identity)
    assert identity in database.tables
    assert database.arrow_writes == []
    assert database.cleaned_locations == []


def test_public_parquet_failed_drop_retains_the_referenced_arrow_files(
    monkeypatch, write_sql_connections
):
    database = parquet_upsert_target(monkeypatch, write_sql_connections)
    database.hive_drop_failure = ConnectionError("The Hive table could not be dropped.")

    assert _load() == 1

    assert sorted(database.target.rows, key=lambda row: row["id"]) == [INCOMING, *SEED[1:]]
    assert len(database.failed_hive_drops) == 1
    (identity,) = database.failed_hive_drops
    assert identity in database.tables
    location = database.location(identity)
    assert set(database.files) == {location}
    assert database.files[location]
    assert database.cleaned_locations == []
