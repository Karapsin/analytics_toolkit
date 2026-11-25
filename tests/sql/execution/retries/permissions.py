"""Deterministic native access failures do not consume data replay budgets."""

from __future__ import annotations

import psycopg2
import pytest
from analytics_toolkit.sql.dml.transfer.runtime import retry
from analytics_toolkit.sql.execution.backend_options import backend_operation
from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError
from clickhouse_driver.errors import ErrorCodes, ServerException


@pytest.mark.parametrize(
    "error",
    [
        PermissionError("Native identity catalog blocked"),
        ServerException("Native identity catalog blocked", code=497),
        ServerException("Native identity catalog blocked", code=ErrorCodes.DATABASE_ACCESS_DENIED),
        DatabaseError(
            "Received ClickHouse exception, code: 497, server response: Code: 497. "
            "DB::Exception: Native identity catalog blocked. (ACCESS_DENIED)"
        ),
        DatabaseError(
            "HTTP driver received HTTP status 403, server response: Code: 497. "
            "DB::Exception: Native identity catalog blocked. (ACCESS_DENIED)"
        ),
    ],
    ids=[
        "permission",
        "native-access-denied",
        "native-database-access-denied",
        "http",
        "http-body",
    ],
)
def test_native_permission_errors_preserve_primary_and_never_retry(monkeypatch, error):
    attempts = []
    waits = []
    monkeypatch.setattr(retry.time, "sleep", waits.append)

    def execute(attempt):
        attempts.append(attempt)
        raise error

    with pytest.raises(type(error)) as caught:
        retry.run_with_retry("native metadata preflight", 3, 7, execute)

    assert caught.value is error
    assert attempts == [1]
    assert waits == []


@pytest.mark.parametrize(
    "error",
    [
        OSError("Response acknowledgement was lost"),
        psycopg2.OperationalError("Response acknowledgement was lost"),
        ServerException("DB::Exception: socket timeout", code=ErrorCodes.NETWORK_ERROR),
        OperationalError("Response acknowledgement was lost"),
        DatabaseError(
            "Received ClickHouse exception, code: 32, server response: Code: 32. "
            "DB::Exception: Attempt to read after eof. (ATTEMPT_TO_READ_AFTER_EOF)"
        ),
        DatabaseError(
            "HTTP driver received HTTP status 500, server response: Code: 209. "
            "DB::Exception: Socket timeout. (SOCKET_TIMEOUT)"
        ),
        ServerException(
            "DB::Exception: Network interrupted while reading a user value 'Code: 497'",
            code=ErrorCodes.NETWORK_ERROR,
        ),
        DatabaseError(
            "Received ClickHouse exception, code: 210, server response: Code: 210. "
            "DB::Exception: Network interrupted while reading user value 'Code: 497'"
        ),
        DatabaseError(
            "HTTP driver received HTTP status 503, server response: "
            "DB::Exception: Interrupted response containing user value 'Code: 497'"
        ),
        DatabaseError("HTTP driver received HTTP status 503"),
    ],
    ids=[
        "os",
        "gp",
        "native",
        "http-operational",
        "http-eof",
        "http-timeout-body",
        "native-user-body",
        "http-user-body",
        "http-unknown-user-body",
        "http-empty-body",
    ],
)
def test_native_transient_errors_replay_acknowledged_data_with_original_delay(monkeypatch, error):
    attempts = []
    writes = []
    waits = []
    monkeypatch.setattr(retry.time, "sleep", waits.append)

    def execute(attempt):
        attempts.append(attempt)
        writes.append(42)
        if attempt == 1:
            raise error
        return len(writes)

    with backend_operation() as state:
        state.live_submitted = True
        result = retry.run_with_retry("data acknowledgement replay", 2, 7, execute)
        assert state.data_write_replay_possible is True
        assert state.structural_replay_blocked is False

    assert result == 2
    assert attempts == [1, 2]
    assert writes == [42, 42]
    assert waits == [7]
