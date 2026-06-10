[All date functions](index.md)

# add_days

Add a number of days to a date.

```python
add_days(dt, n, output_string=True)
```

## Inputs

- `dt` - input date as an ISO string, `date`, or `datetime`
- `n` - number of days to add. Use a negative value to subtract
- `output_string` - when `True`, return an ISO string; when `False`, return a midnight `datetime`

## Usage

```python
from analytics_toolkit.dates import add_days

next_day = add_days("2026-04-10", 1)
previous_day = add_days("2026-04-10", -1)
```

Output example:

```python
next_day
# '2026-04-11'

previous_day
# '2026-04-09'
```

[All date functions](index.md)
