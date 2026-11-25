from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import pandas as pd

from tests.sql._support.row_batches import RecordingSourceCursor, retry_module

if TYPE_CHECKING:
    import pytest


class RecoveringCursor(RecordingSourceCursor):
    def __init__(
        self,
        rows: list[tuple[int]],
        *,
        execute_error: Exception | None = None,
        fetch_error: BaseException | None = None,
    ) -> None:
        super().__init__(rows)
        self.execute_error = execute_error
        self.fetch_error = fetch_error

    def execute(self, query: str) -> None:
        super().execute(query)
        if self.execute_error is not None:
            raise self.execute_error

    def fetchmany(self, size: int) -> list[tuple[int]]:
        if not self._rows and self.fetch_error is not None:
            raise self.fetch_error
        return super().fetchmany(size)


class RecoveringDbapiConnection:
    def __init__(self, cursor: RecoveringCursor, *, cursor_error: Exception | None = None) -> None:
        self.cursor_obj = cursor
        self.cursor_error = cursor_error
        self.cursor_calls = 0
        self.rollback_calls = 0
        self.close_calls = 0

    def cursor(self) -> RecoveringCursor:
        self.cursor_calls += 1
        if self.cursor_error is not None:
            raise self.cursor_error
        return self.cursor_obj

    def rollback(self) -> None:
        self.rollback_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class RecoveringClickHouseStream(Iterator[pd.DataFrame]):
    def __init__(
        self,
        blocks: list[pd.DataFrame],
        *,
        enter_error: Exception | None = None,
        read_error: BaseException | None = None,
    ) -> None:
        self.blocks = list(blocks)
        self.enter_error = enter_error
        self.read_error = read_error
        self.enter_calls = 0
        self.exit_calls = 0

    def __enter__(self) -> Iterator[pd.DataFrame]:
        self.enter_calls += 1
        if self.enter_error is not None:
            raise self.enter_error
        return self

    def __exit__(self, *_args: object) -> None:
        self.exit_calls += 1

    def __next__(self) -> pd.DataFrame:
        if self.blocks:
            return self.blocks.pop(0)
        if self.read_error is not None:
            raise self.read_error
        raise StopIteration


class RecoveringClickHouseConnection:
    def __init__(
        self,
        stream: RecoveringClickHouseStream,
        *,
        query_error: Exception | None = None,
    ) -> None:
        self.stream = stream
        self.query_error = query_error
        self.query_limit = 10
        self.queries: list[str] = []
        self.query_limits: list[int] = []
        self.close_calls = 0

    def query_df_stream(self, query: str) -> RecoveringClickHouseStream:
        self.queries.append(query)
        self.query_limits.append(self.query_limit)
        if self.query_error is not None:
            raise self.query_error
        return self.stream

    def close(self) -> None:
        self.close_calls += 1


def arrange_source_recovery(
    monkeypatch: pytest.MonkeyPatch,
    replacement: Any,
) -> tuple[list[str], list[float]]:
    opened: list[str] = []
    sleeps: list[float] = []

    def open_connection(key: str) -> Any:
        opened.append(key)
        return replacement

    monkeypatch.setattr(retry_module, "get_sql_connection", open_connection)
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)
    return opened, sleeps
