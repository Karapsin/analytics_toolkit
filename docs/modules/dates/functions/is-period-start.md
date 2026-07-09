[All date functions](index.md)

# is_period_start

Return whether a date is the first day of a period.

```python
is_period_start(dt, period="month")
```

## Inputs

- `dt` - input date as an ISO string, `date`, or `datetime`
- `period` - period to check: `"week"`, `"month"`, or `"quarter"`

## Usage

```python
from analytics_toolkit.dates import is_period_start

month_start = is_period_start("2026-05-01")
week_start = is_period_start("2026-05-18", period="week")
```

Output example:

```python
month_start
# True

week_start
# True
```

## Notes

- Weeks start on Monday.
- `datetime` inputs are checked by calendar date and ignore the time component.

[All date functions](index.md)
