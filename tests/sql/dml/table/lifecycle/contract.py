from __future__ import annotations

from tests.sql._support.lifecycle import (
    Any,
    RowCountResult,
    backend_utils,
    date,
    datetime,
    errors,
    pytest,
    table_validation,
    timezone,
)


def test_add_exception_note_supports_legacy_and_locked_exceptions() -> None:
    class LegacyError(Exception):
        add_note = None

    class LockedNotesError(Exception):
        add_note = None

        def __setattr__(self, name: str, value: Any) -> None:
            if name == "__notes__":
                message = "locked"
                raise RuntimeError(message)
            super().__setattr__(name, value)

    legacy = LegacyError("legacy")
    errors._add_exception_note(legacy, "context")
    errors._add_exception_note(LockedNotesError("locked"), "context")

    assert legacy.__notes__ == ["context"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, None), (None, None), ("4", 4), (-1, None), ("bad", None)],
)
def test_backend_row_count_coercion(value: Any, expected: int | None) -> None:
    assert backend_utils._coerce_row_count(value) == expected


def test_backend_row_count_falls_through_summary_to_attributes() -> None:
    result = RowCountResult(
        rowcount=-1,
        summary={"rows": "invalid"},
        written_rows="11",
    )
    assert backend_utils.extract_row_count(result) == 11
    assert backend_utils._extract_row_count_from_mapping({"rows": "invalid"}) is None

    class SummaryMapping(dict):
        def __init__(self, **values: Any) -> None:
            super().__init__(**values)
            self.summary = {"rows": 12}

    assert backend_utils.extract_row_count(SummaryMapping(rows="invalid")) == 12


def test_backend_sql_literal_helpers() -> None:
    assert backend_utils.user_filter("user_name", "current_user", None) == (
        "user_name = current_user"
    )
    assert backend_utils.user_filter("user_name", "current_user", "O'Reilly") == (
        "user_name = 'O''Reilly'"
    )
    assert backend_utils.sql_in_list("name", ["a", "b's"]) == "name in ('a', 'b''s')"
    with pytest.raises(ValueError, match="must not be empty"):
        backend_utils.sql_in_list("name", [])

    assert backend_utils.sql_literal(None) == "NULL"
    assert backend_utils.sql_literal(True) == "TRUE"
    assert backend_utils.sql_literal(False) == "FALSE"
    assert backend_utils.sql_literal(3) == "3"
    assert backend_utils.sql_literal(2.5) == "2.5"
    assert backend_utils.sql_literal(date(2026, 1, 2)) == "'2026-01-02'"
    assert (
        backend_utils.sql_literal(datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
        == "'2026-01-02T03:04:05+00:00'"
    )
    assert backend_utils.sql_literal("x'y") == "'x''y'"


@pytest.mark.parametrize(
    ("executed", "expected"),
    [
        (RowCountResult(rowcount=3), 3),
        ({"writtenRows": "4"}, 4),
        (RowCountResult(summary={"processedRows": 5}), 5),
        (RowCountResult(written_rows=6), 6),
        (RowCountResult(writtenRows=7), 7),
        (RowCountResult(processed_rows=8), 8),
        (RowCountResult(rows=9), 9),
        (RowCountResult(), 0),
    ],
)
def test_extract_row_count_supports_backend_result_shapes(executed: Any, expected: int) -> None:
    assert backend_utils.extract_row_count(executed) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (" id ", ["id"]),
        ((" id ", "date"), ["id", "date"]),
    ],
)
def test_normalize_key_columns_accepts_supported_values(value: Any, expected: Any) -> None:
    assert table_validation.normalize_key_columns(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), (" partition_date ", "partition_date")],
)
def test_normalize_upsert_partition_column_accepts_valid_values(
    value: Any,
    expected: str | None,
) -> None:
    assert table_validation.normalize_upsert_partition_column(value) == expected


@pytest.mark.parametrize(
    ("sql", "max_chars", "expected"),
    [(None, 10, None), (" SELECT   1 ", 20, "SELECT 1"), ("SELECT 123456", 10, "SELECT ...")],
)
def test_sql_preview_normalizes_and_truncates(
    sql: str | None,
    max_chars: int,
    expected: str | None,
) -> None:
    assert errors.sql_preview(sql, max_chars=max_chars) == expected
