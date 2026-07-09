[All date functions](index.md)

# days_between

Count calendar days between two dates.

```python
days_between(start_dt, end_dt, inclusive=False)
```

## Inputs

- `start_dt` - start date as an ISO string, `date`, or `datetime`
- `end_dt` - end date as an ISO string, `date`, or `datetime`
- `inclusive` - when `True`, include both boundary dates in the signed count

## Usage

```python
from analytics_toolkit.dates import days_between

elapsed_days = days_between("2026-05-01", "2026-05-03")
inclusive_days = days_between("2026-05-01", "2026-05-03", inclusive=True)
```

Output example:

```python
elapsed_days
# 2

inclusive_days
# 3
```

## Notes

- Reversed inputs return negative counts.
- `datetime` inputs are compared by calendar date and ignore the time component.

[All date functions](index.md)
