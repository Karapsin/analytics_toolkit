from __future__ import annotations

from tests.sql._support.retries import (
    DatabaseError,
    FakeTrinoSyntaxError,
    FakeTrinoTypeMismatchError,
    FeatureNotSupported,
    GroupingError,
    InsufficientPrivilege,
    SqlConfigError,
    pytest,
    retry_module,
)


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
