"""Native timestamp effective keys are unique before creating writable resources."""

from __future__ import annotations

import datetime as dt
from collections import Counter
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID, uuid4

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.timestamp_keys import source_key_validation_frame
from analytics_toolkit.sql.backends.type_resolution import parse_native_type
from sqlglot import exp, parse_one
from zoneinfo import ZoneInfo

from tests.sql._support.ch_integer_timestamps import _encode_http, _encode_native
from tests.sql._support.load_client_validation import (
    ClientConnection,
    ClientStore,
    _table_identity,
)
from tests.sql._support.load_table import load_df_module

_TARGET = ("sandbox", "events")
_CASES = [
    ("DateTime('UTC')", dt.datetime(1970, 1, 1, 0, 0, 1, tzinfo=dt.timezone.utc)),
    ("DateTime64(3, 'UTC')", dt.datetime(1970, 1, 1, 0, 0, 0, 1000, dt.timezone.utc)),
    ("DateTime64(9, 'UTC')", pd.Timestamp("1970-01-01T00:00:00.000000001Z")),
]


class _TimestampKeyStore(ClientStore):
    def __init__(self, transport, schema, *, exists):
        super().__init__(transport, schema, exists=exists)
        self.table_uuids = {_TARGET: uuid4()} if exists else {}
        self.opened = []
        self.duplicate_queries = []
        if exists:
            self.rows[:] = [{"stamp": 101, "id": 999, "payload": 999}]

    def execute(self, statement, params=None):
        tree = parse_one(statement, read=ClickHouseDialect)
        if isinstance(tree, exp.Insert):
            assert params is None
            assert isinstance(tree.this, exp.Schema)
            target = _table_identity(tree.this.this)
            source = _table_identity(tree.expression.args["from_"].this)
            names = [column.name for column in tree.this.expressions]
            assert len(names) == len(tree.expression.expressions)
            assert self.schemas[source] == self.schemas[target]
            for selection, name in zip(tree.expression.expressions, names):
                assert selection.find(exp.Column).name == name
            self.table_rows[target].extend(
                {name: row[name] for name in names} for row in deepcopy(self.table_rows[source])
            )
            self.submissions.append(("INSERT_SELECT", (source, target)))
            return
        before = set(self.schemas)
        super().execute(statement, params)
        for identity in set(self.schemas) - before:
            self.table_uuids[identity] = uuid4()
        if isinstance(tree, exp.Drop):
            for identity in before - set(self.schemas):
                self.table_uuids.pop(identity)


class _TimestampKeyConnection(ClientConnection):
    def __init__(self, store):
        super().__init__(store)
        self.close_calls = 0

    def query(self, statement, **_kwargs):
        if statement == "SELECT serverUUID(), tcpPort()":
            return SimpleNamespace(result_rows=[(UUID(int=100), 9000)])
        if statement.startswith("SELECT "):
            tree = parse_one(statement, read=ClickHouseDialect)
            source = tree.args.get("from_")
            if source is not None and isinstance(source.this, exp.Table):
                identity = _table_identity(source.this)
                if identity == ("system", "tables"):
                    return self._table_metadata(tree)
                if tree.args.get("joins"):
                    join = tree.args["joins"][0]
                    target = _table_identity(join.this)
                    keys = {comparison.left.name for comparison in join.args["on"].find_all(exp.EQ)}
                    assert keys
                    assert len(tree.args["joins"]) == 1
                    overlaps = any(
                        all(left[name] == right[name] for name in keys)
                        for left in self.store.table_rows[identity]
                        for right in self.store.table_rows[target]
                    )
                    return SimpleNamespace(result_rows=[(1,)] if overlaps else [])
                if tree.args.get("group") is not None:
                    keys = [column.name for column in tree.args["group"].expressions]
                    counts = Counter(
                        tuple(row[name] for name in keys) for row in self.store.table_rows[identity]
                    )
                    self.store.duplicate_queries.append(statement)
                    return SimpleNamespace(
                        result_rows=[(1,)] if any(count > 1 for count in counts.values()) else []
                    )
        return super().query(statement)

    def _table_metadata(self, tree):
        filters = {
            condition.left.name: condition.right.this
            for condition in tree.find_all(exp.EQ)
            if isinstance(condition.left, exp.Column) and isinstance(condition.right, exp.Literal)
        }
        identity = filters["database"], filters["name"]
        if identity not in self.store.schemas:
            rows = []
        elif [column.name for column in tree.expressions] == ["uuid"]:
            rows = [(self.store.table_uuids[identity],)]
        else:
            assert [column.name for column in tree.expressions] == ["create_table_query"]
            rows = [(self.store.ddls[identity],)]
        return SimpleNamespace(result_rows=rows)

    def _write(self, table, column_names, rows):
        identity = _table_identity(table)
        assert identity in self.store.schemas
        rows = deepcopy([tuple(row) for row in rows])
        stored = [dict(zip(column_names, row)) for row in rows]
        encoder = _encode_native if self.is_native_transport else _encode_http
        for index, name in enumerate(column_names):
            native = self.store.schemas[identity][name]
            wire = encoder(name, native, [row[index] for row in rows], self.store.settings)
            self.store.wire_columns.append((identity, name, native, wire))
            base = parse_native_type(native).base.name
            if base in {"datetime", "datetime64"}:
                width = 4 if base == "datetime" else 8
                assert len(wire) == len(rows) * width
                for offset, row in enumerate(stored):
                    row[name] = int.from_bytes(
                        wire[offset * width : (offset + 1) * width],
                        "little",
                        signed=base == "datetime64",
                    )
        self.store.bound_rows.extend(rows)
        self.store.table_rows[identity].extend(stored)
        self.store.submissions.append(("INSERT", identity))

    def insert_df(self, *, table, df, column_names):
        self._write(table, column_names, df.itertuples(index=False, name=None))

    def insert(self, *, table, data, column_names, **_kwargs):
        self._write(table, column_names, data)

    def close(self):
        self.close_calls += 1


def _target(monkeypatch, write_sql_connections, transport, schema, *, exists):
    path = write_sql_connections(
        {
            "ch": {
                "type": "ch",
                "driver": transport,
                "host": "clickhouse.example",
                "port": 9000 if transport == "native" else 8123,
                "user": "user",
                "password": "password",
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
    store = _TimestampKeyStore(transport, schema, exists=exists)

    def connect(_key):
        connection = _TimestampKeyConnection(store)
        store.opened.append(connection)
        return connection

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    monkeypatch.setattr(
        "analytics_toolkit.sql.dml.transfer.runtime.retry.get_sql_connection", connect
    )
    return store


def _frame(calendar, *, different_partition=False):
    if different_partition:
        calendar += calendar - dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    return pd.DataFrame(
        {
            "stamp": pd.Series([1, calendar], dtype=object),
            "id": [7, 7],
            "payload": [101, 102],
        }
    )


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("authority", ["new-explicit", "existing-metadata", "existing-explicit"])
@pytest.mark.parametrize("case", _CASES, ids=["seconds", "millisecond-ticks", "nanosecond-ticks"])
def test_ch_timestamp_keys_reject_equivalent_native_partition_keys_before_create(
    monkeypatch, write_sql_connections, transport, authority, case
):
    native, calendar = case
    schema = {"stamp": native, "id": "UInt64", "payload": "UInt64"}
    store = _target(
        monkeypatch, write_sql_connections, transport, schema, exists=authority != "new-explicit"
    )
    before = deepcopy(store.table_rows)
    original = _frame(calendar)
    frame = original.copy(deep=True)
    assert not frame.duplicated(["stamp", "id"]).any()
    explicit = {"table_schema": schema} if authority != "existing-metadata" else {}

    with pytest.raises(ValueError, match="Duplicate key"):
        sql.load_df(
            "ch",
            "sandbox.events",
            frame,
            write_mode="upsert",
            key_columns="id",
            upsert_partition_column="stamp",
            retry_cnt=1,
            timeout_increment=0,
            order_by="tuple()",
            **explicit,
        )

    assert store.table_rows == before
    assert store.create_schemas == store.submissions == store.wire_columns == store.bound_rows == []
    assert store.duplicate_queries == []
    assert set(store.schemas) == ({_TARGET} if authority != "new-explicit" else set())
    assert all(connection.close_calls == 1 for connection in store.opened)
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("transport", ["http", "native"])
def test_ch_timestamp_keys_numpy_boolean_ticks_share_the_native_partition_identity(
    monkeypatch, write_sql_connections, transport
):
    native, calendar = _CASES[-1]
    schema = {"stamp": native, "id": "UInt64", "payload": "UInt64"}
    store = _target(monkeypatch, write_sql_connections, transport, schema, exists=False)
    frame = _frame(calendar)
    frame.loc[0, "stamp"] = np.bool_(True)
    original = frame.copy(deep=True)

    with pytest.raises(ValueError, match="Duplicate key"):
        sql.load_df(
            "ch",
            "sandbox.events",
            frame,
            write_mode="upsert",
            key_columns="id",
            upsert_partition_column="stamp",
            table_schema=schema,
            retry_cnt=1,
            timeout_increment=0,
        )

    assert store.create_schemas == store.submissions == store.bound_rows == []
    assert all(connection.close_calls == 1 for connection in store.opened)
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("case", _CASES, ids=["seconds", "millisecond-ticks", "nanosecond-ticks"])
def test_ch_timestamp_keys_keep_distinct_native_partitions_and_bare_count(
    monkeypatch, write_sql_connections, transport, case
):
    native, calendar = case
    schema = {"stamp": native, "id": "UInt64", "payload": "UInt64"}
    store = _target(monkeypatch, write_sql_connections, transport, schema, exists=False)
    frame = _frame(calendar, different_partition=True)
    original = frame.copy(deep=True)

    result = sql.load_df(
        "ch",
        "sandbox.events",
        frame,
        write_mode="upsert",
        key_columns="id",
        upsert_partition_column="stamp",
        table_schema=schema,
        retry_cnt=1,
        timeout_increment=0,
        order_by="tuple()",
    )

    assert type(result) is int
    assert result == 2
    assert store.rows == [
        {"stamp": 1, "id": 7, "payload": 101},
        {"stamp": 2, "id": 7, "payload": 102},
    ]
    width = 4 if native.startswith("DateTime(") else 8
    stamp_wire = b"".join(value.to_bytes(width, "little", signed=width == 8) for value in (1, 2))
    assert (_TARGET, "stamp", native, stamp_wire) in store.wire_columns
    assert len(store.create_schemas) == 1
    assert [kind for kind, _detail in store.submissions] == ["CREATE", "INSERT"]
    assert set(store.schemas) == {_TARGET}
    assert all(connection.close_calls == 1 for connection in store.opened)
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("case", _CASES, ids=["seconds", "millisecond-ticks", "nanosecond-ticks"])
def test_ch_timestamp_keys_existing_metadata_append_stages_authoritative_timestamp_columns(
    monkeypatch, write_sql_connections, transport, case
):
    native, calendar = case
    schema = {"stamp": native, "id": "UInt64", "payload": "UInt64"}
    store = _target(monkeypatch, write_sql_connections, transport, schema, exists=True)
    old_rows = deepcopy(store.rows)
    frame = _frame(calendar, different_partition=True)
    original = frame.copy(deep=True)

    result = sql.load_df(
        "ch",
        "sandbox.events",
        frame,
        write_mode="append",
        key_columns=["stamp", "id"],
        retry_cnt=1,
        timeout_increment=0,
    )

    assert type(result) is int
    assert result == 2
    assert store.rows == [
        *old_rows,
        {"stamp": 1, "id": 7, "payload": 101},
        {"stamp": 2, "id": 7, "payload": 102},
    ]
    assert store.create_schemas == [schema]
    width = 4 if native.startswith("DateTime(") else 8
    stamp_wire = b"".join(value.to_bytes(width, "little", signed=width == 8) for value in (1, 2))
    assert [
        (name, column_type, wire) for _table, name, column_type, wire in store.wire_columns
    ] == [
        ("stamp", native, stamp_wire),
        ("id", "UInt64", (7).to_bytes(8, "little") * 2),
        ("payload", "UInt64", (101).to_bytes(8, "little") + (102).to_bytes(8, "little")),
    ]
    assert [kind for kind, _detail in store.submissions] == [
        "CREATE",
        "INSERT",
        "INSERT_SELECT",
        "DROP",
    ]
    assert set(store.schemas) == {_TARGET}
    assert all(connection.close_calls == 1 for connection in store.opened)
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("case", [_CASES[0], _CASES[-1]], ids=["seconds", "nanosecond-ticks"])
def test_ch_timestamp_keys_nested_tuple_ticks_reject_before_target_creation(
    monkeypatch, write_sql_connections, transport, case
):
    inner, calendar = case
    native = f"Tuple(moment {inner}, item UInt64)"
    values = [(1, 7), (calendar, 7)]
    width = 4 if inner.startswith("DateTime(") else 8
    expected = (1).to_bytes(width, "little", signed=width == 8) * 2
    expected += (7).to_bytes(8, "little") * 2
    encoder = _encode_native if transport == "native" else _encode_http
    assert encoder("stamp", native, values, {}) == expected
    schema = {"stamp": native, "p": "UInt32", "payload": "UInt64"}
    store = _target(monkeypatch, write_sql_connections, transport, schema, exists=False)
    frame = pd.DataFrame(
        {"stamp": pd.Series(values, dtype=object), "p": [1, 1], "payload": [101, 102]}
    )
    original = frame.copy(deep=True)
    assert not frame.duplicated(["p", "stamp"]).any()

    with pytest.raises(ValueError, match="Duplicate key"):
        sql.load_df(
            "ch",
            "sandbox.events",
            frame,
            write_mode="upsert",
            key_columns="stamp",
            upsert_partition_column="p",
            table_schema=schema,
            retry_cnt=1,
            timeout_increment=0,
            order_by="tuple()",
        )

    assert store.schemas == store.table_rows == {}
    assert store.create_schemas == store.submissions == store.wire_columns == store.bound_rows == []
    assert all(connection.close_calls == 1 for connection in store.opened)
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("transport", ["http", "native"])
def test_ch_timestamp_keys_hashable_array_payloads_reject_equal_native_keys_before_create(
    monkeypatch, write_sql_connections, transport
):
    inner, calendar = _CASES[0]
    native = f"Array({inner})"
    values = [(1,), (calendar,)]
    expected = (1).to_bytes(8, "little") + (2).to_bytes(8, "little")
    expected += (1).to_bytes(4, "little") * 2
    encoder = _encode_native if transport == "native" else _encode_http
    # Explicit Array schemas historically pass tuple payloads to both SDKs.
    # Their actual bytes establish equality after native temporal encoding.
    assert encoder("stamp", native, values, {}) == expected
    schema = {"stamp": native, "p": "UInt32", "payload": "UInt64"}
    store = _target(monkeypatch, write_sql_connections, transport, schema, exists=False)
    frame = pd.DataFrame(
        {"stamp": pd.Series(values, dtype=object), "p": [1, 1], "payload": [101, 102]}
    )
    original = frame.copy(deep=True)
    assert not frame.duplicated(["p", "stamp"]).any()

    with pytest.raises(ValueError, match="Duplicate key"):
        sql.load_df(
            "ch",
            "sandbox.events",
            frame,
            write_mode="upsert",
            key_columns="stamp",
            upsert_partition_column="p",
            table_schema=schema,
            retry_cnt=1,
            timeout_increment=0,
            order_by="tuple()",
        )

    assert store.schemas == store.table_rows == {}
    assert store.create_schemas == store.submissions == store.wire_columns == store.bound_rows == []
    assert all(connection.close_calls == 1 for connection in store.opened)
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("transport", ["http", "native"])
def test_ch_timestamp_keys_distinct_dst_folds_survive_upfront_python_key_validation(
    monkeypatch, write_sql_connections, transport
):
    zone = ZoneInfo("America/New_York")
    values = [
        dt.datetime(2024, 11, 3, 1, 30, tzinfo=zone, fold=0),
        dt.datetime(2024, 11, 3, 1, 30, tzinfo=zone, fold=1),
    ]
    assert values[0] == values[1]
    native = "DateTime64(6, 'UTC')"
    ticks = [
        pd.Timestamp("2024-11-03T05:30:00Z").value // 1000,
        pd.Timestamp("2024-11-03T06:30:00Z").value // 1000,
    ]
    expected = b"".join(value.to_bytes(8, "little", signed=True) for value in ticks)
    encoder = _encode_native if transport == "native" else _encode_http
    assert encoder("stamp", native, values, {}) == expected
    schema = {"stamp": native, "p": "UInt32", "payload": "UInt64"}
    store = _target(monkeypatch, write_sql_connections, transport, schema, exists=False)
    frame = pd.DataFrame(
        {"stamp": pd.Series(values, dtype=object), "p": [1, 1], "payload": [101, 102]}
    )
    original = frame.copy(deep=True)

    result = sql.load_df(
        "ch",
        "sandbox.events",
        frame,
        write_mode="upsert",
        key_columns="stamp",
        upsert_partition_column="p",
        table_schema=schema,
        retry_cnt=1,
        timeout_increment=0,
        order_by="tuple()",
    )

    assert type(result) is int
    assert result == 2
    assert store.rows == [
        {"stamp": ticks[0], "p": 1, "payload": 101},
        {"stamp": ticks[1], "p": 1, "payload": 102},
    ]
    assert (_TARGET, "stamp", native, expected) in store.wire_columns
    assert [kind for kind, _detail in store.submissions] == ["CREATE", "INSERT"]
    assert all(connection.close_calls == 1 for connection in store.opened)
    pd.testing.assert_frame_equal(frame, original)


def test_ch_timestamp_keys_source_view_preserves_unresolved_wall_and_ordinary_tuple_fields():
    zone = ZoneInfo("America/New_York")
    naive = dt.datetime(2024, 11, 3, 1, 30)  # noqa: DTZ001 -- unresolved source wall coordinates.
    first = dt.datetime(2024, 11, 3, 1, 30, tzinfo=zone, fold=0)
    second = first.replace(fold=1)
    frame = pd.DataFrame(
        {
            "stamp": pd.Series(
                [(naive, (first, 7), None), (naive, (second, 7), None)], dtype=object
            ),
            "p": [1, 1],
            "payload": [101, 102],
        }
    )
    original = frame.copy(deep=True)

    validation = source_key_validation_frame(frame, ["p", "stamp"])

    assert validation is not frame
    assert not validation.duplicated(["p", "stamp"]).any()
    assert validation.stamp.iloc[0][0] is naive
    assert validation.stamp.iloc[1][0] is naive
    assert validation.stamp.iloc[0][1] == (
        dt.datetime(2024, 11, 3, 5, 30, tzinfo=dt.timezone.utc),
        7,
    )
    assert validation.stamp.iloc[1][1] == (
        dt.datetime(2024, 11, 3, 6, 30, tzinfo=dt.timezone.utc),
        7,
    )
    assert validation.payload.tolist() == [101, 102]
    assert source_key_validation_frame(frame, ["p"]) is frame
    assert source_key_validation_frame(frame, None) is frame
    pd.testing.assert_frame_equal(frame, original)
