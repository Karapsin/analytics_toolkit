from __future__ import annotations

from tests.sql._support.retries import (
    AmbiguousColumn,
    CloseFailureConnection,
    DatabaseError,
    FakeConnection,
    FakeUndefinedObjectError,
    FakeUndefinedTableError,
    RollbackFailureConnection,
    execute_read_module,
    execute_sql_module,
    load_df_module,
    operation_runner_module,
    pd,
    pytest,
    read_sql_module,
    retry_module,
)


def test_close_connection_refs_preserves_first_error_and_attempts_every_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = RuntimeError("worker failed")
    closed: list[str] = []

    def close(_ref: object, _connection_type: str, role: str) -> None:
        closed.append(role)
        if role == "source worker":
            raise KeyboardInterrupt

    monkeypatch.setattr(retry_module, "close_connection_ref", close)

    retry_module.close_connection_refs_preserving(
        original,
        ({"connection": object()}, "source", "source worker"),
        ({"connection": object()}, "target", "target worker"),
    )

    assert closed == ["source worker", "target worker"]
    assert original.analytics_toolkit_sql_retry_safe is False


def test_execute_read_does_not_retry_ambiguous_column(monkeypatch) -> None:
    first_connection = FakeConnection("first")
    second_connection = FakeConnection("second")
    connections = [first_connection, second_connection]
    attempts: list[str] = []

    monkeypatch.setattr(
        execute_read_module,
        "get_sql_connection",
        lambda connection_type: connections.pop(0),
    )

    def fake_execute_read_gp(
        conn: FakeConnection,
        statements: list[str],
        *,
        print_queries: bool = False,
        gp_break_query: bool = False,
        gp_commit_each_statement: bool = False,
        progress: bool = True,
    ) -> pd.DataFrame:
        del statements, print_queries, gp_break_query, gp_commit_each_statement, progress
        attempts.append(conn.name)
        raise AmbiguousColumn(
            'column reference "is_qr_plus" is ambiguous\nLINE 55:     is_qr_plus,'
        )

    gp_adapter = execute_read_module.get_backend_adapter("gp")
    monkeypatch.setattr(gp_adapter, "execute_read_sql", fake_execute_read_gp)

    try:
        execute_read_module.execute_read(
            "gp",
            "select is_qr_plus from schema.table",
            retry_cnt=3,
            timeout_increment=0,
        )
    except AmbiguousColumn:
        pass
    else:
        raise AssertionError("Expected ambiguous-column error to be raised.")

    assert attempts == ["first"]
    assert len(connections) == 1
    assert first_connection.rollback_calls == 1
    assert first_connection.close_calls == 1
    assert second_connection.close_calls == 0


def test_execute_sql_gp_failure_preserves_original_exception_and_rolls_back(
    monkeypatch,
) -> None:
    original_error = RuntimeError("database failure")

    class FailingCursor:
        def __init__(self, connection: FailingGpConnection) -> None:
            self.connection = connection

        def __enter__(self) -> FailingCursor:
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            return None

        def execute(self, query: str) -> None:
            self.connection.executed.append(query)
            raise original_error

    class FailingGpConnection(FakeConnection):
        def __init__(self) -> None:
            super().__init__("gp")
            self.executed: list[str] = []
            self.commit_calls = 0

        def cursor(self) -> FailingCursor:
            return FailingCursor(self)

        def commit(self) -> None:
            self.commit_calls += 1

    connection = FailingGpConnection()
    monkeypatch.setattr(
        execute_sql_module,
        "get_sql_connection",
        lambda connection_type: connection,
    )

    try:
        execute_sql_module.execute_sql(
            "gp",
            "select 1",
            retry_cnt=1,
            timeout_increment=0,
        )
    except RuntimeError as exc:
        assert exc is original_error
    else:
        raise AssertionError("Expected original database exception.")

    assert connection.executed == ["select 1"]
    assert connection.rollback_calls == 1
    assert connection.close_calls == 1
    assert connection.commit_calls == 0


def test_execute_sql_retries_whole_flow_with_fresh_connection(monkeypatch) -> None:
    first_connection = FakeConnection("first")
    second_connection = FakeConnection("second")
    connections = [first_connection, second_connection]
    attempts: list[str] = []
    print_flags: list[bool] = []

    monkeypatch.setattr(
        execute_sql_module,
        "get_sql_connection",
        lambda connection_type: connections.pop(0),
    )

    def fake_execute_trino(
        conn: FakeConnection,
        query: str,
        *,
        print_queries: bool = True,
        gp_break_query: bool = False,
        gp_commit_each_statement: bool = False,
        progress: bool = True,
    ) -> None:
        del query, gp_break_query, gp_commit_each_statement, progress
        attempts.append(conn.name)
        print_flags.append(print_queries)
        if conn.name == "first":
            raise RuntimeError("temporary failure")

    trino_adapter = execute_sql_module.get_backend_adapter("trino")
    monkeypatch.setattr(trino_adapter, "execute_sql", fake_execute_trino)

    execute_sql_module.execute_sql(
        "trino",
        "select 1; select 2",
        retry_cnt=2,
        timeout_increment=0,
    )

    assert attempts == ["first", "second"]
    assert print_flags == [False, False]
    assert first_connection.close_calls == 1
    assert second_connection.close_calls == 1


def test_load_df_retries_whole_flow_from_start(monkeypatch) -> None:
    connections: list[FakeConnection] = []
    events: list[tuple[str, str]] = []
    call_count = {"insert": 0}
    df = pd.DataFrame({"id": [1], "value": ["x"]})

    def fake_get_sql_connection(_connection_type: str) -> FakeConnection:
        connection = FakeConnection(f"conn-{len(connections)}")
        connections.append(connection)
        return connection

    monkeypatch.setattr(
        load_df_module,
        "get_sql_connection",
        fake_get_sql_connection,
    )
    monkeypatch.setattr(load_df_module, "table_exists", lambda *args, **kwargs: False)

    def fake_create_sql_table(
        connection_type: str,
        connection: FakeConnection,
        table_name: str,
        df: pd.DataFrame,
        *,
        connection_key: str | None = None,
        gp_distributed_by_key: list[str] | None = None,
    ) -> None:
        del connection_type, table_name, df, connection_key, gp_distributed_by_key
        events.append(("create", connection.name))

    def fake_insert_table_batch(*args, **kwargs) -> int:
        connection_ref = args[1]
        connection = connection_ref["connection"]
        events.append(("insert", connection.name))
        call_count["insert"] += 1
        if call_count["insert"] == 1:
            message = "temporary failure"
            raise RuntimeError(message)
        return len(df)

    def fake_analyze_table(
        connection_type: str, connection: FakeConnection, table_name: str
    ) -> None:
        events.append(("analyze", connection.name))

    def fake_drop_table_with_retry(
        _connection_backend: str,
        _connection_key: str,
        connection_ref: dict[str, FakeConnection],
        _table_name: str,
        **_kwargs: object,
    ) -> None:
        events.append(("drop", connection_ref["connection"].name))

    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        fake_create_sql_table,
    )
    monkeypatch.setattr(load_df_module, "insert_table_batch", fake_insert_table_batch)
    monkeypatch.setattr(load_df_module, "analyze_table", fake_analyze_table)
    monkeypatch.setattr(
        load_df_module,
        "drop_table_with_retry",
        fake_drop_table_with_retry,
    )

    inserted_rows = load_df_module.load_df(
        "gp",
        "schema.target_table",
        df,
        retry_cnt=2,
        timeout_increment=0,
    )

    assert inserted_rows == 1
    assert events == [
        ("create", "conn-1"),
        ("insert", "conn-2"),
        ("drop", "conn-3"),
        ("create", "conn-5"),
        ("insert", "conn-6"),
        ("analyze", "conn-7"),
    ]
    assert [connection.close_calls for connection in connections] == [1] * 8
    assert [connection.rollback_calls for connection in connections] == [0] * 8


def test_operation_runner_does_not_rollback_non_gp_backends() -> None:
    for backend in ("trino", "ch"):
        connection = FakeConnection(backend)

        try:
            operation_runner_module.run_connection_operation(
                operation_name=f"test {backend} operation",
                connection_key=backend,
                backend=backend,
                retry_cnt=1,
                timeout_increment=0,
                open_connection=lambda connection_key, conn=connection: conn,
                operation=lambda connection_ref, attempt: (_ for _ in ()).throw(
                    RuntimeError("failure")
                ),
                context_factory=lambda attempt, name=backend: (
                    operation_runner_module.SqlOperationContext(
                        operation="test",
                        alias=name,
                        backend=name,
                        retry_attempt=attempt,
                    )
                ),
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("Expected operation failure.")

        assert connection.rollback_calls == 0
        assert connection.close_calls == 1


def test_operation_runner_retries_with_fresh_connections_and_gp_rollback() -> None:
    first_connection = FakeConnection("first")
    second_connection = FakeConnection("second")
    connections = [first_connection, second_connection]
    attempts: list[str] = []

    def operation(connection_ref: dict[str, FakeConnection], attempt: int) -> str:
        attempts.append(connection_ref["connection"].name)
        if attempt == 1:
            raise RuntimeError("temporary failure")
        return "ok"

    result = operation_runner_module.run_connection_operation(
        operation_name="test gp operation",
        connection_key="gp",
        backend="gp",
        retry_cnt=2,
        timeout_increment=0,
        open_connection=lambda connection_key: connections.pop(0),
        operation=operation,
        context_factory=lambda attempt: operation_runner_module.SqlOperationContext(
            operation="test",
            alias="gp",
            backend="gp",
            retry_attempt=attempt,
        ),
    )

    assert result == "ok"
    assert attempts == ["first", "second"]
    assert first_connection.rollback_calls == 1
    assert second_connection.rollback_calls == 0
    assert first_connection.close_calls == 1
    assert second_connection.close_calls == 1


def test_read_sql_does_not_retry_undefined_object(monkeypatch) -> None:
    first_connection = FakeConnection("first")
    second_connection = FakeConnection("second")
    connections = [first_connection, second_connection]
    attempts: list[str] = []

    monkeypatch.setattr(
        read_sql_module,
        "get_sql_connection",
        lambda connection_type: connections.pop(0),
    )

    def fake_read_gp(
        conn: FakeConnection,
        query: str,
        *,
        print_queries: bool = True,
        print_query: object | None = None,
        read_dbapi_query: object | None = None,
    ) -> pd.DataFrame:
        del query, print_queries, print_query, read_dbapi_query
        attempts.append(conn.name)
        raise FakeUndefinedObjectError(
            'type "string" does not exist\nLINE 24: cast(start_dt as string)'
        )

    gp_adapter = read_sql_module.get_backend_adapter("gp")
    monkeypatch.setattr(gp_adapter, "read_dataframe", fake_read_gp)

    try:
        read_sql_module.read_sql(
            "gp",
            "select cast(start_dt as string) as start_dt from source_table",
            retry_cnt=3,
            timeout_increment=0,
        )
    except FakeUndefinedObjectError:
        pass
    else:
        raise AssertionError("Expected undefined-object error to be raised.")

    assert attempts == ["first"]
    assert len(connections) == 1
    assert first_connection.rollback_calls == 1
    assert first_connection.close_calls == 1
    assert second_connection.close_calls == 0


def test_read_sql_does_not_retry_undefined_table(monkeypatch) -> None:
    first_connection = FakeConnection("first")
    second_connection = FakeConnection("second")
    connections = [first_connection, second_connection]
    attempts: list[str] = []

    monkeypatch.setattr(
        read_sql_module,
        "get_sql_connection",
        lambda connection_type: connections.pop(0),
    )

    def fake_read_gp(
        conn: FakeConnection,
        query: str,
        *,
        print_queries: bool = True,
        print_query: object | None = None,
        read_dbapi_query: object | None = None,
    ) -> pd.DataFrame:
        del query, print_queries, print_query, read_dbapi_query
        attempts.append(conn.name)
        raise FakeUndefinedTableError('relation "missing_table" does not exist')

    gp_adapter = read_sql_module.get_backend_adapter("gp")
    monkeypatch.setattr(gp_adapter, "read_dataframe", fake_read_gp)

    try:
        read_sql_module.read_sql(
            "gp",
            "select * from missing_table",
            retry_cnt=3,
            timeout_increment=0,
        )
    except FakeUndefinedTableError:
        pass
    else:
        raise AssertionError("Expected undefined-table error to be raised.")

    assert attempts == ["first"]
    assert len(connections) == 1
    assert first_connection.rollback_calls == 1
    assert first_connection.close_calls == 1
    assert second_connection.close_calls == 0


def test_read_sql_retries_whole_flow_with_fresh_gp_connection(monkeypatch) -> None:
    first_connection = FakeConnection("first")
    second_connection = FakeConnection("second")
    connections = [first_connection, second_connection]
    attempts: list[str] = []
    print_flags: list[bool] = []
    expected = pd.DataFrame({"value": [1]})

    monkeypatch.setattr(
        read_sql_module,
        "get_sql_connection",
        lambda connection_type: connections.pop(0),
    )

    def fake_read_gp(
        conn: FakeConnection,
        query: str,
        *,
        print_queries: bool = True,
        print_query: object | None = None,
        read_dbapi_query: object | None = None,
    ) -> pd.DataFrame:
        del query, print_query, read_dbapi_query
        attempts.append(conn.name)
        print_flags.append(print_queries)
        if conn.name == "first":
            raise RuntimeError("temporary failure")
        return expected

    gp_adapter = read_sql_module.get_backend_adapter("gp")
    monkeypatch.setattr(gp_adapter, "read_dataframe", fake_read_gp)

    result = read_sql_module.read_sql(
        "gp",
        "select 1",
        retry_cnt=2,
        timeout_increment=0,
    )

    pd.testing.assert_frame_equal(result, expected)
    assert attempts == ["first", "second"]
    assert print_flags == [False, False]
    assert first_connection.rollback_calls == 1
    assert first_connection.close_calls == 1
    assert second_connection.close_calls == 1


@pytest.mark.parametrize("connection_class", [FakeConnection, CloseFailureConnection])
def test_replace_connection_replaces_after_close_success_or_failure(
    monkeypatch,
    connection_class,
) -> None:
    original = connection_class("original")
    replacement = FakeConnection("replacement")
    connection_ref = {"connection": original}
    opened_keys: list[str] = []

    def open_connection(connection_key: str) -> FakeConnection:
        opened_keys.append(connection_key)
        return replacement

    monkeypatch.setattr(retry_module, "get_sql_connection", open_connection)

    retry_module.replace_connection("warehouse", connection_ref)

    assert original.close_calls == 1
    assert opened_keys == ["warehouse"]
    assert connection_ref["connection"] is replacement


def test_rollback_quietly_succeeds_and_suppresses_rollback_failure() -> None:
    success = FakeConnection("success")
    failure = RollbackFailureConnection("failure")

    retry_module.rollback_quietly(success)
    retry_module.rollback_quietly(failure)

    assert success.rollback_calls == 1
    assert failure.rollback_calls == 1


def test_run_with_fresh_connection_closes_replacement_after_operation_failure() -> None:
    original = FakeConnection("original")
    replacement = FakeConnection("replacement")
    original_error = RuntimeError("operation failed")

    def operation(connection_ref: dict[str, FakeConnection]) -> None:
        connection_ref["connection"] = replacement
        raise original_error

    with pytest.raises(RuntimeError) as caught:
        retry_module.run_with_fresh_connection(
            "warehouse",
            "source",
            operation,
            open_connection=lambda connection_key: original,
        )

    assert caught.value is original_error
    assert original.close_calls == 0
    assert replacement.close_calls == 1


@pytest.mark.parametrize(
    "message",
    [
        (
            "Received ClickHouse exception, code: 32, server response: Code: 32. "
            "DB::Exception: Attempt to read after eof: Cannot parse Int64 from "
            "String, because value is too short: while executing 'FUNCTION "
            "CAST(customer_id, Int64)'. (ATTEMPT_TO_READ_AFTER_EOF)"
        ),
        (
            "Received ClickHouse exception, code: 70, server response: Code: 70. "
            "DB::Exception: Cannot convert String to UInt64. "
            "(CANNOT_CONVERT_TYPE)"
        ),
    ],
)
def test_run_with_retry_does_not_retry_clickhouse_conversion_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    message: str,
) -> None:
    attempts: list[int] = []
    sleeps: list[float] = []
    error = DatabaseError(message)
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    def operation(attempt: int) -> None:
        attempts.append(attempt)
        raise error

    with pytest.raises(DatabaseError) as caught:
        retry_module.run_with_retry(
            operation_name="reading query on ch (ch)",
            retry_cnt=5,
            timeout_increment=600,
            operation=operation,
        )

    assert caught.value is error
    assert attempts == [1]
    assert sleeps == []
    output = capsys.readouterr().out
    assert "Failed with a non-retryable error" in output
    assert "Retrying in" not in output
