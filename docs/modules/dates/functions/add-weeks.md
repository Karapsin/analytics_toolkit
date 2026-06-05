[All date functions](index.md)

# add_weeks

Move by whole weeks from the week start that contains the input date.

```python
add_weeks(dt, n, output_string=True)
```

## Inputs

- `dt`: Input date as an ISO string, `date`, or `datetime`.
- `n`: Number of weeks to add. Use a negative value to subtract.
- `output_string`: When `True`, return an ISO string; when `False`, return a midnight `datetime`.

## Usage

```python
from analytics_toolkit.dates import add_weeks

next_week = add_weeks("2026-04-10", 1)
previous_week = add_weeks("2026-04-10", -1)
```

## Notes

- The calculation starts from the beginning of the input date's week.

[All date functions](index.md)
