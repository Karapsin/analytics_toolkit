from __future__ import annotations

from tests.sql._support.insert_schema import (
    FakeCursor,
    FakeTrinoAdapter,
    SimpleNamespace,
    trino_insert,
)


def test_insert_rows_chunks_parameters_reports_progress_and_closes_cursor() -> None:
    cursor = FakeCursor()
    connection = SimpleNamespace(cursor=lambda: cursor)
    adapter = FakeTrinoAdapter()
    progress: list[int] = []

    trino_insert.insert_rows(
        adapter,
        connection,
        "schema.target",
        ["id", "value"],
        [(1, "a"), (2, "b"), (3, "c")],
        target_column_types={"id": "bigint", "value": "varchar"},
        trino_insert_chunk_size=2,
        query_label="q",
        on_progress=progress.append,
    )

    assert progress == [2, 1]
    assert [call[2] for call in adapter.calls] == [2, 1]
    assert cursor.calls[0][1] == [1, "a", 2, "b"]
    assert cursor.calls[1][1] == [3, "c"]
    assert cursor.closed is True


def test_insert_rows_without_progress_callback() -> None:
    cursor = FakeCursor()
    trino_insert.insert_rows(
        FakeTrinoAdapter(),
        SimpleNamespace(cursor=lambda: cursor),
        "schema.target",
        ["id", "value"],
        [(1, "a"), (2, "b")],
        trino_insert_chunk_size=1,
        on_progress=None,
    )
    assert len(cursor.calls) == 2
    assert cursor.closed is True
