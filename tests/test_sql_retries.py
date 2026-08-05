from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analytics_toolkit.sql.connection.errors import SqlConfigError

execute_sql_module = importlib.import_module("analytics_toolkit.sql.dml.io.execute_sql")
execute_read_module = importlib.import_module("analytics_toolkit.sql.dml.io.execute_read")
read_sql_module = importlib.import_module("analytics_toolkit.sql.dml.io.read_sql")
load_df_module = importlib.import_module("analytics_toolkit.sql.dml.load.load_df")
retry_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.runtime.retry")
operation_runner_module = importlib.import_module(
    "analytics_toolkit.sql.execution.operation_runner"
)


class FakeConnection:
    def __init__(self, name: str) -> None:
        self.name = name
        self.close_calls = 0
        self.rollback_calls = 0

    def close(self) -> None:
        self.close_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1


class DatabaseError(Exception):
    pass


class FakeUndefinedTableError(Exception):
    pgcode = "42P01"


class FakeUndefinedObjectError(Exception):
    pgcode = "42704"


class AmbiguousColumn(Exception):
    pgcode = "42702"


class FakeTrinoSyntaxError(Exception):
    error_name = "SYNTAX_ERROR"


class FakeTrinoTypeMismatchError(Exception):
    error_name = "TYPE_MISMATCH"


class InsufficientPrivilege(Exception):
    pgcode = "42501"


class GroupingError(Exception):
    pgcode = "42803"


class FeatureNotSupported(Exception):
    pgcode = "0A000"


class CloseFailureConnection(FakeConnection):
    def close(self) -> None:
        self.close_calls += 1
        message = f"cannot close {self.name}"
        raise RuntimeError(message)


class RollbackFailureConnection(FakeConnection):
    def rollback(self) -> None:
        self.rollback_calls += 1
        message = f"cannot roll back {self.name}"
        raise RuntimeError(message)


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


def test_run_with_retry_does_not_retry_missing_type_message() -> None:
    attempts: list[int] = []

    def operation(attempt: int) -> None:
        attempts.append(attempt)
        raise RuntimeError('type "string" does not exist')

    try:
        retry_module.run_with_retry(
            operation_name="reading query on gp (gp)",
            retry_cnt=3,
            timeout_increment=0,
            operation=operation,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("Expected missing-type error to be raised.")

    assert attempts == [1]


@pytest.mark.parametrize(
    "error",
    [
        ValueError(
            "Trino table operations for schema-qualified names require "
            ".connections['trino'].catalog."
        ),
        ValueError(
            "Trino table operations for unqualified names require "
            ".connections['trino'].catalog and schema."
        ),
        SqlConfigError(".connections['trino'] is missing required configuration."),
    ],
)
def test_run_with_retry_does_not_retry_deterministic_configuration_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    attempts: list[int] = []
    sleeps: list[float] = []
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    def operation(attempt: int) -> None:
        attempts.append(attempt)
        raise error

    with pytest.raises(type(error)) as caught:
        retry_module.run_with_retry(
            operation_name="resolving Trino transfer target",
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


def test_run_with_retry_keeps_unrelated_value_error_retryable() -> None:
    attempts: list[int] = []
    temporary_error = ValueError("temporary response conversion failure")

    def operation(attempt: int) -> str:
        attempts.append(attempt)
        if attempt == 1:
            raise temporary_error
        return "ok"

    result = retry_module.run_with_retry(
        operation_name="unit retry",
        retry_cnt=2,
        timeout_increment=0,
        operation=operation,
    )

    assert result == "ok"
    assert attempts == [1, 2]


def test_run_with_retry_can_log_only_bounded_exception_type(capsys) -> None:
    secret = "password=hunter2 row=('customer-secret', 42)"
    error = RuntimeError(secret)

    def operation(_attempt: int) -> None:
        raise error

    with pytest.raises(RuntimeError) as caught:
        retry_module.run_with_retry(
            operation_name="safe keyed retry",
            retry_cnt=1,
            timeout_increment=0,
            operation=operation,
            safe_exception_logging=True,
        )

    assert caught.value is error
    output = capsys.readouterr().out
    assert "Failed after 1 attempt(s): RuntimeError" in output
    assert secret not in output
    assert "customer-secret" not in output


def test_run_with_retry_preserves_detailed_logging_by_default(capsys) -> None:
    detail = "legacy retry detail"

    with pytest.raises(RuntimeError, match=detail):
        retry_module.run_with_retry(
            operation_name="legacy retry",
            retry_cnt=1,
            timeout_increment=0,
            operation=lambda _attempt: (_ for _ in ()).throw(RuntimeError(detail)),
        )

    assert detail in capsys.readouterr().out


def test_run_with_retry_supports_a_safe_attempt_status_message(capsys) -> None:
    attempts: list[int] = []

    def operation(attempt: int) -> str:
        attempts.append(attempt)
        if attempt == 1:
            raise OSError("row secret")
        return "ok"

    result = retry_module.run_with_retry(
        operation_name="keyed retry status",
        retry_cnt=2,
        timeout_increment=0,
        operation=operation,
        log_prefix="[slice=1/1] ",
        safe_exception_logging=True,
        retry_status=lambda attempt, total: (
            f"Retrying target-stage batch 1 insert: attempt {attempt}/{total}; "
            "committed total remains 0 rows; ETA unchanged"
        ),
    )

    assert result == "ok"
    assert attempts == [1, 2]
    output = capsys.readouterr().out
    assert "[slice=1/1] Retrying target-stage batch 1 insert: attempt 2/2" in output
    assert "row secret" not in output


def test_run_with_retry_does_not_retry_duplicate_query_columns(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    attempts: list[int] = []
    sleeps: list[float] = []
    error = ValueError("sql must not return duplicate columns: suppliers_cheques")
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    def operation(attempt: int) -> None:
        attempts.append(attempt)
        raise error

    with pytest.raises(
        ValueError,
        match="sql must not return duplicate columns: suppliers_cheques",
    ) as caught:
        retry_module.run_with_retry(
            operation_name="creating table gp.sandbox.target from query",
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


@pytest.mark.parametrize(
    "message",
    [
        (
            "Received ClickHouse exception, code: 36, server response: Code: 36. "
            "DB::Exception: Macro 'uuid' in engine arguments requires an explicit UUID. "
            "(BAD_ARGUMENTS)"
        ),
        (
            "Received ClickHouse exception, code: 53, server response: Code: 53. "
            "DB::Exception: Sharding expression has type Float64, but should be one of "
            "integer type. (TYPE_MISMATCH)"
        ),
    ],
)
def test_run_with_retry_does_not_retry_clickhouse_deterministic_ddl_errors(
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
            operation_name="creating table on ch (ch)",
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


def test_run_with_retry_does_not_retry_clickhouse_unknown_identifier(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    attempts: list[int] = []
    sleeps: list[float] = []
    error = DatabaseError(
        "Received ClickHouse exception, code: 47, server response: Code: 47. "
        "DB::Exception: Missing columns: 'talbe_name'. (UNKNOWN_IDENTIFIER)"
    )
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


def test_run_with_retry_keeps_clickhouse_transport_eof_retryable() -> None:
    attempts: list[int] = []
    error = DatabaseError(
        "Received ClickHouse exception, code: 32, server response: Code: 32. "
        "DB::Exception: Attempt to read after eof. (ATTEMPT_TO_READ_AFTER_EOF)"
    )

    def operation(attempt: int) -> str:
        attempts.append(attempt)
        if attempt == 1:
            raise error
        return "ok"

    result = retry_module.run_with_retry(
        operation_name="reading query on ch (ch)",
        retry_cnt=2,
        timeout_increment=0,
        operation=operation,
    )

    assert result == "ok"
    assert attempts == [1, 2]


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


def test_run_with_retry_does_not_retry_trino_syntax_error() -> None:
    attempts: list[int] = []

    def operation(attempt: int) -> None:
        attempts.append(attempt)
        raise FakeTrinoSyntaxError("line 1:8: mismatched input 'fromm'")

    try:
        retry_module.run_with_retry(
            operation_name="executing SQL on trino",
            retry_cnt=3,
            timeout_increment=0,
            operation=operation,
        )
    except FakeTrinoSyntaxError:
        pass
    else:
        raise AssertionError("Expected syntax error to be raised.")

    assert attempts == [1]


def test_run_with_retry_does_not_retry_trino_type_mismatch() -> None:
    attempts: list[int] = []
    error = FakeTrinoTypeMismatchError("Cannot cast array(varchar) to varchar")

    def operation(attempt: int) -> None:
        attempts.append(attempt)
        raise error

    with pytest.raises(FakeTrinoTypeMismatchError) as caught:
        retry_module.run_with_retry(
            operation_name="creating table from query on trino",
            retry_cnt=5,
            timeout_increment=5,
            operation=operation,
        )

    assert caught.value is error
    assert attempts == [1]


def test_run_with_retry_does_not_retry_insufficient_privilege() -> None:
    attempts: list[int] = []

    def operation(attempt: int) -> None:
        attempts.append(attempt)
        raise InsufficientPrivilege("must be owner of relation sandbox.target")

    try:
        retry_module.run_with_retry(
            operation_name="dropping target table on gp",
            retry_cnt=3,
            timeout_increment=0,
            operation=operation,
        )
    except InsufficientPrivilege:
        pass
    else:
        raise AssertionError("Expected insufficient-privilege error to be raised.")

    assert attempts == [1]


def test_run_with_retry_does_not_retry_grouping_error() -> None:
    attempts: list[int] = []

    def operation(attempt: int) -> None:
        attempts.append(attempt)
        raise GroupingError(
            'column "source.contact_id" must appear in the GROUP BY clause '
            "or be used in an aggregate function"
        )

    try:
        retry_module.run_with_retry(
            operation_name="reading query on gp (gp)",
            retry_cnt=3,
            timeout_increment=0,
            operation=operation,
        )
    except GroupingError:
        pass
    else:
        raise AssertionError("Expected grouping error to be raised.")

    assert attempts == [1]


def test_run_with_retry_does_not_retry_clickhouse_illegal_group_by() -> None:
    attempts: list[int] = []

    def operation(attempt: int) -> None:
        attempts.append(attempt)
        raise RuntimeError(
            "Received ClickHouse exception, code: 43, server response: Code: 43. "
            "DB::Exception: Illegal value (aggregate function) for positional "
            "argument in GROUP BY: While processing SELECT dt, uniqState(magnit_id) "
            "AS magnit_id_state FROM pa_core_stage.target GROUP BY dt, 2. "
            "(ILLEGAL_TYPE_OF_ARGUMENT)"
        )

    try:
        retry_module.run_with_retry(
            operation_name="executing SQL on ch (ch)",
            retry_cnt=3,
            timeout_increment=0,
            operation=operation,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("Expected ClickHouse semantic error to be raised.")

    assert attempts == [1]


def test_run_with_retry_does_not_retry_cross_database_reference_error() -> None:
    attempts: list[int] = []

    def operation(attempt: int) -> None:
        attempts.append(attempt)
        raise FeatureNotSupported(
            "cross-database references are not implemented: "
            '"iceberg.pa_core_sandbox.karapsin_tmp_back_usage_check"\n'
            "LINE 1: select * from iceberg.pa_core_sandbox.karapsin_tmp_back_usag..."
        )

    try:
        retry_module.run_with_retry(
            operation_name="reading query on gp (gp)",
            retry_cnt=3,
            timeout_increment=0,
            operation=operation,
        )
    except FeatureNotSupported:
        pass
    else:
        raise AssertionError("Expected feature-not-supported error to be raised.")

    assert attempts == [1]


def test_run_with_retry_logs_human_readable_wait(
    monkeypatch,
    capsys,
) -> None:
    attempts: list[int] = []
    sleeps: list[float] = []
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    def operation(attempt: int) -> str:
        attempts.append(attempt)
        if attempt == 1:
            raise RuntimeError("temporary failure")
        return "ok"

    result = retry_module.run_with_retry(
        "unit retry",
        retry_cnt=2,
        timeout_increment=90,
        operation=operation,
    )

    output = capsys.readouterr().out
    assert result == "ok"
    assert attempts == [1, 2]
    assert sleeps == [90]
    assert "[unit retry] [retry] Retrying in 1 minute 30 seconds" in output


def test_run_with_retry_reraises_same_exception_without_zero_delay_sleep(
    monkeypatch,
) -> None:
    original_error = RuntimeError("temporary failure")
    attempts: list[int] = []
    sleeps: list[float] = []
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    def operation(attempt: int) -> None:
        attempts.append(attempt)
        raise original_error

    with pytest.raises(RuntimeError) as caught:
        retry_module.run_with_retry(
            "unit retry",
            retry_cnt=2,
            timeout_increment=0,
            operation=operation,
        )

    assert caught.value is original_error
    assert attempts == [1, 2]
    assert sleeps == []


def test_run_with_retry_nonretryable_exception_never_sleeps(monkeypatch) -> None:
    original_error = ValueError("invalid input")
    sleeps: list[float] = []
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    with pytest.raises(ValueError, match="invalid input") as caught:
        retry_module.run_with_retry(
            "unit retry",
            retry_cnt=3,
            timeout_increment=10,
            operation=lambda _attempt: (_ for _ in ()).throw(original_error),
            retryable_exceptions=(RuntimeError,),
        )

    assert caught.value is original_error
    assert sleeps == []


def test_run_with_retry_zero_retries_reports_missing_exception() -> None:
    attempts: list[int] = []

    def operation(attempt: int) -> None:
        attempts.append(attempt)

    with pytest.raises(
        RuntimeError,
        match="unit retry failed without capturing an exception",
    ):
        retry_module.run_with_retry(
            "unit retry",
            retry_cnt=0,
            timeout_increment=0,
            operation=operation,
        )

    assert attempts == []


def test_rollback_quietly_succeeds_and_suppresses_rollback_failure() -> None:
    success = FakeConnection("success")
    failure = RollbackFailureConnection("failure")

    retry_module.rollback_quietly(success)
    retry_module.rollback_quietly(failure)

    assert success.rollback_calls == 1
    assert failure.rollback_calls == 1


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


def test_replace_connection_recovers_when_reference_is_missing(monkeypatch) -> None:
    replacement = FakeConnection("replacement")
    connection_ref: dict[str, FakeConnection] = {}
    monkeypatch.setattr(
        retry_module,
        "get_sql_connection",
        lambda connection_key: replacement,
    )

    retry_module.replace_connection("warehouse", connection_ref)

    assert connection_ref == {"connection": replacement}


def test_run_with_fresh_connection_returns_after_final_cleanup() -> None:
    connection = FakeConnection("fresh")
    seen_refs: list[dict[str, FakeConnection]] = []

    result = retry_module.run_with_fresh_connection(
        "warehouse",
        "target",
        lambda connection_ref: seen_refs.append(connection_ref) or "ok",
        open_connection=lambda connection_key: connection,
    )

    assert result == "ok"
    assert seen_refs == [{"connection": connection}]
    assert connection.close_calls == 1


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


def test_close_connection_ref_handles_missing_and_failed_connections(capsys) -> None:
    retry_module.close_connection_ref({}, "warehouse", "source")
    failing = CloseFailureConnection("failing")

    retry_module.close_connection_ref(
        {"connection": failing},
        "warehouse",
        "target",
    )

    assert failing.close_calls == 1
    output = capsys.readouterr().out
    assert "[warehouse] [close_target] Closing connection" in output
    assert "[warehouse] [close_target] Failed" in output


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


def test_operation_runner_uses_custom_connection_cleanup() -> None:
    connection = FakeConnection("custom")
    cleaned: list[dict[str, FakeConnection]] = []

    result = operation_runner_module.run_connection_operation(
        operation_name="custom cleanup",
        connection_key="gp",
        backend="gp",
        retry_cnt=1,
        timeout_increment=0,
        open_connection=lambda _key: connection,
        operation=lambda connection_ref, _attempt: connection_ref["connection"].name,
        context_factory=lambda attempt: operation_runner_module.SqlOperationContext(
            operation="custom cleanup",
            retry_attempt=attempt,
        ),
        cleanup=cleaned.append,
    )

    assert result == "custom"
    assert cleaned == [{"connection": connection}]
    assert connection.close_calls == 0


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
