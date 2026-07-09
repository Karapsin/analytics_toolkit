[Functions index](index.md)

# is_between

Check whether a timestamp falls within a timestamp range.

```python
is_between(dt, start_dt, end_dt, inclusive=True)
```

## Inputs

- `dt` - timestamp to check as an ISO string, `date`, or timezone-naive `datetime`
- `start_dt` - lower bound as an ISO string, `date`, or timezone-naive `datetime`
- `end_dt` - upper bound as an ISO string, `date`, or timezone-naive `datetime`
- `inclusive` - when `True`, include both bounds

## Usage

```python
from analytics_toolkit import datetime as dttm

in_window = dttm.is_between(
    "2026-01-01 12:13:15",
    "2026-01-01 12:00:00",
    "2026-01-01 13:00:00",
)
```

Output example:

```python
in_window
# True
```

[Functions index](index.md)
