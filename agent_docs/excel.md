# Excel Agent Instructions

Read this file for Excel helper code, tests, docs, API explanation, or behavior
investigation.

## Excel Contracts

- `pivot_and_break_table` and `break_table` accept either one dataframe or a sequence of dataframes.
- Preserve sheet grouping order, table order, side-by-side placement for multiple dataframes, and blank spacing between table blocks.
- Preserve sheet-name sanitization, 31-character truncation, and deduplication for append mode.
- Decimal values are coerced to floats before writing to Excel.
- `enforce_same_row_order=True` aligns later dataframe tables to the first dataframe and rejects extra row labels.
