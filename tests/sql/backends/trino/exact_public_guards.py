"""Raw protocol failures and boundary values enter through the public facade."""

from __future__ import annotations

import time

import pytest
import requests
import trino.dbapi
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.trino import exact_reads
from analytics_toolkit.sql.connection.get_sql_connection import get_sql_connection
from analytics_toolkit.sql.core.exact_values import ExactTemporal

from tests.sql._support.exact_trino_protocol import install_protocol, page
from tests.sql._support.exact_trino_values import column, raw_type, timestamp_type


@pytest.fixture(autouse=True)
def _configuration(write_sql_connections):
    write_sql_connections({"trino": {"type": "trino", "host": "trino.example", "user": "reader"}})


@pytest.mark.parametrize("raw_key", ["p", "[private]"])
def test_public_read_rejects_unverifiable_row_map_key_carriers(monkeypatch, raw_key):
    row = raw_type(
        "row",
        {
            "kind": "NAMED_TYPE",
            "value": {
                "fieldName": {"name": "k", "delimited": False},
                "typeSignature": raw_type("varchar"),
            },
        },
    )
    mapping = raw_type(
        "map",
        {"kind": "TYPE", "value": row},
        {"kind": "TYPE", "value": raw_type("varchar")},
    )
    protocol = install_protocol(
        monkeypatch,
        [
            page(
                [column("payload", "map(row(k varchar), varchar)", mapping)],
                ('[[{"' + raw_key + '":"v"}]]').encode(),
            )
        ],
    )

    with pytest.raises(ValueError, match=r"composite|structural") as error:
        sql.read("trino", "SELECT payload FROM source", retry_cnt=2, timeout_increment=0)

    assert "private" not in str(error.value)
    assert len(protocol.statements) == len(protocol.closed_sessions) == 1


@pytest.mark.parametrize("aware", [False, True], ids=["naive", "utc"])
def test_public_read_preserves_timestamp_at_pandas_nat_tick(monkeypatch, aware):
    native = "timestamp(9)" + (" with time zone" if aware else "")
    text = "1677-09-21 00:12:43.145224192" + (" UTC" if aware else "")
    protocol = install_protocol(
        monkeypatch,
        [page([column("stamp", native, timestamp_type(aware=aware))], f'[["{text}"]]'.encode())],
    )

    value = sql.read("trino", "SELECT stamp FROM source", output_type="scalar", retry_cnt=1)

    assert isinstance(value, ExactTemporal)
    assert value.ticks == -(2**63)
    assert value.scale == 9
    assert value.timezone == ("UTC" if aware else None)
    assert len(protocol.statements) == 1


@pytest.mark.parametrize("year", ["+10000", "0000", "-0001"])
def test_public_read_preserves_expanded_calendar_timestamp(monkeypatch, year):
    text = year + "-01-01 00:00:00"
    protocol = install_protocol(
        monkeypatch,
        [page([column("stamp", "timestamp(0)", timestamp_type(0))], f'[["{text}"]]'.encode())],
    )

    value = sql.read("trino", "SELECT stamp FROM source", output_type="scalar", retry_cnt=1)

    assert isinstance(value, ExactTemporal)
    assert value.isoformat() == year + "-01-01T00:00:00"
    assert len(protocol.statements) == 1


@pytest.mark.parametrize("failure", ["request", "cursor", "assignment"])
@pytest.mark.parametrize("close_fails", [False, True])
def test_public_connection_closes_on_exact_decoder_install_failure(
    monkeypatch, failure, close_fails
):
    class Connection:
        closed = 0

        def _create_request(self):
            return object()

        def cursor(self):
            return object()

        def __setattr__(self, name, value):
            if failure == "assignment" and name == "cursor":
                message = "decoder installation failed"
                raise TypeError(message)
            super().__setattr__(name, value)

        def close(self):
            self.closed += 1
            if close_fails:
                message = "secondary close failure"
                raise OSError(message)

    connection = Connection()
    if failure != "assignment":
        setattr(connection, "_create_request" if failure == "request" else "cursor", None)
    monkeypatch.setattr(trino.dbapi, "connect", lambda **_kwargs: connection)

    with pytest.raises(TypeError, match=r"lossless|installation"):
        get_sql_connection("trino")

    assert connection.closed == 1


def test_public_execute_drains_native_result_in_bounded_batches(monkeypatch):
    protocol = install_protocol(
        monkeypatch,
        [
            page([column("id", "bigint", raw_type("bigint"))], b"[[1],[2]]", next_page=1),
            page([column("id", "bigint", raw_type("bigint"))], b"[[3],[4]]"),
        ],
    )

    def forbidden(_cursor):
        message = "Execution must not materialize its entire discarded result."
        raise AssertionError(message)

    monkeypatch.setattr(exact_reads._ExactCursor, "fetchall", forbidden)

    assert sql.execute("trino", "SELECT id FROM source", retry_cnt=1) is None
    assert protocol.methods == ["POST", "GET"]
    assert len(protocol.closed_sessions) == 1


@pytest.mark.parametrize("budget", [2, 5])
def test_public_connectivity_probe_limits_all_trino_polling(
    monkeypatch, write_sql_connections, budget
):
    write_sql_connections(
        {
            "trino": {
                "type": "trino",
                "host": "trino.example",
                "user": "reader",
                "request_timeout": budget,
            }
        }
    )
    protocol = install_protocol(
        monkeypatch,
        [page([], b"[]", next_page=1), page([], b"[]", next_page=1)],
    )
    now = [0.0]
    request = requests.Session.request

    def polling(session, method, url, **kwargs):
        if len(protocol.methods) > 10:
            message = "Polling exceeded its validation budget."
            raise RuntimeError(message)
        now[0] += min(1.0, kwargs["timeout"]) if method.upper() != "DELETE" else 0
        return request(session, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", polling)
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    result = sql.validate_connections(["trino"], connect=True)[0]

    assert result.connected is False
    assert result.error == "Connectivity probe failed (TimeoutError)."
    assert now[0] == 5
    assert all(0 < timeout <= budget for timeout in protocol.timeouts)
    assert protocol.methods.count("POST") == 1
    assert "DELETE" in protocol.methods
    assert len(protocol.closed_sessions) == 1


def test_public_trino_probe_request_limit_does_not_shorten_the_overall_budget(
    monkeypatch, write_sql_connections
):
    write_sql_connections(
        {
            "trino": {
                "type": "trino",
                "host": "trino.example",
                "user": "reader",
                "request_timeout": 2,
            }
        }
    )
    columns = [column("value", "bigint", raw_type("bigint"))]
    protocol = install_protocol(
        monkeypatch,
        [
            page(columns, b"[]", next_page=1),
            page(columns, b"[]", next_page=2),
            page(columns, b"[[1]]"),
        ],
    )
    now = [0.0]
    request = requests.Session.request

    def polling(session, method, url, **kwargs):
        now[0] += 1
        return request(session, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", polling)
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    result = sql.validate_connections(["trino"], connect=True)[0]

    assert result.connected is True
    assert now[0] == 3
    assert all(0 < timeout <= 2 for timeout in protocol.timeouts)
    assert protocol.methods == ["POST", "GET", "GET"]
    assert len(protocol.closed_sessions) == 1
