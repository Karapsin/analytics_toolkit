"""Exact decoding retains the native Trino protocol and cursor contracts."""

from __future__ import annotations

import datetime as dt
import inspect
import time
from typing import Any

import pandas as pd
import pytest
import requests
import trino.dbapi
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.trino.exact_reads import connect_exact
from analytics_toolkit.sql.connection.get_sql_connection import get_sql_connection

from tests.sql._support.exact_trino_protocol import install_protocol, page
from tests.sql._support.exact_trino_values import column, raw_type, timestamp_type

_AMBIGUOUS = (
    "Trino timestamp timezone is unresolved, ambiguous or nonexistent; "
    "project UTC in source SQL to identify the exact instant."
)


def _probe_clock(monkeypatch, protocol):
    # Each completed HTTP request consumes one second of the overall probe.
    monkeypatch.setattr(time, "monotonic", lambda: float(len(protocol.methods)))


@pytest.fixture(autouse=True)
def _unauthenticated_trino(default_sql_connections: None, write_sql_connections: Any) -> None:
    del default_sql_connections
    write_sql_connections(
        {"trino": {"type": "trino", "host": "trino.example", "user": "reader"}},
    )


@pytest.mark.parametrize(("running", "delete_status"), [(False, 204), (True, 204), (True, 500)])
def test_exact_protocol_duplicate_raw_map_pairs_fail_before_json_dict_coercion(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    running: bool,
    delete_status: int,
) -> None:
    mapping = raw_type(
        "map",
        {"kind": "TYPE", "value": raw_type("varchar")},
        {"kind": "TYPE", "value": raw_type("varchar")},
    )
    protocol = install_protocol(
        monkeypatch,
        [
            page(
                [column("payload", "map(varchar, varchar)", mapping)],
                b'[[{"private-map-key":"private-first","private-map-key":"private-second"}]]',
                next_page=1 if running else None,
            ),
            page([], b"[]"),
        ],
        delete_status=delete_status,
    )

    with pytest.raises(ValueError, match="duplicate JSON object keys") as error:
        sql.read("trino", "SELECT payload FROM source", retry_cnt=1)

    assert "private" not in str(error.value)
    assert protocol.methods == (["POST", "DELETE"] if running else ["POST"])
    assert protocol.statements == ["SELECT payload FROM source"]
    assert len(protocol.closed_sessions) == 1
    captured = capsys.readouterr()
    for private in (
        "private-map-key",
        "private-first",
        "private-second",
        "private-cancellation-error",
    ):
        assert private not in captured.out + captured.err


@pytest.mark.parametrize("wall", ["2023-11-05 01:30:00", "2023-03-12 02:30:00"])
def test_exact_protocol_named_zone_without_unique_instant_fails_without_private_values(
    monkeypatch: pytest.MonkeyPatch,
    wall: str,
) -> None:
    wire = f'[["{wall}.123456789 America/New_York","private-cell"]]'.encode()
    protocol = install_protocol(
        monkeypatch,
        [
            page(
                [
                    column("stamp", "timestamp(9) with time zone", timestamp_type(aware=True)),
                    column(
                        "payload", "varchar", raw_type("varchar", {"kind": "LONG", "value": 65535})
                    ),
                ],
                wire,
            ),
        ],
    )

    with pytest.raises(ValueError, match="project UTC in source SQL") as error:
        sql.read("trino", "SELECT stamp, payload FROM source", retry_cnt=1)

    assert str(error.value) == _AMBIGUOUS
    assert wall not in str(error.value)
    assert "America/New_York" not in str(error.value)
    assert "private-cell" not in str(error.value)
    assert protocol.methods == ["POST"]
    assert len(protocol.closed_sessions) == 1


@pytest.mark.parametrize("running", [False, True])
def test_exact_protocol_unexpected_encoded_segments_are_rejected_without_download(
    monkeypatch: pytest.MonkeyPatch,
    running: bool,
) -> None:
    protocol = install_protocol(
        monkeypatch,
        [
            page(
                [column("value", "bigint", raw_type("bigint"))],
                b'{"encoding":"json","segments":[{"type":"spooled",'
                b'"uri":"http://private-bucket/private-key"}]}',
                next_page=1 if running else None,
            ),
            page([], b"[]"),
        ],
    )

    with pytest.raises(ValueError, match="direct JSON row pages") as error:
        sql.read("trino", "SELECT value FROM source", retry_cnt=1)

    assert "private" not in str(error.value)
    assert protocol.methods == (["POST", "DELETE"] if running else ["POST"])
    assert len(protocol.closed_sessions) == 1


def test_exact_protocol_explicit_spooling_is_rejected_before_connection_or_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol = install_protocol(monkeypatch, [])

    with pytest.raises(ValueError, match="remove the explicit spooling encoding"):
        connect_exact(trino.dbapi.connect, host="trino.example", user="reader", encoding="json")

    assert protocol.methods == []
    assert protocol.closed_sessions == []


def test_exact_protocol_cursor_fetch_methods_preserve_exact_rows_and_native_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol = install_protocol(
        monkeypatch,
        [
            page(
                [column("stamp", "timestamp(9)", timestamp_type())],
                b'[["1970-01-01 00:00:00.000000001"],["1970-01-01 00:00:00.000000002"],'
                b'["1970-01-01 00:00:00.000000003"],["1970-01-01 00:00:00.000000004"]]',
            ),
        ],
    )
    connection = get_sql_connection("trino")
    try:
        with connection.cursor() as cursor:
            cursor.arraysize = 2
            assert cursor.execute("SELECT stamp FROM source") is cursor
            assert cursor.fetchone() == [pd.Timestamp(1, unit="ns")]
            assert cursor.fetchmany() == [
                [pd.Timestamp(2, unit="ns")],
                [pd.Timestamp(3, unit="ns")],
            ]
            assert list(cursor) == [[pd.Timestamp(4, unit="ns")]]
            assert cursor.fetchone() is None
            assert cursor.fetchmany(3) == []
            assert cursor.fetchall() == []
            assert cursor.rowcount == -1
            assert cursor.description[0][0] == "stamp"
            assert cursor.query_id == "exact_protocol"
            assert cursor.info_uri == "http://trino.example/query/exact_protocol"
    finally:
        connection.close()

    assert protocol.methods == ["POST"]
    assert len(protocol.closed_sessions) == 1


@pytest.mark.parametrize("count", [0, 42])
def test_exact_protocol_update_counts_remain_native_cursor_attributes(
    monkeypatch: pytest.MonkeyPatch,
    count: int,
) -> None:
    protocol = install_protocol(monkeypatch, [page([], b"[]", update_count=count)])
    connection = get_sql_connection("trino")
    try:
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO target VALUES (1)")
            assert cursor.fetchall() == []
            assert cursor.rowcount == count
            assert cursor.update_type == "INSERT"
            assert cursor.stats["state"] == "FINISHED"
    finally:
        connection.close()

    assert protocol.methods == ["POST"]
    assert len(protocol.closed_sessions) == 1


@pytest.mark.parametrize("decode_failure", [False, True])
def test_exact_protocol_cursor_context_cancels_pending_pages_on_exit(
    monkeypatch: pytest.MonkeyPatch,
    decode_failure: bool,
) -> None:
    columns = [column("stamp", "timestamp(9) with time zone", timestamp_type(aware=True))]
    valid = b'[["1970-01-01 00:00:00.123456789 UTC"]]'
    second = b'[["2023-11-05 01:30:00.123456789 America/New_York"]]' if decode_failure else valid
    protocol = install_protocol(
        monkeypatch,
        [
            page(columns, valid, next_page=1),
            page(columns, second, next_page=2),
            page(columns, valid, next_page=3),
            page(columns, valid),
        ],
    )
    connection = get_sql_connection("trino")
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT stamp FROM source")
            assert cursor.fetchone() == [pd.Timestamp("1970-01-01T00:00:00.123456789Z")]
            if decode_failure:
                with pytest.raises(ValueError, match="project UTC in source SQL") as error:
                    cursor.fetchone()
                assert str(error.value) == _AMBIGUOUS
    finally:
        connection.close()

    assert protocol.methods == (
        ["POST", "GET", "GET", "DELETE"] if decode_failure else ["POST", "GET", "DELETE"]
    )
    assert len(protocol.closed_sessions) == 1


@pytest.mark.parametrize(("configured", "budget"), [(None, 5), (17, 5), (2, 2)])
def test_exact_protocol_actual_sdk_probe_consumes_one_select_with_finite_budget(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Any,
    configured: int | None,
    budget: int,
) -> None:
    config: dict[str, Any] = {"type": "trino", "host": "trino.example", "user": "reader"}
    if configured is not None:
        config["request_timeout"] = configured
    write_sql_connections({"trino": config})
    columns = [column("one", "bigint", raw_type("bigint"))]
    protocol = install_protocol(
        monkeypatch,
        [page(columns, b"[[1]]", next_page=1), page(columns, b"[]")],
    )
    _probe_clock(monkeypatch, protocol)

    assert sql.validate_connections(["trino"], connect=False)[0].connected is None
    assert protocol.methods == []
    result = sql.validate_connections(["trino"], connect=True)[0]

    assert result.connected is True
    assert protocol.statements == ["SELECT 1"]
    assert protocol.methods == ["POST", "GET"]
    assert protocol.timeouts == [budget, min(budget, 4)]
    assert len(protocol.closed_sessions) == 1


def test_exact_protocol_factory_is_lazy_connection_local_and_uses_supported_raw_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol = install_protocol(
        monkeypatch,
        [
            page(
                [column("stamp", "timestamp(9)", timestamp_type())],
                b'[["1970-01-01 00:00:00.123456789"]]',
            )
        ],
    )
    connection = get_sql_connection("trino")
    ordinary = trino.dbapi.connect(host="trino.example", user="reader")
    try:
        assert type(connection) is trino.dbapi.Connection
        assert protocol.methods == []
        parameters = inspect.signature(trino.dbapi.Connection).parameters
        if "legacy_primitive_types" in parameters:
            assert connection.legacy_primitive_types is True
        else:
            assert connection.experimental_python_types is False
        with connection.cursor() as exact_cursor, ordinary.cursor() as ordinary_cursor:
            exact_cursor.execute("SELECT stamp FROM source")
            exact = exact_cursor.fetchone()[0]
            ordinary_cursor.execute("SELECT stamp FROM source")
            native = ordinary_cursor.fetchone()[0]
        assert exact == pd.Timestamp("1970-01-01 00:00:00.123456789")
        assert type(native) is dt.datetime
        assert native.microsecond == 123457
    finally:
        connection.close()
        ordinary.close()

    assert protocol.methods == ["POST", "POST"]
    if "encoding" in parameters:
        assert "X-Trino-Query-Data-Encoding" not in protocol.headers[0]
    assert len(protocol.closed_sessions) == 2


def test_exact_protocol_actual_sdk_probe_failure_has_one_attempt_and_sanitized_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol = install_protocol(
        monkeypatch, [], request_error=requests.Timeout("private-password-and-host")
    )
    _probe_clock(monkeypatch, protocol)

    result = sql.validate_connections(["trino"], connect=True)[0]

    assert result.connected is False
    assert result.error is not None
    assert "private" not in result.error
    assert protocol.methods == ["POST"]
    assert protocol.timeouts == [5]
    assert len(protocol.closed_sessions) == 1


@pytest.mark.parametrize(
    ("status", "body", "error_class"),
    [
        (
            400,
            b'{"private-map-key":"private-first","private-map-key":"private-second"}',
            "HttpError",
        ),
        (200, b" \r\n", "TrinoConnectionError"),
    ],
)
def test_exact_protocol_error_and_empty_pages_preserve_sdk_probe_errors_and_close(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: int,
    body: bytes,
    error_class: str,
) -> None:
    protocol = install_protocol(monkeypatch, [body], response_status=status)
    _probe_clock(monkeypatch, protocol)

    result = sql.validate_connections(["trino"], connect=True)[0]

    assert result.connected is False
    assert result.error == f"Connectivity probe failed ({error_class})."
    assert protocol.statements == ["SELECT 1"]
    assert protocol.methods == ["POST"]
    assert protocol.timeouts == [5]
    assert len(protocol.closed_sessions) == 1
    captured = capsys.readouterr()
    assert "private-map-key" not in captured.out + captured.err
    assert "private-first" not in captured.out + captured.err
    assert "private-second" not in captured.out + captured.err


@pytest.mark.parametrize(
    ("native_types", "required"),
    [
        (("BIGINT",), False),
        (("DECIMAL(38, 9)", "VARCHAR", "VARBINARY"), False),
        (("TIME(0)",), False),
        (("TIME(9)",), False),
        (("TIME(12)",), False),
        (("TIME(0) WITH TIME ZONE",), False),
        (("TIME(9) WITH TIME ZONE",), False),
        (("TIME(12) WITH TIME ZONE",), False),
        (("MAP(VARCHAR, BIGINT)",), False),
        (("DATE",), True),
        (("ARRAY(DATE)",), True),
        (("ROW(day DATE)",), True),
        (("TIMESTAMP(12)",), True),
        (("ROW(stamp TIMESTAMP(12))",), True),
        (("TIMESTAMP(9) WITH TIME ZONE",), True),
        (("ARRAY(TIMESTAMP(9) WITH TIME ZONE)",), True),
        (("ROW(stamp TIMESTAMP(12) WITH TIME ZONE)",), True),
        (("MAP(VARCHAR, TIMESTAMP(12) WITH TIME ZONE)",), True),
        (("MAP(ROW(id BIGINT), VARCHAR)",), True),
        (("MAP(ARRAY(BIGINT), VARCHAR)",), True),
        (("BIGINT", "TIMESTAMP(9) WITH TIME ZONE"), True),
    ],
)
def test_exact_protocol_adapter_freezes_only_source_families_needing_value_certification(
    native_types: tuple[str, ...],
    required: bool,
) -> None:
    assert get_backend_adapter("trino").requires_value_certification(native_types) is required
