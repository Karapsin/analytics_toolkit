# magnit_utils.excel

Excel helpers for converting long-format data into one or more report tables.

## Available Helpers

- `break_into_tables`: pivot a dataframe and write the resulting table set to Excel

## Example

```python
from magnit_utils.excel import break_into_tables

tables = break_into_tables(
    df=dataframe,
    label="metric",
    value="value",
    output="report.xlsx",
    sheet="summary",
    date="dt",
    rows="labels",
    columns="dates",
)
```

The function returns the written dataframes, which makes it convenient for tests or
for callers that need both the Excel file and the transformed tables.
