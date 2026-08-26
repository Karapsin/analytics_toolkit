from __future__ import annotations

from tests.sql._support.insert_schema import (
    pd,
    trino_insert,
)


def test_build_values_tuple_renders_each_target_type() -> None:
    row = [1, "O'Reilly", pd.Timestamp("2026-01-02"), object()]

    result = trino_insert.build_values_tuple(
        ["id", "name", "event_date", "payload"],
        row,
        {
            "id": "bigint",
            "name": "varchar",
            "event_date": "date",
            "payload": "json",
        },
    )

    assert result.startswith("(1, 'O''Reilly', DATE '2026-01-02', CAST('")
    assert result.endswith("' AS json))")
