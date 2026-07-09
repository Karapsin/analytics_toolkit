[Functions index](index.md)

# add_seconds

Add a number of seconds to a timestamp.

```python
add_seconds(dt, n, output_string=True)
```

## Inputs

- `dt` - input timestamp as an ISO string, `date`, or timezone-naive `datetime`
- `n` - number of seconds to add. Use a negative value to subtract
- `output_string` - when `True`, return `YYYY-MM-DD HH:MM:SS`; when `False`, return a `datetime`

## Usage

```python
from analytics_toolkit import datetime as dttm

next_second = dttm.add_seconds("2026-01-01 12:13:15", 1)
```

Output example:

```python
next_second
# '2026-01-01 12:13:16'
```

[Functions index](index.md)
