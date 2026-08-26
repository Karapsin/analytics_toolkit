from __future__ import annotations

from tests.sql._support.retries import (
    CloseFailureConnection,
    FakeConnection,
    pytest,
    retry_module,
)


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
