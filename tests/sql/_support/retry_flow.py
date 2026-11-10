from __future__ import annotations

from typing import TYPE_CHECKING, Any

from trino.exceptions import TrinoUserError

from tests.sql._support.row_batches import (
    load_sql_table_module,
    make_progress_options,
    source_module,
    transfer_api_module,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest


# SDK exception imports also initialize unrelated native transport extensions.
DatabaseError = type(
    "DatabaseError", (Exception,), {"__module__": "clickhouse_connect.driver.exceptions"}
)


class SourceConversionError(Exception):
    pgcode = "22P02"


class FailingSourceCursor:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.execute_calls = 0
        self.close_calls = 0

    def execute(self, _query: str) -> None:
        self.execute_calls += 1
        raise self.error

    def close(self) -> None:
        self.close_calls += 1
        message = "cursor cleanup failed"
        raise RuntimeError(message)


class FailingSourceConnection:
    def __init__(self, error: Exception) -> None:
        self.cursor_obj = FailingSourceCursor(error)
        self.rollback_calls = 0

    def cursor(self) -> FailingSourceCursor:
        return self.cursor_obj

    def rollback(self) -> None:
        self.rollback_calls += 1
        message = "rollback cleanup failed"
        raise RuntimeError(message)


def terminal_insert_error(backend: str) -> Exception:
    if backend == "ch":
        return DatabaseError("Code: 53. DB::Exception: Invalid insert value. (TYPE_MISMATCH)")
    return TrinoUserError(
        {
            "message": "Invalid insert value",
            "errorName": "INVALID_CAST_ARGUMENT",
            "errorType": "USER_ERROR",
            "errorCode": 9,
        }
    )


def arrange_transfer_attempt(
    monkeypatch: pytest.MonkeyPatch,
    attempt: Callable[..., int],
    *,
    target_backend: str,
    target_key: str | None = None,
) -> Any:
    options = make_progress_options(
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key=target_key or target_backend,
        to_db_backend=target_backend,
        replace_target_table=True,
        retry_cnt=3,
        full_retry_cnt=3,
        timeout_increment=5,
        full_timeout_increment=5,
        progress=False,
    )
    monkeypatch.setattr(transfer_api_module, "build_transfer_options", lambda **_k: options)
    monkeypatch.setattr(transfer_api_module, "run_transfer_attempt", attempt)
    return options


def arrange_stage_insert_failure(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    *,
    dataframe: bool,
) -> list[str]:
    calls: list[str] = []

    def insert(backend: str, *_args: Any, **_kwargs: Any) -> None:
        calls.append(backend)
        raise error

    method = "_insert_batch_backend" if dataframe else "_insert_rows_backend"
    monkeypatch.setattr(load_sql_table_module, method, insert)
    return calls


def record_source_replacements(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    replacements: list[str] = []
    monkeypatch.setattr(
        source_module,
        "replace_connection",
        lambda key, _ref: replacements.append(key),
    )
    return replacements
