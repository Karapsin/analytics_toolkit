[Functions index](index.md)

# add_months

Add a number of months to a timestamp without normalizing to a month start.

```python
add_months(dt, n, output_string=True)
```

## Inputs

- `dt` - input timestamp as an ISO string, `date`, or timezone-naive `datetime`
- `n` - number of months to add. Use a negative value to subtract
- `output_string` - when `True`, return `YYYY-MM-DD HH:MM:SS`; when `False`, return a `datetime`

## Usage

```python
from analytics_toolkit import datetime as dttm

next_month = dttm.add_months("2026-01-31 12:13:15", 1)
```

Output example:

```python
next_month
# '2026-02-28 12:13:15'
```

[Functions index](index.md)
