from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol


class DbApiCursor(Protocol):
    description: Sequence[Sequence[Any]] | None

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> Any: ...

    def fetchone(self) -> Sequence[Any] | None: ...

    def fetchall(self) -> Sequence[Sequence[Any]]: ...

    def close(self) -> None: ...


class DbApiConnection(Protocol):
    autocommit: bool

    def cursor(self) -> DbApiCursor: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


class ClickHouseResult(Protocol):
    result_rows: Sequence[Sequence[Any]]
    column_names: Sequence[str]
    column_types: Sequence[Any]


class ClickHouseClient(Protocol):
    def command(self, sql: str, **kwargs: Any) -> Any: ...

    def query(self, sql: str, **kwargs: Any) -> ClickHouseResult: ...

    def close(self) -> None: ...
