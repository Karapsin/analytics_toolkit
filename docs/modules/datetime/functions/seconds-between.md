[Functions index](index.md)

# seconds_between

Count signed whole seconds between two timestamps.

```python
seconds_between(start_dt, end_dt, inclusive=False)
```

## Inputs

- `start_dt` - start timestamp as an ISO string, `date`, or timezone-naive `datetime`
- `end_dt` - end timestamp as an ISO string, `date`, or timezone-naive `datetime`
- `inclusive` - when `True`, add one second in the count direction

## Usage

```python
from analytics_toolkit import datetime as dttm

seconds = dttm.seconds_between("2026-01-01 12:13:15", "2026-01-01 12:13:45")
```

Output example:

```python
seconds
# 30
```

[Functions index](index.md)
