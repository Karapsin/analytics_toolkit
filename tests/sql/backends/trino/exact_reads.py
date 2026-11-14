"""Real Trino response decoding must precede no lossy temporal conversion."""

from __future__ import annotations

import datetime as dt
import inspect
from decimal import Decimal
from typing import Any
from uuid import UUID

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.connection.get_sql_connection import get_sql_connection
from analytics_toolkit.sql.core.exact_values import ExactClock, ExactTemporal
from analytics_toolkit.sql.dml.io.read_sql import _read_dbapi_columns
from trino.client import RowMapperFactory

from tests.sql._support.exact_trino_values import (
    column as _column,
)
from tests.sql._support.exact_trino_values import (
    raw_response as _raw_response,
)
from tests.sql._support.exact_trino_values import (
    raw_type as _type,
)
from tests.sql._support.exact_trino_values import (
    timestamp_type as _timestamp_type,
)


@pytest.fixture(autouse=True)
def _unauthenticated_trino(default_sql_connections: None, write_sql_connections: Any) -> None:
    del default_sql_connections
    write_sql_connections(
        {"trino": {"type": "trino", "host": "trino.example", "user": "reader"}},
    )


def _timestamp(value: Any) -> pd.Timestamp:
    return value.timestamp() if isinstance(value, ExactTemporal) else pd.Timestamp(value)


def test_exact_reads_public_scalar_keeps_raw_nanosecond_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements = _raw_response(
        monkeypatch,
        [_column("ts", "timestamp(9)", _timestamp_type())],
        [["1970-01-01 00:00:00.123456789"]],
    )

    result = sql.read(
        "trino",
        "SELECT timestamp '1970-01-01 00:00:00.123456789' AS ts",
        output_type="scalar",
        retry_cnt=1,
    )

    assert _timestamp(result) == pd.Timestamp("1970-01-01 00:00:00.123456789")
    assert len(statements) == 1


@pytest.mark.parametrize("output_type", ["df", "list", "dict"])
def test_exact_reads_public_outputs_preserve_nulls_and_paginated_nanoseconds(
    monkeypatch: pytest.MonkeyPatch,
    output_type: Any,
) -> None:
    calls: list[str] = []
    statements = _raw_response(
        monkeypatch,
        [_column("stamp", "timestamp(9)", _timestamp_type())],
        [[None], ["1969-12-31 23:59:59.999999999"]],
        next_rows=[["1970-01-01 00:00:00.123456789"], [None]],
        calls=calls,
    )

    result = sql.read("trino", "SELECT stamp FROM source", output_type=output_type, retry_cnt=1)

    values = result.iloc[:, 0].tolist() if output_type == "df" else result
    if output_type == "dict":
        values = result["stamp"]
    assert pd.isna(values[0])
    assert _timestamp(values[1]) == pd.Timestamp("1969-12-31 23:59:59.999999999")
    assert _timestamp(values[2]) == pd.Timestamp("1970-01-01 00:00:00.123456789")
    assert pd.isna(values[3])
    assert calls == ["POST", "GET"]
    assert len(statements) == 1


@pytest.mark.parametrize("column_reader", [False, True])
@pytest.mark.parametrize("empty", [False, True])
def test_exact_reads_positional_duplicate_columns_and_empty_results(
    monkeypatch: pytest.MonkeyPatch,
    column_reader: bool,
    empty: bool,
) -> None:
    statements = _raw_response(
        monkeypatch,
        [_column("stamp", "timestamp(9)", _timestamp_type())] * 2,
        [] if empty else [["1970-01-01 00:00:00.123456788", "1970-01-01 00:00:00.123456789"]],
    )

    if column_reader:
        connection = get_sql_connection("trino")
        try:
            result = _read_dbapi_columns(
                connection, "SELECT first_stamp AS stamp, second_stamp AS stamp FROM source"
            )
        finally:
            connection.close()
        assert result.column_names == ("stamp", "stamp")
        assert result.row_count == (0 if empty else 1)
        values = [column[0] for column in result.columns] if not empty else []
    else:
        result = sql.read(
            "trino", "SELECT first_stamp AS stamp, second_stamp AS stamp FROM source", retry_cnt=1
        )
        assert list(result.columns) == ["stamp", "stamp"]
        assert result.shape == (0 if empty else 1, 2)
        values = result.iloc[0].tolist() if not empty else []
    if not empty:
        assert [_timestamp(value) for value in values] == [
            pd.Timestamp("1970-01-01 00:00:00.123456788"),
            pd.Timestamp("1970-01-01 00:00:00.123456789"),
        ]
    assert len(statements) == 1


def test_exact_reads_transfer_batches_consume_one_source_query_without_temporal_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    statements = _raw_response(
        monkeypatch,
        [_column("stamp", "timestamp(9)", _timestamp_type())],
        [["1970-01-01 00:00:00.123456788"]],
        next_rows=[[None], ["1970-01-01 00:00:00.123456789"]],
        calls=calls,
    )
    connection = get_sql_connection("trino")
    try:
        batches = list(
            get_backend_adapter("trino").iter_source_batches(
                connection_key="trino",
                connection_ref={"connection": connection},
                query="SELECT stamp FROM source",
                get_batch_size=lambda: 1,
                retry_cnt=1,
                timeout_increment=0,
            ),
        )
    finally:
        connection.close()

    assert [batch.columns for batch in batches] == [["stamp"]] * 3
    values = [batch.rows[0][0] for batch in batches]
    assert _timestamp(values[0]) == pd.Timestamp("1970-01-01 00:00:00.123456788")
    assert values[1] is None
    assert _timestamp(values[2]) == pd.Timestamp("1970-01-01 00:00:00.123456789")
    assert calls == ["POST", "GET"]
    assert statements == ["SELECT stamp FROM source"]


def test_exact_reads_nested_array_row_and_map_keep_original_temporal_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamp = _timestamp_type()
    array = _type("array", {"kind": "TYPE", "value": timestamp})
    row = _type(
        "row",
        {
            "kind": "NAMED_TYPE",
            "value": {
                "fieldName": {"name": "stamp", "delimited": False},
                "typeSignature": timestamp,
            },
        },
        {
            "kind": "NAMED_TYPE",
            "value": {"fieldName": {"name": "values", "delimited": False}, "typeSignature": array},
        },
    )
    mapping = _type(
        "map",
        {"kind": "TYPE", "value": _type("varchar")},
        {"kind": "TYPE", "value": row},
    )
    statements = _raw_response(
        monkeypatch,
        [
            _column(
                "events",
                "map(varchar, row(stamp timestamp(9), values array(timestamp(9))))",
                mapping,
            )
        ],
        [
            [
                {
                    "first": [
                        "1970-01-01 00:00:00.123456789",
                        [None, "1969-12-31 23:59:59.999999999"],
                    ],
                    "missing": None,
                }
            ]
        ],
    )

    result = sql.read(
        "trino", "SELECT nested_events AS events FROM source", output_type="scalar", retry_cnt=1
    )

    assert _timestamp(result["first"][0]) == pd.Timestamp("1970-01-01 00:00:00.123456789")
    assert result["first"][1][0] is None
    assert _timestamp(result["first"][1][1]) == pd.Timestamp("1969-12-31 23:59:59.999999999")
    assert result["missing"] is None
    assert len(statements) == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1969-12-31 23:59:59.999999999 UTC", "1969-12-31 23:59:59.999999999Z"),
        ("1970-01-01 05:30:00.123456789 Asia/Kolkata", "1970-01-01 00:00:00.123456789Z"),
        ("1970-01-01 05:30:00.123456789 +05:30", "1970-01-01 00:00:00.123456789Z"),
    ],
)
def test_exact_reads_zoned_timestamp_keeps_precise_instant(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected: str,
) -> None:
    _raw_response(
        monkeypatch,
        [_column("stamp", "timestamp(9) with time zone", _timestamp_type(aware=True))],
        [[raw]],
    )

    result = sql.read(
        "trino", "SELECT zoned_stamp AS stamp FROM source", output_type="scalar", retry_cnt=1
    )

    assert _timestamp(result) == pd.Timestamp(expected)


@pytest.mark.parametrize("precision", range(13))
@pytest.mark.parametrize("aware", [False, True])
def test_exact_reads_all_supported_timestamp_precisions_keep_raw_negative_ticks(
    monkeypatch: pytest.MonkeyPatch,
    precision: int,
    aware: bool,
) -> None:
    fraction = "123456789123"[:precision]
    raw = "1969-12-31 23:59:59" + ("." + fraction if fraction else "")
    if aware:
        raw += " UTC"
    native = f"timestamp({precision})" + (" with time zone" if aware else "")
    statements = _raw_response(
        monkeypatch,
        [_column("stamp", native, _timestamp_type(precision, aware=aware))],
        [[raw]],
    )

    result = sql.read("trino", "SELECT stamp FROM source", output_type="scalar", retry_cnt=1)

    ticks = -(10**precision) + (int(fraction) if fraction else 0)
    if precision > 9:
        assert isinstance(result, ExactTemporal)
        assert (result.ticks, result.scale, result.timezone) == (
            ticks,
            precision,
            "UTC" if aware else None,
        )
    else:
        expected = pd.Timestamp(ticks * 10 ** (9 - precision), unit="ns")
        if aware:
            expected = expected.tz_localize("UTC")
        assert _timestamp(result) == expected
    assert len(statements) == 1


@pytest.mark.parametrize(
    ("precision", "raw", "ticks", "offset"),
    [
        (9, "12:30:45.123456789", 45045 * 10**9 + 123456789, None),
        (9, "12:30:45.123456789+03:00", 45045 * 10**9 + 123456789, 180),
        (12, "23:59:59.999999999999", 86400 * 10**12 - 1, None),
        (12, "23:59:59.999999999999-03:30", 86400 * 10**12 - 1, -210),
    ],
)
def test_exact_reads_public_date_free_clock_preserves_all_native_ticks(
    monkeypatch: pytest.MonkeyPatch,
    precision: int,
    raw: str,
    ticks: int,
    offset: int | None,
) -> None:
    aware = offset is not None
    family = "time with time zone" if aware else "time"
    native = f"time({precision})" + (" with time zone" if aware else "")
    statements = _raw_response(
        monkeypatch,
        [_column("clock", native, _type(family, {"kind": "LONG", "value": precision}))],
        [[raw]],
    )

    result = sql.read("trino", "SELECT clock FROM source", output_type="scalar", retry_cnt=1)

    assert isinstance(result, ExactClock)
    assert (result.ticks, result.scale, result.offset_minutes) == (ticks, precision, offset)
    assert result.isoformat() == raw
    assert len(statements) == 1


@pytest.mark.parametrize("precision", [9, 12])
@pytest.mark.parametrize("offset", [None, 180])
def test_exact_reads_public_aligned_high_precision_clock_remains_native_time(
    monkeypatch: pytest.MonkeyPatch,
    precision: int,
    offset: int | None,
) -> None:
    aware = offset is not None
    family = "time with time zone" if aware else "time"
    native = f"time({precision})" + (" with time zone" if aware else "")
    raw = "12:30:45." + "123456000000"[:precision] + ("+03:00" if aware else "")
    _raw_response(
        monkeypatch,
        [_column("clock", native, _type(family, {"kind": "LONG", "value": precision}))],
        [[raw]],
    )

    result = sql.read("trino", "SELECT clock FROM source", output_type="scalar", retry_cnt=1)

    assert type(result) is dt.time
    assert (result.hour, result.minute, result.second, result.microsecond) == (12, 30, 45, 123456)
    assert result.utcoffset() == (dt.timedelta(minutes=offset) if aware else None)


def test_exact_reads_ordinary_driver_types_are_still_native_and_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uuid = UUID("12345678-1234-5678-9abc-123456789abc")
    columns = [
        _column("day", "date", _type("date")),
        _column(
            "amount",
            "decimal(38, 9)",
            _type("decimal", {"kind": "LONG", "value": 38}, {"kind": "LONG", "value": 9}),
        ),
        _column("uuid", "uuid", _type("uuid")),
        _column("large", "bigint", _type("bigint")),
        _column("payload", "varbinary", _type("varbinary")),
        _column("flag", "boolean", _type("boolean")),
        _column("micro", "timestamp(6)", _timestamp_type(6)),
        _column("missing", "bigint", _type("bigint")),
    ]
    statements = _raw_response(
        monkeypatch,
        columns,
        [
            [
                "1900-01-01",
                "12345678901234567890.123456789",
                str(uuid),
                2**63 - 1,
                "AP8=",
                True,
                "2024-01-01 12:34:56.123456",
                None,
            ]
        ],
    )
    factory = RowMapperFactory()
    options = (
        {"legacy_primitive_types": False}
        if "legacy_primitive_types" in inspect.signature(factory.create).parameters
        else {"experimental_python_types": True}
    )
    # UUID retains the ordinary installed SDK representation.
    sdk_uuid = factory.create([columns[2]], **options).map([[str(uuid)]])[0][0]

    result = sql.read(
        "trino",
        "SELECT day, amount, uuid, large, payload, flag, micro, missing FROM source",
        output_type="dict",
        retry_cnt=1,
    )

    assert result == {
        "day": [dt.date(1900, 1, 1)],
        "amount": [Decimal("12345678901234567890.123456789")],
        "uuid": [sdk_uuid],
        "large": [2**63 - 1],
        "payload": [b"\x00\xff"],
        "flag": [True],
        "micro": [dt.datetime(2024, 1, 1, 12, 34, 56, 123456)],  # noqa: DTZ001 - native TIMESTAMP is naive.
        "missing": [None],
    }
    assert len(statements) == 1


@pytest.mark.parametrize(("wire", "expected"), [("AP8=", b"\x00\xff"), ("", b""), (None, None)])
def test_exact_reads_public_varbinary_keeps_source_bytes(
    monkeypatch: pytest.MonkeyPatch,
    wire: str | None,
    expected: bytes | None,
) -> None:
    statements = _raw_response(
        monkeypatch,
        [_column("payload", "varbinary", _type("varbinary"))],
        [[wire]],
    )

    result = sql.read("trino", "SELECT payload FROM source", output_type="scalar", retry_cnt=1)

    assert result == expected
    assert len(statements) == 1


def test_exact_reads_public_nested_varbinary_and_map_keys_keep_source_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = _type("varbinary")
    array = _type("array", {"kind": "TYPE", "value": binary})
    mapping = _type(
        "map",
        {"kind": "TYPE", "value": binary},
        {"kind": "TYPE", "value": array},
    )
    row = _type(
        "row",
        {
            "kind": "NAMED_TYPE",
            "value": {
                "fieldName": {"name": "payload", "delimited": False},
                "typeSignature": binary,
            },
        },
        {
            "kind": "NAMED_TYPE",
            "value": {"fieldName": {"name": "items", "delimited": False}, "typeSignature": mapping},
        },
    )
    statements = _raw_response(
        monkeypatch,
        [_column("nested", "row(payload varbinary, items map(varbinary, array(varbinary)))", row)],
        [[["AP8=", {"AGI=": ["AP8=", None, ""], "AP8=": []}]]],
    )

    result = sql.read("trino", "SELECT nested FROM source", output_type="scalar", retry_cnt=1)

    assert result[0] == b"\x00\xff"
    assert result[1] == {b"\x00b": [b"\x00\xff", None, b""], b"\x00\xff": []}
    assert len(statements) == 1


def test_exact_reads_public_map_preserves_nanosecond_distinct_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signature = _type(
        "map",
        {"kind": "TYPE", "value": _timestamp_type()},
        {"kind": "TYPE", "value": _type("varchar")},
    )
    statements = _raw_response(
        monkeypatch,
        [_column("events", "map(timestamp(9), varchar)", signature)],
        [[{"1970-01-01 00:00:00.123456788": "first", "1970-01-01 00:00:00.123456789": "second"}]],
    )

    result = sql.read(
        "trino", "SELECT event_map AS events FROM source", output_type="scalar", retry_cnt=1
    )

    assert len(result) == 2
    assert [(_timestamp(key), value) for key, value in result.items()] == [
        (pd.Timestamp("1970-01-01 00:00:00.123456788"), "first"),
        (pd.Timestamp("1970-01-01 00:00:00.123456789"), "second"),
    ]
    assert len(statements) == 1
