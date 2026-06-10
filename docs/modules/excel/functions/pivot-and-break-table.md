[All Excel functions](index.md)

# pivot_and_break_table

Pivot long-format data and write one or more grouped tables to Excel.

```python
pivot_and_break_table(
    df,
    rows,
    output,
    value=None,
    columns=None,
    break_by=None,
    sheet_by=None,
    append=False,
    enforce_same_row_order=False,
    prettify=False,
)
```

## Inputs

- `df` - one dataframe or a sequence of dataframes
- `rows` - single column used as pivot row labels. When `value` is a sequence or omitted, this is the synthetic metric-name column
- `output` - workbook path for Excel output
- `value` - value column or columns. If omitted, value columns are inferred
- `columns` - optional pivot column
- `break_by` - optional column used to split tables within each sheet
- `sheet_by` - optional column used to split output into sheets
- `append` - whether to append sheets to an existing workbook
- `enforce_same_row_order` - whether later dataframe tables follow the first
  dataframe's row-label order
- `prettify` - whether to apply row-level numeric display formats

## Usage

```python
from analytics_toolkit.excel import pivot_and_break_table

tables = pivot_and_break_table(
    df=report_df,
    rows="metric",
    value="value",
    output="report.xlsx",
    columns="ab_group",
    break_by="segment",
    sheet_by="report_date",
    prettify=True,
)
```

Output example:

```python
tables.keys()
# dict_keys(['2026-06-01', '2026-06-02'])

tables["2026-06-01"][0].head()
# ab_group   metric  control  test_1
# 0          orders      120     133
# 1             ctr     0.08    0.09
```

## Notes

- Returned dataframes and raw Excel cell values are unchanged by `prettify`.
- A sequence of dataframes is written side by side on each sheet.
- `rows` must not conflict with grouping columns or an existing non-value column in multi-value mode.
- A single dataframe returns `{sheet_value: [table, ...]}`. A sequence returns
  `{sheet_value: [[tables_for_df1], [tables_for_df2], ...]}`. `None` is used as
  the key when `sheet_by` is omitted.

[All Excel functions](index.md)
