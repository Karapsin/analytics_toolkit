[Functions index](index.md)

# minutes_between

Count signed whole minutes between two timestamps.

```python
minutes_between(start_dt, end_dt, inclusive=False)
```

## Inputs

- `start_dt` - start timestamp as an ISO string, `date`, or timezone-naive `datetime`
- `end_dt` - end timestamp as an ISO string, `date`, or timezone-naive `datetime`
- `inclusive` - when `True`, add one minute in the count direction

## Usage

```python
from analytics_toolkit import datetime as dttm

minutes = dttm.minutes_between("2026-01-01 12:13:15", "2026-01-01 12:43:14")
```

Output example:

```python
minutes
# 29
```

[Functions index](index.md)
