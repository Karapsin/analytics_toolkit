[Functions index](index.md)

# add_weeks

Add a number of weeks to a timestamp without normalizing to a week start.

```python
add_weeks(dt, n, output_string=True)
```

## Inputs

- `dt` - input timestamp as an ISO string, `date`, or timezone-naive `datetime`
- `n` - number of weeks to add. Use a negative value to subtract
- `output_string` - when `True`, return `YYYY-MM-DD HH:MM:SS`; when `False`, return a `datetime`

## Usage

```python
from analytics_toolkit import datetime as dttm

next_week = dttm.add_weeks("2026-01-01 12:13:15", 1)
```

Output example:

```python
next_week
# '2026-01-08 12:13:15'
```

[Functions index](index.md)
