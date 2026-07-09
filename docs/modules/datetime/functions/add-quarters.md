[Functions index](index.md)

# add_quarters

Add a number of quarters to a timestamp without normalizing to a quarter start.

```python
add_quarters(dt, n, output_string=True)
```

## Inputs

- `dt` - input timestamp as an ISO string, `date`, or timezone-naive `datetime`
- `n` - number of quarters to add. Use a negative value to subtract
- `output_string` - when `True`, return `YYYY-MM-DD HH:MM:SS`; when `False`, return a `datetime`

## Usage

```python
from analytics_toolkit import datetime as dttm

next_quarter = dttm.add_quarters("2026-01-31 12:13:15", 1)
```

Output example:

```python
next_quarter
# '2026-04-30 12:13:15'
```

[Functions index](index.md)
