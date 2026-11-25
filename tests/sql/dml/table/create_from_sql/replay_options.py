"""Public query creation keeps frozen writer options and native retry outcomes."""

from __future__ import annotations

import pytest
from analytics_toolkit.sql.dml.load.load_sql_table import AmbiguousTableLoadError
from analytics_toolkit.sql.dml.table.create_table_from_sql import create_table_from_sql
from sqlglot import exp, parse_one
from trino.exceptions import TrinoConnectionError, TrinoUserError

from tests.sql._support.query_replay_options import gp_replay_target, trino_replay_target


def _create(backend, *, native="BIGINT", retry_cnt=2, chunk_size=None):
    table = "iceberg.sandbox.events" if backend == "trino" else "sandbox.events"
    column = "id" if backend == "trino" else "payload"
    return create_table_from_sql(
        backend,
        table,
        f"SELECT {column} FROM {table}",
        table_schema={column: native},
        insert_data=True,
        drop_target_if_exists=True,
        return_metadata=True,
        retry_cnt=retry_cnt,
        timeout_increment=0,
        trino_insert_chunk_size=chunk_size,
    )


def _assert_one_capture(database):
    assert database.reads == database.probes == 1
    assert database.events.index("input_complete") < database.events.index("drop")
    assert database.events.count("create") == 1
    assert database.opened
    assert all(client.close_calls == 1 for client in database.opened)


def test_query_replay_options_trino_explicit_chunk_size_reaches_real_parameter_batches(
    monkeypatch, write_sql_connections, tmp_path
):
    rows = [(value,) for value in range(1, 6)]
    database = trino_replay_target(monkeypatch, write_sql_connections, tmp_path, rows=rows)

    result = _create("trino", chunk_size=2)

    assert result.rows == result.metadata.input_count == len(rows)
    assert database.rows == rows
    assert [len(params) for _client, _statement, params, _wire in database.insert_attempts] == [
        2,
        2,
        1,
    ]
    assert [wire for _client, _statement, _params, wire in database.insert_attempts] == [
        ["1", "2"],
        ["3", "4"],
        ["5"],
    ]
    assert all(
        "CAST(? AS BIGINT)" in statement
        for _client, statement, _params, _wire in database.insert_attempts
    )
    _assert_one_capture(database)


def test_query_replay_options_gp_inner_retry_rolls_back_and_reconnects_without_recreation(
    monkeypatch, write_sql_connections, tmp_path
):
    database = gp_replay_target(monkeypatch, write_sql_connections, tmp_path)

    result = _create("gp")

    assert result.rows == result.metadata.input_count == 3
    assert database.target.rows == [{"payload": 1}, {"payload": 2}, {"payload": 3}]
    assert len(database.source_evaluations) == 1
    assert len(database.live_insert_sessions) == 2
    first, second = database.live_insert_sessions
    assert first is not second
    assert first.rollback_calls >= 1
    assert database.reused_broken_sessions == []
    kinds = [
        type(parse_one(database._ddl_schema_prefix(statement), read="postgres"))
        for statement in database.live_mutations
    ]
    assert kinds.count(exp.Drop) == kinds.count(exp.Create) == kinds.count(exp.Insert) == 1
    assert database.bound_rows == [(1,), (2,), (3,), (1,), (2,), (3,)]
    assert database.opened
    assert all(client.close_calls == 1 for client in database.opened)


def test_query_replay_options_deterministic_trino_error_is_submitted_once(
    monkeypatch, write_sql_connections, tmp_path
):
    database = trino_replay_target(monkeypatch, write_sql_connections, tmp_path, rows=[(1,), (2,)])
    failure = TrinoUserError(
        {
            "message": "incompatible selected native type",
            "errorName": "TYPE_MISMATCH",
            "errorType": "USER_ERROR",
            "errorCode": 58,
        }
    )
    database.always_fail = failure

    with pytest.raises(AmbiguousTableLoadError, match="Ambiguous stage insert outcome") as caught:
        _create("trino", retry_cnt=3)

    chain = []
    error = caught.value
    while error is not None and error not in chain:
        chain.append(error)
        error = error.__cause__
    assert failure in chain
    assert len(database.insert_attempts) == 1
    assert database.reads == database.probes == 1
    assert database.events.count("create") == 1
    assert all(client.close_calls == 1 for client in database.opened)


def test_query_replay_options_private_driver_diagnostic_never_enters_retry_logs(
    monkeypatch, write_sql_connections, tmp_path, capsys, caplog
):
    private = "private-selected-row-value-286"
    rows = [(private,), ("ordinary",)]
    database = trino_replay_target(
        monkeypatch, write_sql_connections, tmp_path, rows=rows, native="VARCHAR"
    )
    database.failures.append(TrinoConnectionError("driver rejected selected value " + private))

    result = _create("trino", native="VARCHAR")

    output = capsys.readouterr()
    assert private not in output.out + output.err + caplog.text
    assert result.rows == result.metadata.input_count == len(rows)
    assert database.rows == rows
    assert len(database.insert_attempts) == 2
    assert database.insert_attempts[0][2:] == database.insert_attempts[1][2:]
    assert result.metadata.data_write_replay_possible is True
    _assert_one_capture(database)
