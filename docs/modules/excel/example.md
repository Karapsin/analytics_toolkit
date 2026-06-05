[Excel helpers index](index.md)

# Example

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

By default both helpers replace an existing `output` workbook. Pass
`append=True` to keep the existing file and add new sheets using the current
sheet-deduplication behavior.

[Excel helpers index](index.md)
