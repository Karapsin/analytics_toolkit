[Functions index](index.md)

# add_hours

Add a number of hours to a timestamp.

```python
add_hours(dt, n, output_string=True)
```

## Inputs

- `dt` - input timestamp as an ISO string, `date`, or timezone-naive `datetime`
- `n` - number of hours to add. Use a negative value to subtract
- `output_string` - when `True`, return `YYYY-MM-DD HH:MM:SS`; when `False`, return a `datetime`

## Usage

```python
from analytics_toolkit import datetime as dttm

next_hour = dttm.add_hours("2026-01-01 12:13:15", 1)
```

Output example:

```python
next_hour
# '2026-01-01 13:13:15'
```

[Functions index](index.md)
