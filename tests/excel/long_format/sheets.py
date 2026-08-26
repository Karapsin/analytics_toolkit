from __future__ import annotations

from tests.excel._support.long_format import (
    Path,
    break_table,
    load_workbook,
    pd,
    pivot_and_break_table,
    pytest,
)


def test_enforced_order_rejects_extra_and_misaligned_break_groups(tmp_path: Path) -> None:
    first = pd.DataFrame({"metric": ["users"], "break": ["A"], "value": [1]})
    extra = pd.DataFrame({"metric": ["users", "users"], "break": ["A", "B"], "value": [2, 3]})
    misaligned = pd.DataFrame({"metric": ["users"], "break": ["B"], "value": [2]})

    with pytest.raises(ValueError, match="more tables"):
        pivot_and_break_table(
            [first, extra],
            rows="metric",
            value="value",
            output=tmp_path / "extra-groups.xlsx",
            break_by="break",
            enforce_same_row_order=True,
        )
    with pytest.raises(ValueError, match="do not align"):
        pivot_and_break_table(
            [first, misaligned],
            rows="metric",
            value="value",
            output=tmp_path / "misaligned-groups.xlsx",
            break_by="break",
            enforce_same_row_order=True,
        )


def test_sheet_names_cover_missing_empty_and_multiple_deduplications(tmp_path: Path) -> None:
    df = pd.DataFrame(
        {
            "sheet": [None, "''", "X" * 40, "X" * 39 + "A", "X" * 39 + "B"],
            "value": [1, 2, 3, 4, 5],
        }
    )

    output = tmp_path / "edge-sheet-names.xlsx"
    break_table(df, output, sheet_by="sheet")

    workbook = load_workbook(output, read_only=True, data_only=True)
    try:
        expected_names = [
            "sheet_NA",
            "sheet_value",
            "XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
            "XXXXXXXXXXXXXXXXXXXXXXXXXXX (2)",
            "XXXXXXXXXXXXXXXXXXXXXXXXXXX (3)",
        ]
        assert set(workbook.sheetnames) == set(expected_names)
        assert [name for name in workbook.sheetnames if name.startswith("X")] == expected_names[2:]
    finally:
        workbook.close()
