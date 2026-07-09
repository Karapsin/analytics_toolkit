[Functions index](index.md)

# hours_between

Count signed whole hours between two timestamps.

```python
hours_between(start_dt, end_dt, inclusive=False)
```

## Inputs

- `start_dt` - start timestamp as an ISO string, `date`, or timezone-naive `datetime`
- `end_dt` - end timestamp as an ISO string, `date`, or timezone-naive `datetime`
- `inclusive` - when `True`, add one hour in the count direction

## Usage

```python
from analytics_toolkit import datetime as dttm

hours = dttm.hours_between("2026-01-01 12:13:15", "2026-01-01 14:13:14")
```

Output example:

```python
hours
# 1
```

[Functions index](index.md)
