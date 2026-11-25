"""Deterministic exact decoding cannot replay public read operations."""

from __future__ import annotations

import pytest
from analytics_toolkit import sql

from tests.sql._support.exact_trino_protocol import install_protocol, page
from tests.sql._support.exact_trino_values import column, raw_type


@pytest.mark.parametrize("execute_setup", [False, True], ids=["read", "execute_read"])
def test_exact_decode_errors_do_not_retry_acknowledged_setup(
    monkeypatch, write_sql_connections, execute_setup
):
    write_sql_connections({"trino": {"type": "trino", "host": "trino.example", "user": "reader"}})
    query = "SELECT payload FROM source"
    setup = "INSERT INTO destination VALUES (1)"
    mapping = raw_type(
        "map",
        {"kind": "TYPE", "value": raw_type("bigint")},
        {"kind": "TYPE", "value": raw_type("varchar")},
    )
    result = page(
        [column("payload", "map(bigint, varchar)", mapping)],
        b'[[{"1":"private-first","01":"private-second"}]]',
    )
    protocol = install_protocol(
        monkeypatch,
        [result],
        statement_pages={setup: [page([], b"[]", update_count=1)], query: [result]},
    )

    read = sql.execute_read if execute_setup else sql.read
    statement = f"{setup}; {query}" if execute_setup else query
    with pytest.raises(ValueError, match="map keys collide") as error:
        read("trino", statement, retry_cnt=3, timeout_increment=0)

    assert "private" not in str(error.value)
    assert protocol.statements == ([setup, query] if execute_setup else [query])
    assert len(protocol.closed_sessions) == 1
