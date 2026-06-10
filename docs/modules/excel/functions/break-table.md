[All Excel functions](index.md)

# break_table

Write dataframe tables to Excel without pivoting.

```python
break_table(
    df,
    output,
    break_by=None,
    sheet_by=None,
    append=False,
    prettify=False,
)
```

## Inputs

- `df` - one dataframe or a sequence of dataframes
- `output` - workbook path for Excel output
- `break_by` - optional column used to split tables within each sheet
- `sheet_by` - optional column used to split output into sheets
- `append` - whether to append sheets to an existing workbook
- `prettify` - whether to apply row-level numeric display formats

## Usage

```python
from analytics_toolkit.excel import break_table

tables = break_table(
    df=report_df,
    output="raw_report.xlsx",
    break_by="segment",
    sheet_by="report_date",
)
```

Output example:

```python
tables.keys()
# dict_keys(['2026-06-01', '2026-06-02'])

tables["2026-06-01"][0].head()
#   report_date segment  metric  value
# 0  2026-06-01     new  orders    120
```

## Notes

- Use this helper when your dataframe already has the desired table shape.
- A sequence of dataframes is written side by side on each sheet.
- A single dataframe returns `{sheet_value: [table, ...]}`. A sequence returns
  `{sheet_value: [[tables_for_df1], [tables_for_df2], ...]}`. `None` is used as
  the key when `sheet_by` is omitted.

[All Excel functions](index.md)
