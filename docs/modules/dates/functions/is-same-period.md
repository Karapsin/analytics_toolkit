[All date functions](index.md)

# is_same_period

Return whether two dates fall in the same period.

```python
is_same_period(left_dt, right_dt, period="month")
```

## Inputs

- `left_dt` - first date as an ISO string, `date`, or `datetime`
- `right_dt` - second date as an ISO string, `date`, or `datetime`
- `period` - period to compare: `"week"`, `"month"`, or `"quarter"`

## Usage

```python
from analytics_toolkit.dates import is_same_period

same_month = is_same_period("2026-05-01", "2026-05-31")
same_quarter = is_same_period("2026-04-01", "2026-06-30", period="quarter")
```

Output example:

```python
same_month
# True

same_quarter
# True
```

## Notes

- Dates are compared after normalizing each value to the requested period start.
- `datetime` inputs are compared by calendar date and ignore the time component.

[All date functions](index.md)
