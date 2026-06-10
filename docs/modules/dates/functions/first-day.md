[All date functions](index.md)

# first_day

Return the first day of the containing week, month, or quarter.

```python
first_day(dt, period="month", output_string=True)
```

## Inputs

- `dt` - input date as an ISO string, `date`, or `datetime`
- `period` - `"week"`, `"month"`, or `"quarter"`
- `output_string` - when `True`, return an ISO string; when `False`, return a midnight `datetime`

## Usage

```python
from analytics_toolkit.dates import first_day

month_start = first_day("2026-04-10")
week_start = first_day("2026-04-10", period="week")
```

Output example:

```python
month_start
# '2026-04-01'

week_start
# '2026-04-06'
```

[All date functions](index.md)
