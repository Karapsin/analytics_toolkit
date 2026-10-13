from __future__ import annotations

import importlib

import pandas as pd
import pytest

from tests.sql._support.execute_read import FakeClickHouseClient, FakeDbapiConnection
from tests.sql._support.scripted import ConnectionSequence, ScriptedConnection, SqlResponse

execute_read_module = importlib.import_module("analytics_toolkit.sql.dml.io.execute_read")
sql_module = importlib.import_module("analytics_toolkit.sql")


def test_execute_read_is_exported() -> None:
    assert sql_module.execute_read is execute_read_module.execute_read


def test_execute_read_clickhouse_executes_setup_and_reads_last(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected = pd.DataFrame({"value": pd.array([10], dtype="Int64")})
    client = FakeClickHouseClient(expected)
    monkeypatch.setattr(
        execute_read_module,
        "get_sql_connection",
        lambda connection_key: client,
    )

    result = execute_read_module.execute_read(
        "ch",
        """
        CREATE TEMPORARY TABLE tmp AS SELECT 1 AS value;
        SELECT value FROM tmp
        """,
        print_queries=False,
    )

    pd.testing.assert_frame_equal(result, expected)
    assert client.commands == ["CREATE TEMPORARY TABLE tmp AS SELECT 1 AS value"]
    assert client.read_queries == ["SELECT value FROM tmp"]
    assert client.close_calls == 1
    output = capsys.readouterr().out
    assert output.count("[execute_read] [ch/ch] [setup] Finished SQL query in ") == 1
    assert output.count("[execute_read] [ch/ch] [read] Finished SQL query in ") == 1
    assert "[execute_read] [ch/ch] [execute_read]" not in output


def test_execute_read_gp_executes_setup_statement_set_and_reads_last(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection = ScriptedConnection(
        [
            SqlResponse(
                "CREATE TEMP TABLE tmp AS SELECT 1 AS id, 'ok' AS label; "
                "INSERT INTO tmp SELECT 2, 'still ok'"
            ),
            SqlResponse("SELECT id, label FROM tmp", rows=((1, "ok"),), columns=("id", "label")),
        ],
        dialect="postgres",
    )
    monkeypatch.setattr(
        execute_read_module,
        "get_sql_connection",
        lambda connection_key: connection,
    )

    result = execute_read_module.execute_read(
        "gp",
        """
        CREATE TEMP TABLE tmp AS SELECT 1 AS id, 'ok' AS label;
        INSERT INTO tmp SELECT 2, 'still ok';
        SELECT id, label FROM tmp
        """,
        print_queries=False,
    )

    assert connection.executed == [
        "CREATE TEMP TABLE tmp AS SELECT 1 AS id, 'ok' AS label;\n"
        "INSERT INTO tmp SELECT 2, 'still ok'",
        "SELECT id, label FROM tmp",
    ]
    pd.testing.assert_frame_equal(
        result,
        pd.DataFrame(
            {
                "id": pd.array([1], dtype="Int64"),
                "label": pd.array(["ok"], dtype="string"),
            }
        ),
    )
    assert connection.commit_calls == 1
    assert connection.close_calls == 1
    assert len(connection.cursors) == 1
    assert connection.cursors[0].close_calls == 1
    output = capsys.readouterr().out
    assert output.count("[execute_read] [gp/gp] [setup] Finished SQL query in ") == 1
    assert output.count("[execute_read] [gp/gp] [read] Finished SQL query in ") == 1
    assert "[execute_read] [gp/gp] [execute_read]" not in output
    connection.assert_complete()


def test_execute_read_trino_logs_setup_and_read_phases(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection = FakeDbapiConnection()
    monkeypatch.setattr(
        execute_read_module,
        "get_sql_connection",
        lambda connection_key: connection,
    )

    execute_read_module.execute_read(
        "trino",
        "CREATE TABLE tmp AS SELECT 1; SELECT * FROM tmp",
        retry_cnt=1,
        timeout_increment=0,
    )

    output = capsys.readouterr().out
    assert output.count("[execute_read] [trino/trino] [setup] Finished SQL query in ") == 1
    assert output.count("[execute_read] [trino/trino] [read] Finished SQL query in ") == 1
    assert "[execute_read] [trino/trino] [execute_read]" not in output


def test_execute_read_gp_break_query_executes_setup_statements_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = ScriptedConnection(
        [
            SqlResponse("CREATE TEMP TABLE tmp AS SELECT 1"),
            SqlResponse("INSERT INTO tmp SELECT 2"),
            SqlResponse("SELECT * FROM tmp", rows=((1, "ok"),), columns=("id", "label")),
        ],
        dialect="postgres",
    )
    monkeypatch.setattr(
        execute_read_module,
        "get_sql_connection",
        lambda connection_key: connection,
    )

    execute_read_module.execute_read(
        "gp",
        "CREATE TEMP TABLE tmp AS SELECT 1; INSERT INTO tmp SELECT 2; SELECT * FROM tmp",
        print_queries=False,
        gp_break_query=True,
        gp_commit_each_statement=True,
    )

    assert connection.executed == [
        "CREATE TEMP TABLE tmp AS SELECT 1",
        "INSERT INTO tmp SELECT 2",
        "SELECT * FROM tmp",
    ]
    assert connection.commit_calls == 2
    connection.assert_complete()


def test_execute_read_logs_elapsed_for_setup_and_final_query_by_default(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection = FakeDbapiConnection()
    monkeypatch.setattr(
        execute_read_module,
        "get_sql_connection",
        lambda connection_key: connection,
    )

    execute_read_module.execute_read(
        "gp",
        "CREATE TEMP TABLE tmp AS SELECT 1; INSERT INTO tmp SELECT 2; SELECT * FROM tmp",
        gp_break_query=True,
        retry_cnt=1,
        timeout_increment=0,
    )

    output = capsys.readouterr().out
    assert "Executing query:" not in output
    assert output.count("[execute_read] [gp/gp] [setup] Finished SQL query in ") == 2
    assert output.count("[execute_read] [gp/gp] [read] Finished SQL query in ") == 1
    assert "[execute_read] [gp/gp] [execute_read]" not in output


def test_execute_read_retries_with_fresh_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_connection = FakeClickHouseClient()
    second_connection = FakeClickHouseClient(pd.DataFrame({"value": [2]}))
    connections = ConnectionSequence([first_connection, second_connection])
    attempts: list[FakeClickHouseClient] = []
    print_flags: list[bool] = []

    monkeypatch.setattr(
        execute_read_module,
        "get_sql_connection",
        connections,
    )

    def fake_execute_read_ch(
        client: FakeClickHouseClient,
        statements: list[str],
        *,
        print_queries: bool = True,
        gp_break_query: bool = False,
        gp_commit_each_statement: bool = False,
        progress: bool = True,
    ) -> pd.DataFrame:
        del statements, gp_break_query, gp_commit_each_statement, progress
        attempts.append(client)
        print_flags.append(print_queries)
        if client is first_connection:
            raise RuntimeError("temporary failure")
        return client.result

    ch_adapter = execute_read_module.get_backend_adapter("ch")
    monkeypatch.setattr(ch_adapter, "execute_read_sql", fake_execute_read_ch)

    result = execute_read_module.execute_read(
        "ch",
        "SELECT 1",
        retry_cnt=2,
        timeout_increment=0,
    )

    pd.testing.assert_frame_equal(result, pd.DataFrame({"value": [2]}))
    connections.assert_complete()
    assert connections.calls == ["ch", "ch"]
    assert attempts == [first_connection, second_connection]
    assert print_flags == [False, False]
    assert first_connection.close_calls == 1
    assert second_connection.close_calls == 1


def test_execute_read_rejects_empty_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        execute_read_module,
        "get_sql_connection",
        lambda connection_key: pytest.fail("connection should not be opened"),
    )

    with pytest.raises(execute_read_module.InvalidSqlInputError):
        execute_read_module.execute_read("ch", "   ")


def test_execute_read_rejects_statementless_query_and_labels_statements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    common = {
        "db_key": "gp",
        "print_queries": False,
        "gp_break_query": False,
        "gp_commit_each_statement": False,
        "retry_cnt": 1,
        "timeout_increment": 0,
        "return_metadata": False,
        "progress": False,
    }
    original_split = execute_read_module._split_sql_statements
    monkeypatch.setattr(execute_read_module, "_split_sql_statements", lambda _sql: [])
    with pytest.raises(execute_read_module.InvalidSqlInputError):
        execute_read_module._build_execute_read_options(
            query="select 1",
            query_label=None,
            **common,
        )
    monkeypatch.setattr(execute_read_module, "_split_sql_statements", original_split)

    options = execute_read_module._build_execute_read_options(
        query="select 1; select 2",
        query_label="batch",
        **common,
    )
    assert len(options.statements) == 2
    assert all("query_label=batch" in sql for sql in options.statements)
