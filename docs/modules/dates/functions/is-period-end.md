[All date functions](index.md)

# is_period_end

Return whether a date is the last day of a period.

```python
is_period_end(dt, period="month")
```

## Inputs

- `dt` - input date as an ISO string, `date`, or `datetime`
- `period` - period to check: `"week"`, `"month"`, or `"quarter"`

## Usage

```python
from analytics_toolkit.dates import is_period_end

month_end = is_period_end("2026-05-31")
week_end = is_period_end("2026-05-24", period="week")
```

Output example:

```python
month_end
# True

week_end
# True
```

## Notes

- Weeks end on Sunday.
- `datetime` inputs are checked by calendar date and ignore the time component.

[All date functions](index.md)
