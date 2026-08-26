from __future__ import annotations

from tests.sql._support.retries import (
    DatabaseError,
    pytest,
    retry_module,
)


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
