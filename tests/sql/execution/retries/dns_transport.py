from __future__ import annotations

import socket

import pytest
from analytics_toolkit.sql.dml.transfer.runtime.retry import run_with_retry
from analytics_toolkit.sql.execution.retry_errors import classify_sql_error
from requests.exceptions import ConnectionError as RequestsConnectionError
from urllib3.exceptions import MaxRetryError, NewConnectionError


class OperationalError(Exception):
    __module__ = "clickhouse_connect.driver.exceptions"


class TrinoConnectionError(Exception):
    __module__ = "trino.exceptions"


@pytest.mark.parametrize("error_class", [OperationalError, TrinoConnectionError])
@pytest.mark.parametrize(
    ("dns_code", "expected_attempts", "expected_sleeps"),
    [
        (socket.EAI_NONAME, [1], []),
        (socket.EAI_AGAIN, [1, 2], [3]),
    ],
)
def test_real_http_exception_chain_preserves_dns_retry_policy(
    monkeypatch: pytest.MonkeyPatch,
    error_class: type[Exception],
    dns_code: int,
    expected_attempts: list[int],
    expected_sleeps: list[float],
) -> None:
    dns_error = socket.gaierror(dns_code, "neutral DNS diagnostic")
    connection_error = NewConnectionError(None, "Failed to establish a new connection")
    # urllib3 retains the resolver failure through exception context in both 1.x
    # and 2.x; requests then puts MaxRetryError in ConnectionError.args.
    connection_error.__context__ = dns_error
    retries_error = MaxRetryError(None, "/v1/statement", reason=connection_error)
    request_error = RequestsConnectionError(retries_error)
    error = error_class("query HTTP transport failed")
    error.__context__ = request_error
    attempts: list[int] = []
    sleeps: list[float] = []
    monkeypatch.setattr(
        "analytics_toolkit.sql.dml.transfer.runtime.retry.time.sleep", sleeps.append
    )

    def operation(attempt: int) -> None:
        attempts.append(attempt)
        raise error

    with pytest.raises(error_class) as caught:
        run_with_retry("HTTP DNS failure", 2, 3, operation, safe_exception_logging=True)

    assert caught.value is error
    assert attempts == expected_attempts
    assert sleeps == expected_sleeps
    decision = classify_sql_error(error)
    assert decision.retryable is (dns_code == socket.EAI_AGAIN)
    if dns_code == socket.EAI_NONAME:
        assert decision.reason == "permanent_dns_failure"
