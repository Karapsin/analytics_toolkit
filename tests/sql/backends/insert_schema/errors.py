from __future__ import annotations

from tests.sql._support.insert_schema import (
    FakeCursor,
    FakeTrinoAdapter,
    SimpleNamespace,
    pytest,
    trino_insert,
)


def test_insert_rows_closes_cursor_after_execute_failure() -> None:
    message = "insert failed"
    cursor = FakeCursor(error=RuntimeError(message))
    connection = SimpleNamespace(cursor=lambda: cursor)

    with pytest.raises(RuntimeError, match="insert failed"):
        trino_insert.insert_rows(
            FakeTrinoAdapter(),
            connection,
            "schema.target",
            ["id", "value"],
            [(1, "a")],
            trino_insert_chunk_size=1,
        )
    assert cursor.closed is True
