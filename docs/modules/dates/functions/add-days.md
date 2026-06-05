[All date functions](index.md)

# add_days

Add a number of days to a date.

```python
add_days(dt, n, output_string=True)
```

## Inputs

- `dt`: Input date as an ISO string, `date`, or `datetime`.
- `n`: Number of days to add. Use a negative value to subtract.
- `output_string`: When `True`, return an ISO string; when `False`, return a midnight `datetime`.

## Usage

```python
from analytics_toolkit.dates import add_days

next_day = add_days("2026-04-10", 1)
previous_day = add_days("2026-04-10", -1)
```

[All date functions](index.md)
