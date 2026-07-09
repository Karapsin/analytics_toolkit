[Functions index](index.md)

# is_greater

Check whether one timestamp is after another.

```python
is_greater(dt, other_dt, inclusive=False)
```

## Inputs

- `dt` - timestamp to compare as an ISO string, `date`, or timezone-naive `datetime`
- `other_dt` - comparison timestamp as an ISO string, `date`, or timezone-naive `datetime`
- `inclusive` - when `True`, treat equal timestamps as greater

## Usage

```python
from analytics_toolkit import datetime as dttm

is_later = dttm.is_greater("2026-01-01 12:13:16", "2026-01-01 12:13:15")
```

Output example:

```python
is_later
# True
```

[Functions index](index.md)
