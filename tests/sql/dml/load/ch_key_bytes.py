"""Native key equality is independent of unrelated network or temporal fields."""

from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.insert import normalize_typed_row
from sqlglot import exp

from tests.sql._support.load_client_validation import client_target
from tests.sql.backends.ch._support.network_transport import encode_normalized

_KEYS = [
    ("Enum8('one'=1, 'two'=2)", ["one", 1], ["one", 2], b"\x01\x01"),
    ("String", ["a", b"a"], ["a", b"b"], b"\x01a\x01a"),
    ("FixedString(2)", ["a", "a\x00"], ["a", "b\x00"], b"a\x00a\x00"),
]


@pytest.fixture
def key_target(monkeypatch, write_sql_connections):
    def configure(transport, native, *, with_network):
        path = write_sql_connections(
            {
                "ch": {
                    "type": "ch",
                    "driver": transport,
                    "host": "fixture",
                    "user": "fixture",
                    "password": "",
                    "database": "sandbox",
                    "ddl_defaults": {
                        scope: {
                            "create_distributed_pair": False,
                            "shard": {"engine": "MergeTree", "on_cluster": None},
                        }
                        for scope in ("regular", "staging")
                    },
                }
            }
        )
        set_connections_path(path)
        schema = {"p": "UInt8", "id": native}
        if with_network:
            schema["address"] = "IPv4"
        store = client_target(monkeypatch, transport, {}, exists=False)
        store.fail_generation = 0
        return store, schema

    return configure


def _key_frame(values, *, with_network, different_partitions=False):
    frame = pd.DataFrame(
        {
            "p": [1, 2] if different_partitions else [1, 1],
            "id": pd.Series(deepcopy(values), dtype=object),
        }
    )
    if with_network:
        frame["address"] = ["192.0.2.1", "192.0.2.1"]
    return frame


@pytest.mark.parametrize("transport", ["native", "http"])
@pytest.mark.parametrize("with_network", [False, True])
@pytest.mark.parametrize("key_spec", _KEYS)
def test_new_clickhouse_upsert_rejects_effective_keys_equal_in_actual_writer_bytes(
    key_target, transport, with_network, key_spec
):
    native, values, distinct, wire = key_spec
    del distinct
    terminals = [
        normalize_typed_row(
            ["id"], (value,), {"id": native}, native_transport=transport == "native"
        )[0]
        for value in values
    ]
    assert encode_normalized(native, terminals, transport) == wire
    store, schema = key_target(transport, native, with_network=with_network)
    frame = _key_frame(values, with_network=with_network)
    original = frame.copy(deep=True)
    keys = ["id", "address"] if with_network else ["id"]

    with pytest.raises(ValueError, match="Duplicate key"):
        sql.load_df(
            "ch",
            "sandbox.events",
            frame,
            write_mode="upsert",
            key_columns=keys,
            upsert_partition_column="p",
            table_schema=schema,
            order_by="tuple()",
            retry_cnt=1,
            timeout_increment=0,
        )

    assert store.schemas == {}
    assert store.create_schemas == []
    assert store.submissions == []
    assert store.bound_rows == []
    assert store.wire_columns == []
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("transport", ["native", "http"])
@pytest.mark.parametrize("key_spec", _KEYS)
@pytest.mark.parametrize("different_partitions", [False, True])
def test_clickhouse_native_key_view_preserves_distinct_values_and_partition_scoping(
    key_target, transport, key_spec, different_partitions
):
    native, values, distinct, wire = key_spec
    del wire
    store, schema = key_target(transport, native, with_network=False)
    frame = _key_frame(
        values if different_partitions else distinct,
        with_network=False,
        different_partitions=different_partitions,
    )
    original = frame.copy(deep=True)

    assert (
        sql.load_df(
            "ch",
            "sandbox.events",
            frame,
            write_mode="upsert",
            key_columns="id",
            upsert_partition_column="p",
            table_schema=schema,
            order_by="tuple()",
            retry_cnt=1,
            timeout_increment=0,
        )
        == 2
    )

    assert set(store.schema) == set(schema)
    assert all(
        exp.DataType.build(store.schema[name], dialect=ClickHouseDialect)
        == exp.DataType.build(native_type, dialect=ClickHouseDialect)
        for name, native_type in schema.items()
    )
    assert len(store.rows) == 2
    assert store.create_schemas == [store.schema]
    assert len(store.bound_rows) == 2
    assert [column for column, _native, _wire in store.wire_columns] == ["p", "id"]
    pd.testing.assert_frame_equal(frame, original)
