from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit.sql.backends import get_backend_adapter


class CountCursor:
    def __init__(self, row: tuple[int] | None, failure: str | None = None) -> None:
        self.row = row
        self.failure = failure
        self.error = RuntimeError("source count failed")
        self.executed: list[str] = []
        self.close_calls = 0

    def execute(self, statement: str) -> None:
        self.executed.append(statement)
        if self.failure == "execute":
            raise self.error

    def fetchone(self) -> tuple[int] | None:
        if self.failure == "fetch":
            raise self.error
        return self.row

    def close(self) -> None:
        self.close_calls += 1


@pytest.mark.parametrize("backend", ["gp", "trino"])
@pytest.mark.parametrize(("row", "expected"), [((42,), 42), ((0,), 0), (None, 0)])
def test_backend_source_count_consumes_the_final_count_and_always_closes_the_cursor(
    backend: str,
    row: tuple[int] | None,
    expected: int,
) -> None:
    cursor = CountCursor(row)
    connection = SimpleNamespace(cursor=lambda: cursor)

    result = get_backend_adapter(backend).count_source_rows(
        connection, "SELECT id FROM source;", query_label="count-probe"
    )

    assert result == expected
    assert type(result) is int
    assert cursor.close_calls == 1
    assert len(cursor.executed) == 1
    assert cursor.executed[0].endswith(
        "SELECT COUNT(*) FROM (SELECT id FROM source) AS source_count_probe"
    )
    assert "query_label=count-probe" in cursor.executed[0]


@pytest.mark.parametrize("backend", ["gp", "trino"])
@pytest.mark.parametrize("failure", ["execute", "fetch"])
def test_backend_source_count_preserves_the_failure_and_closes_its_cursor(
    backend: str,
    failure: str,
) -> None:
    cursor = CountCursor((42,), failure)
    connection: Any = SimpleNamespace(cursor=lambda: cursor)

    with pytest.raises(RuntimeError, match="source count failed") as caught:
        get_backend_adapter(backend).count_source_rows(connection, "SELECT id FROM source")

    assert caught.value is cursor.error
    assert cursor.close_calls == 1
