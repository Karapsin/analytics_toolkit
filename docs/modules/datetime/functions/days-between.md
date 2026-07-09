[Functions index](index.md)

# days_between

Count signed whole days between two timestamps.

```python
days_between(start_dt, end_dt, inclusive=False)
```

## Inputs

- `start_dt` - start timestamp as an ISO string, `date`, or timezone-naive `datetime`
- `end_dt` - end timestamp as an ISO string, `date`, or timezone-naive `datetime`
- `inclusive` - when `True`, add one day in the count direction

## Usage

```python
from analytics_toolkit import datetime as dttm

days = dttm.days_between("2026-01-01 12:13:15", "2026-01-03 12:13:14")
```

Output example:

```python
days
# 1
```

[Functions index](index.md)
