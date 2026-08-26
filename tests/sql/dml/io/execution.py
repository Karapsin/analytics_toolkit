from __future__ import annotations

from tests.sql._support.cross_area import (
    FakeClickHouseClient,
    FakeDbapiConnection,
    execute_read_module,
    execute_sql_module,
    plans_module,
    pytest,
    read_sql_module,
    sql_module,
)


def test_execute_dry_run_public_timing_uses_optional_time_print_kwargs(
    capsys,
) -> None:
    plan = sql_module.execute("ch", "select 1", dry_run=True)

    output = capsys.readouterr().out
    assert isinstance(plan, plans_module.SqlPlan)
    assert plan.operation == "execute_sql"
    assert "[execute_sql] [timing] Finished SQL function in " in output


def test_read_sql_prefixes_query_label(monkeypatch, capsys) -> None:
    connection = FakeDbapiConnection(
        rows=[(1,)],
        description=[("value",)],
    )
    monkeypatch.setattr(read_sql_module, "get_sql_connection", lambda key: connection)

    result = read_sql_module.read_sql(
        "gp",
        "select 1 as value",
        retry_cnt=1,
        timeout_increment=0,
        query_label="unit-test",
    )

    output = capsys.readouterr().out
    assert result["value"].tolist() == [1]
    assert "Executing query:" not in output
    assert "[read_sql] [gp/gp] [read] Finished SQL query in " in output
    assert ("Finished SQL statement:\n/* analytics_toolkit query_label=unit-test */") in output
    assert connection.executed[0].startswith("/* analytics_toolkit query_label=unit-test */")


def test_read_sql_return_metadata_preserves_dataframe(monkeypatch) -> None:
    connection = FakeDbapiConnection(
        rows=[(1,), (2,)],
        description=[("value",)],
    )
    monkeypatch.setattr(read_sql_module, "get_sql_connection", lambda key: connection)

    result = read_sql_module.read_sql(
        "gp",
        "select value from source_table",
        print_queries=False,
        retry_cnt=1,
        timeout_increment=0,
        query_label="metadata-read",
        return_metadata=True,
    )

    assert result.rows == 2
    assert result.data["value"].tolist() == [1, 2]
    assert result.metadata.read_rows == 2
    assert result.metadata.statement_count == 1
    assert result.metadata.retry_attempts == 1
    assert result.metadata.elapsed_seconds >= 0
    assert result.metadata.operation_status == "success"
    assert result.metadata.query_label == "metadata-read"


def test_read_sql_with_metadata_delegates_to_shared_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = object()
    calls: list[dict[str, object]] = []

    def fake_impl(**kwargs: object) -> object:
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(read_sql_module, "_read_sql_impl", fake_impl)
    result = read_sql_module.read_sql_with_metadata(
        "gp",
        "select 1",
        print_queries=True,
        retry_cnt=2,
        timeout_increment=0.5,
        query_label="metadata",
    )
    assert result is expected
    assert calls == [
        {
            "db_key": "gp",
            "query": "select 1",
            "print_queries": True,
            "retry_cnt": 2,
            "timeout_increment": 0.5,
            "query_label": "metadata",
            "return_metadata": True,
            "output_type": "df",
            "to_excel": None,
        }
    ]


def test_execute_sql_dry_run_does_not_open_connection(monkeypatch) -> None:
    monkeypatch.setattr(
        execute_sql_module,
        "get_sql_connection",
        lambda key: pytest.fail("connection should not be opened"),
    )

    plan = execute_sql_module.execute_sql(
        "trino",
        "select 1; select 2",
        dry_run=True,
        query_label="dry-exec",
    )

    assert plan.operation == "execute_sql"
    assert plan.target_alias == "trino"
    assert [statement.phase for statement in plan.statements] == [
        "execute",
        "execute",
    ]
    assert plan.options["print_queries"] is False
    assert "random_sleep_seconds" not in plan.options
    assert plan.metadata.statement_count == 2
    assert sum("query_label=dry-exec" in sql for sql in plan.sqls) == 1


def test_execute_sql_trino_executes_split_statements_in_order(monkeypatch) -> None:
    connection = FakeDbapiConnection()
    monkeypatch.setattr(
        execute_sql_module,
        "get_sql_connection",
        lambda key: connection,
    )

    execute_sql_module.execute_sql(
        "trino",
        "select 1; select 2; select 3",
        print_queries=False,
        retry_cnt=1,
        timeout_increment=0,
    )

    assert connection.executed == ["select 1", "select 2", "select 3"]
    assert connection.close_calls == 1


def test_execute_sql_logs_elapsed_for_each_statement_by_default(
    monkeypatch,
    capsys,
) -> None:
    connection = FakeDbapiConnection()
    monkeypatch.setattr(
        execute_sql_module,
        "get_sql_connection",
        lambda key: connection,
    )

    execute_sql_module.execute_sql(
        "trino",
        "select 1; select 2",
        retry_cnt=1,
        timeout_increment=0,
    )

    output = capsys.readouterr().out
    assert "Executing query:" not in output
    assert output.count("[execute_sql] [trino/trino] [execute] Finished SQL query in ") == 2
    assert "Finished SQL statement:\nselect 1; select 2" in output
    assert "[execute_sql] [trino/trino] [close] Closing connection" in output


def test_execute_sql_progress_false_suppresses_statement_bar(
    monkeypatch,
    capsys,
) -> None:
    progress_bars: list[object] = []

    class FakeTqdm:
        def __init__(self, values, **kwargs) -> None:
            progress_bars.append((list(values), kwargs))
            self.values = values

        def __iter__(self):
            return iter(self.values)

    client = FakeClickHouseClient()
    monkeypatch.setattr(execute_sql_module, "tqdm", FakeTqdm)
    monkeypatch.setattr(
        execute_sql_module,
        "get_sql_connection",
        lambda key: client,
    )

    execute_sql_module.execute_sql(
        "ch",
        "select 1; select 2",
        retry_cnt=1,
        timeout_increment=0,
        progress=False,
    )

    output = capsys.readouterr().out
    assert progress_bars == []
    assert client.commands == ["select 1", "select 2"]
    assert "Finished SQL statement:\nselect 1; select 2" in output


def test_execute_and_read_validation_and_direct_helper_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(execute_sql_module.InvalidSqlInputError):
        execute_sql_module._build_execute_sql_options(
            db_key="gp",
            query="  ",
            print_queries=False,
            gp_break_query=False,
            gp_commit_each_statement=False,
            retry_cnt=1,
            timeout_increment=0,
            query_label=None,
            dry_run=False,
            return_sql=False,
            return_metadata=False,
            progress=False,
        )
    with pytest.raises(read_sql_module.InvalidSqlInputError):
        read_sql_module._build_read_sql_options(
            db_key="gp",
            query=" ",
            print_queries=False,
            retry_cnt=1,
            timeout_increment=0,
            query_label=None,
            return_metadata=False,
            output_type="df",
        )
    with pytest.raises(read_sql_module.InvalidSqlInputError, match="exactly one"):
        read_sql_module._build_read_sql_options(
            db_key="gp",
            query="select 1; select 2",
            print_queries=False,
            retry_cnt=1,
            timeout_increment=0,
            query_label=None,
            return_metadata=False,
            output_type="df",
        )

    commands: list[str] = []
    execute_sql_module._execute_ch_statement(
        type("Client", (), {"command": lambda self, sql: commands.append(sql)})(),
        "select 1",
    )
    execute_sql_module._execute_trino_statement(
        type("Cursor", (), {"execute": lambda self, sql: commands.append(sql)})(),
        "select 2",
    )
    assert commands == ["select 1", "select 2"]

    printed: list[str] = []
    monkeypatch.setattr(execute_sql_module, "time_print", printed.append)
    execute_sql_module._maybe_print_query("select 1; select 2", True, True)
    execute_sql_module._maybe_print_query("select 3", True, False)
    execute_sql_module._maybe_print_query(" ; ", True, True)
    assert printed == [
        "Executing query:\nselect 1",
        "Executing query:\nselect 3",
        "Executing query:\n",
    ]

    printed.clear()
    monkeypatch.setattr(read_sql_module, "time_print", printed.append)
    read_sql_module._maybe_print_query("select 1", True)
    read_sql_module._maybe_print_query(" ; ", True)
    assert printed == ["Executing query:\nselect 1", "Executing query:\n;"]


def test_execute_statement_progress_wraps_multiple_statements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_tqdm(values, **kwargs):
        calls.append((list(values), kwargs))
        return values

    monkeypatch.setattr(execute_sql_module, "tqdm", fake_tqdm)
    assert list(
        execute_sql_module._iterate_statements_with_progress(
            ["select 1", "select 2"],
            "gp",
            progress=True,
        )
    ) == ["select 1", "select 2"]
    assert calls == [(["select 1", "select 2"], {"desc": "gp statements", "unit": "stmt"})]


def test_execute_sql_clickhouse_executes_split_statements_in_order(monkeypatch) -> None:
    client = FakeClickHouseClient()
    monkeypatch.setattr(
        execute_sql_module,
        "get_sql_connection",
        lambda key: client,
    )

    execute_sql_module.execute_sql(
        "ch",
        "CREATE TABLE tmp (id UInt64); INSERT INTO tmp VALUES (1); DROP TABLE tmp",
        print_queries=False,
        retry_cnt=1,
        timeout_increment=0,
    )

    assert client.commands == [
        "CREATE TABLE tmp (id UInt64)",
        "INSERT INTO tmp VALUES (1)",
        "DROP TABLE tmp",
    ]
    assert client.close_calls == 1


def test_execute_sql_rejects_removed_random_sleep_seconds() -> None:
    with pytest.raises(TypeError, match="random_sleep_seconds"):
        execute_sql_module.execute_sql(
            "trino",
            "select 1",
            random_sleep_seconds=None,
        )


def test_execute_sql_return_metadata_reports_attempt_and_statement_count(
    monkeypatch,
) -> None:
    connection = FakeDbapiConnection()
    monkeypatch.setattr(
        execute_sql_module,
        "get_sql_connection",
        lambda key: connection,
    )

    result = execute_sql_module.execute_sql(
        "gp",
        "select 1",
        print_queries=False,
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
    )

    assert result.rows is None
    assert result.metadata.statement_count == 1
    assert result.metadata.retry_attempts == 1
    assert result.metadata.elapsed_seconds >= 0
    assert result.metadata.operation_status == "success"


def test_execute_read_return_metadata_preserves_dataframe(monkeypatch) -> None:
    connection = FakeDbapiConnection(
        rows=[(1, "ok")],
        description=[("id",), ("status",)],
    )
    monkeypatch.setattr(
        execute_read_module,
        "get_sql_connection",
        lambda key: connection,
    )

    result = execute_read_module.execute_read(
        "gp",
        "CREATE TEMP TABLE tmp AS SELECT 1; SELECT id, status FROM tmp",
        print_queries=False,
        gp_break_query=True,
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
    )

    assert result.rows == 1
    assert result.data["status"].tolist() == ["ok"]
    assert result.metadata.read_rows == 1
    assert result.metadata.statement_count == 2
    assert result.metadata.operation_status == "success"


def test_execute_read_rejects_removed_random_sleep_seconds() -> None:
    with pytest.raises(TypeError, match="random_sleep_seconds"):
        execute_read_module.execute_read(
            "trino",
            "select 1",
            random_sleep_seconds=None,
        )
