from __future__ import annotations

from tests.sql._support.insert_schema import (
    pytest,
    trino_insert,
)


@pytest.mark.parametrize("value", [0, -1])
def test_get_insert_chunk_size_rejects_non_positive_explicit_value(value: int) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        trino_insert.get_insert_chunk_size(value)


def test_validate_row_width_rejects_mismatch() -> None:
    with pytest.raises(ValueError, match="counts must match"):
        trino_insert.validate_row_width(["id", "value"], [1])
