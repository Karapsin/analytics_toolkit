[Functions index](index.md)

# add_days

Add a number of days to a timestamp without dropping the time component.

```python
add_days(dt, n, output_string=True)
```

## Inputs

- `dt` - input timestamp as an ISO string, `date`, or timezone-naive `datetime`
- `n` - number of days to add. Use a negative value to subtract
- `output_string` - when `True`, return `YYYY-MM-DD HH:MM:SS`; when `False`, return a `datetime`

## Usage

```python
from analytics_toolkit import datetime as dttm

next_day = dttm.add_days("2026-01-01 12:13:15", 1)
```

Output example:

```python
next_day
# '2026-01-02 12:13:15'
```

[Functions index](index.md)
