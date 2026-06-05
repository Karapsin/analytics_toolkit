[Excel helpers index](index.md)

# Report Table Workflow

Excel report generation starts from a dataframe that already contains the
reporting dimensions and metric values. Use `columns` for the dimension that
should become table columns, `break_by` for separate tables inside a sheet, and
`sheet_by` for separate worksheets.

```python
from analytics_toolkit.excel import break_table, pivot_and_break_table

pivoted_tables = pivot_and_break_table(
    df=dataframe,
    rows="metric",
    value="value",
    output="report.xlsx",
    columns="ab_group",
    break_by="qr_group",
    sheet_by="start_dt",
)
```

By default [pivot_and_break_table](functions/pivot-and-break-table.md) and
[break_table](functions/break-table.md) replace an existing `output` workbook.
Pass `append=True` to keep the existing file and add new sheets. Sheet names are
sanitized, truncated to 31 characters, and deduplicated against existing workbook
sheets.

Use [pivot_and_break_table](functions/pivot-and-break-table.md) when the input
is long-format data that should be pivoted before writing. Use
[break_table](functions/break-table.md) when the dataframe already has the table
shape that should appear in Excel.

[Excel helpers index](index.md)
