[All date functions](index.md)

# periods_between

Count normalized periods between two dates.

```python
periods_between(start_dt, end_dt, interval="months")
```

## Inputs

- `start_dt` - start date as an ISO string, `date`, or `datetime`
- `end_dt` - end date as an ISO string, `date`, or `datetime`
- `interval` - period interval to count: `"weeks"`, `"months"`, or `"quarters"`

## Usage

```python
from analytics_toolkit.dates import periods_between

month_gap = periods_between("2026-02-15", "2026-05-20")
quarter_gap = periods_between("2026-02-15", "2026-10-20", interval="quarters")
```

Output example:

```python
month_gap
# 3

quarter_gap
# 3
```

## Notes

- Dates are normalized to the start of the requested interval before counting.
- Reversed inputs return negative counts.
- Use [days_between](days-between.md) for day-level counts.

[All date functions](index.md)
