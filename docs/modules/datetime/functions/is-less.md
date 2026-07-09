[Functions index](index.md)

# is_less

Check whether one timestamp is before another.

```python
is_less(dt, other_dt, inclusive=False)
```

## Inputs

- `dt` - timestamp to compare as an ISO string, `date`, or timezone-naive `datetime`
- `other_dt` - comparison timestamp as an ISO string, `date`, or timezone-naive `datetime`
- `inclusive` - when `True`, treat equal timestamps as less

## Usage

```python
from analytics_toolkit import datetime as dttm

is_earlier = dttm.is_less("2026-01-01 12:13:14", "2026-01-01 12:13:15")
```

Output example:

```python
is_earlier
# True
```

[Functions index](index.md)
