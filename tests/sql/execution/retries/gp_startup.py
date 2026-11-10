from __future__ import annotations

import psycopg2
import pytest
from analytics_toolkit import sql

from tests.sql._support.retries import retry_module


@pytest.mark.parametrize("operation_name", ["execute", "read"])
@pytest.mark.parametrize(
    "diagnostic",
    [
        "Name or service not known",
        "Name does not resolve",
        "nodename nor servname provided, or not known",
        "No such host is known.",
    ],
)
def test_public_operation_does_not_reopen_after_permanent_libpq_dns_failure(
    monkeypatch: pytest.MonkeyPatch, operation_name: str, diagnostic: str
) -> None:
    opens: list[int] = []
    sleeps: list[float] = []
    error = psycopg2.OperationalError(
        f'could not translate host name "bad.example" to address: {diagnostic}\n'
    )

    def failed_connect(**_kwargs: object) -> None:
        opens.append(1)
        raise error

    monkeypatch.setattr(psycopg2, "connect", failed_connect)
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    with pytest.raises(psycopg2.OperationalError) as caught:
        getattr(sql, operation_name)("gp", "SELECT 1", retry_cnt=3, timeout_increment=5)

    assert caught.value is error
    assert opens == [1]
    assert sleeps == []


@pytest.mark.parametrize("operation_name", ["execute", "read"])
@pytest.mark.parametrize(
    "diagnostic",
    [
        "Temporary failure in name resolution",
        "Try again",
        "Non-recoverable failure in name resolution",
        "Non-recoverable error",
        "Unknown resolver error",
    ],
)
def test_public_operation_retains_attempt_and_delay_budget_for_other_libpq_dns_failures(
    monkeypatch: pytest.MonkeyPatch, operation_name: str, diagnostic: str
) -> None:
    opens: list[int] = []
    sleeps: list[float] = []
    error = psycopg2.OperationalError(
        f'could not translate host name "bad.example" to address: {diagnostic}\n'
    )

    def failed_connect(**_kwargs: object) -> None:
        opens.append(1)
        raise error

    monkeypatch.setattr(psycopg2, "connect", failed_connect)
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    with pytest.raises(psycopg2.OperationalError) as caught:
        getattr(sql, operation_name)("gp", "SELECT 1", retry_cnt=3, timeout_increment=5)

    assert caught.value is error
    assert opens == [1, 1, 1]
    assert sleeps == [5, 10]
