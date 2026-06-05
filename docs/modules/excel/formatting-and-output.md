[Excel helpers index](index.md)

# Formatting and Output

When writing multiple dataframes with
[pivot_and_break_table](functions/pivot-and-break-table.md), pass
`enforce_same_row_order=True` to align each later dataframe's pivoted row-label
order to the first dataframe. Missing row labels are written as blank rows;
extra row labels in later dataframes raise a `ValueError`.

Pass `prettify=True` to apply row-level numeric display formats in the Excel
file. Returned dataframes and raw Excel cell values are unchanged. For each
table body row, text cells are ignored and only numeric cells are formatted:

- all numeric values in `0..1`: two-decimal percentages, `0.00%`
- otherwise all numeric values in `-100..100`: two-decimal numbers, `0.00`
- otherwise: whole numbers with thousands grouping, `#,##0`

Rows with no numeric values and all header or group-title cells keep their
default formatting.

```python
wide_tables = pivot_and_break_table(
    df=wide_dataframe,
    rows="metric",
    value=["users", "arpu"],
    output="wide_report.xlsx",
    columns="ab_group",
    break_by="qr_group",
    sheet_by="start_dt",
)

auto_value_tables = pivot_and_break_table(
    df=wide_dataframe,
    rows="metric",
    output="auto_value_report.xlsx",
    columns="ab_group",
    break_by="qr_group",
    sheet_by="start_dt",
)

raw_tables = break_table(
    df=dataframe,
    output="raw_report.xlsx",
    break_by="qr_group",
    sheet_by="start_dt",
)

combined_tables = pivot_and_break_table(
    df=[dataframe_a, dataframe_b],
    rows="metric",
    value="value",
    output="combined_report.xlsx",
    columns="ab_group",
    break_by="qr_group",
    sheet_by="start_dt",
    prettify=True,
)
```

[pivot_and_break_table](functions/pivot-and-break-table.md) and
[break_table](functions/break-table.md) return the written dataframes grouped
by the original `sheet_by` values, which makes them convenient for tests or for
callers that need both the Excel file and the transformed tables. When a list of
dataframes is passed, each sheet places the first dataframe's tables in the left
block, the second dataframe's tables in the next block to the right with one
blank column between blocks, and so on.

[Excel helpers index](index.md)
