[All date functions](index.md)

# period_bounds

Return the start and end dates for a period.

```python
period_bounds(dt, period="month", output_string=True)
```

## Inputs

- `dt` - input date as an ISO string, `date`, or `datetime`
- `period` - period to use: `"week"`, `"month"`, or `"quarter"`
- `output_string` - when `True`, return ISO strings; when `False`, return midnight `datetime` values

## Usage

```python
from analytics_toolkit.dates import period_bounds

month_window = period_bounds("2026-05-18")
quarter_window = period_bounds("2026-05-18", period="quarter")
```

Output example:

```python
month_window
# ('2026-05-01', '2026-05-31')

quarter_window
# ('2026-04-01', '2026-06-30')
```

[All date functions](index.md)
