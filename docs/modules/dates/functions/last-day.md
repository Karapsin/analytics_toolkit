[All date functions](index.md)

# last_day

Return the last day of the containing week, month, or quarter.

```python
last_day(dt, period="month", output_string=True)
```

## Inputs

- `dt`: Input date as an ISO string, `date`, or `datetime`.
- `period`: `"week"`, `"month"`, or `"quarter"`.
- `output_string`: When `True`, return an ISO string; when `False`, return a midnight `datetime`.

## Usage

```python
from analytics_toolkit.dates import last_day

month_end = last_day("2026-04-10")
quarter_end = last_day("2026-04-10", period="quarter")
```

[All date functions](index.md)
