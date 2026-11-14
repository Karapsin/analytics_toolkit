"""Native Trino cursor lifecycle and malformed-response guards remain exact."""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit.sql.connection.get_sql_connection import get_sql_connection

from tests.sql._support.exact_trino_protocol import install_protocol, page
from tests.sql._support.exact_trino_values import column, raw_type, timestamp_type


@pytest.fixture(autouse=True)
def _unauthenticated_trino(default_sql_connections: None, write_sql_connections: Any) -> None:
    del default_sql_connections
    write_sql_connections(
        {"trino": {"type": "trino", "host": "trino.example", "user": "reader"}},
    )


def _varchar() -> dict[str, Any]:
    return raw_type("varchar", {"kind": "LONG", "value": 65535})


@pytest.mark.parametrize("failure", ["row_width", "missing_types", "native_date"])
def test_exact_read_interfaces_native_rows_require_valid_complete_type_metadata(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    if failure == "row_width":
        body = page([column("value", "bigint", raw_type("bigint"))], b'[[1,"private-cell"]]')
        message = "row width differs"
    elif failure == "missing_types":
        response = json.loads(page([], b'[["private-cell"]]'))
        response.pop("columns")
        body = json.dumps(response).encode()
        message = "native result type signatures"
    else:
        body = page([column("value", "date", raw_type("date"))], b'[["private-invalid-date"]]')
        message = "incompatible with its declared native type"
    protocol = install_protocol(monkeypatch, [body])
    connection = get_sql_connection("trino")
    cursor = connection.cursor()
    try:
        assert cursor.execute("SELECT value FROM source") is cursor
        with pytest.raises(ValueError, match=message) as error:
            cursor.fetchone()
        assert "private" not in str(error.value)
    finally:
        cursor.close()
        connection.close()

    assert protocol.methods == ["POST"]
    assert len(protocol.closed_sessions) == 1


def test_exact_read_interfaces_sql_null_map_stays_null(monkeypatch: pytest.MonkeyPatch) -> None:
    mapping = raw_type(
        "map", {"kind": "TYPE", "value": _varchar()}, {"kind": "TYPE", "value": raw_type("bigint")}
    )
    protocol = install_protocol(
        monkeypatch, [page([column("payload", "map(varchar, bigint)", mapping)], b"[[null]]")]
    )
    connection = get_sql_connection("trino")
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT payload FROM source")
        assert cursor.fetchone() == [None]
        assert cursor.fetchone() is None
    finally:
        cursor.close()
        connection.close()

    assert protocol.methods == ["POST"]
    assert len(protocol.closed_sessions) == 1


@pytest.mark.parametrize("key_kind", ["array", "decimal_alias"])
def test_exact_read_interfaces_lossy_native_map_key_carriers_never_discard_entries(
    monkeypatch: pytest.MonkeyPatch,
    key_kind: str,
) -> None:
    if key_kind == "array":
        keys = raw_type("array", {"kind": "TYPE", "value": _varchar()})
        native = "map(array(varchar), varchar)"
        wire = b'[[{"[private-key]":"private-value"}]]'
        message = "lossless, hashable Python representation"
    else:
        keys = raw_type("decimal", {"kind": "LONG", "value": 3}, {"kind": "LONG", "value": 0})
        native = "map(decimal(3, 0), varchar)"
        # A corrupt response can spell one native Decimal key twice without
        # repeating a raw JSON key. The decoder must reject the entire row.
        wire = b'[[{"1":"private-first","+1":"private-second"}]]'
        message = "collide after native decoding"
    mapping = raw_type(
        "map", {"kind": "TYPE", "value": keys}, {"kind": "TYPE", "value": _varchar()}
    )
    protocol = install_protocol(monkeypatch, [page([column("payload", native, mapping)], wire)])
    connection = get_sql_connection("trino")
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT payload FROM source")
        with pytest.raises(ValueError, match=message) as error:
            cursor.fetchone()
        assert "private" not in str(error.value)
    finally:
        cursor.close()
        connection.close()

    assert protocol.methods == ["POST"]
    assert len(protocol.closed_sessions) == 1


def test_exact_read_interfaces_native_executemany_resets_result_decoder_and_keeps_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stamp = "1970-01-01 00:00:00.123456789"
    pages = [
        page(
            [column("stamp", "timestamp(9)", timestamp_type())],
            json.dumps([[stamp], [stamp], [stamp]]).encode(),
        )
    ]
    protocol = install_protocol(monkeypatch, pages)
    connection = get_sql_connection("trino")
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT stamp FROM source")
        cursor.arraysize = 2
        assert cursor.arraysize == 2
        expected = pd.Timestamp(stamp)
        assert cursor.fetchmany() == [[expected], [expected]]
        assert cursor.fetchone() == [expected]
        assert cursor.fetchone() is None

        pages[0] = page([column("rows", "bigint", raw_type("bigint"))], b"[[42]]", update_count=42)
        result = cursor.executemany("INSERT INTO sink VALUES (42)", [[]])

        # Supported native SDKs return either None (0.320) or their own cursor.
        assert result is None or result is cursor
        assert cursor.rowcount == 42
        assert cursor.fetchall() == [[42]]
    finally:
        cursor.close()
        connection.close()

    assert protocol.methods == ["POST", "POST"]
    assert protocol.statements == ["SELECT stamp FROM source", "INSERT INTO sink VALUES (42)"]
    assert len(protocol.closed_sessions) == 1
