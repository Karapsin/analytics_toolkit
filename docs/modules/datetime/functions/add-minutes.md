[Functions index](index.md)

# add_minutes

Add a number of minutes to a timestamp.

```python
add_minutes(dt, n, output_string=True)
```

## Inputs

- `dt` - input timestamp as an ISO string, `date`, or timezone-naive `datetime`
- `n` - number of minutes to add. Use a negative value to subtract
- `output_string` - when `True`, return `YYYY-MM-DD HH:MM:SS`; when `False`, return a `datetime`

## Usage

```python
from analytics_toolkit import datetime as dttm

next_minute = dttm.add_minutes("2026-01-01 12:13:15", 1)
```

Output example:

```python
next_minute
# '2026-01-01 12:14:15'
```

[Functions index](index.md)
