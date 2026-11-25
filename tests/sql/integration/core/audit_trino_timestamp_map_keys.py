"""Engine precision-12 identity probes and Iceberg precision-6 map-key guards.

The configured Iceberg format persists timestamp precision 6. The precision-12
SELECT probe exercises engine semantics only; the paired fast public regression
reproduces the precision-12 load failure without claiming unsupported table DDL.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.core.exact_values import ExactTemporal
from sqlglot import exp, parse_one

from tests.sql.integration._support.backends import integration_table
from tests.sql.integration._support.identity import query_label

if TYPE_CHECKING:
    from typing import Any

    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]


def _label(purpose: str) -> str:
    return query_label(
        os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8]),
        os.environ.get("SQL_INTEGRATION_TEST_ID", "manual"),
        purpose,
    )


def _probe_engine_precision_twelve(alias: str) -> None:
    precise = (
        "CAST(TIMESTAMP '1970-01-01 00:00:01.000000000000 UTC' AS TIMESTAMP(12) WITH TIME ZONE)"
    )
    equivalent = "CAST(TIMESTAMP '1970-01-01 00:00:01 UTC' AS TIMESTAMP(12) WITH TIME ZONE)"
    next_nanosecond = (
        "CAST(TIMESTAMP '1970-01-01 00:00:01.000000001000 UTC' AS TIMESTAMP(12) WITH TIME ZONE)"
    )
    result = sql.read(
        alias,
        f"SELECT {precise} = {equivalent} AS equivalent, "
        f"{precise} <> {next_nanosecond} AS nanoseconds_distinct, "
        f"typeof({precise}) AS probe_type",
        retry_cnt=1,
        query_label=_label("trino-timestamp-map-engine12"),
    )
    assert len(result) == 1
    for name in ("equivalent", "nanoseconds_distinct"):
        assert not pd.isna(result.iloc[0][name])
        assert bool(result.iloc[0][name])
    assert result.iloc[0]["probe_type"] == "timestamp(12) with time zone"


def _map_rows(alias: str, table: str) -> list[tuple[int, dt.datetime, str]]:
    frame = sql.read(
        alias,
        "SELECT source.row_id, map_key, map_value "
        f"FROM {table} AS source CROSS JOIN UNNEST(source.payload) "
        "AS map_entry(map_key, map_value) ORDER BY source.row_id, map_key",
        retry_cnt=1,
        query_label=_label("trino-timestamp-map-read"),
    )
    rows = []
    for row_id, key, value in frame.itertuples(index=False, name=None):
        assert isinstance(key, dt.datetime)
        assert key.utcoffset() == dt.timedelta(0)
        assert isinstance(value, str)
        rows.append((int(row_id), key.astimezone(dt.timezone.utc), value))
    return rows


def _observe_values_writes(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[str]]:
    adapter = get_backend_adapter("trino")
    execute_command, insert_batch = adapter.execute_command, adapter.insert_dataframe_batch
    commands: list[str] = []
    inserts: list[str] = []

    def record_command(connection: Any, statement: str) -> Any:
        commands.append(statement)
        return execute_command(connection, statement)

    def record_insert(connection: Any, table_name: str, batch: Any, **kwargs: Any) -> Any:
        inserts.append(table_name)
        return insert_batch(connection, table_name, batch, **kwargs)

    monkeypatch.setattr(adapter, "execute_command", record_command)
    monkeypatch.setattr(adapter, "insert_dataframe_batch", record_insert)
    return commands, inserts


@pytest.mark.sql_scenario("audit.load.trino_timestamp_map_keys")
def test_trino_timestamp_map_identity_rejects_equivalent_exact_keys_before_clear(
    monkeypatch: pytest.MonkeyPatch,
    resource_registry: ResourceRegistry,
) -> None:
    alias = "trino_target_values"
    _probe_engine_precision_twelve(alias)
    table = resource_registry.table(alias, integration_table("trino", "timestamp_map_keys"))
    sql.execute(
        alias,
        f"CREATE TABLE {table} (row_id BIGINT, payload MAP(TIMESTAMP(6) WITH TIME ZONE, VARCHAR))",
        retry_cnt=1,
        query_label=_label("trino-timestamp-map-create"),
    )
    sql.execute(
        alias,
        f"INSERT INTO {table} (row_id, payload) VALUES "
        "(99, MAP(ARRAY[TIMESTAMP '1970-01-01 00:00:00.000000 UTC'], ARRAY['original']))",
        retry_cnt=1,
        query_label=_label("trino-timestamp-map-seed"),
    )
    seeded = [(99, dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc), "original")]
    assert _map_rows(alias, table) == seeded
    equivalent = {ExactTemporal(1, 0, "UTC"): "first", ExactTemporal(1000, 3, "UTC"): "second"}
    assert len(equivalent) == 2
    first, second = ExactTemporal(1000001, 6, "UTC"), ExactTemporal(1000002, 6, "UTC")
    distinct = {first: "first", second: "second"}
    with monkeypatch.context() as operations:
        commands, inserts = _observe_values_writes(operations)
        with pytest.raises(ValueError, match=r"(?i)(map.*key|authoritative map schema)"):
            sql.load_df(
                alias,
                table,
                pd.DataFrame({"row_id": [100], "payload": pd.Series([equivalent], dtype=object)}),
                write_mode="truncate_insert",
                use_parquet_staging=False,
                retry_cnt=1,
                query_label=_label("trino-timestamp-map-reject"),
            )
        assert commands == []
        assert inserts == []
        assert _map_rows(alias, table) == seeded
        assert (
            sql.load_df(
                alias,
                table,
                pd.DataFrame({"row_id": [1], "payload": pd.Series([distinct], dtype=object)}),
                write_mode="truncate_insert",
                use_parquet_staging=False,
                retry_cnt=1,
                query_label=_label("trino-timestamp-map-distinct"),
            )
            == 1
        )
        assert _map_rows(alias, table) == [
            (1, dt.datetime(1970, 1, 1, 0, 0, 1, 1, tzinfo=dt.timezone.utc), "first"),
            (1, dt.datetime(1970, 1, 1, 0, 0, 1, 2, tzinfo=dt.timezone.utc), "second"),
        ]
        assert (
            sql.read(
                alias,
                f"SELECT cardinality(payload) FROM {table} WHERE row_id = 1",
                output_type="scalar",
                retry_cnt=1,
                query_label=_label("trino-timestamp-map-cardinality"),
            )
            == 2
        )
        assert (
            sql.read(
                alias,
                f"SELECT typeof(payload) FROM {table} WHERE row_id = 1",
                output_type="scalar",
                retry_cnt=1,
                query_label=_label("trino-timestamp-map-type"),
            )
            == "map(timestamp(6) with time zone, varchar)"
        )
    assert commands
    assert inserts
    assert all(
        not isinstance(parse_one(statement, read="trino"), exp.Create) for statement in commands
    )
    assert list(distinct.items()) == [(first, "first"), (second, "second")]
