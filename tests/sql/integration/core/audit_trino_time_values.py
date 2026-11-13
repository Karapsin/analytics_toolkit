"""Disposable native Trino TIME loads and incoming-only Parquet staging checks."""

from __future__ import annotations

import datetime as dt
import importlib
import os
import uuid
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
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


def _rows(alias: str, table: str) -> list[tuple[int, dt.time | None]]:
    frame = sql.read(
        alias,
        f"SELECT row_id, payload FROM {table} ORDER BY row_id",
        retry_cnt=1,
        query_label=_label("trino-time-read"),
    )
    rows = []
    for row_id, value in frame.itertuples(index=False, name=None):
        if pd.isna(value):
            clock = None
        else:
            assert isinstance(value, dt.time)
            assert value.tzinfo is None
            clock = value
        rows.append((int(row_id), clock))
    return rows


def _frame(values: list[dt.time | None]) -> pd.DataFrame:
    return pd.DataFrame(
        {"row_id": range(1, len(values) + 1), "payload": pd.Series(values, dtype=object)}
    )


def _observe_writes(
    monkeypatch: pytest.MonkeyPatch,
    resource_registry: ResourceRegistry,
    registered: set[str],
    parquet_alias: str,
) -> tuple[list[str], list[str], list[list[str]], list[str]]:
    adapter = get_backend_adapter("trino")
    load_module = importlib.import_module("analytics_toolkit.sql.dml.load.load_df")
    execute_command, insert_batch = adapter.execute_command, adapter.insert_dataframe_batch
    build_location = load_module.build_stage_external_location
    commands: list[str] = []
    inserts: list[str] = []
    stage_columns: list[list[str]] = []
    locations: list[str] = []

    def record_command(connection: Any, statement: str) -> Any:
        commands.append(statement)
        tree = parse_one(statement, read="trino")
        if isinstance(tree, exp.Create):
            relation = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
            if isinstance(relation, exp.Table):
                name = relation.sql(dialect="trino")
                if name not in registered:
                    resource_registry.table(parquet_alias, name)
                    registered.add(name)
                if relation.catalog == "hive":
                    assert isinstance(tree.this, exp.Schema)
                    stage_columns.append([column.name for column in tree.this.expressions])
        return execute_command(connection, statement)

    def record_insert(connection: Any, table_name: str, batch: Any, **kwargs: Any) -> Any:
        inserts.append(table_name)
        return insert_batch(connection, table_name, batch, **kwargs)

    def register_location(*args: Any, **kwargs: Any) -> str:
        location = build_location(*args, **kwargs)
        parsed = urlsplit(location)
        assert parsed.scheme in {"s3", "s3a"}
        assert parsed.netloc == "warehouse"
        resource_registry.minio(parsed.path)
        locations.append(location)
        return location

    monkeypatch.setattr(adapter, "execute_command", record_command)
    monkeypatch.setattr(adapter, "insert_dataframe_batch", record_insert)
    monkeypatch.setattr(load_module, "build_stage_external_location", register_location)
    return commands, inserts, stage_columns, locations


@pytest.mark.sql_scenario("audit.load.trino_time")
def test_trino_time_values_preserve_precision_and_stage_only_incoming_columns(
    monkeypatch: pytest.MonkeyPatch,
    resource_registry: ResourceRegistry,
) -> None:
    alias, parquet_alias = "trino_target_values", "trino_target_parquet"
    existing = resource_registry.table(alias, integration_table("trino", "time_existing"))
    explicit = resource_registry.table(alias, integration_table("trino", "time_explicit"))
    sql.execute(
        alias,
        f"CREATE TABLE {existing} (row_id BIGINT, payload TIME(6))",
        retry_cnt=1,
        query_label=_label("trino-time-create"),
    )
    sql.execute(
        alias,
        f"INSERT INTO {existing} (row_id, payload) VALUES (99, TIME '03:00:00.000001')",
        retry_cnt=1,
        query_label=_label("trino-time-seed"),
    )
    seeded = [(99, dt.time(3, microsecond=1))]
    assert _rows(alias, existing) == seeded

    values = [
        None,
        dt.time(0, 0, 0, 1),
        None,
        dt.time(12, 30, 45, 123456),
        dt.time(23, 59, 59, 999999),
        None,
    ]
    incoming = _frame(values)
    original = incoming.copy(deep=True)
    with monkeypatch.context() as operations:
        commands, inserts, stage_columns, locations = _observe_writes(
            operations, resource_registry, {existing, explicit}, parquet_alias
        )
        with pytest.raises(ValueError, match=r"TIME|time|Parquet|parquet"):
            sql.load_df(
                parquet_alias,
                existing,
                incoming,
                write_mode="truncate_insert",
                use_parquet_staging=True,
                retry_cnt=1,
                query_label=_label("trino-time-parquet-reject"),
            )
        assert commands == []
        assert inserts == []
        assert locations == []
        assert _rows(alias, existing) == seeded

        for table, mode, schema in (
            (existing, "truncate_insert", None),
            (explicit, "replace", {"row_id": "BIGINT", "payload": "TIME(6)"}),
        ):
            assert sql.load_df(
                alias,
                table,
                incoming,
                write_mode=mode,
                table_schema=schema,
                use_parquet_staging=False,
                retry_cnt=1,
                query_label=_label("trino-time-native-load"),
            ) == len(incoming)
            assert _rows(alias, table) == list(enumerate(values, start=1))

        rounded_input = _frame([dt.time(12, 30, 45, 123789), dt.time(23, 59, 59, 999999)])
        # Iceberg owns TIME(6); explicit input precision requests native rounding
        # without attempting unsupported lower-precision Iceberg table creation.
        assert (
            sql.load_df(
                alias,
                existing,
                rounded_input,
                table_schema={"row_id": "BIGINT", "payload": "TIME(3)"},
                write_mode="truncate_insert",
                use_parquet_staging=False,
                retry_cnt=1,
                query_label=_label("trino-time-round"),
            )
            == 2
        )
        rounded = [(1, dt.time(12, 30, 45, 124000)), (2, dt.time())]
        assert _rows(alias, existing) == rounded
        assert (
            sql.load_df(
                parquet_alias,
                existing,
                pd.DataFrame({"row_id": [7, 8]}),
                write_mode="append",
                use_parquet_staging=True,
                retry_cnt=1,
                query_label=_label("trino-time-parquet-subset"),
            )
            == 2
        )
        assert _rows(alias, existing) == [*rounded, (7, None), (8, None)]
    assert commands
    assert inserts
    assert locations
    assert stage_columns == [["row_id"]]
    assert all(
        not isinstance(parse_one(statement, read="trino"), exp.Alter) for statement in commands
    )
    pd.testing.assert_frame_equal(incoming, original)


def _map_rows(alias: str, table: str) -> list[tuple[int, dt.time, str]]:
    frame = sql.read(
        alias,
        "SELECT source.row_id, map_key, map_value "
        f"FROM {table} AS source CROSS JOIN UNNEST(source.payload) "
        "AS map_entry(map_key, map_value) ORDER BY source.row_id, map_key",
        retry_cnt=1,
        query_label=_label("trino-time-map-read"),
    )
    rows = []
    for row_id, key, value in frame.itertuples(index=False, name=None):
        assert isinstance(key, dt.time)
        assert key.tzinfo is None
        assert isinstance(value, str)
        rows.append((int(row_id), key, value))
    return rows


@pytest.mark.sql_scenario("audit.load.trino_time_map_keys")
def test_trino_time_map_keys_are_certified_before_clearing_existing_rows(
    monkeypatch: pytest.MonkeyPatch,
    resource_registry: ResourceRegistry,
) -> None:
    alias = "trino_target_values"
    table = resource_registry.table(alias, integration_table("trino", "time_map_keys"))
    sql.execute(
        alias,
        f"CREATE TABLE {table} (row_id BIGINT, payload MAP(TIME(6), VARCHAR))",
        retry_cnt=1,
        query_label=_label("trino-time-map-create"),
    )
    sql.execute(
        alias,
        f"INSERT INTO {table} (row_id, payload) VALUES "
        "(99, MAP(ARRAY[TIME '01:00:00.000000'], ARRAY['original']))",
        retry_cnt=1,
        query_label=_label("trino-time-map-seed"),
    )
    seeded = [(99, dt.time(1), "original")]
    assert _map_rows(alias, table) == seeded

    east_one = dt.timezone(dt.timedelta(hours=1))
    colliding = {
        dt.time(12, tzinfo=dt.timezone.utc): "first",
        dt.time(12, tzinfo=east_one): "second",
    }
    assert len(colliding) == 2
    first = dt.time(12, 0, 0, 123400, tzinfo=dt.timezone.utc)
    second = dt.time(13, 0, 0, 123499, tzinfo=east_one)
    distinct = {first: "first", second: "second"}
    assert len(distinct) == 2
    # Native TIME(3) rounding makes these aware Python times equal, while
    # the SQL TIME keys retain distinct wall clocks after their zone cast.
    assert first.replace(microsecond=123000) == second.replace(microsecond=123000)
    incoming = pd.DataFrame({"row_id": [1], "payload": pd.Series([distinct], dtype=object)})
    with monkeypatch.context() as operations:
        commands, inserts, stage_columns, locations = _observe_writes(
            operations, resource_registry, {table}, "trino_target_parquet"
        )
        with pytest.raises(ValueError, match=r"(?i)(map.*key|authoritative map schema)"):
            sql.load_df(
                alias,
                table,
                pd.DataFrame({"row_id": [100], "payload": pd.Series([colliding], dtype=object)}),
                write_mode="truncate_insert",
                use_parquet_staging=False,
                retry_cnt=1,
                query_label=_label("trino-time-map-reject"),
            )
        assert commands == []
        assert inserts == []
        assert _map_rows(alias, table) == seeded

        assert (
            sql.load_df(
                alias,
                table,
                incoming,
                table_schema={"row_id": "BIGINT", "payload": "MAP(TIME(3), VARCHAR)"},
                write_mode="truncate_insert",
                use_parquet_staging=False,
                retry_cnt=1,
                query_label=_label("trino-time-map-distinct"),
            )
            == 1
        )
        assert _map_rows(alias, table) == [
            (1, dt.time(12, 0, 0, 123000), "first"),
            (1, dt.time(13, 0, 0, 123000), "second"),
        ]
        assert (
            sql.read(
                alias,
                f"SELECT cardinality(payload) FROM {table} WHERE row_id = 1",
                output_type="scalar",
                retry_cnt=1,
                query_label=_label("trino-time-map-cardinality"),
            )
            == 2
        )
        assert (
            sql.read(
                alias,
                f"SELECT typeof(payload) FROM {table} WHERE row_id = 1",
                output_type="scalar",
                retry_cnt=1,
                query_label=_label("trino-time-map-type"),
            )
            == "map(time(6), varchar)"
        )
    assert commands
    assert inserts
    assert stage_columns == []
    assert locations == []
    assert list(distinct.items()) == [(first, "first"), (second, "second")]
    assert incoming.iloc[0]["payload"] is distinct
