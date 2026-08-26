from __future__ import annotations

from tests.excel._support.long_format import (
    Decimal,
    Path,
    load_workbook,
    pd,
    pivot_and_break_table,
    pytest,
)


def test_pivot_and_break_table_accepts_multiple_dataframes_side_by_side(tmp_path: Path) -> None:
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

    output = tmp_path / "multi_df_pivot.xlsx"
    tables = pivot_and_break_table(
        df=[first_df, second_df],
        rows="metric",
        value="value",
        output=output,
        columns="ab_group",
        break_by="qr_group",
        sheet_by="start_dt",
    )

    assert tables["2026-03-30"][0][0].to_dict(orient="records") == [
        {"metric": "users", "control": 100, "test_1": 110},
    ]
    assert tables["2026-03-30"][1][0].to_dict(orient="records") == [
        {"metric": "arpu", "control": 2.5, "test_1": 2.7},
    ]

    workbook = load_workbook(output, read_only=True, data_only=True)
    try:
        rows = list(workbook["2026-03-30"].iter_rows(values_only=True))
        assert rows[:3] == [
            ("ALL", None, None, None, "ALL", None, None),
            ("metric", "control", "test_1", None, "metric", "control", "test_1"),
            ("users", 100, 110, None, "arpu", 2.5, 2.7),
        ]
    finally:
        workbook.close()


def test_pivot_and_break_table_accepts_multiple_value_columns(tmp_path: Path) -> None:
    df = pd.DataFrame(
        {
            "start_dt": ["2026-03-30", "2026-03-30"],
            "qr_group": ["ALL", "ALL"],
            "ab_group": ["control", "test_1"],
            "users": [100, 110],
            "arpu": [2.5, 2.7],
        }
    )

    output = tmp_path / "multi_value.xlsx"
    tables = pivot_and_break_table(
        df=df,
        rows="metric",
        value=["users", "arpu"],
        output=output,
        columns="ab_group",
        break_by="qr_group",
        sheet_by="start_dt",
    )

    assert tables["2026-03-30"][0].to_dict(orient="records") == [
        {"metric": "users", "control": 100.0, "test_1": 110.0},
        {"metric": "arpu", "control": 2.5, "test_1": 2.7},
    ]

    workbook = load_workbook(output, read_only=True, data_only=True)
    try:
        rows = list(workbook["2026-03-30"].iter_rows(values_only=True))
        assert rows[:4] == [
            ("ALL", None, None),
            ("metric", "control", "test_1"),
            ("users", 100, 110),
            ("arpu", 2.5, 2.7),
        ]
    finally:
        workbook.close()


def test_pivot_and_break_table_appends_new_sheets_when_requested(tmp_path: Path) -> None:
    output = tmp_path / "append.xlsx"

    first_df = pd.DataFrame(
        {
            "metric": ["users", "users"],
            "ab_group": ["control", "test_1"],
            "start_dt": ["2026-03-30", "2026-03-30"],
            "value": [100, 110],
        }
    )
    second_df = pd.DataFrame(
        {
            "metric": ["arpu", "arpu"],
            "ab_group": ["control", "test_1"],
            "start_dt": ["2026-04-01", "2026-04-01"],
            "value": [2.5, 2.7],
        }
    )

    pivot_and_break_table(
        df=first_df,
        rows="metric",
        value="value",
        output=output,
        columns="ab_group",
        sheet_by="start_dt",
    )
    pivot_and_break_table(
        df=second_df,
        rows="metric",
        value="value",
        output=output,
        columns="ab_group",
        sheet_by="start_dt",
        append=True,
    )

    workbook = load_workbook(output, read_only=True, data_only=True)
    try:
        assert workbook.sheetnames == ["2026-03-30", "2026-04-01"]
    finally:
        workbook.close()


def test_pivot_and_break_table_detects_value_columns_when_omitted(tmp_path: Path) -> None:
    df = pd.DataFrame(
        {
            "start_dt": ["2026-03-30", "2026-03-30"],
            "qr_group": ["ALL", "ALL"],
            "ab_group": ["control", "test_1"],
            "users": [100, 110],
            "arpu": [2.5, 2.7],
        }
    )

    output = tmp_path / "auto_value.xlsx"
    tables = pivot_and_break_table(
        df=df,
        rows="metric",
        output=output,
        columns="ab_group",
        break_by="qr_group",
        sheet_by="start_dt",
    )

    assert tables["2026-03-30"][0].to_dict(orient="records") == [
        {"metric": "users", "control": 100.0, "test_1": 110.0},
        {"metric": "arpu", "control": 2.5, "test_1": 2.7},
    ]


def test_pivot_and_break_table_enforces_same_row_order_by_reordering(tmp_path: Path) -> None:
    first_df = pd.DataFrame(
        {
            "metric": ["users", "users", "arpu", "arpu"],
            "ab_group": ["control", "test_1", "control", "test_1"],
            "start_dt": ["2026-03-30"] * 4,
            "value": [100, 110, 2.5, 2.7],
        }
    )
    second_df = pd.DataFrame(
        {
            "metric": ["arpu", "arpu", "users", "users"],
            "ab_group": ["control", "test_1", "control", "test_1"],
            "start_dt": ["2026-03-30"] * 4,
            "value": [3.1, 3.3, 200, 220],
        }
    )

    output = tmp_path / "enforced_row_order_reordered.xlsx"
    tables = pivot_and_break_table(
        df=[first_df, second_df],
        rows="metric",
        value="value",
        output=output,
        columns="ab_group",
        sheet_by="start_dt",
        enforce_same_row_order=True,
    )

    assert tables["2026-03-30"][1][0].to_dict(orient="records") == [
        {"metric": "users", "control": 200.0, "test_1": 220.0},
        {"metric": "arpu", "control": 3.1, "test_1": 3.3},
    ]


def test_pivot_and_break_table_enforces_same_row_order_with_padding(tmp_path: Path) -> None:
    first_df = pd.DataFrame(
        {
            "metric": ["users", "users", "arpu", "arpu"],
            "ab_group": ["control", "test_1", "control", "test_1"],
            "start_dt": ["2026-03-30"] * 4,
            "value": [100, 110, 2.5, 2.7],
        }
    )
    second_df = pd.DataFrame(
        {
            "metric": ["arpu", "arpu"],
            "ab_group": ["control", "test_1"],
            "start_dt": ["2026-03-30", "2026-03-30"],
            "value": [3.1, 3.3],
        }
    )

    output = tmp_path / "enforced_row_order_padding.xlsx"
    tables = pivot_and_break_table(
        df=[first_df, second_df],
        rows="metric",
        value="value",
        output=output,
        columns="ab_group",
        sheet_by="start_dt",
        enforce_same_row_order=True,
    )

    assert tables["2026-03-30"][0][0].to_dict(orient="records") == [
        {"metric": "users", "control": 100.0, "test_1": 110.0},
        {"metric": "arpu", "control": 2.5, "test_1": 2.7},
    ]
    second_table = tables["2026-03-30"][1][0]
    assert second_table["metric"].tolist() == ["users", "arpu"]
    assert pd.isna(second_table.iloc[0]["control"])
    assert pd.isna(second_table.iloc[0]["test_1"])
    assert second_table.iloc[1]["control"] == 3.1
    assert second_table.iloc[1]["test_1"] == 3.3

    workbook = load_workbook(output, read_only=True, data_only=True)
    try:
        rows = list(workbook["2026-03-30"].iter_rows(values_only=True))
        assert rows[:3] == [
            ("metric", "control", "test_1", None, "metric", "control", "test_1"),
            ("users", 100, 110, None, "users", None, None),
            ("arpu", 2.5, 2.7, None, "arpu", 3.1, 3.3),
        ]
    finally:
        workbook.close()


def test_pivot_and_break_table_prettify_formats_pivoted_body_rows(tmp_path: Path) -> None:
    df = pd.DataFrame(
        {
            "metric": ["conversion", "conversion", "margin", "margin", "users", "users"],
            "ab_group": ["control", "test_1", "control", "test_1", "control", "test_1"],
            "value": [0.125, 0.75, -10, 100, 1000, 50],
        }
    )

    output = tmp_path / "prettified_pivot.xlsx"
    tables = pivot_and_break_table(
        df=df,
        rows="metric",
        value="value",
        output=output,
        columns="ab_group",
        prettify=True,
    )

    assert tables[None][0].to_dict(orient="records") == [
        {"metric": "conversion", "control": 0.125, "test_1": 0.75},
        {"metric": "margin", "control": -10.0, "test_1": 100.0},
        {"metric": "users", "control": 1000.0, "test_1": 50.0},
    ]

    workbook = load_workbook(output, read_only=False, data_only=True)
    try:
        sheet = workbook["Sheet1"]
        assert sheet["A2"].number_format == "General"
        assert sheet["B2"].number_format == "0.00%"
        assert sheet["C2"].number_format == "0.00%"
        assert sheet["B3"].number_format == "0.00"
        assert sheet["C3"].number_format == "0.00"
        assert sheet["B4"].number_format == "#,##0"
        assert sheet["C4"].number_format == "#,##0"
    finally:
        workbook.close()


def test_pivot_and_break_table_rejects_duplicates_within_group_slices(tmp_path: Path) -> None:
    df = pd.DataFrame(
        {
            "metric": ["users", "users"],
            "ab_group": ["control", "control"],
            "start_dt": ["2026-03-30", "2026-03-30"],
            "value": [100, 120],
        }
    )

    with pytest.raises(ValueError, match="not unique"):
        pivot_and_break_table(
            df=df,
            rows="metric",
            value="value",
            output=tmp_path / "duplicates.xlsx",
            columns="ab_group",
            sheet_by="start_dt",
        )


def test_pivot_and_break_table_rejects_extra_rows_when_enforcing_same_row_order(
    tmp_path: Path,
) -> None:
    first_df = pd.DataFrame(
        {
            "metric": ["users", "users"],
            "ab_group": ["control", "test_1"],
            "start_dt": ["2026-03-30", "2026-03-30"],
            "value": [100, 110],
        }
    )
    second_df = pd.DataFrame(
        {
            "metric": ["users", "users", "arpu", "arpu"],
            "ab_group": ["control", "test_1", "control", "test_1"],
            "start_dt": ["2026-03-30"] * 4,
            "value": [200, 220, 3.1, 3.3],
        }
    )

    with pytest.raises(ValueError, match="extra row labels"):
        pivot_and_break_table(
            df=[first_df, second_df],
            rows="metric",
            value="value",
            output=tmp_path / "enforced_row_order_error.xlsx",
            columns="ab_group",
            sheet_by="start_dt",
            enforce_same_row_order=True,
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"value": "missing"}, "missing required columns"),
        ({"value": "value", "columns": "metric"}, "different dataframe columns"),
        ({"value": []}, "at least one"),
        ({"value": ["value", 1]}, "sequence of column names"),
        ({"value": ["value", "value"]}, "duplicate columns"),
    ],
)
def test_pivot_and_break_table_rejects_invalid_column_specs(
    kwargs: dict[str, object],
    message: str,
    tmp_path: Path,
) -> None:
    df = pd.DataFrame({"metric": ["users"], "value": [1]})

    with pytest.raises(ValueError, match=message):
        pivot_and_break_table(
            df,
            rows="metric",
            output=tmp_path / "invalid-columns.xlsx",
            **kwargs,  # type: ignore[arg-type]
        )


def test_pivot_and_break_table_rejects_multi_value_row_role_collisions(tmp_path: Path) -> None:
    df = pd.DataFrame({"group": ["A"], "metric": ["existing"], "users": [1], "orders": [2]})

    with pytest.raises(ValueError, match="grouping column"):
        pivot_and_break_table(
            df,
            rows="group",
            value=["users", "orders"],
            columns="group",
            output=tmp_path / "group-collision.xlsx",
        )
    with pytest.raises(ValueError, match="already exists"):
        pivot_and_break_table(
            df,
            rows="metric",
            value=["users", "orders"],
            output=tmp_path / "existing-row.xlsx",
        )


def test_pivot_and_break_table_replaces_existing_workbook_by_default(tmp_path: Path) -> None:
    output = tmp_path / "replace.xlsx"

    first_df = pd.DataFrame(
        {
            "metric": ["users", "users"],
            "ab_group": ["control", "test_1"],
            "start_dt": ["2026-03-30", "2026-03-30"],
            "value": [100, 110],
        }
    )
    second_df = pd.DataFrame(
        {
            "metric": ["arpu", "arpu"],
            "ab_group": ["control", "test_1"],
            "start_dt": ["2026-04-01", "2026-04-01"],
            "value": [2.5, 2.7],
        }
    )

    pivot_and_break_table(
        df=first_df,
        rows="metric",
        value="value",
        output=output,
        columns="ab_group",
        sheet_by="start_dt",
    )
    pivot_and_break_table(
        df=second_df,
        rows="metric",
        value="value",
        output=output,
        columns="ab_group",
        sheet_by="start_dt",
    )

    workbook = load_workbook(output, read_only=True, data_only=True)
    try:
        assert workbook.sheetnames == ["2026-04-01"]
        rows = list(workbook["2026-04-01"].iter_rows(values_only=True))
        assert rows[:2] == [
            ("metric", "control", "test_1"),
            ("arpu", 2.5, 2.7),
        ]
    finally:
        workbook.close()


def test_pivot_and_break_table_sanitizes_and_deduplicates_sheet_names(tmp_path: Path) -> None:
    df = pd.DataFrame(
        {
            "metric": ["users", "users", "users", "users"],
            "ab_group": ["control", "test_1", "control", "test_1"],
            "sheet_bucket": [
                "Report/Name:One*?",
                "Report/Name:One*?",
                "X" * 40,
                "X" * 40 + " trailing",
            ],
            "value": [1, 2, 3, 4],
        }
    )

    output = tmp_path / "sanitized.xlsx"
    pivot_and_break_table(
        df=df,
        rows="metric",
        value="value",
        output=output,
        columns="ab_group",
        sheet_by="sheet_bucket",
    )

    workbook = load_workbook(output, read_only=True, data_only=True)
    try:
        assert workbook.sheetnames == [
            "Report_Name_One__",
            "XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
            "XXXXXXXXXXXXXXXXXXXXXXXXXXX (2)",
        ]
    finally:
        workbook.close()


def test_pivot_and_break_table_writes_decimal_values_as_numeric_excel_cells(
    tmp_path: Path,
) -> None:
    df = pd.DataFrame(
        {
            "metric": ["arpu", "arpu"],
            "ab_group": ["control", "test_1"],
            "start_dt": ["2026-03-30", "2026-03-30"],
            "value": [Decimal("72.4867078207333238"), Decimal("70.6603563524410553")],
        }
    )

    output = tmp_path / "decimal_pivot.xlsx"
    pivot_and_break_table(
        df=df,
        rows="metric",
        value="value",
        output=output,
        columns="ab_group",
        sheet_by="start_dt",
    )

    workbook = load_workbook(output, read_only=False, data_only=True)
    try:
        control_cell = workbook["2026-03-30"]["B2"]
        test_cell = workbook["2026-03-30"]["C2"]
        assert control_cell.data_type == "n"
        assert test_cell.data_type == "n"
        assert control_cell.value == pytest.approx(72.4867078207333238)
        assert test_cell.value == pytest.approx(70.6603563524410553)
    finally:
        workbook.close()


def test_pivot_and_break_table_writes_multiple_sheets_and_tables(tmp_path: Path) -> None:
    df = pd.DataFrame(
        {
            "metric": ["users", "users", "arpu", "arpu", "users", "users", "arpu", "arpu"],
            "ab_group": [
                "control",
                "test_1",
                "control",
                "test_1",
                "control",
                "test_1",
                "control",
                "test_1",
            ],
            "qr_group": ["ALL", "ALL", "ALL", "ALL", "1", "1", "1", "1"],
            "start_dt": [
                "2026-03-30",
                "2026-03-30",
                "2026-03-30",
                "2026-03-30",
                "2026-04-01",
                "2026-04-01",
                "2026-04-01",
                "2026-04-01",
            ],
            "value": [100, 110, 2.5, 2.7, 50, 55, 1.1, 1.2],
        }
    )

    output = tmp_path / "report.xlsx"
    tables = pivot_and_break_table(
        df=df,
        rows="metric",
        value="value",
        output=output,
        columns="ab_group",
        break_by="qr_group",
        sheet_by="start_dt",
    )

    assert list(tables) == ["2026-03-30", "2026-04-01"]
    assert len(tables["2026-03-30"]) == 1
    assert tables["2026-03-30"][0].columns.tolist() == ["metric", "control", "test_1"]

    workbook = load_workbook(output, read_only=True, data_only=True)
    try:
        assert workbook.sheetnames == ["2026-03-30", "2026-04-01"]
        first_sheet = workbook["2026-03-30"]
        rows = list(first_sheet.iter_rows(values_only=True))
        assert rows[:4] == [
            ("ALL", None, None),
            ("metric", "control", "test_1"),
            ("users", 100, 110),
            ("arpu", 2.5, 2.7),
        ]
    finally:
        workbook.close()


def test_pivot_without_columns_preserves_row_order(tmp_path: Path) -> None:
    df = pd.DataFrame({"metric": ["orders", "users"], "value": [2, 1]})

    tables = pivot_and_break_table(
        df,
        rows="metric",
        value="value",
        output=tmp_path / "no-columns.xlsx",
    )

    assert tables[None][0].to_dict(orient="records") == [
        {"metric": "orders", "value": 2},
        {"metric": "users", "value": 1},
    ]


def test_pivot_without_columns_rejects_conflicting_rows(tmp_path: Path) -> None:
    df = pd.DataFrame({"metric": ["users", "users"], "value": [1, 2]})

    with pytest.raises(ValueError, match="not unique"):
        pivot_and_break_table(
            df,
            rows="metric",
            value="value",
            output=tmp_path / "conflicting-rows.xlsx",
        )
