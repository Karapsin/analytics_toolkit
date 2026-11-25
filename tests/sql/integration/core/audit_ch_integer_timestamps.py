"""Native integer timestamp values through public ClickHouse dataframe loads."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql.integration._support.backends import integration_table
from tests.sql.integration._support.reconfiguration import cleanup_after_acknowledgement, label

if TYPE_CHECKING:
    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]

_SCHEMA = {
    "row_id": "Int64",
    "seconds": "Nullable(DateTime('UTC'))",
    **{f"ticks_{scale}": f"Nullable(DateTime64({scale}, 'UTC'))" for scale in (0, 3, 6, 9)},
}


def _owned_table(registry: ResourceRegistry) -> str:
    table = integration_table("ch", "integer_timestamps")
    acknowledged = False

    def cleanup() -> None:
        if acknowledged:
            sql.execute(
                "ch_target",
                f"DROP TABLE IF EXISTS {table}",
                retry_cnt=1,
                query_label=label("integer-timestamps-cleanup"),
            )

    registry.finalizer(cleanup)
    columns = ", ".join(f"`{name}` {spec}" for name, spec in _SCHEMA.items())
    sql.execute(
        "ch_target",
        f"CREATE TABLE {table} ({columns}) ENGINE=MergeTree ORDER BY row_id",
        retry_cnt=1,
        query_label=label("integer-timestamps-create"),
    )
    acknowledged = True
    return table


def _rows(table: str) -> list[tuple[object, ...]]:
    tick_columns = ", ".join(
        f"toUnixTimestamp64Nano(ticks_{scale}) AS ticks_{scale}_nano" for scale in (0, 3, 6, 9)
    )
    frame = sql.read(
        "ch_target",
        f"SELECT row_id, toUInt32(seconds) AS epoch_seconds, {tick_columns} "
        f"FROM {table} ORDER BY row_id",
        retry_cnt=1,
        query_label=label("integer-timestamps-check"),
    )
    return [
        tuple(None if pd.isna(value) else int(value) for value in row)
        for row in frame.itertuples(index=False, name=None)
    ]


@pytest.mark.sql_scenario("audit.load.clickhouse_integer_timestamps")
def test_integer_timestamps_keep_native_units_for_explicit_and_existing_schemas(
    resource_registry: ResourceRegistry,
) -> None:
    # ch_target follows the integration runner's HTTP/native transport setting.
    table = _owned_table(resource_registry)
    incoming = pd.DataFrame(
        {
            "row_id": [1, 2, 3, 4],
            "seconds": pd.Series([None, 0, 1, 2**32 - 1], dtype=object),
            **{
                f"ticks_{scale}": pd.Series([None, 0, -1, 10**scale + 1], dtype=object)
                for scale in (0, 3, 6, 9)
            },
        }
    )
    expected = [
        (1, None, None, None, None, None),
        (2, 0, 0, 0, 0, 0),
        (3, 1, -(10**9), -(10**6), -(10**3), -1),
        (4, 2**32 - 1, 2 * 10**9, 10**9 + 10**6, 10**9 + 10**3, 10**9 + 1),
    ]
    for explicit in (True, False):
        options = {"table_schema": _SCHEMA} if explicit else {}
        original = incoming.copy(deep=True)
        assert sql.load_df(
            "ch_target",
            table,
            incoming,
            write_mode="truncate_insert",
            ch_distributed_table=False,
            retry_cnt=1,
            query_label=label("integer-timestamps-load"),
            **options,
        ) == len(incoming)
        pd.testing.assert_frame_equal(incoming, original)
        assert _rows(table) == expected

        for invalid_seconds in (-1, 2**32):
            invalid = incoming.copy(deep=True)
            invalid.loc[3, "seconds"] = invalid_seconds
            with pytest.raises(ValueError, match=r"timestamp|DateTime|range|UInt32"):
                sql.load_df(
                    "ch_target",
                    table,
                    invalid,
                    write_mode="truncate_insert",
                    ch_distributed_table=False,
                    retry_cnt=1,
                    query_label=label("integer-timestamps-invalid"),
                    **options,
                )
            assert _rows(table) == expected

        for scale in (0, 3, 6, 9):
            lower = -2_208_988_800 * 10**scale
            upper = min(10_413_792_000 * 10**scale - 1, 2**63 - 1)
            for invalid_ticks in (lower - 1, upper + 1):
                invalid = incoming.copy(deep=True)
                invalid.loc[3, f"ticks_{scale}"] = invalid_ticks
                with pytest.raises(ValueError, match="timestamp range"):
                    sql.load_df(
                        "ch_target",
                        table,
                        invalid,
                        write_mode="truncate_insert",
                        ch_distributed_table=False,
                        retry_cnt=1,
                        query_label=label("integer-timestamps-calendar-invalid"),
                        **options,
                    )
                assert _rows(table) == expected

    native_type = sql.read(
        "ch_target",
        f"SELECT toTypeName(seconds) AS native_type FROM {table} LIMIT 1",
        retry_cnt=1,
        query_label=label("integer-timestamps-canonical-type"),
    )
    assert native_type["native_type"].tolist() == ["Nullable(DateTime('UTC'))"]
    for alias in ("DateTime32('UTC')", "TIMESTAMP('UTC')"):
        alias_schema = {**_SCHEMA, "seconds": f"Nullable({alias})"}
        original = incoming.copy(deep=True)
        assert sql.load_df(
            "ch_target",
            table,
            incoming,
            write_mode="truncate_insert",
            table_schema=alias_schema,
            ch_distributed_table=False,
            retry_cnt=1,
            query_label=label("integer-timestamps-alias-load"),
        ) == len(incoming)
        pd.testing.assert_frame_equal(incoming, original)
        assert _rows(table) == expected
        for invalid_seconds in (-1, 2**32):
            invalid = incoming.copy(deep=True)
            invalid.loc[3, "seconds"] = invalid_seconds
            with pytest.raises(ValueError, match=r"timestamp|DateTime|range|UInt32"):
                sql.load_df(
                    "ch_target",
                    table,
                    invalid,
                    write_mode="truncate_insert",
                    table_schema=alias_schema,
                    ch_distributed_table=False,
                    retry_cnt=1,
                    query_label=label("integer-timestamps-alias-invalid"),
                )
            assert _rows(table) == expected


@pytest.mark.sql_scenario("audit.load.clickhouse_timestamp_native_keys")
def test_timestamp_effective_keys_use_native_ticks_before_upsert_mutation(
    resource_registry: ResourceRegistry,
) -> None:
    table = integration_table("ch", "timestamp_native_keys")
    acknowledge = cleanup_after_acknowledgement(
        resource_registry, f"DROP TABLE IF EXISTS {table} SYNC"
    )
    sql.execute(
        "ch_target",
        f"CREATE TABLE {table} (stamp DateTime64(9, 'UTC'), id UInt64, payload String) "
        "ENGINE=MergeTree PARTITION BY stamp ORDER BY (stamp, id)",
        retry_cnt=1,
        query_label=label("timestamp-native-keys-create"),
    )
    acknowledge()
    sql.execute(
        "ch_target",
        f"INSERT INTO {table} VALUES (fromUnixTimestamp64Nano(9), 99, 'original')",
        retry_cnt=1,
        query_label=label("timestamp-native-keys-seed"),
    )

    def rows():
        frame = sql.read(
            "ch_target",
            f"SELECT toUnixTimestamp64Nano(stamp) AS ticks, id, payload FROM {table} "
            "ORDER BY ticks, id",
            retry_cnt=1,
            query_label=label("timestamp-native-keys-check"),
        )
        return list(frame.itertuples(index=False, name=None))

    schema = {"stamp": "DateTime64(9, 'UTC')", "id": "UInt64", "payload": "String"}
    incoming = pd.DataFrame(
        {
            "stamp": pd.Series([1, pd.Timestamp("1970-01-01T00:00:00.000000001Z")], dtype=object),
            "id": [7, 7],
            "payload": ["first", "second"],
        }
    )
    for explicit in (True, False):
        options = {"table_schema": schema} if explicit else {}
        original = incoming.copy(deep=True)
        with pytest.raises(ValueError, match="Duplicate key"):
            sql.load_df(
                "ch_target",
                table,
                incoming,
                write_mode="upsert",
                key_columns="id",
                upsert_partition_column="stamp",
                ch_distributed_table=False,
                retry_cnt=1,
                query_label=label("timestamp-native-keys-reject"),
                **options,
            )
        pd.testing.assert_frame_equal(incoming, original)
        assert rows() == [(9, 99, "original")]

    distinct = incoming.copy(deep=True)
    distinct.loc[1, "stamp"] = pd.Timestamp("1970-01-01T00:00:00.000000002Z")
    assert (
        sql.load_df(
            "ch_target",
            table,
            distinct,
            write_mode="upsert",
            key_columns="id",
            upsert_partition_column="stamp",
            ch_distributed_table=False,
            retry_cnt=1,
            query_label=label("timestamp-native-keys-distinct"),
        )
        == 2
    )
    assert rows() == [(1, 7, "first"), (2, 7, "second"), (9, 99, "original")]


@pytest.mark.sql_scenario("audit.load.clickhouse_authoritative_tuple_arrays")
def test_authoritative_array_columns_keep_tuple_sequence_values(
    resource_registry: ResourceRegistry,
) -> None:
    table = integration_table("ch", "authoritative_tuple_arrays")
    schema = {"row_id": "UInt64", "payload": "Array(Nullable(DateTime64(3, 'UTC')))"}
    acknowledge = cleanup_after_acknowledgement(
        resource_registry, f"DROP TABLE IF EXISTS {table} SYNC"
    )
    sql.execute(
        "ch_target",
        f"CREATE TABLE {table} (row_id UInt64, payload Array(Nullable(DateTime64(3, 'UTC')))) "
        "ENGINE=MergeTree ORDER BY row_id",
        retry_cnt=1,
        query_label=label("authoritative-tuple-arrays-create"),
    )
    acknowledge()
    incoming = pd.DataFrame(
        {
            "row_id": [1, 2],
            "payload": pd.Series(
                [(-1, None), (pd.Timestamp("1970-01-01T00:00:00.002Z"), 3)], dtype=object
            ),
        }
    )
    for explicit in (True, False):
        options = {"table_schema": schema} if explicit else {}
        original = incoming.copy(deep=True)
        assert (
            sql.load_df(
                "ch_target",
                table,
                incoming,
                write_mode="truncate_insert",
                ch_distributed_table=False,
                retry_cnt=1,
                query_label=label("authoritative-tuple-arrays-load"),
                **options,
            )
            == 2
        )
        pd.testing.assert_frame_equal(incoming, original)
        actual = sql.read(
            "ch_target",
            "SELECT arrayMap(value -> toUnixTimestamp64Milli(value), payload) AS ticks "
            f"FROM {table} ORDER BY row_id",
            retry_cnt=1,
            query_label=label("authoritative-tuple-arrays-check"),
        )
        assert actual["ticks"].tolist() == [[-1, None], [2, 3]]
