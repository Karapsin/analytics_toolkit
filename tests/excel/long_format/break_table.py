from __future__ import annotations

from tests.excel._support.long_format import (
    Decimal,
    Path,
    break_table,
    load_workbook,
    pd,
    pytest,
)


def test_break_table_accepts_multiple_dataframes_side_by_side(tmp_path: Path) -> None:
    first_df = pd.DataFrame(
        {
            "metric": ["users", "users"],
            "ab_group": ["control", "test_1"],
            "qr_group": ["ALL", "ALL"],
            "start_dt": ["2026-03-30", "2026-03-30"],
            "value": [100, 110],
        }
    )
    second_df = pd.DataFrame(
        {
            "metric": ["arpu", "arpu"],
            "ab_group": ["control", "test_1"],
            "qr_group": ["ALL", "ALL"],
            "start_dt": ["2026-03-30", "2026-03-30"],
            "value": [2.5, 2.7],
        }
    )

    output = tmp_path / "multi_df_raw.xlsx"
    tables = break_table(
        df=[first_df, second_df],
        output=output,
        break_by="qr_group",
        sheet_by="start_dt",
    )

    assert tables["2026-03-30"][0][0].to_dict(orient="records") == [
        {"metric": "users", "ab_group": "control", "value": 100},
        {"metric": "users", "ab_group": "test_1", "value": 110},
    ]
    assert tables["2026-03-30"][1][0].to_dict(orient="records") == [
        {"metric": "arpu", "ab_group": "control", "value": 2.5},
        {"metric": "arpu", "ab_group": "test_1", "value": 2.7},
    ]

    workbook = load_workbook(output, read_only=True, data_only=True)
    try:
        rows = list(workbook["2026-03-30"].iter_rows(values_only=True))
        assert rows[:4] == [
            ("ALL", None, None, None, "ALL", None, None),
            ("metric", "ab_group", "value", None, "metric", "ab_group", "value"),
            ("users", "control", 100, None, "arpu", "control", 2.5),
            ("users", "test_1", 110, None, "arpu", "test_1", 2.7),
        ]
    finally:
        workbook.close()


def test_break_table_aligns_headers_by_table_index_across_multiple_dataframes(
    tmp_path: Path,
) -> None:
    first_df = pd.DataFrame(
        {
            "metric": ["users", "orders", "users"],
            "ab_group": ["control", "control", "test_1"],
            "qr_group": ["ALL", "ALL", "B"],
            "start_dt": ["2026-03-30", "2026-03-30", "2026-03-30"],
            "value": [100, 50, 110],
        }
    )
    second_df = pd.DataFrame(
        {
            "metric": ["arpu", "margin", "arpu", "margin"],
            "ab_group": ["control", "control", "test_1", "test_1"],
            "qr_group": ["ALL", "ALL", "B", "B"],
            "start_dt": ["2026-03-30"] * 4,
            "value": [2.5, 0.2, 2.7, 0.25],
        }
    )

    output = tmp_path / "aligned_multi_df_raw.xlsx"
    break_table(
        df=[first_df, second_df],
        output=output,
        break_by="qr_group",
        sheet_by="start_dt",
    )

    workbook = load_workbook(output, read_only=True, data_only=True)
    try:
        rows = list(workbook["2026-03-30"].iter_rows(values_only=True))
        assert rows[0] == ("ALL", None, None, None, "ALL", None, None)
        assert rows[1] == ("metric", "ab_group", "value", None, "metric", "ab_group", "value")
        assert rows[6] == ("B", None, None, None, "B", None, None)
        assert rows[7] == ("metric", "ab_group", "value", None, "metric", "ab_group", "value")
    finally:
        workbook.close()


def test_break_table_does_not_apply_custom_formats_by_default(tmp_path: Path) -> None:
    df = pd.DataFrame(
        {
            "metric": ["conversion", "users"],
            "control": [0.125, 1000],
            "test_1": [0.75, 50],
        }
    )

    output = tmp_path / "plain_raw.xlsx"
    break_table(
        df=df,
        output=output,
    )

    workbook = load_workbook(output, read_only=False, data_only=True)
    try:
        sheet = workbook["Sheet1"]
        assert sheet["B2"].number_format == "General"
        assert sheet["C2"].number_format == "General"
        assert sheet["B3"].number_format == "General"
        assert sheet["C3"].number_format == "General"
    finally:
        workbook.close()


def test_break_table_handles_disjoint_sheets_and_special_numeric_values(tmp_path: Path) -> None:
    first = pd.DataFrame(
        {"sheet": ["first"], "label": ["flag"], "flag": [True], "missing": [float("nan")]}
    )
    second = pd.DataFrame(
        {"sheet": ["second"], "label": ["ratio"], "flag": [0.5], "missing": [None]}
    )

    output = tmp_path / "disjoint-sheets.xlsx"
    break_table([first, second], output, sheet_by="sheet", prettify=True)

    workbook = load_workbook(output, read_only=False, data_only=True)
    try:
        assert workbook.sheetnames == ["first", "second"]
        assert workbook["first"]["B2"].number_format == "General"
        assert workbook["first"]["C2"].number_format == "General"
        assert workbook["second"]["B2"].number_format == "0.00%"
    finally:
        workbook.close()


def test_break_table_places_sparse_dataframe_groups_at_stable_coordinates(tmp_path: Path) -> None:
    first = pd.DataFrame({"metric": ["users", "orders"], "break": ["A", "B"], "value": [1, 2]})
    second = pd.DataFrame({"metric": ["margin"], "break": ["A"], "value": [0.5]})

    output = tmp_path / "sparse-groups.xlsx"
    break_table([first, second], output, break_by="break")

    workbook = load_workbook(output, read_only=False, data_only=True)
    try:
        sheet = workbook["Sheet1"]
        assert sheet["A1"].value == "A"
        assert sheet["D1"].value == "A"
        assert sheet["A6"].value == "B"
        assert sheet["D6"].value is None
    finally:
        workbook.close()


def test_break_table_prettify_formats_numeric_cells_by_body_row(tmp_path: Path) -> None:
    df = pd.DataFrame(
        {
            "metric": ["conversion", "mixed", "margin", "users", "comment"],
            "note": ["all_numeric", "has_text", "bounded", "large", "text_only"],
            "control": [0.125, "n/a", -10, 1000, "n/a"],
            "test_1": [0.75, 0.4, 100, 50, "missing"],
        }
    )

    output = tmp_path / "prettified_raw.xlsx"
    tables = break_table(
        df=df,
        output=output,
        prettify=True,
    )

    assert tables[None][0].equals(df)

    workbook = load_workbook(output, read_only=False, data_only=True)
    try:
        sheet = workbook["Sheet1"]
        assert sheet["A2"].number_format == "General"
        assert sheet["B2"].number_format == "General"
        assert sheet["C2"].number_format == "0.00%"
        assert sheet["D2"].number_format == "0.00%"

        assert sheet["C3"].number_format == "General"
        assert sheet["D3"].number_format == "0.00%"

        assert sheet["C4"].number_format == "0.00"
        assert sheet["D4"].number_format == "0.00"

        assert sheet["C5"].number_format == "#,##0"
        assert sheet["D5"].number_format == "#,##0"

        assert sheet["C6"].number_format == "General"
        assert sheet["D6"].number_format == "General"
    finally:
        workbook.close()


def test_break_table_rejects_missing_and_colliding_group_columns(tmp_path: Path) -> None:
    df = pd.DataFrame({"metric": ["users"]})

    with pytest.raises(ValueError, match="missing required columns"):
        break_table(df, tmp_path / "missing-break.xlsx", break_by="missing")
    with pytest.raises(ValueError, match="different dataframe columns"):
        break_table(df, tmp_path / "colliding-break.xlsx", break_by="metric", sheet_by="metric")


def test_break_table_writes_decimal_values_as_numeric_excel_cells(tmp_path: Path) -> None:
    df = pd.DataFrame(
        {
            "metric": ["arpu"],
            "ab_group": ["control"],
            "qr_group": ["ALL"],
            "start_dt": ["2026-03-30"],
            "value": [Decimal("72.4867078207333238")],
        }
    )

    output = tmp_path / "decimal_raw.xlsx"
    break_table(
        df=df,
        output=output,
        break_by="qr_group",
        sheet_by="start_dt",
    )

    workbook = load_workbook(output, read_only=False, data_only=True)
    try:
        cell = workbook["2026-03-30"]["C3"]
        assert cell.data_type == "n"
        assert cell.value == pytest.approx(72.4867078207333238)
    finally:
        workbook.close()


def test_break_table_writes_grouped_raw_tables_without_uniqueness_checks(tmp_path: Path) -> None:
    df = pd.DataFrame(
        {
            "metric": ["users", "users", "users", "users"],
            "ab_group": ["control", "control", "test_1", "test_1"],
            "qr_group": ["ALL", "ALL", "ALL", "ALL"],
            "start_dt": ["2026-03-30"] * 4,
            "value": [100, 120, 110, 130],
        }
    )

    output = tmp_path / "raw_tables.xlsx"
    tables = break_table(
        df=df,
        output=output,
        break_by="qr_group",
        sheet_by="start_dt",
    )

    assert list(tables) == ["2026-03-30"]
    assert tables["2026-03-30"][0].to_dict(orient="records") == [
        {"metric": "users", "ab_group": "control", "value": 100},
        {"metric": "users", "ab_group": "control", "value": 120},
        {"metric": "users", "ab_group": "test_1", "value": 110},
        {"metric": "users", "ab_group": "test_1", "value": 130},
    ]

    workbook = load_workbook(output, read_only=True, data_only=True)
    try:
        assert workbook.sheetnames == ["2026-03-30"]
        rows = list(workbook["2026-03-30"].iter_rows(values_only=True))
        assert rows[:6] == [
            ("ALL", None, None),
            ("metric", "ab_group", "value"),
            ("users", "control", 100),
            ("users", "control", 120),
            ("users", "test_1", 110),
            ("users", "test_1", 130),
        ]
    finally:
        workbook.close()
